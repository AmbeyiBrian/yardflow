"""T11.6 — verify_ledger checks the box projection (§4.15.1, §4.15.12; P8)."""

from decimal import Decimal

import pytest
from django.db import transaction
from django.utils import timezone

from catalogue.factories import ItemTypeFactory
from catalogue.models import TrackingMode
from locations.factories import StoreFactory, YardFactory
from locations.nodes import external_node
from stock.factories import SerialUnitFactory
from stock.models import (
    Box,
    BoxBulkContent,
    BoxSource,
    BoxStatus,
    Condition,
    MovementType,
    SerialUnit,
    StockBalance,
)
from stock.services import MovementRequest, post_movement
from stock.verification import verify_ledger


@pytest.fixture
def world(tenant):
    """A yard holding 10 bulk items and a unit, with an outer box nesting an inner one.

    The unit and the bulk lot are real ledger movements; only the boxes are
    written directly, since there are no box services yet and drift is injected
    around them anyway.
    """
    yard = YardFactory(name="Main yard")
    store = StoreFactory(parent=yard)
    bulk = ItemTypeFactory(name="Jumper", default_tracking_mode=TrackingMode.BULK)
    unit = SerialUnitFactory(current_node=external_node(tenant.pk))
    with transaction.atomic():
        post_movement(
            MovementRequest(
                item_type=bulk,
                quantity=Decimal("10"),
                from_node=external_node(tenant.pk),
                to_node=yard.node,
                movement_type=MovementType.RECEIPT,
            )
        )
        post_movement(
            MovementRequest(
                item_type=unit.item_type,
                quantity=Decimal("1"),
                from_node=external_node(tenant.pk),
                to_node=yard.node,
                movement_type=MovementType.RECEIPT,
                tracking_mode=TrackingMode.SERIALIZED,
                serial_unit=unit,
            )
        )
    unit.refresh_from_db()
    outer = Box.objects.create(code="OUT", source=BoxSource.INTERNAL, current_node=yard.node)
    inner = Box.objects.create(
        code="IN", source=BoxSource.INTERNAL, current_node=yard.node, parent=outer, depth=2
    )
    SerialUnit.objects.filter(pk=unit.pk).update(box=inner)
    BoxBulkContent.objects.create(
        box=inner, item_type=bulk, condition=Condition.NEW, quantity=Decimal("6")
    )
    return {
        "yard": yard,
        "store": store,
        "bulk": bulk,
        "unit": unit,
        "outer": outer,
        "inner": inner,
    }


def kinds(tenant):
    return {drift.kind for drift in verify_ledger(tenant.pk).drifts}


def close(box):
    Box.objects.filter(pk=box.pk).update(status=BoxStatus.CLOSED, closed_at=timezone.now())


class TestBoxVerification:
    def test_a_clean_nested_box_reports_nothing(self, tenant, world):
        result = verify_ledger(tenant.pk)

        assert result.ok, [str(d) for d in result.drifts]
        assert result.boxes_checked == 2

    def test_a_unit_at_another_node_than_its_box_is_reported(self, tenant, world):
        SerialUnit.objects.filter(pk=world["unit"].pk).update(current_node=world["store"].node)

        assert "box unit position" in kinds(tenant)

    def test_a_unit_in_a_closed_box_is_reported(self, tenant, world):
        close(world["inner"])

        found = kinds(tenant)

        assert "box unit in closed box" in found
        assert "closed box not empty" in found

    def test_claims_above_the_balance_are_reported(self, tenant, world):
        StockBalance.objects.filter(node=world["yard"].node, item_type=world["bulk"]).update(
            quantity=Decimal("5")
        )

        assert "box claims exceed balance" in kinds(tenant)

    def test_claims_across_open_boxes_are_summed(self, tenant, world):
        """Two boxes each within the balance, together over it."""
        BoxBulkContent.objects.create(
            box=world["outer"],
            item_type=world["bulk"],
            condition=Condition.NEW,
            quantity=Decimal("5"),
        )

        assert "box claims exceed balance" in kinds(tenant)

    def test_a_claim_with_no_balance_row_is_reported(self, tenant, world):
        StockBalance.objects.filter(node=world["yard"].node, item_type=world["bulk"]).delete()

        assert "box claims exceed balance" in kinds(tenant)

    def test_a_closed_boxs_claim_does_not_count_against_the_balance(self, tenant, world):
        """Closed boxes are checked by rule 4; here only open boxes claim."""
        StockBalance.objects.filter(node=world["yard"].node, item_type=world["bulk"]).update(
            quantity=Decimal("5")
        )
        close(world["inner"])

        assert "box claims exceed balance" not in kinds(tenant)

    def test_a_cycle_is_reported(self, tenant, world):
        Box.objects.filter(pk=world["outer"].pk).update(parent=world["inner"])

        assert "box cycle" in kinds(tenant)

    def test_a_box_that_is_its_own_parent_is_reported(self, tenant, world):
        Box.objects.filter(pk=world["outer"].pk).update(parent=world["outer"])

        assert "box cycle" in kinds(tenant)

    def test_a_wrong_depth_is_reported(self, tenant, world):
        Box.objects.filter(pk=world["inner"].pk).update(depth=3)

        assert "box depth" in kinds(tenant)

    def test_a_root_that_is_not_depth_one_is_reported(self, tenant, world):
        Box.objects.filter(pk=world["outer"].pk).update(depth=2)

        assert "box depth" in kinds(tenant)

    def test_a_child_at_another_node_than_its_parent_is_reported(self, tenant, world):
        Box.objects.filter(pk=world["inner"].pk).update(current_node=world["store"].node)

        assert "box child location" in kinds(tenant)

    def test_an_open_child_of_a_closed_parent_is_reported(self, tenant, world):
        close(world["outer"])

        found = kinds(tenant)

        assert "open box in closed box" in found
        assert "closed box not empty" in found

    def test_a_closed_box_holding_bulk_is_reported(self, tenant, world):
        # Take the unit out so only the claim remains.
        SerialUnit.objects.filter(pk=world["unit"].pk).update(box=None)
        close(world["inner"])

        result = verify_ledger(tenant.pk)

        assert [d.kind for d in result.drifts] == ["closed box not empty"]
        assert "bulk contents" in result.drifts[0].description

    def test_drift_is_reported_and_never_corrected(self, tenant, world):
        SerialUnit.objects.filter(pk=world["unit"].pk).update(current_node=world["store"].node)

        verify_ledger(tenant.pk)

        world["unit"].refresh_from_db()
        assert world["unit"].box_id == world["inner"].pk

    def test_the_check_does_not_query_per_box(self, tenant, world, django_assert_max_num_queries):
        for n in range(20):
            Box.objects.create(
                code=f"X{n}", source=BoxSource.INTERNAL, current_node=world["yard"].node
            )

        with django_assert_max_num_queries(25):
            verify_ledger(tenant.pk)
