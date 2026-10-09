"""Isolation fixtures for the location endpoints (T1.20, A3)."""

from decimal import Decimal

from core.isolation import register_isolation_fixture


def register() -> None:
    from locations.models import Location, LocationType

    def make_location(organization):
        return Location.objects.create(
            organization=organization,
            name="Isolation yard",
            type=LocationType.YARD,
            latitude=Decimal("-1.264000"),
            longitude=Decimal("36.803000"),
        )

    def make_stock_node(organization):
        # Nodes are auto-created with their location (§3.1).
        return make_location(organization).node

    register_isolation_fixture("location", make_location, payload={"name": "Renamed"})
    register_isolation_fixture("stock-node", make_stock_node)
