"""Test factories for the ledger (design §14)."""

from decimal import Decimal

import factory
from factory.django import DjangoModelFactory

from catalogue.factories import ItemTypeFactory
from catalogue.models import TrackingMode
from stock.models import Condition, OwnerType, Reel, SerialUnit


class SerialUnitFactory(DjangoModelFactory):
    class Meta:
        model = SerialUnit

    item_type = factory.SubFactory(
        ItemTypeFactory, default_tracking_mode=TrackingMode.SERIALIZED
    )
    serial_number = factory.Sequence(lambda n: f"SN{100000 + n}")
    condition = Condition.NEW
    owner_type = OwnerType.OWN


class ReelFactory(DjangoModelFactory):
    class Meta:
        model = Reel

    item_type = factory.SubFactory(
        ItemTypeFactory, default_tracking_mode=TrackingMode.REEL, uom="m"
    )
    drum_number = factory.Sequence(lambda n: f"D-{1000 + n}")
    initial_length = Decimal("500.000")
    remaining_length = Decimal("500.000")
    uom = "m"
    condition = Condition.NEW
    owner_type = OwnerType.OWN
