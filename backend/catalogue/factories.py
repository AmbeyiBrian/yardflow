"""Test factories for the catalogue (design §14)."""

import factory
from factory.django import DjangoModelFactory

from catalogue.models import CategoryCustomField, ItemCategory, ItemType, TrackingMode


class ItemCategoryFactory(DjangoModelFactory):
    class Meta:
        model = ItemCategory

    name = factory.Sequence(lambda n: f"Category {n}")


class CategoryCustomFieldFactory(DjangoModelFactory):
    class Meta:
        model = CategoryCustomField

    category = factory.SubFactory(ItemCategoryFactory)
    label = factory.Sequence(lambda n: f"Field {n}")
    key = factory.Sequence(lambda n: f"field_{n}")
    field_type = CategoryCustomField.FieldType.TEXT


class ItemTypeFactory(DjangoModelFactory):
    class Meta:
        model = ItemType

    category = factory.SubFactory(ItemCategoryFactory)
    name = factory.Sequence(lambda n: f"Item {n}")
    default_tracking_mode = TrackingMode.BULK
    uom = "ea"
