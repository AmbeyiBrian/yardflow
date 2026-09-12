"""Isolation fixture for the receiving endpoint (T1.20, A3)."""

from decimal import Decimal

from django.utils import timezone

from core.isolation import register_isolation_fixture


def register() -> None:
    from catalogue.models import ItemCategory, ItemType, TrackingMode
    from locations.models import Location, LocationType
    from receiving.models import GateIn, GateInLine, GateInSource

    def make_gate_in(organization):
        yard = Location.objects.filter(
            organization=organization, type=LocationType.YARD
        ).first() or Location.objects.create(
            organization=organization, name="Isolation yard", type=LocationType.YARD
        )
        category = ItemCategory.objects.filter(organization=organization).first() or (
            ItemCategory.objects.create(organization=organization, name="Isolation")
        )
        item = ItemType.objects.filter(organization=organization).first() or (
            ItemType.objects.create(
                organization=organization, category=category, name="Isolation item"
            )
        )

        gate_in = GateIn.objects.create(
            organization=organization,
            source_type=GateInSource.PURCHASE,
            supplier_name="Isolation supplier",
            to_location=yard,
            received_at=timezone.now(),
        )
        GateInLine.objects.create(
            organization=organization,
            gate_in=gate_in,
            item_type=item,
            tracking_mode=TrackingMode.BULK,
            quantity=Decimal("1"),
            uom=item.uom,
        )
        return gate_in

    register_isolation_fixture("gate-in", make_gate_in, payload={"notes": "renamed"})
