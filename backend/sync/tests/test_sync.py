"""T8.4, T8.6 and the §8.3 constraint (design §8; N1–N3).

Three criteria, and the third is the one the whole offline design rests on:

* T8.4 — "the same payload submitted **ten times** yields exactly one document"
* T8.6 — "an offline issue of a serial that was issued elsewhere meanwhile
  produces an exception, **not a corrupted balance**"
* §8.3 — "**release of an unapproved gate-out is impossible offline**", which the
  design calls "the single most important constraint in the offline design:
  without it, offline mode is a bypass around the entire approval control the
  system exists to provide"

So the last one is tested from both directions: the happy path (an approved pass
releases) and the refusal (anything else does not), because a control that only
ever gets tested by its happy path is a control nobody has checked.
"""

from decimal import Decimal
from uuid import uuid4

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
    owner.full_name = "Sam Owner"
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
def yard_and_item(signed_in):
    _http, _token, organization, _owner = signed_in
    with tenant_context(organization):
        from catalogue.factories import ItemTypeFactory
        from locations.factories import YardFactory
        from network.factories import SiteFactory

        yard = YardFactory(name="Sync yard")
        site = SiteFactory(internal_ref="SYN-1", name="Sync site")
        item = ItemTypeFactory(name="Sync clamp", uom="ea")
    return yard, site, item


def a_gate_in_payload(yard, item, *, quantity="10"):
    return {
        "source_type": "PURCHASE",
        "supplier_name": "Huawei Kenya",
        "to_location": yard.pk,
        "received_at": timezone.now().isoformat(),
        "lines": [
            {
                "item_type": item.pk,
                "tracking_mode": "BULK",
                "quantity": quantity,
                "uom": "ea",
                "condition": "NEW",
            }
        ],
    }


def submit(http, token, items):
    return http.post(
        reverse("v1:sync-submissions"),
        {"submissions": items},
        content_type="application/json",
        **auth(token),
    )


class TestIdempotency:
    """T8.4: "the same payload submitted ten times yields exactly one document"."""

    def test_ten_submissions_of_one_payload_make_one_document(
        self, signed_in, yard_and_item
    ):
        from receiving.models import GateIn

        http, token, organization, _owner = signed_in
        yard, _site, item = yard_and_item

        item_payload = {
            "client_uuid": str(uuid4()),
            "operation": "GATE_IN",
            "payload": a_gate_in_payload(yard, item),
            "captured_at": timezone.now().isoformat(),
        }

        responses = [submit(http, token, [item_payload]) for _ in range(10)]

        assert all(response.status_code == 200 for response in responses)
        bodies = [response.json() for response in responses]
        # The first applied; the other nine recognised the replay.
        assert bodies[0]["applied"] == 1
        assert all(body["replayed"] == 1 for body in bodies[1:])

        with tenant_context(organization):
            assert GateIn.objects.count() == 1
            # And the stock moved exactly once.
            from stock.services import balance_at

            assert balance_at(yard.node, item) == Decimal("10")

    def test_a_replay_returns_the_original_document(self, signed_in, yard_and_item):
        """So the queue can clear the item and show the storekeeper its number."""
        http, token, _organization, _owner = signed_in
        yard, _site, item = yard_and_item

        payload = {
            "client_uuid": str(uuid4()),
            "operation": "GATE_IN",
            "payload": a_gate_in_payload(yard, item),
        }

        first = submit(http, token, [payload]).json()["results"][0]
        again = submit(http, token, [payload]).json()["results"][0]

        assert again["document_id"] == first["document_id"]
        assert again["document_number"] == first["document_number"]
        assert again["replayed"] is True

    def test_a_batch_applies_what_it_can(self, signed_in, yard_and_item):
        """One bad item must not strand a shift's capture (§8.1)."""
        http, token, _organization, _owner = signed_in
        yard, _site, item = yard_and_item

        good = a_gate_in_payload(yard, item)
        broken = a_gate_in_payload(yard, item)
        broken["lines"][0]["item_type"] = 999_999  # gone, or another tenant's

        body = submit(
            http,
            token,
            [
                {"client_uuid": str(uuid4()), "operation": "GATE_IN", "payload": good},
                {"client_uuid": str(uuid4()), "operation": "GATE_IN", "payload": broken},
            ],
        ).json()

        assert body["applied"] == 1
        assert body["rejected"] == 1

    def test_an_operation_outside_the_queue_is_refused(self, signed_in):
        """§8: offline capture is gate-in and gate-out only.

        A queue that accepted everything would be a second write path around
        every control in the system.
        """
        http, token, _organization, _owner = signed_in

        response = submit(
            http,
            token,
            [
                {
                    "client_uuid": str(uuid4()),
                    "operation": "STOCK_COUNT",
                    "payload": {},
                }
            ],
        )

        # Refused by the serializer's choice field, before anything looks at it.
        assert response.status_code == 400


class TestTheApprovalHoleStaysClosed:
    """§8.3, N3 — the constraint the offline design rests on."""

    @pytest.fixture
    def approved_pass(self, signed_in, yard_and_item):
        """A gate pass approved online, as a device would have downloaded it."""
        from django.db import transaction

        from dispatch.models import GateOut, GateOutLine, GateOutPurpose
        from dispatch.services import submit_gate_out
        from locations.nodes import external_node, node_for_location
        from stock.models import MovementType
        from stock.services import MovementRequest, post_movement

        _http, _token, organization, owner = signed_in
        yard, site, item = yard_and_item

        with tenant_context(organization):
            with transaction.atomic():
                post_movement(
                    MovementRequest(
                        item_type=item,
                        quantity=Decimal("50"),
                        from_node=external_node(organization.pk),
                        to_node=node_for_location(yard),
                        movement_type=MovementType.RECEIPT,
                    )
                )
            gate_out = GateOut.objects.create(
                organization=organization,
                from_location=yard,
                site=site,
                custody_holder=owner,
                requested_by=owner,
                purpose_type=GateOutPurpose.INSTALLATION,
            )
            GateOutLine.objects.create(
                organization=organization,
                gate_out=gate_out,
                item_type=item,
                tracking_mode="BULK",
                requested_qty=Decimal("5"),
                uom="ea",
            )
            submit_gate_out(gate_out, submitted_by=owner)
            gate_out.refresh_from_db()
        return gate_out

    def test_an_already_approved_pass_can_be_released_offline(
        self, signed_in, approved_pass
    ):
        """The half §8.3 permits, and the reason offline release is useful at all."""
        http, token, organization, _owner = signed_in

        assert approved_pass.status == "APPROVED"

        body = submit(
            http,
            token,
            [
                {
                    "client_uuid": str(uuid4()),
                    "operation": "GATE_OUT_RELEASE",
                    "payload": {
                        "gate_out": approved_pass.pk,
                        "vehicle_reg": "KDG 900X",
                        "driver_name": "Wanjiku",
                    },
                }
            ],
        ).json()

        assert body["applied"] == 1, body
        with tenant_context(organization):
            approved_pass.refresh_from_db()
            assert approved_pass.status == "RELEASED"

    def test_releasing_an_unapproved_pass_offline_is_refused(
        self, signed_in, yard_and_item
    ):
        """The half that matters. §8.3: "impossible offline".

        A draft pass has nobody's authority behind it. If a queued release could
        apply it, offline mode would be a way to take material out of the yard
        without an approval — which is the entire control the product exists for.
        """
        from dispatch.models import GateOut, GateOutLine, GateOutPurpose

        http, token, organization, owner = signed_in
        yard, site, item = yard_and_item

        with tenant_context(organization):
            draft = GateOut.objects.create(
                organization=organization,
                from_location=yard,
                site=site,
                custody_holder=owner,
                requested_by=owner,
                purpose_type=GateOutPurpose.INSTALLATION,
            )
            GateOutLine.objects.create(
                organization=organization,
                gate_out=draft,
                item_type=item,
                tracking_mode="BULK",
                requested_qty=Decimal("1"),
                uom="ea",
            )

        body = submit(
            http,
            token,
            [
                {
                    "client_uuid": str(uuid4()),
                    "operation": "GATE_OUT_RELEASE",
                    "payload": {"gate_out": draft.pk, "vehicle_reg": "KDG 900X"},
                }
            ],
        ).json()

        assert body["rejected"] == 1
        result = body["results"][0]
        assert result["exception"]["code"] == "OFFLINE_APPROVAL_NOT_ALLOWED"

        with tenant_context(organization):
            draft.refresh_from_db()
            assert draft.status == "DRAFT", "nothing about it changed"
            # And the material is still in the yard.
            from stock.services import balance_at

            assert balance_at(yard.node, item) == Decimal("0")

    def test_a_queued_request_is_submitted_never_approved(self, signed_in, yard_and_item):
        """The other direction: an offline *request* cannot arrive approved."""
        from django.db import transaction

        from locations.nodes import external_node, node_for_location
        from stock.models import MovementType
        from stock.services import MovementRequest, post_movement

        http, token, organization, owner = signed_in
        yard, site, item = yard_and_item

        with tenant_context(organization), transaction.atomic():
            post_movement(
                MovementRequest(
                    item_type=item,
                    quantity=Decimal("10"),
                    from_node=external_node(organization.pk),
                    to_node=node_for_location(yard),
                    movement_type=MovementType.RECEIPT,
                )
            )

        body = submit(
            http,
            token,
            [
                {
                    "client_uuid": str(uuid4()),
                    "operation": "GATE_OUT_REQUEST",
                    "payload": {
                        "purpose_type": "INSTALLATION",
                        "from_location": yard.pk,
                        "site": site.pk,
                        "custody_holder": owner.pk,
                        "lines": [
                            {
                                "item_type": item.pk,
                                "tracking_mode": "BULK",
                                "requested_qty": "2",
                                "uom": "ea",
                            }
                        ],
                    },
                }
            ],
        ).json()

        assert body["applied"] == 1, body["results"][0].get("exception") or body
        with tenant_context(organization):
            from dispatch.models import GateOut

            gate_out = GateOut.objects.get(pk=body["results"][0]["document_id"])
            # Either routed for approval, or auto-approved by a rule that exists
            # — never released, and never approved *by the device*.
            assert gate_out.status in ("PENDING_APPROVAL", "APPROVED")
            assert gate_out.released_at is None

    def test_the_offline_bundle_carries_only_approved_passes(
        self, signed_in, approved_pass, yard_and_item
    ):
        """§8.3: a device holding an unapproved pass could release it.

        So the bundle it downloads contains only what is already authorised.
        """
        from dispatch.models import GateOut, GateOutLine, GateOutPurpose

        http, token, organization, owner = signed_in
        yard, site, item = yard_and_item

        with tenant_context(organization):
            draft = GateOut.objects.create(
                organization=organization,
                from_location=yard,
                site=site,
                custody_holder=owner,
                requested_by=owner,
                purpose_type=GateOutPurpose.INSTALLATION,
            )
            GateOutLine.objects.create(
                organization=organization,
                gate_out=draft,
                item_type=item,
                tracking_mode="BULK",
                requested_qty=Decimal("1"),
                uom="ea",
            )

        response = http.get(reverse("v1:sync-bundle"), **auth(token))

        assert response.status_code == 200, response.content
        bundle = response.json()
        ids = {row["id"] for row in bundle["releasable_gate_outs"]}
        assert approved_pass.pk in ids
        assert draft.pk not in ids
        # T8.2: enough reference data to build a line with no signal.
        assert bundle["item_types"]
        assert bundle["locations"]


class TestConflicts:
    """T8.6: an offline document that is no longer valid becomes an exception."""

    def test_a_serial_issued_elsewhere_meanwhile_produces_an_exception(
        self, signed_in, yard_and_item
    ):
        """T8.6's criterion, almost word for word.

        The phone captured a receipt of serial X. While it was offline, X was
        received by somebody else — so the offline document is no longer
        possible. §8.4: not force-posted (that would double the unit), not
        dropped (that would lose the capture).
        """
        from receiving.models import GateIn

        http, token, organization, _owner = signed_in
        yard, _site, _item = yard_and_item

        with tenant_context(organization):
            from catalogue.models import ItemCategory, ItemType, TrackingMode

            category = ItemCategory.objects.first() or ItemCategory.objects.create(
                organization=organization, name="Radios"
            )
            serialized = ItemType.objects.create(
                organization=organization,
                category=category,
                name="Sync RRU",
                uom="ea",
                default_tracking_mode=TrackingMode.SERIALIZED,
            )

        payload = {
            "source_type": "PURCHASE",
            "supplier_name": "Huawei Kenya",
            "to_location": yard.pk,
            "received_at": timezone.now().isoformat(),
            "lines": [
                {
                    "item_type": serialized.pk,
                    "tracking_mode": "SERIALIZED",
                    "quantity": "1",
                    "uom": "ea",
                    "condition": "NEW",
                    "serials": [{"serial_number": "CONTESTED-1"}],
                }
            ],
        }

        # Somebody else received it while the phone was offline.
        online = http.post(
            reverse("v1:gate-in-list"),
            payload,
            content_type="application/json",
            **auth(token),
        )
        assert online.status_code == 201, online.content
        http.post(
            reverse("v1:gate-in-post-document", args=[online.json()["id"]]),
            content_type="application/json",
            **auth(token),
        )

        # Now the queue drains.
        body = submit(
            http,
            token,
            [{"client_uuid": str(uuid4()), "operation": "GATE_IN", "payload": payload}],
        ).json()

        assert body["rejected"] == 1, body
        exception = body["results"][0]["exception"]
        # Whichever layer catches it, the point is that it *was* caught and
        # recorded rather than posted.
        assert exception["code"] in (
            "DUPLICATE_SERIAL",
            "VALIDATION_ERROR",
            "GATE_IN_NOT_READY",
        )

        with tenant_context(organization):
            from stock.models import SerialUnit

            # Exactly one unit, not two. That is the "not a corrupted balance"
            # half of the criterion.
            assert SerialUnit.objects.filter(serial_number="CONTESTED-1").count() == 1
            assert GateIn.objects.count() == 1

    def test_an_exception_keeps_the_payload_for_resolution(self, signed_in, yard_and_item):
        """T8.7: "resolvable without developer intervention" needs the payload."""
        http, token, _organization, _owner = signed_in
        yard, _site, item = yard_and_item

        broken = a_gate_in_payload(yard, item)
        broken["lines"][0]["item_type"] = 999_999

        submit(
            http,
            token,
            [{"client_uuid": str(uuid4()), "operation": "GATE_IN", "payload": broken}],
        )

        listing = http.get(reverse("v1:sync-exception-list"), **auth(token))

        assert listing.status_code == 200, listing.content
        row = listing.json()["results"][0]
        assert row["status"] == "OPEN"
        assert row["payload"]["supplier_name"] == "Huawei Kenya"
        assert row["captured_by"]

    def test_resolving_an_exception_records_the_decision(self, signed_in, yard_and_item):
        """§8.4: closing one with no explanation is the same as dropping it."""
        http, token, _organization, _owner = signed_in
        yard, _site, item = yard_and_item

        broken = a_gate_in_payload(yard, item)
        broken["lines"][0]["item_type"] = 999_999
        submit(
            http,
            token,
            [{"client_uuid": str(uuid4()), "operation": "GATE_IN", "payload": broken}],
        )
        exception_id = http.get(reverse("v1:sync-exception-list"), **auth(token)).json()[
            "results"
        ][0]["id"]

        empty = http.post(
            reverse("v1:sync-exception-resolve", args=[exception_id]),
            {"resolution": ""},
            content_type="application/json",
            **auth(token),
        )
        assert empty.status_code == 400

        resolved = http.post(
            reverse("v1:sync-exception-resolve", args=[exception_id]),
            {"resolution": "Received on GRN-000004 the next morning instead."},
            content_type="application/json",
            **auth(token),
        )
        assert resolved.status_code == 200, resolved.content
        assert resolved.json()["status"] == "RESOLVED"
        assert resolved.json()["resolved_by_name"]

        # And it cannot be resolved twice, which would overwrite the first
        # explanation with the second.
        again = http.post(
            reverse("v1:sync-exception-resolve", args=[exception_id]),
            {"resolution": "Actually something else."},
            content_type="application/json",
            **auth(token),
        )
        assert again.status_code == 409

    def test_an_exception_cannot_be_created_by_hand(self, signed_in):
        """It is a finding, like a variance (H3)."""
        http, token, _organization, _owner = signed_in

        response = http.post(
            reverse("v1:sync-exception-list"),
            {},
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 405


class TestTheOneRegister:
    """T8.7, M1: "joined into the T5.5 exceptions register"."""

    def test_a_sync_conflict_appears_in_the_exceptions_register(
        self, signed_in, yard_and_item
    ):
        """M1 says "an exceptions register" — singular.

        A storekeeper working through unresolved things should not have to know
        that one of them arrived from a phone.
        """
        http, token, _organization, _owner = signed_in
        yard, _site, item = yard_and_item

        broken = a_gate_in_payload(yard, item)
        broken["lines"][0]["item_type"] = 999_999
        submit(
            http,
            token,
            [{"client_uuid": str(uuid4()), "operation": "GATE_IN", "payload": broken}],
        )

        register = http.get(reverse("v1:exceptions"), **auth(token))

        assert register.status_code == 200, register.content
        kinds = {entry["kind"] for entry in register.json()["items"]}
        assert "sync" in kinds

        # And it can be narrowed to just those.
        only = http.get(reverse("v1:exceptions"), {"kind": "sync"}, **auth(token))
        assert only.json()["count"] == 1
        assert only.json()["items"][0]["detail_url"].startswith("/api/v1/sync-exceptions/")

    def test_a_resolved_conflict_leaves_the_register(self, signed_in, yard_and_item):
        http, token, _organization, _owner = signed_in
        yard, _site, item = yard_and_item

        broken = a_gate_in_payload(yard, item)
        broken["lines"][0]["item_type"] = 999_999
        submit(
            http,
            token,
            [{"client_uuid": str(uuid4()), "operation": "GATE_IN", "payload": broken}],
        )
        exception_id = http.get(reverse("v1:sync-exception-list"), **auth(token)).json()[
            "results"
        ][0]["id"]

        http.post(
            reverse("v1:sync-exception-resolve", args=[exception_id]),
            {"resolution": "Re-keyed against the right item and received."},
            content_type="application/json",
            **auth(token),
        )

        register = http.get(reverse("v1:exceptions"), {"kind": "sync"}, **auth(token))
        assert register.json()["count"] == 0
