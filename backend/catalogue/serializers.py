"""Catalogue serializers (design §6; C1, C2, C3)."""

from __future__ import annotations

from rest_framework import serializers

from catalogue.models import CategoryCustomField, ItemCategory, ItemType


class CategoryCustomFieldSerializer(serializers.ModelSerializer):
    class Meta:
        model = CategoryCustomField
        fields = (
            "id",
            "category",
            "label",
            "key",
            "field_type",
            "required_at_gate_in",
            "options",
            "order",
            "is_archived",
        )

    def validate(self, attrs):  # type: ignore[no-untyped-def]
        # The key is immutable once set, because it is written into every stored
        # value and changing it would orphan captured history.
        if self.instance and "key" in attrs and attrs["key"] != self.instance.key:
            raise serializers.ValidationError(
                {
                    "key": [
                        "A custom field key cannot be changed. Archive this field "
                        "and add a new one instead."
                    ]
                }
            )
        return attrs


class ItemCategorySerializer(serializers.ModelSerializer):
    custom_fields = CategoryCustomFieldSerializer(many=True, read_only=True)
    effective_criticality = serializers.SerializerMethodField()
    item_type_count = serializers.SerializerMethodField()

    class Meta:
        model = ItemCategory
        fields = (
            "id",
            "parent",
            "name",
            "code",
            "criticality",
            "effective_criticality",
            "is_archived",
            "custom_fields",
            "item_type_count",
        )

    def get_effective_criticality(self, category: ItemCategory) -> str:
        """What routing will actually use, including inheritance (C1, F3)."""
        return category.effective_criticality()

    def get_item_type_count(self, category: ItemCategory) -> int:
        return category.item_types.count()


class ItemTypeSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source="category.name", read_only=True)
    criticality = serializers.SerializerMethodField()
    # A method field rather than a DecimalField: the value is withheld
    # entirely when money tracking is off (D16), and one place deciding that is
    # better than a declared field plus an override that has to remember to.
    unit_cost = serializers.SerializerMethodField()

    class Meta:
        model = ItemType
        fields = (
            "id",
            "category",
            "category_name",
            "name",
            "code",
            "description",
            "default_tracking_mode",
            "uom",
            "is_returnable",
            "default_return_days",
            "min_stock_qty",
            "unit_cost",
            "is_archived",
            "criticality",
            "attributes",
        )

    def get_criticality(self, item: ItemType) -> str:
        return item.criticality

    def get_unit_cost(self, item: ItemType) -> str | None:
        """D16: money tracking is off by default.

        The cost is *withheld*, not sent-and-hidden: a figure the client is not
        meant to display should not be in the payload at all, or it ends up on a
        screen by accident.
        """
        organization = getattr(self.context.get("request"), "organization", None)
        settings = getattr(organization, "settings", None)
        if settings is not None and not settings.money_tracking_enabled:
            return None
        return str(item.unit_cost) if item.unit_cost is not None else None
