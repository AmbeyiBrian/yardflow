"""Location and stock node endpoints (design §6; C4, §3.1)."""

from __future__ import annotations

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.response import Response

from accounts.permissions_registry import PERM
from attendance.places import AreaHistoryMixin, coordinates_needed
from core.api import TenantScopedViewSet
from core.geo import CoordinatesMixin
from locations.models import Location, LocationType, StockNode


class LocationSerializer(CoordinatesMixin, serializers.ModelSerializer):
    children = serializers.SerializerMethodField()
    node_id = serializers.SerializerMethodField()
    has_coordinates = serializers.SerializerMethodField()

    def coordinates_required(self, attrs):  # type: ignore[no-untyped-def,override]
        """R13, §4.18.8: a YARD or OFFICE needs them; system rows are exempt."""
        instance = self.instance
        kind = attrs.get("type", instance.type if instance is not None else None)
        is_system = bool(instance.is_system) if instance is not None else False
        rule = kind in (LocationType.YARD, LocationType.OFFICE) and not is_system
        return coordinates_needed(self, rule)

    def get_has_coordinates(self, location: Location) -> bool:
        return location.latitude is not None and location.longitude is not None

    class Meta:
        model = Location
        fields = (
            "id",
            "parent",
            "name",
            "code",
            "type",
            "vehicle_reg",
            "is_active",
            "is_system",
            "latitude",
            "longitude",
            "radius_m",
            "area_history",
            "has_coordinates",
            "children",
            "node_id",
        )
        # area_history is written by the area-change service (§4.18.4), never by a client.
        read_only_fields = ("is_system", "area_history")

    def get_children(self, location: Location) -> list[dict]:
        return [
            {"id": child.pk, "name": child.name, "type": child.type}
            for child in location.children.all()
        ]

    def get_node_id(self, location: Location) -> int | None:
        """The stock node for this location, which movements reference (§3.1)."""
        node = getattr(location, "node", None)
        return node.pk if node else None


class StockNodeSerializer(serializers.ModelSerializer):
    holds_available_stock = serializers.BooleanField(read_only=True)

    class Meta:
        model = StockNode
        fields = (
            "id",
            "type",
            "label",
            "location",
            "user",
            "site",
            "client",
            "holds_available_stock",
        )
        read_only_fields = fields


class LocationViewSet(AreaHistoryMixin, TenantScopedViewSet):
    """``/api/v1/locations`` (C4)."""

    serializer_class = LocationSerializer
    model = Location
    select_related = ("parent",)
    prefetch_related = ("children",)
    required_permissions = {
        "create": PERM.CATALOGUE_MANAGE,
        "update": PERM.CATALOGUE_MANAGE,
        "partial_update": PERM.CATALOGUE_MANAGE,
        "destroy": PERM.CATALOGUE_MANAGE,
        "deactivate": PERM.CATALOGUE_MANAGE,
    }
    filterset_fields = ["type", "parent", "is_active"]
    search_fields = ["name", "code", "vehicle_reg"]
    ordering_fields = ["name", "type"]

    def get_queryset(self):  # type: ignore[no-untyped-def]
        """R13, §4.18.2: an office is not a stock location, so the stock pickers
        (which list locations unfiltered) never see one. Asking for
        ``?type=OFFICE`` still returns them, for Settings to manage."""
        queryset = super().get_queryset()
        # Settings' "places without coordinates" list needs the offices too.
        if self.request.query_params.get("type") != LocationType.OFFICE and not self._missing():
            queryset = queryset.exclude(type=LocationType.OFFICE)
        return queryset

    def _missing(self) -> bool:
        raw = self.request.query_params.get("missing_coordinates", "")
        return raw.lower() in {"1", "true", "yes"}

    def filter_queryset(self, queryset):  # type: ignore[no-untyped-def]
        queryset = super().filter_queryset(queryset)
        if self._missing():
            # R13: the places that need coordinates and have none.
            queryset = queryset.filter(
                type__in=(LocationType.YARD, LocationType.OFFICE),
                is_system=False,
                latitude__isnull=True,
            )
        return queryset

    def perform_destroy(self, instance):  # type: ignore[no-untyped-def]
        """C4: a location holding stock is deactivated, never deleted."""
        try:
            instance.delete()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc

    @action(detail=True, methods=["post"])
    def deactivate(self, request, pk=None):  # type: ignore[no-untyped-def]
        location = self.get_object()
        location.is_active = False
        location.save(update_fields=["is_active"])
        return Response(self.get_serializer(location).data)


class StockNodeViewSet(TenantScopedViewSet):
    """``/api/v1/stock-nodes`` — read-only (§3.1).

    Nodes are auto-created and never deleted, so there is nothing for a client to
    create or destroy here. Exposed because every movement references one, and
    the UI needs to name them.
    """

    serializer_class = StockNodeSerializer
    model = StockNode
    select_related = ("location", "user", "site", "client")
    http_method_names = ["get", "head", "options"]
    filterset_fields = ["type", "location", "user", "site", "client"]
    search_fields = ["label"]

    @action(detail=False, methods=["get"])
    def available(self, request):  # type: ignore[no-untyped-def]
        """Nodes whose contents count as available stock (§3.3, J1)."""
        nodes = self.filter_queryset(self.get_queryset()).available()
        page = self.paginate_queryset(nodes)
        if page is not None:
            return self.get_paginated_response(self.get_serializer(page, many=True).data)
        return Response(self.get_serializer(nodes, many=True).data)
