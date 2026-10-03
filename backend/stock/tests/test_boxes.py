"""T11.5 — box services (§4.15.4; P1, P4, P7, P9, P10, E4)."""

from decimal import Decimal

import pytest
from django.db import transaction

from accounts.factories import UserFactory
from catalogue.factories import ItemTypeFactory
from catalogue.models import TrackingMode
from core.models import AuditLog
from dispatch.models import GateOut, GateOutLine, GateOutLineSerial, GateOutPurpose, GateOutStatus
from locations.factories import StoreFactory, VehicleFactory, YardFactory
from locations.nodes import external_node, node_for_location, node_for_site, quarantine_location
from network.factories import ClientFactory, SiteFactory
from stock.box_hooks import EventContext
from stock.boxes import (
    AlreadyInBox,
    BoxClosed,
    BoxCodeInUse,
    BoxCycle,
    BoxElsewhere,
    BoxInsideAnotherBox,
    BoxOutsidePerimeter,
    BoxTooDeep,
    NotEnoughLooseStock,
    NotInThisBox,
    box_tree,
    create_box,
    empty_box,
    issuable_contents,
    move_box,
    put_box,
    put_bulk,
    put_units,
    take_out,
)
from stock.counting import TransferNotAllowed
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
    SerialUnitStatus,
    StockBalance,
    StockMovement,
)
from stock.services import BoxClaimShort, MovementRequest, post_movement
from stock.verification import verify_ledger


@pytest.fixture
def yard(tenant):
    return YardFactory(name="Main yard")


@pytest.fixture
def yard_node(yard):
    return node_for_location(yard)


@pytest.fixture
def store_node(tenant):
    return node_for_location(StoreFactory(name="Store B"))


@pytest.fixture
def ctx():
    return EventContext(actor=UserFactory())


@pytest.fixture
def bulk(tenant):
    return ItemTypeFactory(name="Jumper", default_tracking_mode=TrackingMode.BULK)


def post(**kwargs):
    with transaction.atomic():
        return post_movement(MovementRequest(**kwargs))


def receive_units(tenant, node, count, item_type=None, **extra):
    units = []
    for _ in range(count):
        unit = SerialUnitFactory(
            current_node=external_node(tenant.pk),
            **({"item_type": item_type} if item_type else {}),
            **extra,
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
            owner_type=unit.owner_type,
            owner_client=unit.owner_client,
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


def box_at(node, code="", parent=None, ctx=None):
    return create_box(code=code, node=node, parent=parent, ctx=ctx)


def no_drift(tenant):
    result = verify_ledger(tenant.pk)
    assert result.ok, result.summary()


class TestCreateBox:
    def test_a_printed_code_is_kept_as_a_label(self, tenant, yard_node, ctx):
        box = create_box(code="CTN-77", node=yard_node, label_text="raw", ctx=ctx)
        assert (box.code, box.source, box.depth, box.status) == (
            "CTN-77",
            BoxSource.LABEL,
            1,
            BoxStatus.OPEN,
        )
        assert box.label_text == "raw"
        event = BoxEvent.objects.get(box=box)
        assert event.action == BoxAction.CREATED
        assert event.actor == ctx.actor
        no_drift(tenant)

    def test_a_blank_code_is_generated(self, tenant, yard_node):
        first = create_box(code="", node=yard_node)
        second = create_box(code="  ", node=yard_node)
        assert first.source == BoxSource.INTERNAL
        assert first.code.startswith("BX-")
        assert second.code != first.code

    def test_a_code_in_use_is_refused_in_any_case_and_names_where_it_is(
        self, tenant, yard_node, store_node
    ):
        create_box(code="CTN-1", node=store_node)
        with pytest.raises(BoxCodeInUse) as caught:
            create_box(code="ctn-1", node=yard_node)
        assert caught.value.code == "BOX_CODE_IN_USE"
        assert caught.value.status_code == 409
        assert "Store B" in caught.value.message
        assert "open" in caught.value.message

    def test_a_closed_boxs_code_is_never_reused(self, tenant, yard_node, ctx):
        box = create_box(code="CTN-1", node=yard_node)
        empty_box(box, ctx=ctx)
        with pytest.raises(BoxCodeInUse) as caught:
            create_box(code="CTN-1", node=yard_node)
        assert "closed" in caught.value.message

    def test_a_node_outside_the_perimeter_is_refused(self, tenant):
        vehicle = node_for_location(VehicleFactory())
        with pytest.raises(BoxOutsidePerimeter):
            create_box(code="X", node=vehicle)
        with pytest.raises(BoxOutsidePerimeter):
            create_box(code="Y", node=node_for_site(SiteFactory()))

    def test_a_quarantine_node_is_allowed(self, tenant, yard):
        quarantine = node_for_location(quarantine_location(tenant.pk, yard))
        assert create_box(code="Q-1", node=quarantine).current_node == quarantine

    def test_a_child_takes_its_parents_depth_and_writes_box_in(self, tenant, yard_node, ctx):
        parent = box_at(yard_node, "P")
        child = create_box(code="C", node=yard_node, parent=parent, ctx=ctx)
        assert child.depth == 2
        event = BoxEvent.objects.get(box=parent, action=BoxAction.BOX_IN)
        assert event.child_box == child
        no_drift(tenant)

    def test_a_parent_must_be_open_and_at_the_same_node(self, tenant, yard_node, store_node):
        parent = box_at(yard_node, "P")
        with pytest.raises(BoxElsewhere):
            create_box(code="C", node=store_node, parent=parent)
        closed = box_at(yard_node, "Z")
        empty_box(closed, ctx=EventContext())
        with pytest.raises(BoxClosed):
            create_box(code="C2", node=yard_node, parent=closed)

    def test_nesting_stops_at_three(self, tenant, yard_node):
        a = box_at(yard_node, "A")
        b = box_at(yard_node, "B", parent=a)
        c = box_at(yard_node, "C", parent=b)
        assert c.depth == 3
        with pytest.raises(BoxTooDeep) as caught:
            box_at(yard_node, "D", parent=c)
        assert caught.value.code == "BOX_TOO_DEEP"


class TestPutUnits:
    def test_units_go_in_and_are_recorded(self, tenant, yard_node, ctx):
        units = receive_units(tenant, yard_node, 2)
        box = box_at(yard_node, "CTN-1")
        put_units(box, units, ctx=ctx)
        assert SerialUnit.objects.filter(box=box).count() == 2
        events = BoxEvent.objects.filter(box=box, action=BoxAction.UNIT_IN)
        assert {e.serial_unit_id for e in events} == {u.pk for u in units}
        no_drift(tenant)

    def test_a_unit_already_in_a_box_is_refused_naming_both(self, tenant, yard_node, ctx):
        (unit,) = receive_units(tenant, yard_node, 1)
        first = box_at(yard_node, "CTN-1")
        put_units(first, [unit], ctx=ctx)
        other = box_at(yard_node, "CTN-2")
        with pytest.raises(AlreadyInBox) as caught:
            put_units(other, [unit], ctx=ctx)
        assert unit.serial_number in caught.value.message
        assert "CTN-1" in caught.value.message

    def test_a_unit_at_another_node_is_refused(self, tenant, yard_node, store_node, ctx):
        (unit,) = receive_units(tenant, store_node, 1)
        with pytest.raises(BoxElsewhere):
            put_units(box_at(yard_node, "CTN-1"), [unit], ctx=ctx)
        assert SerialUnit.objects.get(pk=unit.pk).box_id is None

    def test_a_closed_box_takes_nothing(self, tenant, yard_node, ctx):
        (unit,) = receive_units(tenant, yard_node, 1)
        box = box_at(yard_node, "CTN-1")
        empty_box(box, ctx=ctx)
        with pytest.raises(BoxClosed):
            put_units(box, [unit], ctx=ctx)


class TestPutBulk:
    def test_a_claim_is_made_and_added_to(self, tenant, yard_node, bulk, ctx):
        receive_bulk(tenant, yard_node, bulk, "100")
        box = box_at(yard_node, "CTN-1")
        put_bulk(
            box, item_type=bulk, owner_client=None, condition=Condition.NEW,
            quantity=Decimal("30"), ctx=ctx,
        )
        claim = put_bulk(
            box, item_type=bulk, owner_client=None, condition=Condition.NEW,
            quantity=Decimal("20"), ctx=ctx,
        )
        assert claim.quantity == Decimal("50")
        assert BoxBulkContent.objects.filter(box=box).count() == 1
        events = BoxEvent.objects.filter(box=box, action=BoxAction.BULK_IN)
        assert sorted(e.quantity for e in events) == [Decimal("20"), Decimal("30")]
        no_drift(tenant)

    def test_a_claim_beyond_loose_stock_is_refused(self, tenant, yard_node, bulk, ctx):
        receive_bulk(tenant, yard_node, bulk, "100")
        put_bulk(
            box_at(yard_node, "A"), item_type=bulk, owner_client=None,
            condition=Condition.NEW, quantity=Decimal("80"), ctx=ctx,
        )
        with pytest.raises(NotEnoughLooseStock) as caught:
            put_bulk(
                box_at(yard_node, "B"), item_type=bulk, owner_client=None,
                condition=Condition.NEW, quantity=Decimal("30"), ctx=ctx,
            )
        assert caught.value.code == "NOT_ENOUGH_LOOSE_STOCK"
        assert "20" in caught.value.message and "30" in caught.value.message

    def test_a_lot_with_no_balance_row_counts_as_zero(self, tenant, yard_node, bulk, ctx):
        with pytest.raises(NotEnoughLooseStock):
            put_bulk(
                box_at(yard_node, "A"), item_type=bulk, owner_client=None,
                condition=Condition.NEW, quantity=Decimal("1"), ctx=ctx,
            )

    def test_a_closed_boxs_claim_does_not_count_against_loose_stock(
        self, tenant, yard_node, bulk, ctx
    ):
        receive_bulk(tenant, yard_node, bulk, "10")
        box = box_at(yard_node, "A")
        put_bulk(
            box, item_type=bulk, owner_client=None, condition=Condition.NEW,
            quantity=Decimal("10"), ctx=ctx,
        )
        empty_box(box, ctx=ctx)
        put_bulk(
            box_at(yard_node, "B"), item_type=bulk, owner_client=None,
            condition=Condition.NEW, quantity=Decimal("10"), ctx=ctx,
        )


class TestPutBox:
    def test_a_box_goes_inside_another_and_depths_follow(self, tenant, yard_node, ctx):
        pallet = box_at(yard_node, "PAL")
        carton = box_at(yard_node, "CTN")
        inner = box_at(yard_node, "IN", parent=carton)
        put_box(pallet, carton, ctx=ctx)
        carton.refresh_from_db()
        inner.refresh_from_db()
        assert (carton.parent_id, carton.depth, inner.depth) == (pallet.pk, 2, 3)
        event = BoxEvent.objects.get(box=pallet, action=BoxAction.BOX_IN)
        assert event.child_box == carton
        no_drift(tenant)

    def test_a_cycle_is_refused(self, tenant, yard_node, ctx):
        outer = box_at(yard_node, "OUT")
        inner = box_at(yard_node, "IN", parent=outer)
        with pytest.raises(BoxCycle) as caught:
            put_box(inner, outer, ctx=ctx)
        assert caught.value.code == "BOX_CYCLE"
        with pytest.raises(BoxCycle):
            put_box(outer, outer, ctx=ctx)

    def test_the_whole_subtree_must_fit_in_three(self, tenant, yard_node, ctx):
        top = box_at(yard_node, "TOP")
        mid = box_at(yard_node, "MID", parent=top)
        loose = box_at(yard_node, "LOOSE")
        box_at(yard_node, "LEAF", parent=loose)
        with pytest.raises(BoxTooDeep):
            put_box(mid, loose, ctx=ctx)
        loose.refresh_from_db()
        assert loose.parent_id is None

    def test_a_child_already_in_a_box_is_refused(self, tenant, yard_node, ctx):
        a = box_at(yard_node, "A")
        child = box_at(yard_node, "C", parent=a)
        with pytest.raises(AlreadyInBox):
            put_box(box_at(yard_node, "B"), child, ctx=ctx)

    def test_both_must_be_open_and_at_the_same_node(self, tenant, yard_node, store_node, ctx):
        a = box_at(yard_node, "A")
        with pytest.raises(BoxElsewhere):
            put_box(a, box_at(store_node, "B"), ctx=ctx)
        done = box_at(yard_node, "D")
        empty_box(done, ctx=ctx)
        with pytest.raises(BoxClosed):
            put_box(a, done, ctx=ctx)


class TestTakeOut:
    def test_units_come_out_nothing_moves_and_the_box_stays_open(
        self, tenant, yard_node, ctx
    ):
        units = receive_units(tenant, yard_node, 3)
        box = box_at(yard_node, "CTN")
        put_units(box, units, ctx=ctx)
        movements = StockMovement.objects.count()

        take_out(box, units=units[:1], ctx=ctx)

        assert StockMovement.objects.count() == movements
        assert SerialUnit.objects.get(pk=units[0].pk).box_id is None
        assert BoxEvent.objects.filter(box=box, action=BoxAction.UNIT_OUT).count() == 1
        assert Box.objects.get(pk=box.pk).status == BoxStatus.OPEN
        assert AuditLog.objects.filter(target_id=str(box.pk)).count() == 1
        no_drift(tenant)

    def test_a_unit_not_in_this_box_is_refused(self, tenant, yard_node, ctx):
        (unit,) = receive_units(tenant, yard_node, 1)
        box = box_at(yard_node, "CTN")
        with pytest.raises(NotInThisBox):
            take_out(box, units=[unit], ctx=ctx)

    def test_bulk_reduces_the_claim_and_refuses_beyond_it(self, tenant, yard_node, bulk, ctx):
        receive_bulk(tenant, yard_node, bulk, "50")
        box = box_at(yard_node, "CTN")
        put_bulk(
            box, item_type=bulk, owner_client=None, condition=Condition.NEW,
            quantity=Decimal("50"), ctx=ctx,
        )
        lot = {"item_type": bulk, "owner_client": None, "condition": Condition.NEW}
        with pytest.raises(BoxClaimShort):
            take_out(box, bulk=[{**lot, "quantity": Decimal("51")}], ctx=ctx)
        take_out(box, bulk=[{**lot, "quantity": Decimal("20")}], ctx=ctx)
        assert BoxBulkContent.objects.get(box=box).quantity == Decimal("30")
        event = BoxEvent.objects.get(box=box, action=BoxAction.BULK_OUT)
        assert event.quantity == Decimal("20")
        # The balance never moved (P7).
        assert StockBalance.objects.get(node=yard_node, item_type=bulk).quantity == 50
        no_drift(tenant)

    def test_a_child_box_is_detached_to_the_top_with_its_subtree(
        self, tenant, yard_node, ctx
    ):
        pallet = box_at(yard_node, "PAL")
        carton = box_at(yard_node, "CTN", parent=pallet)
        inner = box_at(yard_node, "IN", parent=carton)
        (unit,) = receive_units(tenant, yard_node, 1)
        put_units(pallet, [unit], ctx=ctx)

        take_out(pallet, boxes=[carton], ctx=ctx)

        carton.refresh_from_db()
        inner.refresh_from_db()
        assert (carton.parent_id, carton.depth, inner.depth) == (None, 1, 2)
        assert BoxEvent.objects.filter(box=pallet, action=BoxAction.BOX_OUT).count() == 1
        with pytest.raises(NotInThisBox):
            take_out(pallet, boxes=[carton], ctx=ctx)
        no_drift(tenant)

    def test_the_last_thing_out_closes_the_box_and_its_parents(self, tenant, yard_node, ctx):
        pallet = box_at(yard_node, "PAL")
        carton = box_at(yard_node, "CTN", parent=pallet)
        (unit,) = receive_units(tenant, yard_node, 1)
        put_units(carton, [unit], ctx=ctx)
        take_out(carton, units=[unit], ctx=ctx)
        assert Box.objects.get(pk=carton.pk).status == BoxStatus.CLOSED
        assert Box.objects.get(pk=pallet.pk).status == BoxStatus.CLOSED
        no_drift(tenant)

    def test_naming_nothing_is_refused(self, tenant, yard_node, ctx):
        from stock.services import LedgerRuleViolation

        with pytest.raises(LedgerRuleViolation):
            take_out(box_at(yard_node, "A"), ctx=ctx)

    def test_empty_box_takes_everything_out_and_closes(self, tenant, yard_node, bulk, ctx):
        receive_bulk(tenant, yard_node, bulk, "10")
        pallet = box_at(yard_node, "PAL")
        carton = box_at(yard_node, "CTN", parent=pallet)
        units = receive_units(tenant, yard_node, 2)
        put_units(pallet, units, ctx=ctx)
        put_bulk(
            pallet, item_type=bulk, owner_client=None, condition=Condition.NEW,
            quantity=Decimal("10"), ctx=ctx,
        )

        empty_box(pallet, ctx=ctx)

        pallet.refresh_from_db()
        carton.refresh_from_db()
        assert pallet.status == BoxStatus.CLOSED
        assert carton.parent_id is None and carton.status == BoxStatus.OPEN
        assert not SerialUnit.objects.filter(box__isnull=False).exists()
        assert not BoxBulkContent.objects.exists()
        assert AuditLog.objects.filter(target_id=str(pallet.pk)).count() == 1
        no_drift(tenant)


class TestMoveBox:
    def build_pallet(self, tenant, yard_node, bulk, ctx):
        client = ClientFactory()
        pallet = box_at(yard_node, "PAL")
        carton = box_at(yard_node, "CTN", parent=pallet)
        loose_units = receive_units(tenant, yard_node, 1)
        carton_units = receive_units(tenant, yard_node, 2)
        client_units = receive_units(
            tenant, yard_node, 1, owner_type="CLIENT", owner_client=client
        )
        put_units(pallet, loose_units + client_units, ctx=ctx)
        put_units(carton, carton_units, ctx=ctx)
        receive_bulk(tenant, yard_node, bulk, "40")
        put_bulk(
            carton, item_type=bulk, owner_client=None, condition=Condition.NEW,
            quantity=Decimal("25"), ctx=ctx,
        )
        return pallet, carton, loose_units + carton_units + client_units

    def test_a_pallet_moves_with_everything_in_it(self, tenant, yard_node, store_node, bulk, ctx):
        pallet, carton, units = self.build_pallet(tenant, yard_node, bulk, ctx)
        actor = UserFactory()
        no_drift(tenant)

        moved = move_box(pallet, store_node.location, actor=actor)

        assert moved.current_node == store_node
        carton.refresh_from_db()
        assert carton.current_node == store_node
        assert carton.parent_id == pallet.pk
        for unit in SerialUnit.objects.filter(pk__in=[u.pk for u in units]):
            assert unit.current_node == store_node
            assert unit.box_id is not None
            assert unit.status == SerialUnitStatus.IN_STOCK
        assert BoxBulkContent.objects.get(box=carton).quantity == Decimal("25")
        # Only the claim travels; the 15 nobody boxed stays loose in the yard.
        assert StockBalance.objects.get(node=store_node, item_type=bulk).quantity == 25
        assert StockBalance.objects.get(node=yard_node, item_type=bulk).quantity == 15

        movements = StockMovement.objects.filter(document_type="stock.BoxMove")
        assert movements.count() == len(units) + 1
        assert {m.document_number for m in movements} == {movements.first().document_number}
        assert movements.first().document_number.startswith("TR-")
        assert {m.movement_type for m in movements} == {MovementType.TRANSFER}
        # Nothing left its box: no UNIT_OUT / BULK_OUT, one MOVED on the root.
        assert not BoxEvent.objects.filter(
            action__in=[BoxAction.UNIT_OUT, BoxAction.BULK_OUT]
        ).exists()
        assert BoxEvent.objects.filter(box=pallet, action=BoxAction.MOVED).count() == 1
        assert not BoxEvent.objects.filter(box=carton, action=BoxAction.MOVED).exists()
        assert AuditLog.objects.filter(target_id=str(pallet.pk)).count() == 1
        no_drift(tenant)

    def test_moving_back_works_too(self, tenant, yard_node, store_node, bulk, ctx):
        pallet, _, _ = self.build_pallet(tenant, yard_node, bulk, ctx)
        move_box(pallet, store_node.location, actor=UserFactory())
        move_box(pallet, yard_node.location, actor=UserFactory())
        assert Box.objects.get(pk=pallet.pk).current_node == yard_node
        no_drift(tenant)

    def test_an_empty_box_moves(self, tenant, yard_node, store_node):
        box = box_at(yard_node, "E")
        assert move_box(box, store_node.location, actor=None).current_node == store_node

    def test_a_destination_outside_the_yard_is_refused(self, tenant, yard_node):
        box = box_at(yard_node, "A")
        with pytest.raises(TransferNotAllowed):
            move_box(box, VehicleFactory(), actor=None)

    def test_a_box_outside_the_yard_cannot_be_moved(self, tenant, yard_node):
        box = box_at(yard_node, "A")
        Box.objects.filter(pk=box.pk).update(current_node=node_for_site(SiteFactory()))
        with pytest.raises(TransferNotAllowed):
            move_box(Box.objects.get(pk=box.pk), yard_node.location, actor=None)

    def test_a_box_inside_another_is_refused(self, tenant, yard_node, store_node):
        pallet = box_at(yard_node, "P")
        carton = box_at(yard_node, "C", parent=pallet)
        with pytest.raises(BoxInsideAnotherBox):
            move_box(carton, store_node.location, actor=None)

    def test_a_closed_box_and_the_same_place_are_refused(self, tenant, yard_node, store_node):
        from stock.services import LedgerRuleViolation

        box = box_at(yard_node, "A")
        with pytest.raises(LedgerRuleViolation):
            move_box(box, yard_node.location, actor=None)
        empty_box(box, ctx=EventContext())
        with pytest.raises(BoxClosed):
            move_box(Box.objects.get(pk=box.pk), store_node.location, actor=None)

    def test_moving_to_quarantine_follows_the_ledger_status_rules(
        self, tenant, yard, yard_node, ctx
    ):
        (unit,) = receive_units(tenant, yard_node, 1)
        box = box_at(yard_node, "A")
        put_units(box, [unit], ctx=ctx)
        quarantine = quarantine_location(tenant.pk, yard)
        move_box(box, quarantine, actor=None)
        unit.refresh_from_db()
        assert unit.status == SerialUnitStatus.QUARANTINED
        assert unit.box_id == box.pk
        no_drift(tenant)


class TestBoxTree:
    def test_the_tree_shows_contents_and_received_against_now(
        self, tenant, yard_node, bulk, ctx
    ):
        client = ClientFactory(name="Acme")
        pallet = box_at(yard_node, "PAL")
        carton = box_at(yard_node, "CTN", parent=pallet)
        units = receive_units(tenant, yard_node, 3)
        put_units(carton, units, ctx=ctx)
        receive_bulk(tenant, yard_node, bulk, "10")
        put_bulk(
            pallet, item_type=bulk, owner_client=None, condition=Condition.NEW,
            quantity=Decimal("10"), ctx=ctx,
        )

        take_out(carton, units=units[:1], ctx=ctx)
        take_out(
            pallet,
            bulk=[{"item_type": bulk, "owner_client": None, "condition": Condition.NEW,
                   "quantity": Decimal("4")}],
            ctx=ctx,
        )

        tree = box_tree(pallet)
        assert tree["code"] == "PAL" and tree["depth"] == 1 and tree["status"] == "OPEN"
        assert tree["node"] == yard_node.label
        assert tree["counts"] == {
            "received": {"units": 3, "bulk": Decimal("10")},
            "now": {"units": 2, "bulk": Decimal("6")},
        }
        (child,) = tree["children"]
        assert child["code"] == "CTN" and child["depth"] == 2
        assert child["counts"]["received"]["units"] == 3
        assert child["counts"]["now"]["units"] == 2
        assert {u["serial_number"] for u in child["units"]} == {
            units[1].serial_number,
            units[2].serial_number,
        }
        assert set(child["units"][0]) == {
            "id", "serial_number", "asset_tag", "item_type", "item_name", "status", "condition",
        }
        (claim,) = tree["bulk"]
        assert claim["quantity"] == Decimal("6")
        assert set(claim) == {
            "item_type", "item_name", "owner_client", "owner_name", "condition", "quantity",
            "uom",
        }
        assert client  # owner names are exercised below
        no_drift(tenant)

    def test_owner_name_and_query_count(
        self, tenant, yard_node, bulk, ctx, django_assert_max_num_queries
    ):
        client = ClientFactory(name="Acme")
        pallet = box_at(yard_node, "PAL")
        for n in range(3):
            carton = box_at(yard_node, f"CTN-{n}", parent=pallet)
            put_units(carton, receive_units(tenant, yard_node, 2), ctx=ctx)
        post(
            item_type=bulk, quantity=Decimal("5"), from_node=external_node(tenant.pk),
            to_node=yard_node, movement_type=MovementType.RECEIPT,
            owner_type="CLIENT", owner_client=client,
        )
        put_bulk(
            pallet, item_type=bulk, owner_client=client, condition=Condition.NEW,
            quantity=Decimal("5"), ctx=ctx,
        )
        with django_assert_max_num_queries(12):
            tree = box_tree(pallet)
        assert tree["bulk"][0]["owner_name"] == "Acme"
        assert len(tree["children"]) == 3
        assert tree["counts"]["now"]["units"] == 6


class TestIssuableContents:
    def make_pass(self, tenant, yard, units, *, status, number="GP-1"):
        gate_out = GateOut.objects.create(
            organization=tenant,
            number=number,
            status=status,
            from_location=yard,
            client=ClientFactory(),
            custody_holder=UserFactory(),
            requested_by=UserFactory(),
            purpose_type=GateOutPurpose.RETURN_TO_CLIENT,
        )
        line = GateOutLine.objects.create(
            organization=tenant,
            gate_out=gate_out,
            item_type=units[0].item_type,
            tracking_mode="SERIALIZED",
            requested_qty=Decimal(len(units)),
            uom="ea",
        )
        for unit in units:
            GateOutLineSerial.objects.create(organization=tenant, line=line, serial_unit=unit)
        return gate_out

    def test_a_pallet_expands_to_one_line_per_innermost_box_and_lot(
        self, tenant, yard_node, bulk, ctx
    ):
        pallet = box_at(yard_node, "PAL")
        carton = box_at(yard_node, "CTN", parent=pallet)
        item = ItemTypeFactory(default_tracking_mode=TrackingMode.SERIALIZED)
        other = ItemTypeFactory(default_tracking_mode=TrackingMode.SERIALIZED)
        in_carton = receive_units(tenant, yard_node, 2, item_type=item)
        on_pallet = receive_units(tenant, yard_node, 1, item_type=item)
        odd = receive_units(tenant, yard_node, 1, item_type=other)
        put_units(carton, in_carton, ctx=ctx)
        put_units(pallet, on_pallet + odd, ctx=ctx)
        receive_bulk(tenant, yard_node, bulk, "9")
        put_bulk(
            carton, item_type=bulk, owner_client=None, condition=Condition.NEW,
            quantity=Decimal("9"), ctx=ctx,
        )

        result = issuable_contents(pallet, from_node=yard_node)

        assert result["excluded"] == []
        lines = {(ln["box_code"], ln["item_name"]): ln for ln in result["lines"]}
        assert len(result["lines"]) == 4
        carton_line = lines[("CTN", item.name)]
        assert carton_line["requested_qty"] == 2
        assert carton_line["tracking_mode"] == TrackingMode.SERIALIZED
        assert carton_line["box_path"] == ["PAL", "CTN"]
        assert carton_line["box"] == carton.pk
        assert {u["serial_unit"] for u in carton_line["units"]} == {u.pk for u in in_carton}
        assert set(carton_line["units"][0]) == {"serial_unit", "serial_number"}
        assert lines[("PAL", item.name)]["requested_qty"] == 1
        assert lines[("PAL", other.name)]["box_path"] == ["PAL"]
        bulk_line = lines[("CTN", bulk.name)]
        assert bulk_line["requested_qty"] == Decimal("9")
        assert bulk_line["tracking_mode"] == TrackingMode.BULK
        assert bulk_line["units"] == []
        assert set(bulk_line) == set(carton_line)

    def test_each_exclusion_reason(self, tenant, yard, yard_node, store_node, ctx):
        box = box_at(yard_node, "CTN")
        good, elsewhere, quarantined, held, scrapped, claimed, past = receive_units(
            tenant, yard_node, 7
        )
        put_units(box, [good, elsewhere, quarantined, held, scrapped, claimed, past], ctx=ctx)
        SerialUnit.objects.filter(pk=elsewhere.pk).update(current_node=store_node)
        SerialUnit.objects.filter(pk=quarantined.pk).update(status=SerialUnitStatus.QUARANTINED)
        SerialUnit.objects.filter(pk=held.pk).update(status=SerialUnitStatus.IN_CUSTODY)
        SerialUnit.objects.filter(pk=scrapped.pk).update(status=SerialUnitStatus.SCRAPPED)
        self.make_pass(tenant, yard, [claimed], status=GateOutStatus.APPROVED, number="GP-77")
        # A finished pass does not block.
        self.make_pass(tenant, yard, [past], status=GateOutStatus.CANCELLED, number="GP-78")

        result = issuable_contents(box, from_node=yard_node)

        reasons = {e["serial_number"]: e for e in result["excluded"]}
        assert reasons[elsewhere.serial_number]["reason"] == "NOT_HERE"
        assert reasons[quarantined.serial_number]["reason"] == "QUARANTINED"
        assert reasons[held.serial_number]["reason"] == "HELD_BY_PERSON"
        assert reasons[scrapped.serial_number]["reason"] == "NOT_IN_STOCK"
        assert reasons[claimed.serial_number]["reason"] == "ON_ANOTHER_PASS"
        assert "GP-77" in reasons[claimed.serial_number]["message"]
        assert len(reasons) == 5
        (line,) = result["lines"]
        assert {u["serial_unit"] for u in line["units"]} == {good.pk, past.pk}
        assert line["requested_qty"] == 2

    def test_a_released_serial_on_an_open_pass_does_not_block(
        self, tenant, yard, yard_node, ctx
    ):
        (unit,) = receive_units(tenant, yard_node, 1)
        box = box_at(yard_node, "CTN")
        put_units(box, [unit], ctx=ctx)
        gate_out = self.make_pass(tenant, yard, [unit], status=GateOutStatus.PARTIALLY_RELEASED)
        GateOutLineSerial.objects.filter(line__gate_out=gate_out).update(released=True)
        assert len(issuable_contents(box, from_node=yard_node)["lines"]) == 1

    def test_a_box_at_another_node_is_not_here(self, tenant, yard_node, store_node, bulk, ctx):
        receive_bulk(tenant, store_node, bulk, "5")
        box = box_at(store_node, "CTN")
        put_bulk(
            box, item_type=bulk, owner_client=None, condition=Condition.NEW,
            quantity=Decimal("5"), ctx=ctx,
        )
        result = issuable_contents(box, from_node=yard_node)
        assert result["lines"] == []
        (excluded,) = result["excluded"]
        assert excluded["reason"] == "NOT_HERE" and excluded["kind"] == "bulk"
