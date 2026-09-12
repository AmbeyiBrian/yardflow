"""Catalogue endpoints (design §6, §2.4; C1, C2, C3).

Every viewset here inherits :class:`~core.api.TenantScopedViewSet`, so scoping
and the 404-not-403 rule come from the base class rather than being
re-implemented — and T1.20's suite discovers each one automatically.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError as DjangoValidationError
from django_filters import rest_framework as filters
from rest_framework.decorators import action
from rest_framework.response import Response

from accounts.permissions_registry import PERM
from catalogue.models import CategoryCustomField, ItemCategory, ItemType
from catalogue.serializers import (
    CategoryCustomFieldSerializer,
    ItemCategorySerializer,
    ItemTypeSerializer,
)
from core.api import TenantScopedViewSet


class ItemCategoryViewSet(TenantScopedViewSet):
    """``/api/v1/item-categories`` (C1, C2)."""

    serializer_class = ItemCategorySerializer
    model = ItemCategory
    select_related = ("parent",)
    prefetch_related = ("custom_fields",)
    # Reading the catalogue is needed by anyone receiving a delivery or raising
    # a gate-out; only changing it needs catalogue.manage (C3, B4).
    required_permissions = {
        "create": PERM.CATALOGUE_MANAGE,
        "update": PERM.CATALOGUE_MANAGE,
        "partial_update": PERM.CATALOGUE_MANAGE,
        "destroy": PERM.CATALOGUE_MANAGE,
    }
    filterset_fields = ["parent", "criticality", "is_archived"]
    search_fields = ["name", "code"]
    ordering_fields = ["name", "criticality", "created_at"]

    @action(detail=True, methods=["get"])
    def custom_fields(self, request, pk=None):  # type: ignore[no-untyped-def]
        """The custom fields defined for this category (C2)."""
        category = self.get_object()
        fields = category.custom_fields.filter(is_archived=False)
        return Response(CategoryCustomFieldSerializer(fields, many=True).data)

    def perform_destroy(self, instance):  # type: ignore[no-untyped-def]
        try:
            instance.delete()
        except DjangoValidationError as exc:
            from rest_framework import serializers

            raise serializers.ValidationError(exc.messages) from exc


class CategoryCustomFieldViewSet(TenantScopedViewSet):
    """``/api/v1/category-custom-fields`` (C2)."""

    serializer_class = CategoryCustomFieldSerializer
    model = CategoryCustomField
    select_related = ("category",)
    required_permission = PERM.CATALOGUE_MANAGE
    filterset_fields = ["category", "field_type", "required_at_gate_in", "is_archived"]
    ordering_fields = ["order", "label"]


class ItemTypeFilter(filters.FilterSet):
    """Filters the pickers need (C3)."""

    category = filters.NumberFilter(field_name="category")
    # Archived items are excluded from pickers by default (C3), but must remain
    # reachable: historical documents reference them.
    include_archived = filters.BooleanFilter(method="filter_include_archived")
    returnable = filters.BooleanFilter(field_name="is_returnable")
    tracking_mode = filters.CharFilter(field_name="default_tracking_mode")

    class Meta:
        model = ItemType
        fields = ["category", "returnable", "tracking_mode"]

    def filter_include_archived(self, queryset, name, value):  # type: ignore[no-untyped-def]
        return queryset if value else queryset.filter(is_archived=False)


class ItemTypeViewSet(TenantScopedViewSet):
    """``/api/v1/item-types`` (C3)."""

    serializer_class = ItemTypeSerializer
    model = ItemType
    select_related = ("category", "category__parent")
    filterset_class = ItemTypeFilter
    search_fields = ["name", "code", "description"]
    ordering_fields = ["name", "created_at"]
    required_permissions = {
        "create": PERM.CATALOGUE_MANAGE,
        "update": PERM.CATALOGUE_MANAGE,
        "partial_update": PERM.CATALOGUE_MANAGE,
        "destroy": PERM.CATALOGUE_MANAGE,
        "archive": PERM.CATALOGUE_MANAGE,
        "unarchive": PERM.CATALOGUE_MANAGE,
    }

    def get_queryset(self):  # type: ignore[no-untyped-def]
        queryset = super().get_queryset()
        # C3: "the archive is excluded from pickers". Opt in with
        # ?include_archived=true when looking at history.
        if self.action == "list" and "include_archived" not in self.request.query_params:
            queryset = queryset.filter(is_archived=False)
        return queryset

    @action(detail=True, methods=["post"])
    def archive(self, request, pk=None):  # type: ignore[no-untyped-def]
        """C3: item types are archived, never deleted once movements exist.

        A POST action rather than a PATCH on a field, per §6's convention that
        state changes are explicit sub-resources.
        """
        item = self.get_object()
        item.is_archived = True
        item.save(update_fields=["is_archived"])
        return Response(self.get_serializer(item).data)

    @action(detail=True, methods=["post"])
    def unarchive(self, request, pk=None):  # type: ignore[no-untyped-def]
        item = self.get_object()
        item.is_archived = False
        item.save(update_fields=["is_archived"])
        return Response(self.get_serializer(item).data)
