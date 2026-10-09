"""Test factories for locations (design §14)."""

from decimal import Decimal

import factory
from factory.django import DjangoModelFactory

from locations.models import Location, LocationType


class YardFactory(DjangoModelFactory):
    """A yard. Its quarantine child and stock node are created by signal."""

    class Meta:
        model = Location

    name = factory.Sequence(lambda n: f"Yard {n}")
    type = LocationType.YARD
    # R13: a yard is clockable, so tests get a real place (Westlands, Nairobi).
    latitude = Decimal("-1.264000")
    longitude = Decimal("36.803000")


class OfficeFactory(DjangoModelFactory):
    """An office (R13): clockable, never stocked, so it has no StockNode."""

    class Meta:
        model = Location

    name = factory.Sequence(lambda n: f"Office {n}")
    type = LocationType.OFFICE
    latitude = Decimal("-1.286000")
    longitude = Decimal("36.817000")


class StoreFactory(DjangoModelFactory):
    class Meta:
        model = Location

    name = factory.Sequence(lambda n: f"Store {n}")
    type = LocationType.STORE
    parent = factory.SubFactory(YardFactory)


class VehicleFactory(DjangoModelFactory):
    class Meta:
        model = Location

    name = factory.Sequence(lambda n: f"Vehicle {n}")
    type = LocationType.VEHICLE
    vehicle_reg = factory.Sequence(lambda n: f"KDA {100 + n}X")
