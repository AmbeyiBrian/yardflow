"""The receiving and stock endpoints (§6, §4.6, §3.3; D1–D8, E1–E6, M4).

T3.19's criterion is a phone flow, so the shape that matters here is that a whole
mixed delivery arrives as **one request**: header, lines, serials and drums
together. A screen that had to make a call per line could post half a delivery
when the connection drops, and half a delivery in the ledger is worse than none.

T3.20's criterion — "typing a serial number anywhere in search jumps straight to
its history" — needs the lookup endpoint to say *what* it found, so the client
knows where to go without guessing.
"""

from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from core.provisioning import provision_tenant
from core.tenancy import tenant_context


@pytest.fixture
def signed_in(db, client, settings):
    settings.TENANT_BASE_DOMAIN = "localhost"
    result = provision_tenant(
        name="Silvertech", slug="silvertech", owner_email="owner@silvertech.co.ke"
    )
    owner = result["owner"]
    owner.set_password("a good long password")
    owner.save()

    client.defaults["HTTP_HOST"] = "silvertech.localhost"
    token = client.post(
        reverse("v1:auth:login"),
        {"identifier": "owner@silvertech.co.ke", "password": "a good long password"},
        content_type="application/json",
    ).json()["access"]
    return client, token, result["organization"], owner


def auth(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


@pytest.fixture
def fixtures(signed_in):
    """A yard and three item types, one per tracking mode."""
    _http, _token, organization, _owner = signed_in
    with tenant_context(organization):
        from catalogue.factories import ItemTypeFactory
        from catalogue.models import TrackingMode
        from locations.factories import YardFactory

        yard = YardFactory(name="Api receiving yard")
        bulk = ItemTypeFactory(name="Api cable tie", uom="ea")
        serialized = ItemTypeFactory(
            name="Api RRU", default_tracking_mode=TrackingMode.SERIALIZED, uom="ea"
        )
        reel = ItemTypeFactory(name="Api feeder", default_tracking_mode=TrackingMode.REEL, uom="m")
    return yard, bulk, serialized, reel


class TestGateInApi:
    def test_a_mixed_delivery_arrives_as_one_document(self, signed_in, fixtures):
        """D2, T3.19: serialized, bulk and reel lines in one request."""
        http, token, _organization, _owner = signed_in
        yard, bulk, serialized, reel = fixtures

        response = http.post(
            reverse("v1:gate-in-list"),
            {
                "source_type": "PURCHASE",
                "supplier_name": "Huawei Kenya",
                "to_location": yard.pk,
                "received_at": timezone.now().isoformat(),
                "lines": [
                    {
                        "item_type": bulk.pk,
                        "tracking_mode": "BULK",
                        "quantity": "500",
                        "uom": "ea",
                        "condition": "NEW",
                    },
                    {
                        "item_type": serialized.pk,
                        "tracking_mode": "SERIALIZED",
                        "quantity": "2",
                        "uom": "ea",
                        "condition": "NEW",
                        "serials": [
                            {"serial_number": "API-RRU-001"},
                            {"serial_number": "API-RRU-002"},
                        ],
                    },
                    {
                        "item_type": reel.pk,
                        "tracking_mode": "REEL",
                        "quantity": "1000",
                        "uom": "m",
                        "condition": "NEW",
                        "reels": [
                            {"drum_number": "API-D-001", "length": "500"},
                            {"drum_number": "API-D-002", "length": "500"},
                        ],
                    },
                ],
            },
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 201, response.content
        body = response.json()
        assert body["status"] == "DRAFT"
        assert body["number"] == ""
        assert len(body["lines"]) == 3
        assert len(body["lines"][1]["serials"]) == 2
        assert len(body["lines"][2]["reels"]) == 2

    def test_posting_creates_stock(self, signed_in, fixtures):
        """D8: posting is the moment stock exists, and it is its own action."""
        http, token, organization, _owner = signed_in
        yard, bulk, _serialized, _reel = fixtures

        created = http.post(
            reverse("v1:gate-in-list"),
            {
                "source_type": "PURCHASE",
                "supplier_name": "Huawei Kenya",
                "to_location": yard.pk,
                "received_at": timezone.now().isoformat(),
                "lines": [
                    {
                        "item_type": bulk.pk,
                        "tracking_mode": "BULK",
                        "quantity": "500",
                        "uom": "ea",
                        "condition": "NEW",
                    }
                ],
            },
            content_type="application/json",
            **auth(token),
        ).json()

        posted = http.post(
            reverse("v1:gate-in-post-document", args=[created["id"]]),
            content_type="application/json",
            **auth(token),
        )

        assert posted.status_code == 200, posted.content
        assert posted.json()["status"] == "POSTED"
        # D15: a gap-free number, allocated at posting rather than at creation.
        assert posted.json()["number"].startswith("GRN-")

        with tenant_context(organization):
            from stock.services import balance_at

            assert balance_at(yard.node, bulk) == Decimal("500")

    def test_a_posted_gate_in_cannot_be_edited(self, signed_in, fixtures):
        """M4: it is voided and received again, so the ledger keeps agreeing."""
        http, token, _organization, _owner = signed_in
        yard, bulk, _serialized, _reel = fixtures

        created = http.post(
            reverse("v1:gate-in-list"),
            {
                "source_type": "PURCHASE",
                "to_location": yard.pk,
                "received_at": timezone.now().isoformat(),
                "supplier_name": "Huawei Kenya",
                "lines": [
                    {
                        "item_type": bulk.pk,
                        "tracking_mode": "BULK",
                        "quantity": "5",
                        "uom": "ea",
                        "condition": "NEW",
                    }
                ],
            },
            content_type="application/json",
            **auth(token),
        ).json()
        http.post(
            reverse("v1:gate-in-post-document", args=[created["id"]]),
            content_type="application/json",
            **auth(token),
        )

        response = http.patch(
            reverse("v1:gate-in-detail", args=[created["id"]]),
            {"supplier_name": "Someone else"},
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 400
        assert "status" in response.json()["error"]["field_errors"]

    def test_voiding_reverses_the_stock_and_keeps_the_number(self, signed_in, fixtures):
        """M4, M6, D15: the number stays used so the sequence has no hole."""
        http, token, organization, _owner = signed_in
        yard, bulk, _serialized, _reel = fixtures

        created = http.post(
            reverse("v1:gate-in-list"),
            {
                "source_type": "PURCHASE",
                "to_location": yard.pk,
                "received_at": timezone.now().isoformat(),
                "supplier_name": "Huawei Kenya",
                "lines": [
                    {
                        "item_type": bulk.pk,
                        "tracking_mode": "BULK",
                        "quantity": "9",
                        "uom": "ea",
                        "condition": "NEW",
                    }
                ],
            },
            content_type="application/json",
            **auth(token),
        ).json()
        posted = http.post(
            reverse("v1:gate-in-post-document", args=[created["id"]]),
            content_type="application/json",
            **auth(token),
        ).json()

        voided = http.post(
            reverse("v1:gate-in-void", args=[created["id"]]),
            {"reason": "Wrong supplier note; received again on GRN-000002."},
            content_type="application/json",
            **auth(token),
        )

        assert voided.status_code == 200, voided.content
        assert voided.json()["number"] == posted["number"]
        assert voided.json()["status"] == "VOID"

        with tenant_context(organization):
            from stock.services import balance_at

            assert balance_at(yard.node, bulk) == Decimal("0")

    def test_voiding_without_a_reason_is_refused(self, signed_in, fixtures):
        http, token, _organization, _owner = signed_in
        yard, bulk, _serialized, _reel = fixtures

        created = http.post(
            reverse("v1:gate-in-list"),
            {
                "source_type": "PURCHASE",
                "to_location": yard.pk,
                "received_at": timezone.now().isoformat(),
                "supplier_name": "Huawei Kenya",
                "lines": [
                    {
                        "item_type": bulk.pk,
                        "tracking_mode": "BULK",
                        "quantity": "1",
                        "uom": "ea",
                        "condition": "NEW",
                    }
                ],
            },
            content_type="application/json",
            **auth(token),
        ).json()
        http.post(
            reverse("v1:gate-in-post-document", args=[created["id"]]),
            content_type="application/json",
            **auth(token),
        )

        response = http.post(
            reverse("v1:gate-in-void", args=[created["id"]]),
            {"reason": ""},
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 400


class TestStockApi:
    @pytest.fixture
    def received(self, signed_in, fixtures):
        """A posted mixed delivery, so there is stock to ask about."""
        http, token, _organization, _owner = signed_in
        yard, bulk, serialized, reel = fixtures

        created = http.post(
            reverse("v1:gate-in-list"),
            {
                "source_type": "PURCHASE",
                "supplier_name": "Huawei Kenya",
                "to_location": yard.pk,
                "received_at": timezone.now().isoformat(),
                "lines": [
                    {
                        "item_type": bulk.pk,
                        "tracking_mode": "BULK",
                        "quantity": "500",
                        "uom": "ea",
                        "condition": "NEW",
                    },
                    {
                        "item_type": serialized.pk,
                        "tracking_mode": "SERIALIZED",
                        "quantity": "1",
                        "uom": "ea",
                        "condition": "NEW",
                        "serials": [{"serial_number": "LOOKUP-RRU-7"}],
                    },
                    {
                        "item_type": reel.pk,
                        "tracking_mode": "REEL",
                        "quantity": "500",
                        "uom": "m",
                        "condition": "NEW",
                        "reels": [{"drum_number": "LOOKUP-D-7", "length": "500"}],
                    },
                ],
            },
            content_type="application/json",
            **auth(token),
        ).json()
        http.post(
            reverse("v1:gate-in-post-document", args=[created["id"]]),
            content_type="application/json",
            **auth(token),
        )
        return yard, bulk, serialized, reel

    def test_stock_on_hand_answers_the_three_questions(self, signed_in, received):
        """E1: do we have it, where is it, whose is it."""
        http, token, _organization, _owner = signed_in
        yard, bulk, _serialized, _reel = received

        response = http.get(reverse("v1:stock-on-hand"), **auth(token))

        assert response.status_code == 200, response.content
        rows = response.json()["results"]
        clamp = next(row for row in rows if row["item_type"] == bulk.pk)
        assert clamp["quantity"] == "500.000"
        assert clamp["node_label"] == yard.name
        # E1: the client's name travels with the row, so no screen has to look it
        # up and forget to.
        assert clamp["owner_client_name"] == ""

    def test_a_scanned_serial_resolves_without_being_told_what_it_is(self, signed_in, received):
        """D7, T3.20: one lookup covers serials, asset tags and drums."""
        http, token, _organization, _owner = signed_in

        response = http.get(reverse("v1:stock-lookup"), {"q": "lookup-rru-7"}, **auth(token))

        assert response.status_code == 200, response.content
        body = response.json()
        assert body["kind"] == "serial_unit"
        # The client is told where to go, so search can jump straight there.
        assert body["resource"] == "/stock/serials/LOOKUP-RRU-7"

    def test_a_scanned_drum_resolves_too(self, signed_in, received):
        http, token, _organization, _owner = signed_in

        response = http.get(reverse("v1:stock-lookup"), {"q": "LOOKUP-D-7"}, **auth(token))

        assert response.status_code == 200
        assert response.json()["kind"] == "reel"
        assert response.json()["object"]["remaining_length"] == "500.000"

    def test_an_unknown_identifier_is_404(self, signed_in, received):
        """§2.4: indistinguishable from another tenant's identifier."""
        http, token, _organization, _owner = signed_in

        response = http.get(reverse("v1:stock-lookup"), {"q": "NOT-A-THING"}, **auth(token))

        assert response.status_code == 404

    def test_a_serials_history_reads_in_order(self, signed_in, received):
        """E2, T3.14: the most likely question in an operator audit."""
        http, token, _organization, _owner = signed_in

        response = http.get(reverse("v1:serial-history", args=["LOOKUP-RRU-7"]), **auth(token))

        assert response.status_code == 200, response.content
        body = response.json()
        assert body["unit"]["serial_number"] == "LOOKUP-RRU-7"
        assert [movement["movement_type"] for movement in body["movements"]] == ["RECEIPT"]
        assert body["movements"][0]["document_type"] == "receiving.GateIn"

    def test_a_transfer_inside_the_yard_posts(self, signed_in, received):
        """E4: an internal move."""
        http, token, organization, _owner = signed_in
        yard, bulk, _serialized, _reel = received

        with tenant_context(organization):
            from locations.models import Location, LocationType

            bin_location = Location.objects.create(
                organization=organization,
                name="Store A",
                type=LocationType.STORE,
                parent=yard,
            )

        response = http.post(
            reverse("v1:stock-transfer"),
            {
                "item_type": bulk.pk,
                "quantity": "50",
                "from_location": yard.pk,
                "to_location": bin_location.pk,
                "note": "Put away.",
            },
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 201, response.content
        assert response.json()["movement_type"] == "TRANSFER"

    def test_a_transfer_out_of_the_yard_is_refused_with_the_alternative(self, signed_in, received):
        """E4: "raise a gate-out instead" — a refusal that says what to do."""
        http, token, organization, _owner = signed_in
        yard, bulk, _serialized, _reel = received

        with tenant_context(organization):
            from locations.models import Location, LocationType

            vehicle = Location.objects.create(
                organization=organization,
                name="KDA 123A",
                type=LocationType.VEHICLE,
                vehicle_reg="KDA 123A",
            )

        response = http.post(
            reverse("v1:stock-transfer"),
            {
                "item_type": bulk.pk,
                "quantity": "5",
                "from_location": yard.pk,
                "to_location": vehicle.pk,
            },
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 400
        assert "gate-out" in response.json()["error"]["message"]

    def test_a_count_with_a_variance_posts_an_adjustment(self, signed_in, received):
        """E5, T3.21: counted on a phone, posted with reasons."""
        http, token, organization, _owner = signed_in
        yard, bulk, _serialized, _reel = received

        count = http.post(
            reverse("v1:stock-count-list"),
            {"location": yard.pk, "counted_at": timezone.now().isoformat()},
            content_type="application/json",
            **auth(token),
        )
        assert count.status_code == 201, count.content
        count_id = count.json()["id"]

        line = http.post(
            reverse("v1:stock-count-add-line", args=[count_id]),
            {
                "item_type": bulk.pk,
                "counted_quantity": "480",
                "reason": "Twenty short; box opened.",
            },
            content_type="application/json",
            **auth(token),
        )
        assert line.status_code == 201, line.content
        # E5: expected is captured now, so the variance is against the figure
        # being disputed rather than against today's.
        assert line.json()["lines"][0]["expected_quantity"] == "500.000"
        assert line.json()["lines"][0]["variance"] == "-20.000"

        posted = http.post(
            reverse("v1:stock-count-post-document", args=[count_id]),
            content_type="application/json",
            **auth(token),
        )

        assert posted.status_code == 200, posted.content
        with tenant_context(organization):
            from stock.services import balance_at

            assert balance_at(yard.node, bulk) == Decimal("480")

    def test_the_ledger_is_read_only_over_the_api(self, signed_in, received):
        """§3.2: every movement belongs to a document.

        One that belonged to nothing could not be explained to an auditor, so
        there is no endpoint that makes one.
        """
        http, token, _organization, _owner = signed_in

        response = http.post(
            reverse("v1:movement-list"), {}, content_type="application/json", **auth(token)
        )

        assert response.status_code == 405


class TestPressingSaveTwice:
    """From the yard, not from a theory.

    A storekeeper pressed "Save as draft" three times on a slow connection and
    got three identical drafts. The button disables itself while a request is in
    flight, which is not enough: the second press can leave before the first
    reply arrives, and no amount of client-side care closes that window.

    The device queue has always sent a ``client_uuid`` for exactly this reason
    (N2). It just was not used when the connection was good — the case that
    happens all day.
    """

    def _delivery(self, yard, bulk, client_uuid):
        return {
            "source_type": "PURCHASE",
            "supplier_name": "Trade Winds Hardware",
            "to_location": yard.pk,
            "received_at": timezone.now().isoformat(),
            "client_uuid": client_uuid,
            "lines": [
                {
                    "item_type": bulk.pk,
                    "tracking_mode": "BULK",
                    "quantity": "10",
                    "uom": "ea",
                    "condition": "NEW",
                }
            ],
        }

    def test_the_same_delivery_sent_twice_is_one_delivery(self, signed_in, fixtures):
        http, token, organization, _owner = signed_in
        yard, bulk, _serialized, _reel = fixtures
        body = self._delivery(yard, bulk, "6f1f7b4e-6b1a-4a2e-9c39-3c3a0d5f7a11")

        first = http.post(
            reverse("v1:gate-in-list"), body, content_type="application/json", **auth(token)
        )
        second = http.post(
            reverse("v1:gate-in-list"), body, content_type="application/json", **auth(token)
        )

        assert first.status_code == 201, first.content
        assert second.status_code == 201, second.content
        assert second.json()["id"] == first.json()["id"], "the same draft, not a second"

        from receiving.models import GateIn

        with tenant_context(organization):
            assert GateIn.objects.count() == 1

    def test_a_repeat_does_not_duplicate_its_lines(self, signed_in, fixtures):
        """The lines are written separately from the header, so returning the
        existing document has to stop before writing them again."""
        http, token, _organization, _owner = signed_in
        yard, bulk, _serialized, _reel = fixtures
        body = self._delivery(yard, bulk, "6f1f7b4e-6b1a-4a2e-9c39-3c3a0d5f7a22")

        http.post(reverse("v1:gate-in-list"), body, content_type="application/json", **auth(token))
        second = http.post(
            reverse("v1:gate-in-list"), body, content_type="application/json", **auth(token)
        )

        assert len(second.json()["lines"]) == 1

    def test_two_genuinely_separate_deliveries_are_both_kept(self, signed_in, fixtures):
        """The guard must not swallow a second real delivery from the same
        supplier into the same yard, which is an ordinary morning."""
        http, token, organization, _owner = signed_in
        yard, bulk, _serialized, _reel = fixtures

        http.post(
            reverse("v1:gate-in-list"),
            self._delivery(yard, bulk, "6f1f7b4e-6b1a-4a2e-9c39-3c3a0d5f7a33"),
            content_type="application/json",
            **auth(token),
        )
        http.post(
            reverse("v1:gate-in-list"),
            self._delivery(yard, bulk, "6f1f7b4e-6b1a-4a2e-9c39-3c3a0d5f7a44"),
            content_type="application/json",
            **auth(token),
        )

        from receiving.models import GateIn

        with tenant_context(organization):
            assert GateIn.objects.count() == 2


class TestDiscardingADraft:
    """A draft started by mistake should not be permanent.

    M6 says a posted gate-in is voided rather than deleted, because its number
    is in the sequence and its movements are in the ledger. A draft has neither:
    no number, no stock. Keeping one for ever because somebody opened the screen
    twice is how a list stops being worth reading — and the storekeeper who made
    it is the person who should be able to clear it.
    """

    def _draft(self, http, token, yard, bulk):
        return http.post(
            reverse("v1:gate-in-list"),
            {
                "source_type": "PURCHASE",
                "supplier_name": "Trade Winds Hardware",
                "to_location": yard.pk,
                "received_at": timezone.now().isoformat(),
                "lines": [
                    {
                        "item_type": bulk.pk,
                        "tracking_mode": "BULK",
                        "quantity": "4",
                        "uom": "ea",
                        "condition": "NEW",
                    }
                ],
            },
            content_type="application/json",
            **auth(token),
        ).json()

    def test_a_draft_can_be_discarded(self, signed_in, fixtures):
        http, token, organization, _owner = signed_in
        yard, bulk, _serialized, _reel = fixtures
        draft = self._draft(http, token, yard, bulk)

        response = http.delete(reverse("v1:gate-in-detail", args=[draft["id"]]), **auth(token))

        assert response.status_code == 204, response.content

        from receiving.models import GateIn

        with tenant_context(organization):
            assert not GateIn.objects.filter(pk=draft["id"]).exists()

    def test_a_posted_delivery_cannot_be(self, signed_in, fixtures):
        """The important half. Deleting this would leave stock in the ledger
        with nothing saying where it came from."""
        http, token, organization, _owner = signed_in
        yard, bulk, _serialized, _reel = fixtures
        draft = self._draft(http, token, yard, bulk)
        posted = http.post(reverse("v1:gate-in-post-document", args=[draft["id"]]), **auth(token))
        assert posted.status_code == 200, posted.content

        response = http.delete(reverse("v1:gate-in-detail", args=[draft["id"]]), **auth(token))

        assert response.status_code == 400
        assert "void" in str(response.json()["error"]).lower()

        from receiving.models import GateIn

        with tenant_context(organization):
            assert GateIn.objects.filter(pk=draft["id"]).exists()

    def test_a_line_says_whose_material_it_is(self, signed_in, fixtures):
        """ "Client owned" on its own raises the question it is meant to answer,
        and the answer was already on the record."""
        http, token, organization, _owner = signed_in
        yard, bulk, _serialized, _reel = fixtures
        with tenant_context(organization):
            from network.factories import ClientFactory

            owner_client = ClientFactory(name="Safaricom")

        created = http.post(
            reverse("v1:gate-in-list"),
            {
                "source_type": "CLIENT_ISSUE",
                "client": owner_client.pk,
                "to_location": yard.pk,
                "received_at": timezone.now().isoformat(),
                "lines": [
                    {
                        "item_type": bulk.pk,
                        "tracking_mode": "BULK",
                        "quantity": "4",
                        "uom": "ea",
                        "condition": "NEW",
                        "owner_type": "CLIENT",
                        "owner_client": owner_client.pk,
                    }
                ],
            },
            content_type="application/json",
            **auth(token),
        )

        assert created.status_code == 201, created.content
        assert created.json()["lines"][0]["owner_client_name"] == "Safaricom"
