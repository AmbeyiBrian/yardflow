"""T11.4 — ledger hooks for boxes (§4.15.3; P5, P7, P9, P10)."""

from decimal import Decimal

import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts.factories import UserFactory
from catalogue.factories import ItemTypeFactory
from catalogue.models import TrackingMode
from locations.factories import StoreFactory, YardFactory
from locations.nodes import external_node, node_for_site, scrap_node
from network.factories import SiteFactory
from stock.factories import SerialUnitFactory
from stock.models import (
    Box,
    BoxAction,
    BoxBulkContent,
    BoxEvent,
    BoxSource,
    BoxStatus,
    Condition,
    MovementType,
    SerialUnit,
)
from stock.services import (
    BoxClaimShort,
    BoxedStockOnly,
    InsufficientStock,
    MovementRequest,
    post_movement,
    reverse_movement,
)
from stock.verification import verify_ledger


@pytest.fixture
def yard(tenant):
    return YardFactory(name="Main yard")


@pytest.fixture
def site(tenant):
    return node_for_site(SiteFactory())


@pytest.fixture
def bulk(tenant):
    return ItemTypeFactory(name="Jumper", default_tracking_mode=TrackingMode.BULK)


def post(**kwargs):
    with transaction.atomic():
        return post_movement(MovementRequest(**kwargs))


def receive_units(tenant, node, count, item_type=None):
    """Real receipts: each unit starts at the external node and is received in."""
    units = []
    for _ in range(count):
        unit = SerialUnitFactory(
            current_node=external_node(tenant.pk),
            **({"item_type": item_type} if item_type else {}),
        )
        item_type = unit.item_type
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
        units.append(unit)
    return units


def receive_bulk(tenant, node, item_type, quantity):
    return post(
        item_type=item_type,
        quantity=Decimal(quantity),
        from_node=external_node(tenant.pk),
        to_node=node,
        movement_type=MovementType.RECEIPT,
    )


def make_box(code, node, parent=None):
    return Box.objects.create(
        code=code,
        source=BoxSource.INTERNAL,
        current_node=node,
        parent=parent,
        depth=parent.depth + 1 if parent else 1,
    )


def put(units, box):
    SerialUnit.objects.filter(pk__in=[u.pk for u in units]).update(box=box)


def claim(box, item_type, quantity, condition=Condition.NEW):
    return BoxBulkContent.objects.create(
        box=box, item_type=item_type, condition=condition, quantity=Decimal(quantity)
    )


def issue_unit(unit, from_node, to_node, **extra):
    return post(
        item_type=unit.item_type,
        quantity=Decimal("1"),
        from_node=from_node,
        to_node=to_node,
        movement_type=MovementType.ISSUE,
        tracking_mode=TrackingMode.SERIALIZED,
        serial_unit=unit,
        **extra,
    )


def issue_bulk(item_type, from_node, to_node, quantity, **extra):
    return post(
        item_type=item_type,
        quantity=Decimal(quantity),
        from_node=from_node,
        to_node=to_node,
        movement_type=extra.pop("movement_type", MovementType.ISSUE),
        **extra,
    )


def no_drift(tenant):
    result = verify_ledger(tenant.pk)
    assert result.ok, result.summary()


class TestUnitLeavesItsBox:
    def test_an_issued_unit_leaves_its_box_and_the_rest_stay(self, tenant, yard, site):
        units = receive_units(tenant, yard.node, 3)
        box = make_box("CTN-1", yard.node)
        put(units, box)
        actor = UserFactory()

        issue_unit(
            units[0],
            yard.node,
            site,
            posted_by=actor,
            document_type="GATE_OUT",
            document_id="7",
            document_number="GO-0007",
        )

        units[0].refresh_from_db()
        assert units[0].box_id is None
        assert SerialUnit.objects.filter(box=box).count() == 2
        box.refresh_from_db()
        assert box.status == BoxStatus.OPEN
        event = BoxEvent.objects.get(box=box, action=BoxAction.UNIT_OUT)
        assert event.serial_unit == units[0]
        assert (event.document_type, event.document_id, event.document_number) == (
            "GATE_OUT",
            "7",
            "GO-0007",
        )
        assert event.actor == actor
        no_drift(tenant)

    def test_the_callers_instance_does_not_keep_the_box(self, tenant, yard, site):
        (unit,) = receive_units(tenant, yard.node, 2)[:1]
        put([unit], make_box("CTN-1", yard.node))
        unit.refresh_from_db()
        issue_unit(unit, yard.node, site)
        assert unit.box is None

    def test_the_last_unit_leaving_closes_the_box(self, tenant, yard, site):
        units = receive_units(tenant, yard.node, 2)
        box = make_box("CTN-1", yard.node)
        put(units, box)

        issue_unit(units[0], yard.node, site)
        box.refresh_from_db()
        assert box.status == BoxStatus.OPEN
        issue_unit(units[1], yard.node, site, document_number="GO-9")

        box.refresh_from_db()
        assert box.status == BoxStatus.CLOSED
        assert box.closed_at is not None
        closed = BoxEvent.objects.get(box=box, action=BoxAction.CLOSED)
        assert closed.document_number == "GO-9"
        no_drift(tenant)

    def test_a_pallet_emptied_by_issuing_its_units_closes_at_every_level(
        self, tenant, yard, site
    ):
        units = receive_units(tenant, yard.node, 2)
        pallet = make_box("PAL", yard.node)
        carton = make_box("CTN", yard.node, parent=pallet)
        inner = make_box("PACK", yard.node, parent=carton)
        put(units, inner)

        issue_unit(units[0], yard.node, site)
        for box in (pallet, carton, inner):
            box.refresh_from_db()
            assert box.status == BoxStatus.OPEN
        issue_unit(units[1], yard.node, site)

        for box in (pallet, carton, inner):
            box.refresh_from_db()
            assert box.status == BoxStatus.CLOSED
            assert BoxEvent.objects.filter(box=box, action=BoxAction.CLOSED).count() == 1
        no_drift(tenant)

    def test_a_pallet_with_another_open_box_stays_open(self, tenant, yard, site):
        a, b = receive_units(tenant, yard.node, 2)
        pallet = make_box("PAL", yard.node)
        carton_a = make_box("CTN-A", yard.node, parent=pallet)
        carton_b = make_box("CTN-B", yard.node, parent=pallet)
        put([a], carton_a)
        put([b], carton_b)

        issue_unit(a, yard.node, site)

        carton_a.refresh_from_db()
        pallet.refresh_from_db()
        assert carton_a.status == BoxStatus.CLOSED
        assert pallet.status == BoxStatus.OPEN

    def test_a_unit_moved_with_a_box_that_covers_it_stays_in_it(self, tenant, yard):
        store = StoreFactory(parent=yard)
        (unit,) = receive_units(tenant, yard.node, 1)
        pallet = make_box("PAL", yard.node)
        carton = make_box("CTN", yard.node, parent=pallet)
        put([unit], carton)

        for moving in (carton, pallet):
            post(
                item_type=unit.item_type,
                quantity=Decimal("1"),
                from_node=yard.node if moving is carton else store.node,
                to_node=store.node if moving is carton else yard.node,
                movement_type=MovementType.TRANSFER,
                tracking_mode=TrackingMode.SERIALIZED,
                serial_unit=unit,
                moving_box=moving,
            )
            unit.refresh_from_db()
            assert unit.box_id == carton.pk
        assert not BoxEvent.objects.filter(action=BoxAction.UNIT_OUT).exists()

    def test_moving_an_unrelated_box_does_not_cover_the_unit(self, tenant, yard):
        store = StoreFactory(parent=yard)
        (unit,) = receive_units(tenant, yard.node, 1)
        box = make_box("CTN", yard.node)
        other = make_box("OTHER", yard.node)
        put([unit], box)

        post(
            item_type=unit.item_type,
            quantity=Decimal("1"),
            from_node=yard.node,
            to_node=store.node,
            movement_type=MovementType.TRANSFER,
            tracking_mode=TrackingMode.SERIALIZED,
            serial_unit=unit,
            moving_box=other,
        )
        unit.refresh_from_db()
        assert unit.box_id is None


class TestBulkOutOfABox:
    def test_issuing_from_a_box_reduces_its_claim(self, tenant, yard, site, bulk):
        receive_bulk(tenant, yard.node, bulk, "10")
        box = make_box("CTN-1", yard.node)
        claim(box, bulk, "6")

        issue_bulk(
            bulk,
            yard.node,
            site,
            "2",
            from_box=box,
            document_type="GATE_OUT",
            document_id="3",
            document_number="GO-3",
        )

        assert BoxBulkContent.objects.get(box=box).quantity == Decimal("4")
        event = BoxEvent.objects.get(box=box, action=BoxAction.BULK_OUT)
        assert event.quantity == Decimal("2")
        assert event.item_type == bulk
        assert event.document_number == "GO-3"
        no_drift(tenant)

    def test_a_claim_taken_to_zero_is_deleted_and_the_box_closes(
        self, tenant, yard, site, bulk
    ):
        receive_bulk(tenant, yard.node, bulk, "10")
        box = make_box("CTN-1", yard.node)
        claim(box, bulk, "6")

        issue_bulk(bulk, yard.node, site, "6", from_box=box)

        assert not BoxBulkContent.objects.filter(box=box).exists()
        box.refresh_from_db()
        assert box.status == BoxStatus.CLOSED
        no_drift(tenant)

    def test_a_box_holding_less_than_asked_is_refused_naming_it(
        self, tenant, yard, site, bulk
    ):
        receive_bulk(tenant, yard.node, bulk, "10")
        box = make_box("CTN-1", yard.node)
        claim(box, bulk, "3")

        with pytest.raises(BoxClaimShort) as err:
            issue_bulk(bulk, yard.node, site, "5", from_box=box)

        assert err.value.code == "BOX_CLAIM_SHORT"
        assert err.value.status_code == 409
        assert "CTN-1" in str(err.value.message)
        assert "3" in str(err.value.message) and "5" in str(err.value.message)
        assert BoxBulkContent.objects.get(box=box).quantity == Decimal("3")

    def test_a_box_at_another_node_or_closed_is_refused(self, tenant, yard, site, bulk):
        receive_bulk(tenant, yard.node, bulk, "10")
        store = StoreFactory(parent=yard)
        receive_bulk(tenant, store.node, bulk, "10")
        elsewhere = make_box("CTN-S", store.node)
        claim(elsewhere, bulk, "6")

        with pytest.raises(BoxClaimShort):
            issue_bulk(bulk, yard.node, site, "2", from_box=elsewhere)

    def test_a_box_without_that_lot_is_refused(self, tenant, yard, site, bulk):
        receive_bulk(tenant, yard.node, bulk, "10")
        box = make_box("CTN-1", yard.node)
        with pytest.raises(BoxClaimShort):
            issue_bulk(bulk, yard.node, site, "2", from_box=box)

    def test_a_box_travelling_whole_leaves_its_claims_alone(self, tenant, yard, bulk):
        store = StoreFactory(parent=yard)
        receive_bulk(tenant, yard.node, bulk, "10")
        box = make_box("CTN-1", yard.node)
        claim(box, bulk, "6")

        issue_bulk(
            bulk,
            yard.node,
            store.node,
            "6",
            movement_type=MovementType.TRANSFER,
            moving_box=box,
        )

        assert BoxBulkContent.objects.get(box=box).quantity == Decimal("6")
        assert not BoxEvent.objects.filter(action=BoxAction.BULK_OUT).exists()


class TestBulkOutOfLooseStock:
    def test_loose_stock_can_be_issued_without_a_box(self, tenant, yard, site, bulk):
        receive_bulk(tenant, yard.node, bulk, "10")
        box = make_box("CTN-1", yard.node)
        claim(box, bulk, "6")

        issue_bulk(bulk, yard.node, site, "4")

        assert BoxBulkContent.objects.get(box=box).quantity == Decimal("6")
        no_drift(tenant)

    def test_more_than_loose_is_refused_naming_the_boxes(self, tenant, yard, site, bulk):
        receive_bulk(tenant, yard.node, bulk, "10")
        claim(make_box("CTN-B", yard.node), bulk, "3")
        claim(make_box("ctn-a", yard.node), bulk, "3")

        with pytest.raises(BoxedStockOnly) as err:
            issue_bulk(bulk, yard.node, site, "5")

        assert err.value.code == "BOXED_STOCK_ONLY"
        assert err.value.status_code == 409
        assert err.value.details == {"boxes": ["ctn-a", "CTN-B"], "loose": "4.000"}
        assert "ctn-a" in str(err.value.message) and "CTN-B" in str(err.value.message)

    def test_a_true_over_issue_is_still_not_enough_stock(self, tenant, yard, site, bulk):
        receive_bulk(tenant, yard.node, bulk, "10")
        claim(make_box("CTN-1", yard.node), bulk, "6")

        with pytest.raises(InsufficientStock):
            issue_bulk(bulk, yard.node, site, "11")

    def test_claims_of_another_condition_do_not_count_against_this_lot(
        self, tenant, yard, site, bulk
    ):
        receive_bulk(tenant, yard.node, bulk, "10")
        claim(make_box("CTN-1", yard.node), bulk, "6", condition=Condition.USED_SERVICEABLE)

        issue_bulk(bulk, yard.node, site, "10")

    def test_a_closed_boxs_claim_is_ignored(self, tenant, yard, site, bulk):
        receive_bulk(tenant, yard.node, bulk, "10")
        box = make_box("CTN-1", yard.node)
        claim(box, bulk, "6")
        Box.objects.filter(pk=box.pk).update(
            status=BoxStatus.CLOSED, closed_at=timezone.now()
        )

        issue_bulk(bulk, yard.node, site, "10")


class TestCorrectionsReduceClaims:
    def test_an_adjustment_takes_the_shortfall_from_boxes_in_code_order(
        self, tenant, yard, bulk
    ):
        receive_bulk(tenant, yard.node, bulk, "10")
        b = make_box("B-2", yard.node)
        a = make_box("a-1", yard.node)
        claim(b, bulk, "3")
        claim(a, bulk, "2")
        # Loose is 5; the count says 7 are gone, 2 more than loose.

        post(
            item_type=bulk,
            quantity=Decimal("7"),
            from_node=yard.node,
            to_node=scrap_node(tenant.pk),
            movement_type=MovementType.ADJUST,
            posted_by=UserFactory(),
            document_type="STOCK_COUNT",
            document_id="5",
            document_number="CNT-5",
        )

        # 2 over loose: "a-1" (code order, case-insensitive) is emptied first.
        assert not BoxBulkContent.objects.filter(box=a).exists()
        assert BoxBulkContent.objects.get(box=b).quantity == Decimal("3")
        a.refresh_from_db()
        assert a.status == BoxStatus.CLOSED
        event = BoxEvent.objects.get(box=a, action=BoxAction.BULK_OUT)
        assert event.quantity == Decimal("2")
        assert event.document_number == "CNT-5"
        assert "correction" in event.note
        assert not BoxEvent.objects.filter(box=b, action=BoxAction.BULK_OUT).exists()
        no_drift(tenant)

    def test_an_adjustment_can_spread_across_boxes(self, tenant, yard, bulk):
        receive_bulk(tenant, yard.node, bulk, "10")
        a = make_box("A", yard.node)
        b = make_box("B", yard.node)
        claim(a, bulk, "4")
        claim(b, bulk, "6")

        post(
            item_type=bulk,
            quantity=Decimal("7"),
            from_node=yard.node,
            to_node=scrap_node(tenant.pk),
            movement_type=MovementType.ADJUST,
        )

        assert not BoxBulkContent.objects.filter(box=a).exists()
        assert BoxBulkContent.objects.get(box=b).quantity == Decimal("3")
        assert BoxEvent.objects.filter(action=BoxAction.BULK_OUT).count() == 2
        no_drift(tenant)

    def test_a_reversal_reduces_claims_too(self, tenant, yard, bulk):
        receipt = receive_bulk(tenant, yard.node, bulk, "10")
        box = make_box("CTN-1", yard.node)
        claim(box, bulk, "6")

        reverse_movement(receipt, posted_by=UserFactory())

        assert not BoxBulkContent.objects.filter(box=box).exists()
        box.refresh_from_db()
        assert box.status == BoxStatus.CLOSED
        event = BoxEvent.objects.get(box=box, action=BoxAction.BULK_OUT)
        assert event.quantity == Decimal("6")
        assert event.document_type == receipt.document_type
        no_drift(tenant)

    def test_an_adjustment_within_loose_stock_touches_no_box(self, tenant, yard, bulk):
        receive_bulk(tenant, yard.node, bulk, "10")
        box = make_box("CTN-1", yard.node)
        claim(box, bulk, "6")

        post(
            item_type=bulk,
            quantity=Decimal("4"),
            from_node=yard.node,
            to_node=scrap_node(tenant.pk),
            movement_type=MovementType.ADJUST,
        )

        assert BoxBulkContent.objects.get(box=box).quantity == Decimal("6")


class TestHotPath:
    def test_a_bulk_issue_at_a_node_with_no_boxes_adds_one_query(
        self, tenant, yard, site, bulk
    ):
        receive_bulk(tenant, yard.node, bulk, "100")
        # Warm caches (content types, node labels) so the counts compare cleanly.
        issue_bulk(bulk, yard.node, site, "1")

        with CaptureQueriesContext(connection) as without_hooks:
            issue_bulk(bulk, yard.node, site, "1")
        box_queries = [
            q for q in without_hooks.captured_queries if "stock_boxbulkcontent" in q["sql"]
        ]
        assert len(box_queries) == 1
        assert "SUM" in box_queries[0]["sql"].upper()
        assert not [q for q in without_hooks.captured_queries if "stock_box" in q["sql"]
                    and "stock_boxbulkcontent" not in q["sql"]]

    def test_a_receipt_adds_no_box_queries(self, tenant, yard, bulk):
        with CaptureQueriesContext(connection) as ctx:
            receive_bulk(tenant, yard.node, bulk, "10")
        assert not [q for q in ctx.captured_queries if "stock_box" in q["sql"]]


class TestHeldCondition:
    def test_a_reclassifying_movement_draws_on_the_claim_of_the_held_condition(
        self, tenant, yard, bulk
    ):
        """A return assessed at the gate leaves in the condition it was held in
        (``from_condition``), so that is the lot whose claim is reduced."""
        post(
            item_type=bulk,
            quantity=Decimal("10"),
            from_node=external_node(tenant.pk),
            to_node=yard.node,
            movement_type=MovementType.RECEIPT,
            condition=Condition.USED_SERVICEABLE,
        )
        box = make_box("CTN-1", yard.node)
        claim(box, bulk, "6", condition=Condition.USED_SERVICEABLE)

        post(
            item_type=bulk,
            quantity=Decimal("2"),
            from_node=yard.node,
            to_node=scrap_node(tenant.pk),
            movement_type=MovementType.DISPOSE,
            condition=Condition.SCRAP,
            from_condition=Condition.USED_SERVICEABLE,
            from_box=box,
        )

        assert BoxBulkContent.objects.get(box=box).quantity == Decimal("4")
        # No verify_ledger here: its outbound totals group by `condition`, not the
        # held condition, so it misreads any reclassifying movement (pre-existing).
