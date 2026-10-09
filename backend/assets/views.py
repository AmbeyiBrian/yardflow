"""``/api/v1/assets`` (R14, design §4.20.6).

Every member reads the register (a fuel entry needs to pick a vehicle); writes
are ``asset.manage``. Handing over is also open to the current holder, which the
service decides, so the viewset leaves that action unmapped.
"""

from __future__ import annotations

from datetime import date

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.response import Response

from accounts.models import User
from accounts.permissions_registry import PERM
from accounts.services import resolve_permissions
from assets import services
from assets.models import Asset
from assets.serializers import (
    AssetCloseInputSerializer,
    AssetFuelSerializer,
    AssetFuelSummarySerializer,
    AssetHandOverInputSerializer,
    AssetHandoverSerializer,
    AssetSerializer,
)
from core.api import TenantScopedViewSet
from core.exceptions import PermissionDeniedError

FROM = OpenApiParameter("from", str, description="First day, YYYY-MM-DD.")
TO = OpenApiParameter("to", str, description="Last day, YYYY-MM-DD.")


def _date_param(request, name: str) -> date | None:  # type: ignore[no-untyped-def]
    raw = request.query_params.get(name)
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise serializers.ValidationError({name: ["Use a date as YYYY-MM-DD."]}) from exc


def _fuel_data(position: services.FuelPosition) -> dict:
    return {
        "litres": position.litres,
        "spend": position.spend,
        "fill_count": position.fill_count,
        "spend_per_litre": position.spend_per_litre,
        "pending_spend": position.pending_spend,
    }


class AssetViewSet(TenantScopedViewSet):
    """The register. Never deleted: close it instead (§4.20.2)."""

    serializer_class = AssetSerializer
    model = Asset
    select_related = ("supplier", "holder")
    required_permissions = {
        "create": PERM.ASSET_MANAGE,
        "update": PERM.ASSET_MANAGE,
        "partial_update": PERM.ASSET_MANAGE,
        "close": PERM.ASSET_MANAGE,
    }
    filterset_fields = ["type", "status", "holder", "supplier"]
    search_fields = ["name", "tag", "make", "model"]
    ordering_fields = ["name", "type", "created_at"]
    http_method_names = ["get", "post", "patch", "head", "options"]

    def perform_create(self, serializer):  # type: ignore[no-untyped-def]
        # The service stamps who did it (audit, first handover); the model has no
        # ``created_by`` to pass.
        serializer.save()

    @extend_schema(request=AssetHandOverInputSerializer, responses={201: AssetHandoverSerializer})
    @action(detail=True, methods=["post"])
    def handover(self, request, pk=None):  # type: ignore[no-untyped-def]
        """Give it on, or back to the yard (``to_holder: null``)."""
        asset = self.get_object()
        body = AssetHandOverInputSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        to_id = body.validated_data["to_holder"]
        to_holder = None
        if to_id is not None:
            to_holder = User.objects.filter(pk=to_id).first()
            if to_holder is None:
                raise serializers.ValidationError({"to_holder": ["That person was not found."]})
        handover = services.hand_over(
            asset,
            actor=request.user,
            to_holder=to_holder,
            note=body.validated_data.get("note", ""),
            handed_over_on=body.validated_data.get("handed_over_on"),
            request=request,
        )
        return Response(AssetHandoverSerializer(handover).data, status=201)

    @extend_schema(request=AssetCloseInputSerializer, responses={200: AssetSerializer})
    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):  # type: ignore[no-untyped-def]
        """Sold or written off; frozen afterwards (§4.20.4)."""
        asset = self.get_object()
        body = AssetCloseInputSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        closed = services.close_asset(
            asset,
            actor=request.user,
            closed_on=body.validated_data["closed_on"],
            closed_reason=body.validated_data["closed_reason"],
            closed_note=body.validated_data.get("closed_note", ""),
            request=request,
        )
        return Response(self.get_serializer(self.get_queryset().get(pk=closed.pk)).data)

    @extend_schema(responses={200: AssetHandoverSerializer(many=True)})
    @action(detail=True, methods=["get"])
    def handovers(self, request, pk=None):  # type: ignore[no-untyped-def]
        """Every holder, newest first (R14)."""
        rows = services.handovers_of(self.get_object())
        return Response(AssetHandoverSerializer(rows, many=True).data)

    @extend_schema(parameters=[FROM, TO], responses={200: AssetFuelSerializer})
    @action(detail=True, methods=["get"])
    def fuel(self, request, pk=None):  # type: ignore[no-untyped-def]
        """Litres, spend, fills and spend per litre; pending apart (§4.20.4)."""
        asset = self.get_object()
        position = services.fuel_position(
            asset, _date_param(request, "from"), _date_param(request, "to")
        )
        return Response(AssetFuelSerializer(_fuel_data(position)).data)

    @extend_schema(parameters=[FROM, TO], responses={200: AssetFuelSummarySerializer(many=True)})
    @action(detail=False, methods=["get"], url_path="fuel-summary")
    def fuel_summary(self, request):  # type: ignore[no-untyped-def]
        """Vehicles ranked by fuel spend, for the owner (§4.20.6)."""
        permissions = resolve_permissions(request.user)
        if not (permissions.has(PERM.ASSET_MANAGE) or permissions.has(PERM.REPORT_VIEW_ALL)):
            raise PermissionDeniedError()
        rows = [
            {
                "id": asset.pk,
                "name": asset.name,
                "tag": asset.tag,
                "type": asset.type,
                **_fuel_data(position),
            }
            for asset, position in services.fuel_ranking(
                _date_param(request, "from"), _date_param(request, "to")
            )
        ]
        return Response(AssetFuelSummarySerializer(rows, many=True).data)
