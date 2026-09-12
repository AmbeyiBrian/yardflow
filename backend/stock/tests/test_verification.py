"""T3.6 — verify_ledger (§3.3, §13)."""

from decimal import Decimal

import pytest
from django.core.management import CommandError, call_command
from django.db import connection, transaction

from catalogue.factories import ItemTypeFactory
from catalogue.models import TrackingMode
from locations.factories import StoreFactory, YardFactory
from locations.nodes import external_node
from stock.factories import ReelFactory, SerialUnitFactory
from stock.models import MovementType, StockBalance
from stock.services import MovementRequest, post_movement
from stock.verification import verify_ledger


@pytest.fixture
def stocked_yard(tenant):
    yard = YardFactory(name="Main yard")
    item = ItemTypeFactory(name="Jumper")
    with transaction.atomic():
        post_movement(
            MovementRequest(
                item_type=item,
                quantity=Decimal("40"),
                from_node=external_node(tenant.pk),
                to_node=yard.node,
                movement_type=MovementType.RECEIPT,
            )
        )
    return yard, item


def corrupt_balance(balance_pk, quantity):
    """Corrupt the cache the only way reality could: outside the application."""
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE stock_stockbalance SET quantity = %s WHERE id = %s",
            [quantity, balance_pk],
        )


class TestVerification:
    def test_a_consistent_ledger_reports_no_drift(self, tenant, stocked_yard):
        result = verify_ledger(tenant.pk)

        assert result.ok is True
        assert result.balances_checked > 0

    def test_a_corrupted_balance_is_detected(self, tenant, stocked_yard):
        """T3.6's stated criterion."""
        yard, item = stocked_yard
        balance = StockBalance.objects.get(node=yard.node, item_type=item)

        corrupt_balance(balance.pk, 999)
        result = verify_ledger(tenant.pk)

        assert result.ok is False
        assert any(drift.kind == "balance" for drift in result.drifts)

    def test_drift_is_reported_and_never_corrected(self, tenant, stocked_yard):
        """§13: "alert, never auto-correct".

        Correcting the cache would hide the cause, and the same cause would go on
        corrupting the next figure.
        """
        yard, item = stocked_yard
        balance = StockBalance.objects.get(node=yard.node, item_type=item)
        corrupt_balance(balance.pk, 999)

        verify_ledger(tenant.pk)

        balance.refresh_from_db()
        assert balance.quantity == Decimal("999.000"), (
            "verification must report drift, not rewrite the cache"
        )

    def test_a_missing_balance_row_is_drift_too(self, tenant, stocked_yard):
        """A missing row reads as zero on every screen, which is just as wrong."""
        yard, item = stocked_yard
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM stock_stockbalance WHERE node_id = %s AND item_type_id = %s",
                [yard.node.pk, item.pk],
            )

        result = verify_ledger(tenant.pk)

        assert any(drift.kind == "missing balance" for drift in result.drifts)

    def test_a_serial_units_position_is_checked(self, tenant):
        """§3.5: the denormalisation is checked against the ledger as well.

        Serial position is what the stock screens read, so drift here is visible
        to users even while every balance looks right.
        """
        yard = YardFactory(name="Main yard")
        store = StoreFactory(parent=yard)
        unit = SerialUnitFactory(current_node=external_node(tenant.pk))
        with transaction.atomic():
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

        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE stock_serialunit SET current_node_id = %s WHERE id = %s",
                [store.node.pk, unit.pk],
            )

        result = verify_ledger(tenant.pk)

        assert any(drift.kind == "serial unit position" for drift in result.drifts)

    def test_a_drums_remainder_is_checked(self, tenant):
        yard = YardFactory(name="Main yard")
        reel = ReelFactory(current_node=yard.node, initial_length=500, remaining_length=500)

        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE stock_reel SET remaining_length = 100 WHERE id = %s", [reel.pk]
            )

        result = verify_ledger(tenant.pk)

        assert any(drift.kind == "reel remainder" for drift in result.drifts)


class TestManagementCommand:
    def test_the_command_succeeds_on_a_clean_ledger(self, tenant, stocked_yard):
        call_command("verify_ledger", org=str(tenant.pk))

    def test_the_command_fails_on_drift(self, tenant, stocked_yard):
        """A non-zero exit, so a scheduler treats drift as a failure."""
        yard, item = stocked_yard
        balance = StockBalance.objects.get(node=yard.node, item_type=item)
        corrupt_balance(balance.pk, 1)

        with pytest.raises(CommandError, match="disagreement"):
            call_command("verify_ledger", org=str(tenant.pk))
