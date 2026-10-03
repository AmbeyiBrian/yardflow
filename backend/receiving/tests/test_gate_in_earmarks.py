"""T13.3 — gate-in earmarks (§4.16.4; Epic Q, Q1)."""

from decimal import Decimal
from uuid import uuid4

import pytest
from django.urls import reverse
from django.utils import timezone

from catalogue.factories import ItemTypeFactory
from catalogue.models import TrackingMode
from core.provisioning import provision_tenant
from core.tenancy import tenant_context
from locations.factories import YardFactory
from network.factories import SiteFactory
from network.models import SiteStatus
from receiving.models import GateInLine, GateInReel, GateInSerial
from receiving.services import post_gate_in, void_gate_in
from receiving.tests.test_gate_in import add_bulk_line, draft
from stock.models import BulkEarmark, EarmarkAction, EarmarkEvent, Reel, SerialUnit


@pytest.fixture
def yard(tenant):
    return YardFactory(name="Main yard")


def add_serialized_line(gate_in, serials, **kwargs):
    item = ItemTypeFactory(default_tracking_mode=TrackingMode.SERIALIZED)
    line = GateInLine.objects.create(
        organization=gate_in.organization,
        gate_in=gate_in,
        item_type=item,
        tracking_mode=TrackingMode.SERIALIZED,
        quantity=Decimal(len(serials)),
        uom="ea",
        **kwargs,
    )
    for number in serials:
        GateInSerial.objects.create(
            organization=gate_in.organization, line=line, serial_number=number
        )
    return line


def add_reel_line(gate_in, length="500", **kwargs):
    item = ItemTypeFactory(default_tracking_mode=TrackingMode.REEL, uom="m")
    line = GateInLine.objects.create(
        organization=gate_in.organization,
        gate_in=gate_in,
        item_type=item,
        tracking_mode=TrackingMode.REEL,
        quantity=Decimal(length),
        uom="m",
        **kwargs,
    )
    GateInReel.objects.create(
        organization=gate_in.organization, line=line, drum_number="D-1", length=Decimal(length)
    )
    return line


class TestPostingEarmarks:
    def test_a_delivery_for_a_site_earmarks_units_drums_and_bulk(self, tenant, yard):
        site = SiteFactory()
        gate_in = draft(tenant, yard, for_site=site)
        add_serialized_line(gate_in, ["RRU-1", "RRU-2"])
        add_reel_line(gate_in)
        bulk = add_bulk_line(gate_in, quantity=25)

        post_gate_in(gate_in)

        assert set(SerialUnit.objects.values_list("earmark_site", flat=True)) == {site.pk}
        assert Reel.objects.get().earmark_site == site
        claim = BulkEarmark.objects.get()
        assert (claim.site, claim.node, claim.item_type, claim.condition, claim.quantity) == (
            site,
            yard.node,
            bulk.item_type,
            bulk.condition,
            Decimal("25"),
        )
        events = EarmarkEvent.objects.all()
        assert events.count() == 4
        assert {e.action for e in events} == {EarmarkAction.EARMARKED}
        assert {e.document_number for e in events} == {gate_in.number}
        assert {e.document_type for e in events} == {"receiving.GateIn"}
        assert events.get(reel__isnull=False).quantity == Decimal("500")
        assert events.get(item_type__isnull=False).quantity == Decimal("25")

    def test_a_second_delivery_adds_to_the_same_claim(self, tenant, yard):
        site = SiteFactory()
        item = ItemTypeFactory(default_tracking_mode=TrackingMode.BULK)
        for quantity in (10, 5):
            gate_in = draft(tenant, yard, for_site=site)
            add_bulk_line(gate_in, quantity=quantity, item=item)
            post_gate_in(gate_in)

        assert BulkEarmark.objects.get().quantity == Decimal("15")

    def test_a_line_can_override_the_delivery_site(self, tenant, yard):
        site_x, site_y = SiteFactory(), SiteFactory()
        gate_in = draft(tenant, yard, for_site=site_x)
        add_serialized_line(gate_in, ["A-1"], for_site=site_y)
        add_serialized_line(gate_in, ["B-1"])

        post_gate_in(gate_in)

        assert SerialUnit.objects.get(serial_number="A-1").earmark_site == site_y
        assert SerialUnit.objects.get(serial_number="B-1").earmark_site == site_x

    def test_a_line_alone_can_name_a_site(self, tenant, yard):
        site = SiteFactory()
        gate_in = draft(tenant, yard)
        add_bulk_line(gate_in, quantity=3, for_site=site)
        add_bulk_line(gate_in, quantity=4)

        post_gate_in(gate_in)

        assert BulkEarmark.objects.get().quantity == Decimal("3")

    def test_no_site_anywhere_earmarks_nothing(self, tenant, yard):
        gate_in = draft(tenant, yard)
        add_serialized_line(gate_in, ["S-1"])
        add_reel_line(gate_in)
        add_bulk_line(gate_in)

        post_gate_in(gate_in)

        assert not EarmarkEvent.objects.exists()
        assert not BulkEarmark.objects.exists()
        assert SerialUnit.objects.get().earmark_site is None
        assert Reel.objects.get().earmark_site is None

    def test_a_decommissioned_site_cannot_be_posted_for(self, tenant, yard):
        from receiving.services import GateInNotReady

        site = SiteFactory(status=SiteStatus.DECOMMISSIONED)
        gate_in = draft(tenant, yard)
        add_bulk_line(gate_in, for_site=site)

        with pytest.raises(GateInNotReady) as caught:
            post_gate_in(gate_in)
        assert "lines.0.for_site" in caught.value.field_errors


class TestVoid:
    def test_void_clears_what_it_earmarked(self, tenant, yard):
        site = SiteFactory()
        item = ItemTypeFactory(default_tracking_mode=TrackingMode.BULK)
        earlier = draft(tenant, yard, for_site=site)
        add_bulk_line(earlier, quantity=10, item=item)
        post_gate_in(earlier)

        gate_in = draft(tenant, yard, for_site=site)
        add_serialized_line(gate_in, ["V-1"])
        add_reel_line(gate_in)
        add_bulk_line(gate_in, quantity=6, item=item)
        post_gate_in(gate_in)
        assert BulkEarmark.objects.get().quantity == Decimal("16")

        void_gate_in(gate_in, reason="Wrong delivery")

        assert SerialUnit.objects.get().earmark_site is None
        assert Reel.objects.get().earmark_site is None
        # Only this GRN's share is taken back; the earlier delivery's stays.
        assert BulkEarmark.objects.get().quantity == Decimal("10")
        cleared = EarmarkEvent.objects.filter(document_number=gate_in.number).exclude(
            action=EarmarkAction.EARMARKED
        )
        assert {e.action for e in cleared} == {EarmarkAction.CLEARED, EarmarkAction.REDUCED}
        assert all("Wrong delivery" in e.reason for e in cleared)

    def test_void_removes_a_claim_that_reaches_zero(self, tenant, yard):
        site = SiteFactory()
        gate_in = draft(tenant, yard, for_site=site)
        add_bulk_line(gate_in, quantity=6)
        post_gate_in(gate_in)

        void_gate_in(gate_in, reason="Mistake")

        assert not BulkEarmark.objects.exists()


# ---- API, and offline replay -------------------------------------------------


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
def api_world(signed_in):
    _http, _token, organization, _owner = signed_in
    with tenant_context(organization):
        yard = YardFactory(name="API yard")
        site = SiteFactory(internal_ref="X-1", name="Site X")
        item = ItemTypeFactory(name="Clamp", uom="ea")
    other = provision_tenant(name="Other Co", slug="otherco", owner_email="o@otherco.co.ke")
    with tenant_context(other["organization"]):
        foreign_site = SiteFactory(internal_ref="F-1")
    return yard, site, item, foreign_site


def payload(yard, item, **header):
    return {
        "source_type": "PURCHASE",
        "supplier_name": "Huawei Kenya",
        "to_location": yard.pk,
        "received_at": timezone.now().isoformat(),
        "lines": [
            {
                "item_type": item.pk,
                "tracking_mode": "BULK",
                "quantity": "10",
                "uom": "ea",
                "condition": "NEW",
            }
        ],
        **header,
    }


class TestApi:
    def test_for_site_is_written_and_read_with_its_name(self, signed_in, api_world):
        http, token, _org, _owner = signed_in
        yard, site, item, _foreign = api_world
        body = payload(yard, item, for_site=site.pk)
        body["lines"][0]["for_site"] = site.pk

        response = http.post(
            reverse("v1:gate-in-list"), body, content_type="application/json", **auth(token)
        )

        assert response.status_code == 201, response.content
        data = response.json()
        assert data["for_site"] == site.pk
        assert data["for_site_name"] == "Site X"
        assert data["lines"][0]["for_site_name"] == "Site X"

    def test_old_payloads_without_for_site_still_work(self, signed_in, api_world):
        http, token, _org, _owner = signed_in
        yard, _site, item, _foreign = api_world

        response = http.post(
            reverse("v1:gate-in-list"),
            payload(yard, item),
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 201
        assert response.json()["for_site"] is None

    def test_another_tenants_site_is_refused(self, signed_in, api_world):
        http, token, _org, _owner = signed_in
        yard, _site, item, foreign = api_world

        header = http.post(
            reverse("v1:gate-in-list"),
            payload(yard, item, for_site=foreign.pk),
            content_type="application/json",
            **auth(token),
        )
        body = payload(yard, item)
        body["lines"][0]["for_site"] = foreign.pk
        line = http.post(
            reverse("v1:gate-in-list"), body, content_type="application/json", **auth(token)
        )

        assert header.status_code == 400
        assert "for_site" in str(header.json())
        assert line.status_code == 400
        assert "for_site" in str(line.json())

    def test_a_decommissioned_site_is_refused(self, signed_in, api_world):
        http, token, organization, _owner = signed_in
        yard, site, item, _foreign = api_world
        with tenant_context(organization):
            site.status = SiteStatus.DECOMMISSIONED
            site.save()

        response = http.post(
            reverse("v1:gate-in-list"),
            payload(yard, item, for_site=site.pk),
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 400
        assert "for_site" in str(response.json())

    def test_an_offline_replay_earmarks_once(self, signed_in, api_world):
        http, token, organization, _owner = signed_in
        yard, site, item, _foreign = api_world
        submission = {
            "client_uuid": str(uuid4()),
            "operation": "GATE_IN",
            "payload": payload(yard, item, for_site=site.pk),
            "captured_at": timezone.now().isoformat(),
        }

        responses = [
            http.post(
                reverse("v1:sync-submissions"),
                {"submissions": [submission]},
                content_type="application/json",
                **auth(token),
            )
            for _ in range(2)
        ]

        assert [r.json()["applied"] for r in responses] == [1, 0]
        with tenant_context(organization):
            claim = BulkEarmark.objects.get()
            assert (claim.site, claim.quantity) == (site, Decimal("10"))
            assert EarmarkEvent.objects.count() == 1
