"""T10.6 — what a movement was worth when it moved (§3.2, §4.14; O11, D27).

D27 is the rule: the valuation is captured when the movement posts and never
recomputed. A margin that moves months after a PO closed is worth nothing in a
conversation with a client, and repricing an item type is an ordinary thing to
do.

The second rule is subtler and is the one a reversal would break. A correction
has to cancel the cost it corrects, so a REVERSAL inherits the valuation of the
movement it reverses rather than taking today's price.
"""

from decimal import Decimal

import pytest
from django.db import transaction

from catalogue.factories import ItemTypeFactory
from locations.factories import YardFactory
from locations.nodes import external_node, node_for_location
from network.factories import ClientFactory
from stock.models import MovementType, OwnerType, UnitCostSource
from stock.services import MovementRequest, post_movement


@pytest.fixture
def yard(tenant):
    return YardFactory(name="Main yard")


def receive(tenant, item, node, quantity=Decimal("5"), **kwargs):
    with transaction.atomic():
        return post_movement(
            MovementRequest(
                item_type=item,
                quantity=quantity,
                from_node=external_node(tenant.pk),
                to_node=node,
                movement_type=MovementType.RECEIPT,
                **kwargs,
            )
        )


@pytest.mark.django_db
class TestValuationIsCapturedAtPosting:
    def test_own_material_takes_the_catalogue_price(self, tenant, yard):
        item = ItemTypeFactory(unit_cost=Decimal("1200.00"))
        movement = receive(tenant, item, node_for_location(yard))

        assert movement.unit_cost == Decimal("1200.00")
        assert movement.unit_cost_source == UnitCostSource.CATALOGUE

    def test_repricing_the_item_does_not_move_a_posted_movement(self, tenant, yard):
        """D27: the whole reason the column exists."""
        item = ItemTypeFactory(unit_cost=Decimal("1200.00"))
        movement = receive(tenant, item, node_for_location(yard))

        item.unit_cost = Decimal("9999.00")
        item.save()
        movement.refresh_from_db()

        assert movement.unit_cost == Decimal("1200.00")

    def test_an_item_with_no_price_is_unvalued_not_zero(self, tenant, yard):
        """A zero would be a silent understatement; NONE is answerable."""
        item = ItemTypeFactory(unit_cost=None)
        movement = receive(tenant, item, node_for_location(yard))

        assert movement.unit_cost is None
        assert movement.unit_cost_source == UnitCostSource.NONE

    def test_client_owned_material_is_unvalued_without_a_declared_figure(
        self, tenant, yard
    ):
        """O11: the operator's figure is the one that matters, and T10.7
        supplies it. Until it does, this is honestly unvalued."""
        item = ItemTypeFactory(unit_cost=Decimal("50.00"))
        movement = receive(
            tenant,
            item,
            node_for_location(yard),
            owner_type=OwnerType.CLIENT,
            owner_client=ClientFactory(),
        )

        assert movement.unit_cost is None
        assert movement.unit_cost_source == UnitCostSource.NONE

    def test_an_explicit_valuation_wins(self, tenant, yard):
        item = ItemTypeFactory(unit_cost=Decimal("50.00"))
        movement = receive(
            tenant,
            item,
            node_for_location(yard),
            owner_type=OwnerType.CLIENT,
            owner_client=ClientFactory(),
            unit_cost=Decimal("475.00"),
            unit_cost_source=UnitCostSource.CLIENT_DECLARED,
        )

        assert movement.unit_cost == Decimal("475.00")
        assert movement.unit_cost_source == UnitCostSource.CLIENT_DECLARED


@pytest.mark.django_db
class TestAReversalCancelsWhatItCorrects:
    def test_it_inherits_the_original_valuation(self, tenant, yard):
        """Otherwise a correction would not cancel the cost it corrects, and a
        project's margin would drift every time somebody fixed a mistake."""
        item = ItemTypeFactory(unit_cost=Decimal("1200.00"))
        node = node_for_location(yard)
        original = receive(tenant, item, node)

        item.unit_cost = Decimal("9999.00")
        item.save()

        with transaction.atomic():
            reversal = post_movement(
                MovementRequest(
                    item_type=item,
                    quantity=original.quantity,
                    from_node=node,
                    to_node=external_node(tenant.pk),
                    movement_type=MovementType.REVERSAL,
                    reversal_of=original,
                )
            )

        assert reversal.unit_cost == original.unit_cost == Decimal("1200.00")
        assert reversal.unit_cost_source == UnitCostSource.CATALOGUE

    def test_reversing_an_unvalued_movement_stays_unvalued(self, tenant, yard):
        item = ItemTypeFactory(unit_cost=None)
        node = node_for_location(yard)
        original = receive(tenant, item, node)

        with transaction.atomic():
            reversal = post_movement(
                MovementRequest(
                    item_type=item,
                    quantity=original.quantity,
                    from_node=node,
                    to_node=external_node(tenant.pk),
                    movement_type=MovementType.REVERSAL,
                    reversal_of=original,
                )
            )

        assert reversal.unit_cost is None
        assert reversal.unit_cost_source == UnitCostSource.NONE
