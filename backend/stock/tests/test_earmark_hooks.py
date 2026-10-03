"""T13.2 — ledger hook for earmarks (§4.16.3; Q2, Q3)."""

from decimal import Decimal

import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext

from catalogue.factories import ItemTypeFactory
from catalogue.models import TrackingMode
from locations.factories import StoreFactory, YardFactory
from locations.nodes import consumed_node, external_node, node_for_site
from network.factories import SiteFactory
from stock.factories import ReelFactory, SerialUnitFactory
from stock.models import (
    BulkEarmark,
    Condition,
    EarmarkAction,
    EarmarkEvent,
    MovementType,
    ReelStatus,
)
from stock.services import (
    EarmarkDiversionNeedsReason,
    MovementRequest,
    post_movement,
)
from stock.verification import verify_ledger


def post(**kwargs):
    with transaction.atomic():
        return post_movement(MovementRequest(**kwargs))


@pytest.fixture
def yard(tenant):
    return YardFactory(name="Main yard")


@pytest.fixture
def store(yard):
    return StoreFactory(name="Store B", parent=yard)


@pytest.fixture
def alpha(tenant):
    return SiteFactory(name="Alpha")


@pytest.fixture
def bravo(tenant):
    return SiteFactory(name="Bravo")


@pytest.fixture
def bulk(tenant):
    return ItemTypeFactory(name="Jumper", default_tracking_mode=TrackingMode.BULK)


def clean(tenant):
    result = verify_ledger(tenant.pk)
    assert result.ok, [str(d) for d in result.drifts]


def receive_unit(tenant, node):
    unit = SerialUnitFactory(current_node=external_node(tenant.pk))
    post(
        item_type=unit.item_type,
        quantity=Decimal("1"),
        from_node=external_node(tenant.pk),
        to_node=node,
        movement_type=MovementType.RECEIPT,
        tracking_mode=TrackingMode.SERIALIZED,
        serial_unit=unit,
    )
    unit.refresh_from_db()
    return unit


def move_unit(unit, from_node, to_node, movement_type=MovementType.ISSUE, **extra):
    return post(
        item_type=unit.item_type,
        quantity=Decimal("1"),
        from_node=from_node,
        to_node=to_node,
        movement_type=movement_type,
        tracking_mode=TrackingMode.SERIALIZED,
        serial_unit=unit,
        **extra,
    )


def receive_bulk(tenant, node, item, quantity):
    return post(
        item_type=item,
        quantity=Decimal(quantity),
        from_node=external_node(tenant.pk),
        to_node=node,
        movement_type=MovementType.RECEIPT,
    )


def send_bulk(item, from_node, to_node, quantity, movement_type=MovementType.ISSUE, **extra):
    return post(
        item_type=item,
        quantity=Decimal(quantity),
        from_node=from_node,
        to_node=to_node,
        movement_type=movement_type,
        **extra,
    )


def earmark(site, node, item, quantity):
    return BulkEarmark.objects.create(
        site=site,
        node=node,
        item_type=item,
        condition=Condition.NEW,
        quantity=Decimal(quantity),
    )


def claims(node, item):
    return {
        c.site.name: c.quantity
        for c in BulkEarmark.objects.filter(node=node, item_type=item).select_related("site")
    }


def events(action):
    return list(EarmarkEvent.objects.filter(action=action).order_by("id"))


class TestUnits:
    def test_a_unit_delivered_to_its_site(self, tenant, yard, alpha):
        unit = receive_unit(tenant, yard.node)
        unit.earmark_site = alpha
        unit.save()
        move_unit(unit, yard.node, node_for_site(alpha), for_sites=frozenset({alpha}))
        unit.refresh_from_db()
        assert unit.earmark_site is None
        (event,) = events(EarmarkAction.DELIVERED)
        assert event.site == alpha and event.serial_unit == unit
        assert event.document_type == "" and event.occurred_at is not None
        clean(tenant)

    def test_a_unit_diverted_with_a_reason(self, tenant, yard, alpha, bravo):
        unit = receive_unit(tenant, yard.node)
        unit.earmark_site = alpha
        unit.save()
        move_unit(
            unit,
            yard.node,
            node_for_site(bravo),
            for_sites=frozenset({bravo}),
            divert_reason="Alpha was cancelled",
            document_type="GateOut",
            document_number="GO-1",
        )
        unit.refresh_from_db()
        assert unit.earmark_site is None
        (event,) = events(EarmarkAction.DIVERTED)
        assert event.site == alpha and event.to_site == bravo
        assert event.reason == "Alpha was cancelled"
        assert event.document_number == "GO-1"
        clean(tenant)

    def test_a_unit_refused_without_a_reason(self, tenant, yard, alpha, bravo):
        unit = receive_unit(tenant, yard.node)
        unit.earmark_site = alpha
        unit.save()
        with pytest.raises(EarmarkDiversionNeedsReason) as raised:
            move_unit(unit, yard.node, node_for_site(bravo), for_sites=frozenset({bravo}))
        assert "Alpha" in str(raised.value)
        assert raised.value.code == "EARMARK_DIVERSION_NEEDS_REASON"
        unit.refresh_from_db()
        assert unit.earmark_site == alpha
        assert not EarmarkEvent.objects.exists()

    def test_no_sites_at_all_is_a_diversion_too(self, tenant, yard, alpha):
        unit = receive_unit(tenant, yard.node)
        unit.earmark_site = alpha
        unit.save()
        with pytest.raises(EarmarkDiversionNeedsReason):
            move_unit(unit, yard.node, consumed_node(tenant.pk))

    def test_a_unit_moved_to_another_store_keeps_its_earmark(self, tenant, yard, store, alpha):
        unit = receive_unit(tenant, yard.node)
        unit.earmark_site = alpha
        unit.save()
        move_unit(unit, yard.node, store.node, MovementType.TRANSFER)
        unit.refresh_from_db()
        assert unit.earmark_site == alpha
        (event,) = events(EarmarkAction.MOVED)
        assert event.site == alpha and event.node == store.node
        clean(tenant)

    def test_an_unearmarked_unit_writes_nothing(self, tenant, yard, alpha):
        unit = receive_unit(tenant, yard.node)
        move_unit(unit, yard.node, node_for_site(alpha))
        assert not EarmarkEvent.objects.exists()


class TestDrums:
    def build(self, tenant, yard, site):
        item = ItemTypeFactory(name="Fibre", default_tracking_mode=TrackingMode.REEL, uom="m")
        reel = ReelFactory(
            item_type=item,
            drum_number="D-1",
            current_node=external_node(tenant.pk),
            initial_length=Decimal("500"),
            remaining_length=Decimal("500"),
        )
        post(
            item_type=item,
            quantity=Decimal("500"),
            from_node=external_node(tenant.pk),
            to_node=yard.node,
            movement_type=MovementType.RECEIPT,
            tracking_mode=TrackingMode.REEL,
            reel=reel,
        )
        reel.earmark_site = site
        reel.save()
        return item, reel

    def cut(self, item, reel, yard, to, length, **extra):
        return post(
            item_type=item,
            quantity=Decimal(length),
            from_node=yard.node,
            to_node=to,
            movement_type=MovementType.ISSUE,
            tracking_mode=TrackingMode.REEL,
            reel=reel,
            **extra,
        )

    def test_a_drum_issued_partly_keeps_its_earmark_and_records_the_length(
        self, tenant, yard, alpha
    ):
        item, reel = self.build(tenant, yard, alpha)
        self.cut(item, reel, yard, node_for_site(alpha), "120", for_sites=frozenset({alpha}))
        reel.refresh_from_db()
        assert reel.remaining_length == Decimal("380")
        assert reel.earmark_site == alpha
        (event,) = events(EarmarkAction.DELIVERED)
        assert event.reel == reel and event.quantity == Decimal("120")
        clean(tenant)

        # The rest goes: the drum closes and its earmark is used up.
        self.cut(item, reel, yard, node_for_site(alpha), "380", for_sites=frozenset({alpha}))
        reel.refresh_from_db()
        assert reel.status == ReelStatus.CLOSED and reel.earmark_site is None
        assert [e.quantity for e in events(EarmarkAction.DELIVERED)] == [
            Decimal("120"),
            Decimal("380"),
        ]
        clean(tenant)

    def test_a_cut_to_another_site_needs_a_reason(self, tenant, yard, alpha, bravo):
        item, reel = self.build(tenant, yard, alpha)
        with pytest.raises(EarmarkDiversionNeedsReason) as raised:
            self.cut(item, reel, yard, node_for_site(bravo), "50", for_sites=frozenset({bravo}))
        assert "D-1" in str(raised.value) and "Alpha" in str(raised.value)
        self.cut(
            item,
            reel,
            yard,
            node_for_site(bravo),
            "50",
            for_sites=frozenset({bravo}),
            divert_reason="Urgent",
        )
        reel.refresh_from_db()
        assert reel.earmark_site == alpha
        (event,) = events(EarmarkAction.DIVERTED)
        assert event.quantity == Decimal("50") and event.to_site == bravo

    def test_a_cut_that_stays_in_the_yard_records_nothing(self, tenant, yard, store, alpha):
        item, reel = self.build(tenant, yard, alpha)
        self.cut(item, reel, yard, store.node, "100")
        assert not EarmarkEvent.objects.exists()

    def test_a_whole_drum_moved_to_a_store_keeps_its_earmark(self, tenant, yard, store, alpha):
        item, reel = self.build(tenant, yard, alpha)
        self.cut(item, reel, yard, store.node, "500")
        reel.refresh_from_db()
        assert reel.earmark_site == alpha and reel.current_node == store.node
        assert len(events(EarmarkAction.MOVED)) == 1
        clean(tenant)


class TestBulk:
    @pytest.fixture
    def stocked(self, tenant, yard, bulk, alpha, bravo):
        """40 in the yard: 10 for Alpha, 15 for Bravo, 15 free."""
        receive_bulk(tenant, yard.node, bulk, "40")
        earmark(alpha, yard.node, bulk, "10")
        earmark(bravo, yard.node, bulk, "15")
        return yard.node

    def test_own_then_free_needs_no_reason(self, tenant, stocked, bulk, alpha, bravo):
        send_bulk(bulk, stocked, node_for_site(alpha), "25", for_sites=frozenset({alpha}))
        assert claims(stocked, bulk) == {"Bravo": Decimal("15")}
        (event,) = events(EarmarkAction.DELIVERED)
        assert event.site == alpha and event.quantity == Decimal("10")
        clean(tenant)

    def test_then_other_sites_with_a_reason(self, tenant, stocked, bulk, alpha, bravo):
        send_bulk(
            bulk,
            stocked,
            node_for_site(alpha),
            "30",
            for_sites=frozenset({alpha}),
            divert_reason="Bravo slipped",
        )
        assert claims(stocked, bulk) == {"Bravo": Decimal("10")}
        (delivered,) = events(EarmarkAction.DELIVERED)
        (diverted,) = events(EarmarkAction.DIVERTED)
        assert delivered.quantity == Decimal("10")
        assert diverted.site == bravo and diverted.to_site == alpha
        assert diverted.quantity == Decimal("5") and diverted.reason == "Bravo slipped"
        clean(tenant)

    def test_to_site_bravo_draws_bravo_first(self, tenant, stocked, bulk, alpha, bravo):
        # Bravo's 15, free 15, then Alpha's 5.
        with pytest.raises(EarmarkDiversionNeedsReason) as raised:
            send_bulk(bulk, stocked, node_for_site(bravo), "35", for_sites=frozenset({bravo}))
        assert "Alpha (5)" in str(raised.value)
        assert claims(stocked, bulk) == {"Alpha": Decimal("10"), "Bravo": Decimal("15")}
        send_bulk(
            bulk,
            stocked,
            node_for_site(bravo),
            "35",
            for_sites=frozenset({bravo}),
            divert_reason="Alpha waits",
        )
        assert claims(stocked, bulk) == {"Alpha": Decimal("5")}
        assert [e.site for e in events(EarmarkAction.DELIVERED)] == [bravo]
        assert [e.site for e in events(EarmarkAction.DIVERTED)] == [alpha]
        clean(tenant)

    def test_with_no_destination_site_free_goes_first_then_by_name(
        self, tenant, stocked, bulk, alpha, bravo
    ):
        send_bulk(bulk, stocked, consumed_node(tenant.pk), "15")
        assert claims(stocked, bulk) == {"Alpha": Decimal("10"), "Bravo": Decimal("15")}
        with pytest.raises(EarmarkDiversionNeedsReason):
            send_bulk(bulk, stocked, consumed_node(tenant.pk), "12")

    def test_a_transfer_carries_earmarks_to_the_destination(
        self, tenant, stocked, store, bulk, alpha, bravo
    ):
        send_bulk(bulk, stocked, store.node, "30", movement_type=MovementType.TRANSFER)
        # Free 15 first, then Alpha's 10 (by name), then 5 of Bravo's.
        assert claims(stocked, bulk) == {"Bravo": Decimal("10")}
        assert claims(store.node, bulk) == {"Alpha": Decimal("10"), "Bravo": Decimal("5")}
        assert not events(EarmarkAction.DIVERTED)
        assert {e.node_id for e in events(EarmarkAction.MOVED)} == {store.node.pk}
        # A second transfer adds to the claim already there.
        send_bulk(bulk, stocked, store.node, "10", movement_type=MovementType.TRANSFER)
        assert claims(store.node, bulk) == {"Alpha": Decimal("10"), "Bravo": Decimal("15")}
        assert not claims(stocked, bulk)
        clean(tenant)

    def test_a_correction_takes_free_then_reduces_earmarks(
        self, tenant, stocked, bulk, alpha, bravo
    ):
        send_bulk(bulk, stocked, consumed_node(tenant.pk), "20", movement_type=MovementType.ADJUST)
        assert claims(stocked, bulk) == {"Alpha": Decimal("5"), "Bravo": Decimal("15")}
        (event,) = events(EarmarkAction.REDUCED)
        assert event.site == alpha and event.quantity == Decimal("5")
        clean(tenant)

        send_bulk(bulk, stocked, consumed_node(tenant.pk), "10", movement_type=MovementType.ADJUST)
        assert claims(stocked, bulk) == {"Bravo": Decimal("10")}
        clean(tenant)

    def test_a_reversal_reduces_too(self, tenant, yard, bulk, alpha):
        receipt = receive_bulk(tenant, yard.node, bulk, "10")
        earmark(alpha, yard.node, bulk, "10")
        from stock.services import reverse_movement

        with transaction.atomic():
            reverse_movement(receipt)
        assert not BulkEarmark.objects.exists()
        assert len(events(EarmarkAction.REDUCED)) == 1
        clean(tenant)

    def test_nothing_earmarked_costs_one_query(self, tenant, yard, bulk, alpha):
        receive_bulk(tenant, yard.node, bulk, "40")
        with CaptureQueriesContext(connection) as queries:
            send_bulk(bulk, yard.node, node_for_site(alpha), "5", for_sites=frozenset({alpha}))
        sql = [q["sql"] for q in queries.captured_queries]
        assert sum("stock_bulkearmark" in s for s in sql) == 1
        assert not any("stock_earmarkevent" in s for s in sql)

    def test_receipts_add_nothing(self, tenant, yard, bulk):
        with CaptureQueriesContext(connection) as queries:
            receive_bulk(tenant, yard.node, bulk, "40")
        assert not any("stock_bulkearmark" in q["sql"] for q in queries.captured_queries)


class TestVerification:
    def test_earmarks_beyond_the_balance_are_drift(self, tenant, yard, bulk, alpha):
        receive_bulk(tenant, yard.node, bulk, "10")
        earmark(alpha, yard.node, bulk, "10")
        clean(tenant)
        BulkEarmark.objects.update(quantity=Decimal("11"))
        kinds = [d.kind for d in verify_ledger(tenant.pk).drifts]
        assert kinds == ["earmarks exceed balance"]

    def test_an_earmarked_unit_outside_the_yard_is_drift(self, tenant, yard, alpha):
        unit = receive_unit(tenant, yard.node)
        move_unit(unit, yard.node, node_for_site(alpha))
        clean(tenant)
        unit.earmark_site = alpha
        unit.save()
        kinds = [d.kind for d in verify_ledger(tenant.pk).drifts]
        assert kinds == ["earmarked unit outside the yard"]

    def test_an_earmarked_open_drum_outside_the_yard_is_drift(self, tenant, yard, alpha):
        item, reel = TestDrums().build(tenant, yard, None)
        TestDrums().cut(item, reel, yard, node_for_site(alpha), "100")
        clean(tenant)
        # The drum stays in the yard, so move its node out of the perimeter.
        reel.current_node = node_for_site(alpha)
        reel.earmark_site = alpha
        reel.save()
        kinds = [d.kind for d in verify_ledger(tenant.pk).drifts]
        assert "earmarked drum outside the yard" in kinds
