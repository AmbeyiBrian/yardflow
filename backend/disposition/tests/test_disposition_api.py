"""The Phase 6 endpoints (§6, §4.11, §4.12; J1–J3, K1–K3).

T6.2's criterion is the one to be careful about — "disposing of client-owned
material without approval is **impossible regardless of configured rules**" — so
it is tested here over HTTP as well as in the service layer. A control that holds
in Python but not through the API is not a control.
"""

from decimal import Decimal

import pytest
from django.db import transaction
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
def quarantined_stock(signed_in):
    """A faulty unit and a client-owned faulty unit, both in quarantine (J1)."""
    _http, _token, organization, _owner = signed_in
    with tenant_context(organization):
        from catalogue.factories import ItemTypeFactory
        from locations.factories import YardFactory
        from locations.nodes import (
            external_node,
            node_for_location,
            quarantine_location,
        )
        from network.factories import ClientFactory
        from stock.models import Condition, MovementType, OwnerType
        from stock.services import MovementRequest, post_movement

        yard = YardFactory(name="API yard")
        quarantine = quarantine_location(organization.pk, yard)
        ours = ItemTypeFactory(name="Our faulty RRU", uom="ea")
        client = ClientFactory(name="Safaricom")
        theirs = ItemTypeFactory(name="Their faulty RRU", uom="ea")

        with transaction.atomic():
            post_movement(
                MovementRequest(
                    item_type=ours,
                    quantity=Decimal("2"),
                    from_node=external_node(organization.pk),
                    to_node=node_for_location(quarantine),
                    movement_type=MovementType.RECEIPT,
                    condition=Condition.FAULTY,
                )
            )
            post_movement(
                MovementRequest(
                    item_type=theirs,
                    quantity=Decimal("1"),
                    from_node=external_node(organization.pk, client=client),
                    to_node=node_for_location(quarantine),
                    movement_type=MovementType.RECEIPT,
                    condition=Condition.FAULTY,
                    owner_type=OwnerType.CLIENT,
                    owner_client=client,
                )
            )
    return yard, quarantine, ours, theirs, client


class TestTheQuarantineList:
    def test_it_says_what_is_there_and_for_how_long(self, signed_in, quarantined_stock):
        """J2: quarantine becomes a graveyard when nobody is looking at it, so
        the age is part of the answer rather than something to work out."""
        http, token, _organization, _owner = signed_in

        response = http.get(reverse("v1:quarantine"), **auth(token))

        assert response.status_code == 200, response.content
        body = response.json()
        assert body["count"] == 2
        first = body["items"][0]
        assert first["days_in_quarantine"] is not None
        assert first["since"] is not None
        # E1: whose it is, wherever it appears.
        assert any(entry["owner_client"] for entry in body["items"])


class TestDispositionEndpoints:
    def test_a_restore_is_raised_approved_and_posted(self, signed_in, quarantined_stock):
        http, token, _organization, _owner = signed_in
        yard, quarantine, ours, _theirs, _client = quarantined_stock

        created = http.post(
            reverse("v1:disposition-list"),
            {
                "decision": "RESTORE_TO_SERVICEABLE",
                "reason": "Bench-tested and passed.",
                "from_location": quarantine.pk,
                "to_location": yard.pk,
                "lines": [
                    {
                        "item_type": ours.pk,
                        "quantity": "2",
                        "uom": "ea",
                        "condition": "FAULTY",
                        "to_condition": "USED_SERVICEABLE",
                    }
                ],
            },
            content_type="application/json",
            **auth(token),
        )
        assert created.status_code == 201, created.content
        disposition_id = created.json()["id"]
        assert created.json()["status"] == "DRAFT"

        submitted = http.post(
            reverse("v1:disposition-submit", args=[disposition_id]),
            content_type="application/json",
            **auth(token),
        )
        assert submitted.status_code == 200, submitted.content

        posted = http.post(
            reverse("v1:disposition-post-document", args=[disposition_id]),
            content_type="application/json",
            **auth(token),
        )
        assert posted.status_code == 200, posted.content
        assert posted.json()["status"] == "POSTED"

        # T6.1: back in free stock.
        stock = http.get(
            reverse("v1:stock-on-hand"), {"item_type": ours.pk}, **auth(token)
        )
        total = sum(Decimal(row["quantity"]) for row in stock.json()["results"])
        assert total == Decimal("2")

    def test_status_is_not_writable(self, signed_in, quarantined_stock):
        """§6: status moves through the actions, never through a PATCH."""
        http, token, _organization, _owner = signed_in
        _yard, quarantine, ours, _theirs, _client = quarantined_stock

        created = http.post(
            reverse("v1:disposition-list"),
            {
                "decision": "SCRAP",
                "reason": "Beyond economic repair.",
                "from_location": quarantine.pk,
                "lines": [{"item_type": ours.pk, "quantity": "1", "uom": "ea"}],
            },
            content_type="application/json",
            **auth(token),
        ).json()

        response = http.patch(
            reverse("v1:disposition-detail", args=[created["id"]]),
            {"status": "POSTED"},
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 200
        assert response.json()["status"] == "DRAFT"

    def test_posting_before_approval_is_refused(self, signed_in, quarantined_stock):
        http, token, _organization, _owner = signed_in
        _yard, quarantine, ours, _theirs, _client = quarantined_stock

        created = http.post(
            reverse("v1:disposition-list"),
            {
                "decision": "SCRAP",
                "reason": "Beyond economic repair.",
                "from_location": quarantine.pk,
                "lines": [{"item_type": ours.pk, "quantity": "1", "uom": "ea"}],
            },
            content_type="application/json",
            **auth(token),
        ).json()

        response = http.post(
            reverse("v1:disposition-post-document", args=[created["id"]]),
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "DISPOSITION_NOT_READY"


class TestDisposalEndpoints:
    def test_client_owned_disposal_cannot_be_posted_without_approval(
        self, signed_in, quarantined_stock
    ):
        """T6.2's criterion, over HTTP, with every rule deleted.

        A control that holds in the service layer but not through the API is not
        a control — this is the path an integration or a script would take.
        """
        http, token, organization, _owner = signed_in
        _yard, quarantine, _ours, theirs, client = quarantined_stock

        with tenant_context(organization):
            from approvals.models import ApprovalRule

            ApprovalRule.objects.all().delete()

        created = http.post(
            reverse("v1:disposal-list"),
            {
                "method": "LICENSED_HANDLER",
                "from_location": quarantine.pk,
                "handler_name": "WEEE Centre",
                "lines": [
                    {
                        "item_type": theirs.pk,
                        "quantity": "1",
                        "uom": "ea",
                        "condition": "FAULTY",
                        "owner_type": "CLIENT",
                        "owner_client": client.pk,
                    }
                ],
            },
            content_type="application/json",
            **auth(token),
        )
        assert created.status_code == 201, created.content
        assert created.json()["involves_client_owned_material"] is True

        submitted = http.post(
            reverse("v1:disposal-submit", args=[created.json()["id"]]),
            content_type="application/json",
            **auth(token),
        )
        assert submitted.status_code == 200, submitted.content
        # §5.2's hardcoded escalation, with nothing in the rules table.
        assert submitted.json()["status"] == "PENDING_APPROVAL"

        posted = http.post(
            reverse("v1:disposal-post-document", args=[created.json()["id"]]),
            content_type="application/json",
            **auth(token),
        )
        assert posted.status_code == 409
        assert posted.json()["error"]["code"] == "DISPOSAL_NOT_READY"

    def test_the_certificate_prints(self, signed_in, quarantined_stock):
        """J3: the auditable half of the requirement (§11)."""
        http, token, _organization, _owner = signed_in
        _yard, quarantine, ours, _theirs, _client = quarantined_stock

        created = http.post(
            reverse("v1:disposal-list"),
            {
                "method": "SCRAP_DEALER",
                "from_location": quarantine.pk,
                "handler_name": "Ngong Road scrap",
                "handler_reference": "SD-9912",
                "lines": [
                    {
                        "item_type": ours.pk,
                        "quantity": "2",
                        "uom": "ea",
                        "condition": "FAULTY",
                    }
                ],
            },
            content_type="application/json",
            **auth(token),
        ).json()

        response = http.get(
            reverse("v1:disposal-certificate", args=[created["id"]]),
            {"output": "html"},
            **auth(token),
        )

        assert response.status_code == 200, response.content[:400]
        body = response.content.decode()
        assert "Certificate of disposal" in body
        # The handler's own reference is what makes it checkable from outside.
        assert "SD-9912" in body


class TestClientReturnEndpoints:
    @pytest.fixture
    def released_return(self, signed_in, quarantined_stock):
        http, token, organization, owner = signed_in
        yard, _quarantine, _ours, theirs, client = quarantined_stock

        with tenant_context(organization):
            from decimal import Decimal as D

            from locations.nodes import external_node, node_for_location
            from stock.models import MovementType, OwnerType
            from stock.services import MovementRequest, post_movement

            with transaction.atomic():
                post_movement(
                    MovementRequest(
                        item_type=theirs,
                        quantity=D("3"),
                        from_node=external_node(organization.pk, client=client),
                        to_node=node_for_location(yard),
                        movement_type=MovementType.RECEIPT,
                        owner_type=OwnerType.CLIENT,
                        owner_client=client,
                    )
                )

        created = http.post(
            reverse("v1:gate-out-list"),
            {
                "purpose_type": "RETURN_TO_CLIENT",
                "from_location": yard.pk,
                "client": client.pk,
                "custody_holder": owner.pk,
                "lines": [
                    {
                        "item_type": theirs.pk,
                        "tracking_mode": "BULK",
                        "requested_qty": "3",
                        "uom": "ea",
                        "owner_type": "CLIENT",
                        "owner_client": client.pk,
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
            {"vehicle_reg": "KBZ 001A", "driver_name": "Otieno"},
            content_type="application/json",
            **auth(token),
        )
        assert released.status_code == 200, released.content
        return created["id"], client, theirs

    def test_the_position_report_splits_in_transit_from_acknowledged(
        self, signed_in, released_return
    ):
        """T6.3 and T6.4, over HTTP."""
        http, token, _organization, _owner = signed_in
        gate_out_id, client, _item = released_return

        before = http.get(
            reverse("v1:client-return-position"), {"client": client.pk}, **auth(token)
        ).json()
        states = {row["state"] for row in before["rows"]}
        assert "IN_TRANSIT" in states
        assert "ACKNOWLEDGED" not in states

        ack = http.post(
            reverse("v1:client-return-ack-list"),
            {
                "gate_out": gate_out_id,
                "acknowledged_ref": "SAF-GRN-55012",
                "acknowledged_at": "2026-08-20T10:00:00+03:00",
                "acknowledged_by_name": "J. Mwangi",
            },
            content_type="application/json",
            **auth(token),
        )
        assert ack.status_code == 201, ack.content

        after = http.get(
            reverse("v1:client-return-position"), {"client": client.pk}, **auth(token)
        ).json()
        states = {row["state"] for row in after["rows"]}
        assert "ACKNOWLEDGED" in states
        assert "IN_TRANSIT" not in states
        # K3: exposure drops by what they signed for.
        assert Decimal(after["exposure"]) < Decimal(before["exposure"])

    def test_the_waybill_is_off_unless_the_tenant_enables_it(
        self, signed_in, released_return
    ):
        """K2, C8: optional per tenant, and the refusal says how to turn it on."""
        http, token, organization, _owner = signed_in
        gate_out_id, _client, _item = released_return

        refused = http.get(
            reverse("v1:gate-out-waybill", args=[gate_out_id]),
            {"output": "html"},
            **auth(token),
        )
        assert refused.status_code == 400
        assert "client_waybill_enabled" in refused.json()["error"]["field_errors"]

        with tenant_context(organization):
            settings_row = organization.settings
            settings_row.client_waybill_enabled = True
            settings_row.save(update_fields=["client_waybill_enabled"])

        printed = http.get(
            reverse("v1:gate-out-waybill", args=[gate_out_id]),
            {"output": "html"},
            **auth(token),
        )
        assert printed.status_code == 200, printed.content
        body = printed.content.decode()
        assert "Return to client" in body
        # T6.5: tenant branding and line detail.
        assert "Silvertech" in body
        assert "Their faulty RRU" in body

    def test_an_acknowledged_waybill_prints_as_acknowledged(
        self, signed_in, released_return
    ):
        """The same document doubles as the file copy (K3)."""
        http, token, organization, _owner = signed_in
        gate_out_id, _client, _item = released_return

        with tenant_context(organization):
            settings_row = organization.settings
            settings_row.client_waybill_enabled = True
            settings_row.save(update_fields=["client_waybill_enabled"])

        http.post(
            reverse("v1:client-return-ack-list"),
            {
                "gate_out": gate_out_id,
                "acknowledged_ref": "SAF-GRN-99001",
                "acknowledged_at": "2026-08-21T09:00:00+03:00",
            },
            content_type="application/json",
            **auth(token),
        )

        printed = http.get(
            reverse("v1:gate-out-waybill", args=[gate_out_id]),
            {"output": "html"},
            **auth(token),
        )

        assert printed.status_code == 200
        body = printed.content.decode()
        assert "SAF-GRN-99001" in body
        assert "Responsibility for this material passed" in body

    def test_a_waybill_is_refused_for_a_pass_that_is_not_a_return(
        self, signed_in, quarantined_stock
    ):
        http, token, organization, owner = signed_in
        yard, _quarantine, ours, _theirs, _client = quarantined_stock

        with tenant_context(organization):
            settings_row = organization.settings
            settings_row.client_waybill_enabled = True
            settings_row.save(update_fields=["client_waybill_enabled"])

            from locations.nodes import external_node, node_for_location
            from stock.models import MovementType
            from stock.services import MovementRequest, post_movement

            with transaction.atomic():
                post_movement(
                    MovementRequest(
                        item_type=ours,
                        quantity=Decimal("5"),
                        from_node=external_node(organization.pk),
                        to_node=node_for_location(yard),
                        movement_type=MovementType.RECEIPT,
                    )
                )

        from network.factories import SiteFactory

        with tenant_context(organization):
            site = SiteFactory(internal_ref="WB-1", name="Waybill site")

        created = http.post(
            reverse("v1:gate-out-list"),
            {
                "purpose_type": "INSTALLATION",
                "from_location": yard.pk,
                "site": site.pk,
                "custody_holder": owner.pk,
                "lines": [
                    {
                        "item_type": ours.pk,
                        "tracking_mode": "BULK",
                        "requested_qty": "1",
                        "uom": "ea",
                    }
                ],
            },
            content_type="application/json",
            **auth(token),
        ).json()

        response = http.get(
            reverse("v1:gate-out-waybill", args=[created["id"]]),
            {"output": "html"},
            **auth(token),
        )

        assert response.status_code == 400
        assert "purpose_type" in response.json()["error"]["field_errors"]

    def test_the_signature_can_be_recorded_without_a_timestamp(
        self, signed_in, released_return
    ):
        """The ordinary case: somebody is standing there with the signed copy.

        Found by driving the screen's own payload: ``acknowledged_at`` was
        required, so recording an acknowledgement *now* — what the button does —
        was a 400, while backdating one worked.
        """
        http, token, _organization, _owner = signed_in
        gate_out_id, _client, _item = released_return

        response = http.post(
            reverse("v1:client-return-ack-list"),
            {"gate_out": gate_out_id, "acknowledged_ref": "SAF-GRN-NOW-1"},
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 201, response.content
        assert response.json()["acknowledged_at"]
