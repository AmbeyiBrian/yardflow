"""The dispatch and notification endpoints (§6, §4.7, §9.1; F1–F8, G1, L1).

T4.21's criterion is "a technician raises a request from a phone in under a
minute", which means the whole request — destination, holder, lines — arrives in
**one** call. T4.24's is "an approval request appears without a page reload",
which needs an unread count cheap enough to poll.
"""

from decimal import Decimal

import pytest
from django.urls import reverse

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
def yard_with_stock(signed_in):
    """A yard holding 100 of one item, and a site to send it to."""
    _http, _token, organization, _owner = signed_in
    with tenant_context(organization):
        from django.db import transaction

        from catalogue.factories import ItemTypeFactory
        from locations.factories import YardFactory
        from locations.nodes import external_node
        from network.factories import SiteFactory
        from stock.models import MovementType
        from stock.services import MovementRequest, post_movement

        yard = YardFactory(name="Dispatch yard")
        site = SiteFactory(internal_ref="DSP-1", name="Dispatch site")
        item = ItemTypeFactory(name="Dispatch clamp", uom="ea")
        with transaction.atomic():
            post_movement(
                MovementRequest(
                    item_type=item,
                    quantity=Decimal("100"),
                    from_node=external_node(organization.pk),
                    to_node=yard.node,
                    movement_type=MovementType.RECEIPT,
                )
            )
    return yard, site, item


class TestRaisingARequest:
    def test_a_request_arrives_with_its_lines_in_one_call(self, signed_in, yard_with_stock):
        """T4.21: under a minute on a phone means one round trip.

        A request assembled line by line could be submitted half-built, and a
        pass approved for two of the five things somebody meant to take is worse
        than no pass at all.
        """
        http, token, _organization, owner = signed_in
        yard, site, item = yard_with_stock

        response = http.post(
            reverse("v1:gate-out-list"),
            {
                "purpose_type": "INSTALLATION",
                "from_location": yard.pk,
                "site": site.pk,
                "custody_holder": owner.pk,
                "lines": [
                    {
                        "item_type": item.pk,
                        "tracking_mode": "BULK",
                        "requested_qty": "10",
                        "uom": "ea",
                    },
                    {
                        "item_type": item.pk,
                        "tracking_mode": "BULK",
                        "requested_qty": "4",
                        "uom": "ea",
                        "is_returnable": True,
                    },
                ],
            },
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 201, response.content
        body = response.json()
        assert body["status"] == "DRAFT"
        assert len(body["lines"]) == 2
        assert body["lines"][0]["line_number"] == 1
        assert body["lines"][1]["is_returnable"] is True

    def test_lines_cannot_be_replaced_after_submission(self, signed_in, yard_with_stock):
        """F6: changing an approved pass is an amendment, which voids approval.

        Silently swapping the lines under an approval would mean material leaving
        on authority nobody gave for it.
        """
        http, token, _organization, owner = signed_in
        yard, site, item = yard_with_stock

        created = http.post(
            reverse("v1:gate-out-list"),
            {
                "purpose_type": "INSTALLATION",
                "from_location": yard.pk,
                "site": site.pk,
                "custody_holder": owner.pk,
                "lines": [
                    {
                        "item_type": item.pk,
                        "tracking_mode": "BULK",
                        "requested_qty": "5",
                        "uom": "ea",
                    }
                ],
            },
            content_type="application/json",
            **auth(token),
        ).json()

        submitted = http.post(
            reverse("v1:gate-out-submit", args=[created["id"]]),
            content_type="application/json",
            **auth(token),
        )
        assert submitted.status_code == 200, submitted.content

        response = http.patch(
            reverse("v1:gate-out-detail", args=[created["id"]]),
            {
                "lines": [
                    {
                        "item_type": item.pk,
                        "tracking_mode": "BULK",
                        "requested_qty": "90",
                        "uom": "ea",
                    }
                ]
            },
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 400
        assert "lines" in response.json()["error"]["field_errors"]

    def test_the_whole_loop_runs_over_the_api(self, signed_in, yard_with_stock):
        """M4: "approved, auditable gate-outs. This is the product."

        Raise, submit, release — and the stock has actually moved to the
        technician's record at the end of it.
        """
        http, token, organization, owner = signed_in
        yard, site, item = yard_with_stock

        created = http.post(
            reverse("v1:gate-out-list"),
            {
                "purpose_type": "INSTALLATION",
                "from_location": yard.pk,
                "site": site.pk,
                "custody_holder": owner.pk,
                "lines": [
                    {
                        "item_type": item.pk,
                        "tracking_mode": "BULK",
                        "requested_qty": "12",
                        "uom": "ea",
                    }
                ],
            },
            content_type="application/json",
            **auth(token),
        ).json()

        http.post(
            reverse("v1:gate-out-submit", args=[created["id"]]),
            content_type="application/json",
            **auth(token),
        )

        released = http.post(
            reverse("v1:gate-out-release", args=[created["id"]]),
            {"vehicle_reg": "KDA 123A", "driver_name": "Peter Driver"},
            content_type="application/json",
            **auth(token),
        )

        assert released.status_code == 200, released.content
        assert released.json()["status"] == "RELEASED"
        assert released.json()["number"].startswith("GP-")

        with tenant_context(organization):
            from locations.nodes import node_for_user
            from stock.services import balance_at

            assert balance_at(node_for_user(owner), item) == Decimal("12")
            assert balance_at(yard.node, item) == Decimal("88")

    def test_a_short_release_records_a_variance_and_still_completes(
        self, signed_in, yard_with_stock
    ):
        """T4.23, G1: "releasing a load with one short line records the variance
        and completes the release." A blocked gate is a gate people drive round.
        """
        http, token, organization, owner = signed_in
        yard, site, item = yard_with_stock

        created = http.post(
            reverse("v1:gate-out-list"),
            {
                "purpose_type": "INSTALLATION",
                "from_location": yard.pk,
                "site": site.pk,
                "custody_holder": owner.pk,
                "lines": [
                    {
                        "item_type": item.pk,
                        "tracking_mode": "BULK",
                        "requested_qty": "10",
                        "uom": "ea",
                    }
                ],
            },
            content_type="application/json",
            **auth(token),
        ).json()
        http.post(
            reverse("v1:gate-out-submit", args=[created["id"]]),
            content_type="application/json",
            **auth(token),
        )

        line_id = created["lines"][0]["id"]
        released = http.post(
            reverse("v1:gate-out-release", args=[created["id"]]),
            {
                "vehicle_reg": "KDA 123A",
                "released_lines": {str(line_id): "7"},
                "variance_reasons": {str(line_id): "Only seven on the shelf."},
            },
            content_type="application/json",
            **auth(token),
        )

        assert released.status_code == 200, released.content
        with tenant_context(organization):
            from dispatch.models import ReleaseVariance
            from locations.nodes import node_for_user
            from stock.services import balance_at

            assert balance_at(node_for_user(owner), item) == Decimal("7")
            variance = ReleaseVariance.objects.get()
            assert variance.approved_qty == Decimal("10")
            assert variance.released_qty == Decimal("7")
            assert variance.reason == "Only seven on the shelf."

        # M1: and it is on the exceptions register until acknowledged.
        register = http.get(reverse("v1:exceptions"), **auth(token)).json()
        assert any(entry["kind"] == "release_variance" for entry in register["items"])


class TestNotifications:
    def test_the_unread_count_is_pollable(self, signed_in):
        """T4.24: an approval request appears without a page reload."""
        http, token, organization, owner = signed_in

        assert http.get(reverse("v1:notification-unread"), **auth(token)).json() == {
            "unread": 0
        }

        with tenant_context(organization):
            from notifications.models import NotificationDelivery, NotificationEvent

            event = NotificationEvent.objects.create(
                organization=organization,
                event_key="approval.requested",
                target_type="dispatch.GateOut",
                target_id="41",
                target_label="GP-000041",
            )
            NotificationDelivery.objects.create(
                organization=organization,
                event=event,
                recipient=owner,
                channel="in_app",
                subject="GP-000041 needs your approval",
                body="Two high-criticality lines.",
            )

        assert http.get(reverse("v1:notification-unread"), **auth(token)).json() == {
            "unread": 1
        }

        listing = http.get(reverse("v1:notification-list"), **auth(token)).json()
        row = listing["results"][0]
        # §9.2: every row is tappable, and the route is computed server-side so a
        # new document type needs no frontend release.
        assert row["resource"] == "/gate-out/41"
        assert row["is_unread"] is True

        read = http.post(
            reverse("v1:notification-mark-read", args=[row["id"]]),
            content_type="application/json",
            **auth(token),
        )
        assert read.status_code == 200
        assert read.json()["is_unread"] is False
        assert http.get(reverse("v1:notification-unread"), **auth(token)).json() == {
            "unread": 0
        }

    def test_one_person_never_sees_anothers_notifications(self, signed_in):
        """A notification is addressed to a person.

        Showing one colleague another's would leak who is being asked to approve
        what — inside the same tenant, where A3's scoping does not help.
        """
        http, token, organization, _owner = signed_in

        with tenant_context(organization):
            from accounts.factories import UserFactory
            from notifications.models import NotificationDelivery, NotificationEvent

            someone_else = UserFactory(organization=organization, full_name="Other Olga")
            event = NotificationEvent.objects.create(
                organization=organization,
                event_key="approval.requested",
                target_type="dispatch.GateOut",
                target_id="42",
                target_label="GP-000042",
            )
            NotificationDelivery.objects.create(
                organization=organization,
                event=event,
                recipient=someone_else,
                channel="in_app",
                subject="Not for you",
                body="Not for you",
            )

        listing = http.get(reverse("v1:notification-list"), **auth(token)).json()

        assert listing["results"] == []
        assert http.get(reverse("v1:notification-unread"), **auth(token)).json() == {
            "unread": 0
        }

    def test_the_matrix_says_what_will_be_sent_and_how(self, signed_in):
        """L2: a notification nobody expected reads as spam."""
        http, token, _organization, _owner = signed_in

        response = http.get(reverse("v1:notification-preferences"), **auth(token))

        assert response.status_code == 200
        events = response.json()["events"]
        assert len(events) > 0
        # Q1: WhatsApp ships disabled, so it must not be advertised as active.
        assert all("whatsapp" not in entry["channels"] for entry in events)
