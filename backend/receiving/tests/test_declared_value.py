"""T10.7 — the client's declared value on a receipt (§4.6, §4.14; O11).

For consignment stock the figure that matters is **what the operator will debit
if it goes missing**, not what the item would cost us. That number lives on
their issue note, so it is captured when the material is received and carried
onto every movement of it from there (D27).
"""

from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.factories import UserFactory
from catalogue.factories import ItemTypeFactory
from catalogue.models import TrackingMode
from locations.factories import YardFactory
from network.factories import ClientFactory
from receiving.models import GateIn, GateInLine, GateInSource
from receiving.services import post_gate_in
from stock.models import Condition, OwnerType, StockMovement, UnitCostSource


@pytest.fixture
def yard(tenant):
    return YardFactory(name="Main yard")


@pytest.fixture
def storekeeper(tenant):
    return UserFactory(organization=tenant, full_name="Sara Storekeeper")


def receipt(tenant, yard, storekeeper, *, unit_cost, owner_type, declared=None):
    gate_in = GateIn.objects.create(
        organization=tenant,
        to_location=yard,
        received_at=timezone.now(),
        source_type=GateInSource.PURCHASE,
        supplier_name="Cable Supplies Ltd",
    )
    item = ItemTypeFactory(
        default_tracking_mode=TrackingMode.BULK, unit_cost=unit_cost
    )
    GateInLine.objects.create(
        organization=tenant,
        gate_in=gate_in,
        item_type=item,
        tracking_mode=TrackingMode.BULK,
        quantity=Decimal("10"),
        uom=item.uom,
        condition=Condition.NEW,
        owner_type=owner_type,
        owner_client=ClientFactory() if owner_type == OwnerType.CLIENT else None,
        declared_unit_value=declared,
    )
    post_gate_in(gate_in, posted_by=storekeeper)
    return gate_in


def movements_for(gate_in):
    return StockMovement.objects.filter(
        document_type="receiving.GateIn", document_id=str(gate_in.pk)
    )


@pytest.mark.django_db
class TestDeclaredValue:
    def test_a_client_owned_line_carries_the_clients_figure(
        self, tenant, yard, storekeeper
    ):
        """Their number, not ours — it is the one they will debit."""
        gate_in = receipt(
            tenant,
            yard,
            storekeeper,
            unit_cost=Decimal("50.00"),
            owner_type=OwnerType.CLIENT,
            declared=Decimal("475.00"),
        )

        movement = movements_for(gate_in).first()
        assert movement is not None
        assert movement.unit_cost == Decimal("475.00")
        assert movement.unit_cost_source == UnitCostSource.CLIENT_DECLARED

    def test_without_a_figure_the_line_is_unvalued_not_zero(
        self, tenant, yard, storekeeper
    ):
        """O11: reported as unvalued, so §10 states no confident understatement."""
        gate_in = receipt(
            tenant,
            yard,
            storekeeper,
            unit_cost=Decimal("50.00"),
            owner_type=OwnerType.CLIENT,
            declared=None,
        )

        movement = movements_for(gate_in).first()
        assert movement is not None
        assert movement.unit_cost is None
        assert movement.unit_cost_source == UnitCostSource.NONE

    def test_our_own_stock_uses_the_catalogue(self, tenant, yard, storekeeper):
        """Supplying the same figure twice gives it two ways to go stale."""
        gate_in = receipt(
            tenant,
            yard,
            storekeeper,
            unit_cost=Decimal("1200.00"),
            owner_type=OwnerType.OWN,
        )

        movement = movements_for(gate_in).first()
        assert movement is not None
        assert movement.unit_cost == Decimal("1200.00")
        assert movement.unit_cost_source == UnitCostSource.CATALOGUE

    def test_a_declared_figure_on_our_own_stock_is_ignored(
        self, tenant, yard, storekeeper
    ):
        """The field means "what the client says it is worth". On our own
        material there is no client saying anything."""
        gate_in = receipt(
            tenant,
            yard,
            storekeeper,
            unit_cost=Decimal("1200.00"),
            owner_type=OwnerType.OWN,
            declared=Decimal("999.00"),
        )

        movement = movements_for(gate_in).first()
        assert movement is not None
        assert movement.unit_cost == Decimal("1200.00")
        assert movement.unit_cost_source == UnitCostSource.CATALOGUE
