"""Isolation fixtures for the catalogue endpoints (T1.20, A3).

Registered here rather than in the test module so the declaration sits with the
app that owns the endpoint. A tenant-scoped endpoint with no fixture fails the
suite, so this is how coverage stays automatic.
"""

from core.isolation import register_isolation_fixture


def register() -> None:
    from catalogue.models import CategoryCustomField, ItemCategory, ItemType

    def make_category(organization):
        return ItemCategory.objects.create(organization=organization, name="Isolation")

    def make_custom_field(organization):
        return CategoryCustomField.objects.create(
            organization=organization,
            category=make_category(organization),
            label="Vendor",
            key="vendor",
            field_type=CategoryCustomField.FieldType.TEXT,
        )

    def make_item_type(organization):
        return ItemType.objects.create(
            organization=organization,
            category=make_category(organization),
            name="Isolation item",
        )

    register_isolation_fixture(
        "item-category", make_category, payload={"name": "Renamed", "criticality": "LOW"}
    )
    register_isolation_fixture(
        "category-custom-field",
        make_custom_field,
        payload={"label": "Renamed", "key": "vendor", "field_type": "TEXT"},
    )
    register_isolation_fixture(
        "item-type", make_item_type, payload={"name": "Renamed", "uom": "ea"}
    )
