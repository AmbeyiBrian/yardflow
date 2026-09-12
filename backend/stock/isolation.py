"""Isolation fixtures for the stock endpoints (T1.20, A3).

The ledger endpoint matters most: a movement is the most sensitive row in the
system, and a leak here would show a rival exactly what we hold and for whom.
"""

from decimal import Decimal

from django.utils import timezone

from core.isolation import register_isolation_fixture


def register() -> None:
    from catalogue.models import ItemCategory, ItemType, TrackingMode
    from locations.models import Location, LocationType
    from locations.nodes import external_node, node_for_location
    from stock.models import (
        MovementType,
        OwnerType,
        Reel,
        SerialUnit,
        StockCount,
        StockMovement,
    )

    def _yard(organization):
        return Location.objects.filter(
            organization=organization, type=LocationType.YARD
        ).first() or Location.objects.create(
            organization=organization, name="Isolation yard", type=LocationType.YARD
        )

    def _item(organization):
        category = ItemCategory.objects.filter(organization=organization).first() or (
            ItemCategory.objects.create(organization=organization, name="Isolation")
        )
        return ItemType.objects.filter(organization=organization).first() or (
            ItemType.objects.create(
                organization=organization, category=category, name="Isolation item"
            )
        )

    def make_movement(organization):
        """Written directly rather than through ``post_movement``.

        This is a fixture for an isolation check, not a ledger test: going through
        the posting service would need balances, locks and a transaction, and
        would test the service rather than the endpoint's scoping.
        """
        yard = _yard(organization)
        item = _item(organization)
        return StockMovement.objects.create(
            organization=organization,
            occurred_at=timezone.now(),
            movement_type=MovementType.RECEIPT,
            item_type=item,
            tracking_mode=TrackingMode.BULK,
            uom=item.uom,
            quantity=Decimal("1"),
            # The constraint pairs these two: own stock names no client (D3).
            owner_type=OwnerType.OWN,
            from_node=external_node(organization.pk),
            to_node=node_for_location(yard),
        )

    def make_serial(organization):
        return SerialUnit.objects.create(
            organization=organization,
            item_type=_item(organization),
            serial_number="ISO-SERIAL-1",
            owner_type=OwnerType.OWN,
            current_node=node_for_location(_yard(organization)),
        )

    def make_reel(organization):
        item = _item(organization)
        return Reel.objects.create(
            organization=organization,
            item_type=item,
            drum_number="ISO-DRUM-1",
            initial_length=Decimal("500"),
            remaining_length=Decimal("500"),
            uom="m",
            owner_type=OwnerType.OWN,
            current_node=node_for_location(_yard(organization)),
        )

    def make_count(organization):
        return StockCount.objects.create(
            organization=organization,
            location=_yard(organization),
            counted_at=timezone.now(),
        )

    register_isolation_fixture("movement", make_movement)
    register_isolation_fixture("serial", make_serial)
    register_isolation_fixture("drum", make_reel)
    register_isolation_fixture("stock-count", make_count, payload={"notes": "renamed"})
