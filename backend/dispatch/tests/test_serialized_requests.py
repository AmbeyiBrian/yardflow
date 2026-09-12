"""A serialized item cannot leave the yard anonymously (§4.7, §6; F1, D3, G2).

Found in the ledger of the running demo tenant, not by a test. The request screen
scans an RRU and posts its serial number; the gate-out line serializer declared
``serials`` read-only, so DRF dropped it without a word. The pass was raised,
approved and released as a *quantity* of a serialized item, and an antenna landed
on a technician's custody with no identity at all — absent from the serial
history, absent from the installed base, and nothing for the gate checklist (G1)
to check the physical load against.

Two halves, so a regression on either fails:

1. a scanned serial actually reaches the line
2. a serialized item with no serial and no explanation is refused
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
def a_serialized_radio(signed_in):
    """One serialized unit in the yard, and somewhere to send it."""
    _http, _token, organization, _owner = signed_in
    with tenant_context(organization):
        from django.db import transaction

        from catalogue.models import ItemCategory, ItemType, TrackingMode
        from locations.factories import YardFactory
        from locations.nodes import external_node
        from network.factories import SiteFactory
        from stock.models import MovementType, SerialUnit
        from stock.services import MovementRequest, post_movement

        yard = YardFactory(name="Serial yard")
        site = SiteFactory(internal_ref="SER-1", name="Serial site")
        category = ItemCategory.objects.create(organization=organization, name="Radios")
        item = ItemType.objects.create(
            organization=organization,
            category=category,
            name="RRU 2600",
            uom="ea",
            default_tracking_mode=TrackingMode.SERIALIZED,
        )
        unit = SerialUnit.objects.create(
            organization=organization,
            item_type=item,
            serial_number="RRU-IDENTITY-1",
            current_node=yard.node,
        )
        with transaction.atomic():
            post_movement(
                MovementRequest(
                    item_type=item,
                    quantity=Decimal("1"),
                    from_node=external_node(organization.pk),
                    to_node=yard.node,
                    movement_type=MovementType.RECEIPT,
                    tracking_mode="SERIALIZED",
                    serial_unit=unit,
                )
            )
    return yard, site, item, unit


def a_request(yard, site, owner, line):
    return {
        "purpose_type": "INSTALLATION",
        "from_location": yard.pk,
        "site": site.pk,
        "custody_holder": owner.pk,
        "lines": [line],
    }


class TestASerializedRequestNamesItsUnits:
    def test_a_scanned_serial_reaches_the_line(self, signed_in, a_serialized_radio):
        """F1: "serialized material is requested by identity, not by count"."""
        http, token, _organization, owner = signed_in
        yard, site, item, unit = a_serialized_radio

        response = http.post(
            reverse("v1:gate-out-list"),
            a_request(
                yard,
                site,
                owner,
                {
                    "item_type": item.pk,
                    "tracking_mode": "SERIALIZED",
                    "requested_qty": "1",
                    "uom": "ea",
                    "serials": [{"serial_unit": unit.pk}],
                },
            ),
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 201, response.content
        line = response.json()["lines"][0]
        # The identity is on the pass, which is what the gate checks the load
        # against (G1) and what the serial history will show later (E2).
        assert [entry["serial_number"] for entry in line["serials"]] == ["RRU-IDENTITY-1"]

    def test_a_serialized_item_cannot_go_out_as_an_anonymous_quantity(
        self, signed_in, a_serialized_radio
    ):
        """The exact request that produced a ghost antenna in the ledger."""
        http, token, _organization, owner = signed_in
        yard, site, item, _unit = a_serialized_radio

        response = http.post(
            reverse("v1:gate-out-list"),
            a_request(
                yard,
                site,
                owner,
                {
                    "item_type": item.pk,
                    "tracking_mode": "BULK",
                    "requested_qty": "1",
                    "uom": "ea",
                },
            ),
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 400, response.content
        assert "lines.0.tracking_mode" in response.json()["error"]["field_errors"]

    def test_untagged_stock_can_still_go_out_with_a_reason(
        self, signed_in, a_serialized_radio
    ):
        """D3's escape hatch, at the other gate.

        A yard really can hold untagged units — D3 allows a serialized item to be
        *received* as bulk when no serial is available, provided the system
        records why. Refusing to issue those would strand real stock, so the same
        recorded reason lets them out.
        """
        http, token, _organization, owner = signed_in
        yard, site, item, _unit = a_serialized_radio

        response = http.post(
            reverse("v1:gate-out-list"),
            a_request(
                yard,
                site,
                owner,
                {
                    "item_type": item.pk,
                    "tracking_mode": "BULK",
                    "requested_qty": "1",
                    "uom": "ea",
                    "no_serial_reason": "Received without a legible label (GRN-000014).",
                },
            ),
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 201, response.content
        assert response.json()["lines"][0]["no_serial_reason"]

    def test_the_count_and_the_named_units_have_to_agree(
        self, signed_in, a_serialized_radio
    ):
        """Three requested and one named is a pass nobody can fill (F1)."""
        http, token, _organization, owner = signed_in
        yard, site, item, unit = a_serialized_radio

        response = http.post(
            reverse("v1:gate-out-list"),
            a_request(
                yard,
                site,
                owner,
                {
                    "item_type": item.pk,
                    "tracking_mode": "SERIALIZED",
                    "requested_qty": "3",
                    "uom": "ea",
                    "serials": [{"serial_unit": unit.pk}],
                },
            ),
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 400, response.content
        assert "lines.0.serials" in response.json()["error"]["field_errors"]

    def test_a_serialized_line_with_no_units_named_is_refused(
        self, signed_in, a_serialized_radio
    ):
        """Zero named is the same failure as too few, not a softer one.

        Choosing a serialized item from a list without scanning it is the easy
        way to produce exactly the anonymous unit this file exists to prevent.
        """
        http, token, _organization, owner = signed_in
        yard, site, item, _unit = a_serialized_radio

        response = http.post(
            reverse("v1:gate-out-list"),
            a_request(
                yard,
                site,
                owner,
                {
                    "item_type": item.pk,
                    "tracking_mode": "SERIALIZED",
                    "requested_qty": "1",
                    "uom": "ea",
                    "serials": [],
                },
            ),
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 400, response.content
        assert "lines.0.serials" in response.json()["error"]["field_errors"]
