"""T3.1–T3.5 — the stock ledger and its invariants (§3; E1, E3, M3, M4).

§14: "the ledger, approval engine and tenancy layers are the parts where a bug is
expensive and invisible. Coverage effort concentrates there."

The invariant that matters most, stated once: **for any node and item, inbound
minus outbound movements equals the cached balance** (§3.2).
"""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction

from accounts.factories import UserFactory
from catalogue.factories import ItemTypeFactory
from catalogue.models import TrackingMode
from locations.factories import StoreFactory, VehicleFactory, YardFactory
from locations.models import Location, LocationType
from locations.nodes import consumed_node, external_node, node_for_user, scrap_node
from network.factories import ClientFactory
from stock.models import (
    Condition,
    MovementType,
    OwnerType,
    StockBalance,
    StockMovement,
)
from stock.services import (
    InsufficientStock,
    LedgerRuleViolation,
    MovementRequest,
    balance_at,
    post_movement,
    recompute_balance,
    reverse_movement,
)


@pytest.fixture
def yard(tenant):
    return YardFactory(name="Main yard")


@pytest.fixture
def supplier(tenant):
    return external_node(tenant.pk)


@pytest.fixture
def bulk_item(tenant):
    return ItemTypeFactory(name="Jumper 1/2 inch 3m", default_tracking_mode=TrackingMode.BULK)


def receive(
    item, node, quantity, *, owner_client=None, condition=Condition.NEW, supplier_node=None
):
    """Receive stock, the way a posted gate-in will (D1)."""
    from locations.nodes import external_node as external

    source = supplier_node or external(node.organization_id, client=owner_client)
    with transaction.atomic():
        return post_movement(
            MovementRequest(
                item_type=item,
                quantity=Decimal(str(quantity)),
                from_node=source,
                to_node=node,
                movement_type=MovementType.RECEIPT,
                owner_type=OwnerType.CLIENT if owner_client else OwnerType.OWN,
                owner_client=owner_client,
                condition=condition,
            )
        )


class TestAppendOnly:
    """§3.2, M3: no UPDATE and no DELETE, ever."""

    def test_a_movement_cannot_be_modified_in_python(self, tenant, yard, bulk_item):
        movement = receive(bulk_item, yard.node, 10)

        movement.note = "tampered"
        with pytest.raises(ValidationError, match="append-only"):
            movement.save()

    def test_a_movement_cannot_be_deleted_in_python(self, tenant, yard, bulk_item):
        movement = receive(bulk_item, yard.node, 10)

        with pytest.raises(ValidationError, match="append-only"):
            movement.delete()

    def test_update_raises_in_the_database(self, tenant, yard, bulk_item):
        """The guarantee that matters: it holds against a raw query.

        A Python-only guard would not survive a management command, a data
        migration or a psql session — and an auditor is entitled to the stronger
        statement (M3).
        """
        movement = receive(bulk_item, yard.node, 10)

        with pytest.raises(IntegrityError, match="not permitted"), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE stock_stockmovement SET quantity = 999 WHERE id = %s",
                    [movement.pk],
                )

    def test_delete_raises_in_the_database(self, tenant, yard, bulk_item):
        movement = receive(bulk_item, yard.node, 10)

        with pytest.raises(IntegrityError, match="not permitted"), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM stock_stockmovement WHERE id = %s", [movement.pk])


class TestQuantitiesAreAlwaysPositive:
    """§3.2: direction lives in the node pair, never in a sign."""

    def test_a_negative_quantity_is_refused(self, tenant, yard, bulk_item, supplier):
        with pytest.raises(LedgerRuleViolation), transaction.atomic():
            post_movement(
                MovementRequest(
                    item_type=bulk_item,
                    quantity=Decimal("-5"),
                    from_node=supplier,
                    to_node=yard.node,
                    movement_type=MovementType.RECEIPT,
                )
            )

    def test_a_zero_quantity_is_refused(self, tenant, yard, bulk_item, supplier):
        with pytest.raises(LedgerRuleViolation), transaction.atomic():
            post_movement(
                MovementRequest(
                    item_type=bulk_item,
                    quantity=Decimal("0"),
                    from_node=supplier,
                    to_node=yard.node,
                    movement_type=MovementType.RECEIPT,
                )
            )

    def test_the_database_also_refuses_a_negative_quantity(self, tenant, yard, bulk_item, supplier):
        with pytest.raises(IntegrityError), transaction.atomic():
            StockMovement.objects.create(
                organization=tenant,
                occurred_at="2026-01-01T00:00:00Z",
                movement_type=MovementType.RECEIPT,
                item_type=bulk_item,
                tracking_mode=TrackingMode.BULK,
                uom="ea",
                quantity=Decimal("-1"),
                from_node=supplier,
                to_node=yard.node,
                owner_type=OwnerType.OWN,
                condition=Condition.NEW,
            )

    def test_a_movement_must_go_somewhere(self, tenant, yard, bulk_item):
        """A movement from a node to itself changes nothing and would inflate
        both sides of the balance invariant."""
        with pytest.raises(LedgerRuleViolation, match="different node"), transaction.atomic():
            post_movement(
                MovementRequest(
                    item_type=bulk_item,
                    quantity=Decimal("1"),
                    from_node=yard.node,
                    to_node=yard.node,
                    movement_type=MovementType.TRANSFER,
                )
            )


class TestBalancesFollowMovements:
    """§3.3: the cache is updated in the same transaction as the movement."""

    def test_receiving_increases_the_balance(self, tenant, yard, bulk_item):
        receive(bulk_item, yard.node, 40)

        assert balance_at(yard.node, bulk_item) == Decimal("40")

    def test_receiving_twice_accumulates_in_one_row(self, tenant, yard, bulk_item):
        """The cache must stay a cache. If own stock created a new row per post
        it would become a second movement log, and N-2's target would be lost."""
        receive(bulk_item, yard.node, 40)
        receive(bulk_item, yard.node, 10)

        assert balance_at(yard.node, bulk_item) == Decimal("50")
        assert StockBalance.objects.filter(node=yard.node, item_type=bulk_item).count() == 1

    def test_transferring_moves_quantity_between_nodes(self, tenant, yard, bulk_item):
        store = StoreFactory(parent=yard)
        receive(bulk_item, yard.node, 40)

        with transaction.atomic():
            post_movement(
                MovementRequest(
                    item_type=bulk_item,
                    quantity=Decimal("15"),
                    from_node=yard.node,
                    to_node=store.node,
                    movement_type=MovementType.TRANSFER,
                )
            )

        assert balance_at(yard.node, bulk_item) == Decimal("25")
        assert balance_at(store.node, bulk_item) == Decimal("15")

    def test_the_invariant_holds_after_a_sequence_of_postings(self, tenant, yard, bulk_item):
        """§3.2's stated invariant, over a realistic day in the yard."""
        store = StoreFactory(parent=yard)
        vehicle = VehicleFactory()
        technician = node_for_user(UserFactory(organization=tenant))

        receive(bulk_item, yard.node, 100)
        for source, destination, quantity in (
            (yard.node, store.node, 40),
            (store.node, vehicle.node, 25),
            (vehicle.node, technician, 10),
            (technician, consumed_node(tenant.pk), 6),
            (yard.node, scrap_node(tenant.pk), 5),
        ):
            with transaction.atomic():
                post_movement(
                    MovementRequest(
                        item_type=bulk_item,
                        quantity=Decimal(str(quantity)),
                        from_node=source,
                        to_node=destination,
                        movement_type=MovementType.TRANSFER,
                    )
                )

        # Every cached balance must equal the ledger recomputed from scratch.
        for balance in StockBalance.objects.all():
            recomputed = recompute_balance(
                tenant.pk,
                balance.node,
                balance.item_type,
                balance.owner_client,
                balance.condition,
            )
            assert balance.quantity == recomputed, (
                f"cached balance at {balance.node.label} is {balance.quantity} but "
                f"the ledger says {recomputed}"
            )

    def test_no_node_inside_the_yard_holds_a_negative_balance(self, tenant, yard, bulk_item):
        """§14: "no negative balance exists at any node"."""
        receive(bulk_item, yard.node, 10)

        with pytest.raises(InsufficientStock), transaction.atomic():
            post_movement(
                MovementRequest(
                    item_type=bulk_item,
                    quantity=Decimal("11"),
                    from_node=yard.node,
                    to_node=scrap_node(tenant.pk),
                    movement_type=MovementType.DISPOSE,
                )
            )

        assert balance_at(yard.node, bulk_item) == Decimal("10")

    def test_the_external_node_may_go_negative(self, tenant, yard, bulk_item, supplier):
        """It represents the outside world, which is where receipts come from.

        Without this, the very first receipt into an empty yard would be refused
        for lack of stock at the supplier.
        """
        receive(bulk_item, yard.node, 40)

        assert balance_at(supplier, bulk_item) == Decimal("-40")

    def test_insufficient_stock_reports_what_is_available(self, tenant, yard, bulk_item):
        """§6.1: the client needs the figures to render a useful message."""
        receive(bulk_item, yard.node, 3)

        with pytest.raises(InsufficientStock) as caught, transaction.atomic():
            post_movement(
                MovementRequest(
                    item_type=bulk_item,
                    quantity=Decimal("10"),
                    from_node=yard.node,
                    to_node=consumed_node(tenant.pk),
                    movement_type=MovementType.CONSUME,
                )
            )

        assert caught.value.code == "INSUFFICIENT_STOCK"
        assert caught.value.details["available"] == "3.000"


class TestOwnershipIsTracked:
    """D3: client-owned and own stock, separately and unambiguously."""

    def test_client_stock_and_own_stock_are_separate_balances(self, tenant, yard, bulk_item):
        """The same item type, in the same place, counted apart.

        Mixing them would make the client-owned position report (M1) meaningless
        and an operator audit unanswerable.
        """
        safaricom = ClientFactory(name="Safaricom")
        receive(bulk_item, yard.node, 40)
        receive(bulk_item, yard.node, 25, owner_client=safaricom)

        assert balance_at(yard.node, bulk_item) == Decimal("40")
        assert balance_at(yard.node, bulk_item, owner_client=safaricom) == Decimal("25")

    def test_client_owned_stock_must_name_its_client(self, tenant, yard, bulk_item, supplier):
        with pytest.raises(LedgerRuleViolation, match="name its client"), transaction.atomic():
            post_movement(
                MovementRequest(
                    item_type=bulk_item,
                    quantity=Decimal("1"),
                    from_node=supplier,
                    to_node=yard.node,
                    movement_type=MovementType.RECEIPT,
                    owner_type=OwnerType.CLIENT,
                    owner_client=None,
                )
            )

    def test_own_stock_must_not_name_a_client(self, tenant, yard, bulk_item, supplier):
        with pytest.raises(
            LedgerRuleViolation, match="must not name a client"
        ), transaction.atomic():
            post_movement(
                MovementRequest(
                    item_type=bulk_item,
                    quantity=Decimal("1"),
                    from_node=supplier,
                    to_node=yard.node,
                    movement_type=MovementType.RECEIPT,
                    owner_type=OwnerType.OWN,
                    owner_client=ClientFactory(),
                )
            )

    def test_client_stock_cannot_be_issued_against_own_stock(self, tenant, yard, bulk_item):
        """Ownership is part of a balance's identity, so it cannot be swapped."""
        safaricom = ClientFactory(name="Safaricom")
        receive(bulk_item, yard.node, 5, owner_client=safaricom)

        # No own stock exists, even though there are 5 client-owned units there.
        with pytest.raises(InsufficientStock), transaction.atomic():
            post_movement(
                MovementRequest(
                    item_type=bulk_item,
                    quantity=Decimal("1"),
                    from_node=yard.node,
                    to_node=consumed_node(tenant.pk),
                    movement_type=MovementType.CONSUME,
                    owner_type=OwnerType.OWN,
                )
            )


class TestConditionIsPartOfIdentity:
    """D2, J1: faulty material is not the same stock as serviceable material."""

    def test_conditions_are_counted_separately(self, tenant, yard, bulk_item):
        receive(bulk_item, yard.node, 10, condition=Condition.NEW)
        receive(bulk_item, yard.node, 3, condition=Condition.FAULTY)

        assert balance_at(yard.node, bulk_item, condition=Condition.NEW) == Decimal("10")
        assert balance_at(yard.node, bulk_item, condition=Condition.FAULTY) == Decimal("3")

    def test_quarantined_stock_is_not_available(self, tenant, yard, bulk_item):
        """J1: "quarantined stock never appears as available"."""
        quarantine = Location.objects.get(parent=yard, type=LocationType.QUARANTINE)
        receive(bulk_item, quarantine.node, 3, condition=Condition.FAULTY)
        receive(bulk_item, yard.node, 10)

        available = StockBalance.objects.available()

        assert quarantine.node not in {balance.node for balance in available}
        assert yard.node in {balance.node for balance in available}


class TestReversal:
    """M4: corrections are reversals, never edits."""

    def test_a_reversal_returns_the_material(self, tenant, yard, bulk_item):
        store = StoreFactory(parent=yard)
        receive(bulk_item, yard.node, 40)

        with transaction.atomic():
            movement = post_movement(
                MovementRequest(
                    item_type=bulk_item,
                    quantity=Decimal("15"),
                    from_node=yard.node,
                    to_node=store.node,
                    movement_type=MovementType.TRANSFER,
                )
            )
        with transaction.atomic():
            reverse_movement(movement)

        assert balance_at(yard.node, bulk_item) == Decimal("40")
        assert balance_at(store.node, bulk_item) == Decimal("0")

    def test_the_original_movement_survives_untouched(self, tenant, yard, bulk_item):
        """The point of a reversal: history shows both entries."""
        store = StoreFactory(parent=yard)
        receive(bulk_item, yard.node, 40)
        with transaction.atomic():
            movement = post_movement(
                MovementRequest(
                    item_type=bulk_item,
                    quantity=Decimal("15"),
                    from_node=yard.node,
                    to_node=store.node,
                    movement_type=MovementType.TRANSFER,
                )
            )
        with transaction.atomic():
            reversal = reverse_movement(movement)

        movement.refresh_from_db()
        assert movement.quantity == Decimal("15")
        assert reversal.reversal_of_id == movement.pk
        assert reversal.movement_type == MovementType.REVERSAL

    def test_a_movement_cannot_be_reversed_twice(self, tenant, yard, bulk_item):
        """Otherwise a double-click would silently double the correction."""
        store = StoreFactory(parent=yard)
        receive(bulk_item, yard.node, 40)
        with transaction.atomic():
            movement = post_movement(
                MovementRequest(
                    item_type=bulk_item,
                    quantity=Decimal("15"),
                    from_node=yard.node,
                    to_node=store.node,
                    movement_type=MovementType.TRANSFER,
                )
            )
        with transaction.atomic():
            reverse_movement(movement)

        with pytest.raises(
            LedgerRuleViolation, match="already been reversed"
        ), transaction.atomic():
            reverse_movement(movement)


class TestPostingRequiresATransaction:
    @pytest.mark.django_db(transaction=True)
    def test_posting_outside_a_transaction_is_refused(self, organization):
        """The movement and the balances it changes must commit together."""
        from core.tenancy import tenant_context

        # The fixtures themselves need a transaction, because the tenant context
        # is published to Postgres transaction-locally for row-level security.
        with transaction.atomic(), tenant_context(organization):
            yard = YardFactory(name="Main yard")
            item = ItemTypeFactory()
            source = external_node(organization.pk)

        # Now attempt the post in autocommit mode. It must refuse before
        # touching the database at all.
        with tenant_context(organization):
            assert not connection.in_atomic_block

            with pytest.raises(RuntimeError, match="inside a transaction"):
                post_movement(
                    MovementRequest(
                        item_type=item,
                        quantity=Decimal("1"),
                        from_node=source,
                        to_node=yard.node,
                        movement_type=MovementType.RECEIPT,
                    )
                )
