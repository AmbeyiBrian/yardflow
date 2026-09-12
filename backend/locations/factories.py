"""Test factories for locations (design §14)."""

import factory
from factory.django import DjangoModelFactory

from locations.models import Location, LocationType


class YardFactory(DjangoModelFactory):
    """A yard. Its quarantine child and stock node are created by signal."""

    class Meta:
        model = Location

    name = factory.Sequence(lambda n: f"Yard {n}")
    type = LocationType.YARD


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
