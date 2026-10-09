"""T14.3 — ``GET /stock/find`` and ``GET /stock/summary`` (§7.3d, E8)."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
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
    return client, token, result["organization"]


def auth(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def receive(item, quantity, to_node, organization):
    from locations.nodes import external_node
    from stock.models import MovementType
    from stock.services import MovementRequest, post_movement

    with transaction.atomic():
        post_movement(
            MovementRequest(
                item_type=item,
                quantity=Decimal(quantity),
                from_node=external_node(organization.pk),
                to_node=to_node,
                movement_type=MovementType.RECEIPT,
            )
        )


def item(name, code="", **kwargs):
    from catalogue.factories import ItemTypeFactory
    from catalogue.models import TrackingMode

    kwargs.setdefault("default_tracking_mode", TrackingMode.BULK)
    return ItemTypeFactory(name=name, code=code, **kwargs)


def find(http, token, q):
    r = http.get(reverse("v1:stock-find"), {"q": q}, **auth(token))
    assert r.status_code == 200, r.content
    return r.json()["results"]


class TestFind:
    def test_ranks_by_name_start_then_name_then_code_and_stock_comes_first(
        self, signed_in
    ):
        http, token, organization = signed_in
        with tenant_context(organization):
            from locations.factories import YardFactory
            from locations.nodes import node_for_location

            yard_node = node_for_location(YardFactory())
            held_code = item("Zeta", code="XFIBRE-1")
            starts = item("Xfibre patch lead")
            contains = item("Armoured xfibre")
            nothing = item("Xfibre splice tray")
            receive(held_code, "3", yard_node, organization)
            receive(contains, "5", yard_node, organization)
            receive(starts, "2.5", yard_node, organization)
            del nothing

        names = [row["name"] for row in find(http, token, "xfibre")]
        # Held first, ranked: name starts, name contains, code contains. Then the
        # one we hold none of.
        assert names == ["Xfibre patch lead", "Armoured xfibre", "Zeta", "Xfibre splice tray"]

    def test_row_shape_and_decimal_string(self, signed_in):
        http, token, organization = signed_in
        with tenant_context(organization):
            from locations.factories import YardFactory
            from locations.nodes import node_for_location

            thing = item("Qjumper lead", code="JL-1", uom="ea")
            receive(thing, "2.5", node_for_location(YardFactory()), organization)

        [row] = find(http, token, "qjumper")
        assert row == {
            "id": thing.pk,
            "code": "JL-1",
            "name": "Qjumper lead",
            "unit": "ea",
            "on_hand": "2.500",
        }

    def test_on_hand_counts_only_nodes_inside_the_perimeter(self, signed_in):
        http, token, organization = signed_in
        with tenant_context(organization):
            from locations.factories import StoreFactory, VehicleFactory, YardFactory
            from locations.models import LocationType
            from locations.nodes import node_for_location

            yard = YardFactory()
            store = StoreFactory(parent=yard)
            vehicle = VehicleFactory()
            quarantine = yard.children.get(type=LocationType.QUARANTINE)
            thing = item("Zsplitter")
            receive(thing, "10", node_for_location(yard), organization)
            receive(thing, "5", node_for_location(store), organization)
            receive(thing, "1", node_for_location(quarantine), organization)
            receive(thing, "100", node_for_location(vehicle), organization)

        [row] = find(http, token, "zsplitter")
        assert row["on_hand"] == "16.000"

    def test_nothing_held_is_still_listed_with_zero(self, signed_in):
        http, token, organization = signed_in
        with tenant_context(organization):
            item("Zcabinet key")
        assert find(http, token, "zcabinet")[0]["on_hand"] == "0.000"

    def test_archived_items_are_left_out(self, signed_in):
        http, token, organization = signed_in
        with tenant_context(organization):
            item("Old zrouter", is_archived=True)
        assert find(http, token, "zrouter") == []

    def test_blank_or_one_character_gives_nothing(self, signed_in):
        http, token, organization = signed_in
        with tenant_context(organization):
            item("Zrouter")
        assert find(http, token, "") == []
        assert find(http, token, " r ") == []
        assert find(http, token, "r") == []

    def test_at_most_eight(self, signed_in):
        http, token, organization = signed_in
        with tenant_context(organization):
            for n in range(10):
                item(f"Bracket {n}")
        assert len(find(http, token, "bracket")) == 8

    def test_needs_a_signed_in_user(self, signed_in):
        http, _token, _organization = signed_in
        assert http.get(reverse("v1:stock-find"), {"q": "ab"}).status_code in (401, 403)

    def test_the_query_count_does_not_grow_with_the_rows(self, signed_in):
        http, token, organization = signed_in

        def queries():
            with CaptureQueriesContext(connection) as captured:
                assert http.get(reverse("v1:stock-find"), {"q": "item"}, **auth(token))
            return len(captured)

        queries()
        before = queries()
        with tenant_context(organization):
            from locations.factories import YardFactory
            from locations.nodes import node_for_location

            node = node_for_location(YardFactory())
            for n in range(6):
                receive(item(f"Item {n}"), "4", node, organization)
        assert queries() == before


def make_world(organization, sites=6):
    """Six sites earmarked in different ways; returns the sites, largest first.

    Site 0 has 3 items (a unit, a drum and a bulk claim), site 1 has 2, sites 2
    to 5 have 1 each, so the tie among the last four breaks by name.
    """
    from locations.factories import YardFactory
    from locations.nodes import node_for_location
    from network.factories import ClientFactory, SiteFactory
    from stock.factories import ReelFactory, SerialUnitFactory
    from stock.models import BulkEarmark

    node = node_for_location(YardFactory())
    client_ = ClientFactory()
    made = [SiteFactory(client=client_, name=f"Site {n}") for n in range(sites)]

    def unit(site):
        return SerialUnitFactory(current_node=node, earmark_site=site)

    unit(made[0])
    ReelFactory(current_node=node, earmark_site=made[0])
    bulk = item(f"Bulk lot {organization.slug}-{sites}-{BulkEarmark.objects.count()}")
    BulkEarmark.objects.create(
        organization=organization,
        site=made[0],
        node=node,
        item_type=bulk,
        condition="NEW",
        quantity=Decimal("1"),
    )
    # A second unit of the same item for the same site is still one item.
    if sites < 2:
        return made
    first = unit(made[1])
    SerialUnitFactory(item_type=first.item_type, current_node=node, earmark_site=made[1])
    BulkEarmark.objects.create(
        organization=organization,
        site=made[1],
        node=node,
        item_type=bulk,
        condition="NEW",
        quantity=Decimal("1"),
    )
    for site in made[2:]:
        unit(site)
    return made


class TestSummary:
    def url(self):
        return reverse("v1:stock-summary")

    def test_figures_on_a_scenario_with_six_earmarked_sites(self, signed_in):
        http, token, organization = signed_in
        with tenant_context(organization):
            from locations.factories import VehicleFactory, YardFactory
            from locations.nodes import node_for_location

            node = node_for_location(YardFactory())
            for n in range(3):
                receive(item(f"Stocked {n}"), "2", node, organization)
            receive(
                item("Out of the yard"), "9", node_for_location(VehicleFactory()), organization
            )
            made = make_world(organization)

        body = http.get(self.url(), **auth(token)).json()
        assert body["items_in_stock"] == 3
        assert body["deliveries_7d"] == 0
        assert [(e["name"], e["items"]) for e in body["earmarks"]] == [
            ("Site 0", 3),
            ("Site 1", 2),
            ("Site 2", 1),
            ("Site 3", 1),
            ("Site 4", 1),
        ]
        assert body["earmarks"][0]["site"] == made[0].pk
        assert body["earmark_sites_more"] == 1

    def test_deliveries_count_posted_in_the_last_week_and_not_voided(self, signed_in):
        http, token, organization = signed_in
        with tenant_context(organization):
            from locations.factories import YardFactory
            from receiving.models import DocumentStatus, GateIn, GateInSource

            yard = YardFactory()
            now = timezone.now()

            def gate_in(status, posted_at):
                GateIn.objects.create(
                    organization=organization,
                    source_type=GateInSource.PURCHASE,
                    supplier_name="Acme",
                    to_location=yard,
                    received_at=now,
                    status=status,
                    number=f"GI-{GateIn.objects.count() + 1}"
                    if status != DocumentStatus.DRAFT
                    else "",
                    posted_at=posted_at,
                )

            gate_in(DocumentStatus.POSTED, now - timedelta(days=1))
            gate_in(DocumentStatus.POSTED, now - timedelta(days=6))
            gate_in(DocumentStatus.POSTED, now - timedelta(days=8))
            gate_in(DocumentStatus.VOID, now - timedelta(days=1))
            gate_in(DocumentStatus.DRAFT, None)

        assert http.get(self.url(), **auth(token)).json()["deliveries_7d"] == 2

    def test_an_empty_yard(self, signed_in):
        http, token, _organization = signed_in
        assert http.get(self.url(), **auth(token)).json() == {
            "items_in_stock": 0,
            "deliveries_7d": 0,
            "earmarks": [],
            "earmark_sites_more": 0,
        }

    def test_a_unit_no_longer_in_stock_does_not_count_as_earmarked(self, signed_in):
        http, token, organization = signed_in
        with tenant_context(organization):
            from locations.factories import YardFactory
            from locations.nodes import node_for_location
            from network.factories import ClientFactory, SiteFactory
            from stock.factories import SerialUnitFactory
            from stock.models import SerialUnitStatus

            site = SiteFactory(client=ClientFactory())
            SerialUnitFactory(
                current_node=node_for_location(YardFactory()),
                earmark_site=site,
                status=SerialUnitStatus.INSTALLED,
            )
        assert http.get(self.url(), **auth(token)).json()["earmarks"] == []

    def test_the_query_count_is_constant(self, signed_in):
        http, token, organization = signed_in

        def queries():
            with CaptureQueriesContext(connection) as captured:
                assert http.get(self.url(), **auth(token)).status_code == 200
            return len(captured)

        # One earmark first: with none, the site-name lookup is skipped, which
        # would make the "before" figure one short.
        with tenant_context(organization):
            make_world(organization, sites=1)
        queries()
        before = queries()
        with tenant_context(organization):
            make_world(organization)
        assert queries() == before


class TestIsolation:
    def test_another_tenants_stock_and_earmarks_are_invisible(self, signed_in):
        http, token, organization = signed_in
        other = provision_tenant(
            name="Rival", slug="rival", owner_email="owner@rival.co.ke"
        )["organization"]
        with tenant_context(other):
            from locations.factories import YardFactory
            from locations.nodes import node_for_location

            receive(item("Rival xfibre"), "7", node_for_location(YardFactory()), other)
            make_world(other)
        with tenant_context(organization):
            item("Our xfibre")

        assert [r["name"] for r in find(http, token, "xfibre")] == ["Our xfibre"]
        assert find(http, token, "xfibre")[0]["on_hand"] == "0.000"
        assert http.get(reverse("v1:stock-summary"), **auth(token)).json() == {
            "items_in_stock": 0,
            "deliveries_7d": 0,
            "earmarks": [],
            "earmark_sites_more": 0,
        }
