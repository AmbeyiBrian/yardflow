"""Stock endpoints (design §6, §3.3, §3.4; E1–E6, M1).

Reads first. E1's question — "do we have it, where is it, whose is it?" — is one
endpoint with filters rather than one per question, because a storekeeper does not
know in advance which of the three they are asking.

Two things here are deliberately *not* generic CRUD:

* ``/stock/lookup`` resolves a scanned identifier without being told what kind of
  identifier it is (D7, E2). A storekeeper with a barcode should not have to pick
  a search mode before scanning.
* transfers and counts go through their services, because both write movements
  and neither is expressible as a field edit (E4, E5).
"""

from __future__ import annotations

from decimal import Decimal

import django_filters
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.exceptions import MethodNotAllowed
from rest_framework.pagination import CursorPagination
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.api_permissions import HasPermission
from accounts.permissions_registry import PERM
from accounts.services import resolve_permissions
from core.api import TenantScopedViewSet
from core.api_permissions import OrganizationIsActive
from core.pagination import OccurrenceCursorPagination
from locations.models import NodeType
from stock.box_hooks import EventContext
from stock.boxes import (
    _box_path,
    _subtree,
    box_tree,
    empty_box,
    issuable_contents,
    move_box,
    take_out,
)
from stock.counting import add_count_line, post_stock_count, transfer_stock
from stock.models import (
    Box,
    BoxEvent,
    BoxStatus,
    Condition,
    Reel,
    SerialUnit,
    StockBalance,
    StockCount,
    StockCountStatus,
    StockMovement,
)
from stock.queries import (
    annotated_boxes,
    below_minimum_stock,
    client_owned_position,
    custody_holdings,
    find_by_identifier,
    installed_base,
    reel_history,
    serial_history,
    stock_as_at,
    stock_on_hand,
)


class StockBalanceSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="item_type.name", read_only=True)
    item_code = serializers.CharField(source="item_type.code", read_only=True)
    # How the item is tracked (§3.1). A serialized unit and a drum also show as a
    # balance here, so a screen offering "what am I carrying" needs this to avoid
    # listing the same radio twice — once as a quantity and once by serial.
    tracking_mode = serializers.CharField(
        source="item_type.default_tracking_mode", read_only=True
    )
    node_label = serializers.CharField(source="node.label", read_only=True)
    node_type = serializers.CharField(source="node.type", read_only=True)
    # I4, T5.11: for a PERSON node, who. The custody screen groups by holder and
    # opens one, and a label is not something to look a user up by.
    holder_id = serializers.IntegerField(source="node.user_id", read_only=True)
    site_id = serializers.IntegerField(source="node.site_id", read_only=True)
    # E1: "client-owned stock must be visually distinct wherever it appears." The
    # client's name travels with the row so no screen has to look it up and
    # forget to.
    owner_client_name = serializers.CharField(
        source="owner_client.name", read_only=True, default=""
    )

    class Meta:
        model = StockBalance
        fields = (
            "id",
            "item_type",
            "item_name",
            "item_code",
            "tracking_mode",
            "node",
            "node_label",
            "node_type",
            "holder_id",
            "site_id",
            "owner_client",
            "owner_client_name",
            "condition",
            "quantity",
            "uom",
        )


class MovementSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="item_type.name", read_only=True)
    from_label = serializers.CharField(source="from_node.label", read_only=True)
    to_label = serializers.CharField(source="to_node.label", read_only=True)
    posted_by_name = serializers.CharField(
        source="posted_by.full_name", read_only=True, default=""
    )
    owner_client_name = serializers.CharField(
        source="owner_client.name", read_only=True, default=""
    )
    serial_number = serializers.CharField(
        source="serial_unit.serial_number", read_only=True, default=""
    )
    drum_number = serializers.CharField(source="reel.drum_number", read_only=True, default="")

    class Meta:
        model = StockMovement
        fields = (
            "id",
            "occurred_at",
            "movement_type",
            "item_type",
            "item_name",
            "quantity",
            "uom",
            "from_node",
            "from_label",
            "to_node",
            "to_label",
            "condition",
            "from_condition",
            "owner_type",
            "owner_client",
            "owner_client_name",
            "serial_number",
            "drum_number",
            "document_type",
            "document_number",
            "document_id",
            "posted_by",
            "posted_by_name",
            "note",
        )


class SerialUnitSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="item_type.name", read_only=True)
    node_label = serializers.CharField(source="current_node.label", read_only=True)
    owner_client_name = serializers.CharField(
        source="owner_client.name", read_only=True, default=""
    )
    origin_site_ref = serializers.CharField(
        source="origin_site.internal_ref", read_only=True, default=""
    )
    # P4: the box the unit is in now, so a scan or a lookup can say "in CTN-1".
    box_code = serializers.CharField(source="box.code", read_only=True, default=None)
    box_path = serializers.SerializerMethodField()

    def get_box_path(self, unit) -> list[str]:
        """Codes outermost to the unit's own box; empty when loose (P4, P5)."""
        path: list[str] = []
        box = unit.box
        while box is not None:
            path.insert(0, box.code)
            box = box.parent
        return path

    class Meta:
        model = SerialUnit
        fields = (
            "id",
            "serial_number",
            "asset_tag",
            "item_type",
            "item_name",
            "status",
            "condition",
            "current_node",
            "node_label",
            "owner_type",
            "owner_client",
            "owner_client_name",
            "origin_site",
            "origin_site_ref",
            "box_code",
            "box_path",
        )


class ReelSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="item_type.name", read_only=True)
    node_label = serializers.CharField(source="current_node.label", read_only=True)
    owner_client_name = serializers.CharField(
        source="owner_client.name", read_only=True, default=""
    )

    class Meta:
        model = Reel
        fields = (
            "id",
            "drum_number",
            "item_type",
            "item_name",
            "status",
            "condition",
            "initial_length",
            "remaining_length",
            "uom",
            "current_node",
            "node_label",
            "owner_type",
            "owner_client",
            "owner_client_name",
        )


class StockOnHandView(APIView):
    """``/api/v1/stock`` (E1, T3.12).

    ``available_only`` defaults to true, which is J1: "quarantined stock never
    appears as available." A storekeeper asking what they have is asking what they
    can issue, and including a faulty unit in that answer is how it gets issued.
    """

    permission_classes = [IsAuthenticated, OrganizationIsActive]

    @extend_schema(
        parameters=[
            OpenApiParameter("item_type", required=False),
            OpenApiParameter("node", required=False),
            OpenApiParameter("owner_client", required=False),
            OpenApiParameter("condition", required=False),
            OpenApiParameter(
                "available_only",
                required=False,
                description="Default true. False includes quarantine, sites and custody.",
            ),
            OpenApiParameter(
                "as_at",
                required=False,
                description="ISO timestamp. Computed from the ledger rather than the cache (§3.4).",
            ),
        ],
        responses={200: StockBalanceSerializer(many=True)},
    )
    def get(self, request):  # type: ignore[no-untyped-def]
        params = request.query_params
        available_only = params.get("available_only", "true").lower() != "false"

        as_at = params.get("as_at")
        if as_at:
            from django.utils.dateparse import parse_datetime

            moment = parse_datetime(as_at)
            if moment is None:
                raise serializers.ValidationError(
                    {"as_at": ["Not a timestamp this can read. Use ISO 8601."]}
                )
            rows = stock_as_at(
                request.user.organization_id,
                moment,
                item_type=_lookup_item(params.get("item_type")),
                available_only=available_only,
            )
            # §3.4: computed from the ledger, so the shape is a list of dicts
            # rather than balance rows. Named separately so nobody mistakes a
            # historical figure for a live one.
            return Response({"as_at": as_at, "results": list(rows)})

        balances = stock_on_hand(
            item_type=_lookup_item(params.get("item_type")),
            node=_lookup(params.get("node"), "locations.StockNode"),
            owner_client=_lookup(params.get("owner_client"), "network.Client"),
            condition=params.get("condition") or None,
            available_only=available_only,
        )
        return Response({"results": StockBalanceSerializer(balances, many=True).data})


def _lookup_item(value):  # type: ignore[no-untyped-def]
    return _lookup(value, "catalogue.ItemType")


def _lookup(value, label):  # type: ignore[no-untyped-def]
    """Resolve an id inside the tenant, or 404 — never silently ignore it.

    A filter that quietly does nothing when its id is wrong shows the whole yard
    when the caller asked for one bin, which reads as a data error rather than a
    bad request.
    """
    if not value:
        return None

    from django.apps import apps

    model = apps.get_model(label)
    return get_object_or_404(model.objects.all(), pk=value)


class StockLookupView(APIView):
    """``/api/v1/stock/lookup`` (D7, E2, E3).

    One identifier in, whatever it is out. T3.20's criterion — "typing a serial
    number anywhere in search jumps straight to its history" — depends on the
    server saying *what* it found, so the client knows where to go.
    """

    permission_classes = [IsAuthenticated, OrganizationIsActive]

    @extend_schema(
        parameters=[OpenApiParameter("q", required=True, description="Serial, asset tag or drum.")],
        responses={
            200: inline_serializer(
                "StockLookup",
                {
                    "kind": serializers.CharField(),
                    "resource": serializers.CharField(),
                    "object": serializers.DictField(),
                },
            )
        },
    )
    def get(self, request):  # type: ignore[no-untyped-def]
        found = find_by_identifier(request.query_params.get("q", ""))
        if found is None:
            from django.http import Http404

            # A3, §2.4: an unknown identifier and another tenant's identifier
            # must be indistinguishable.
            raise Http404()

        if found["kind"] == "serial_unit":
            unit = found["object"]
            return Response(
                {
                    "kind": "serial_unit",
                    "resource": f"/stock/serials/{unit.serial_number}",
                    "object": SerialUnitSerializer(unit).data,
                }
            )

        if found["kind"] == "box":
            box = annotated_boxes().get(pk=found["object"].pk)
            return Response(
                {
                    "kind": "box",
                    "resource": f"/stock/boxes/{box.code}",
                    "object": BoxRowSerializer(box).data,
                }
            )

        reel = found["object"]
        return Response(
            {
                "kind": "reel",
                "resource": f"/stock/drums/{reel.drum_number}",
                "object": ReelSerializer(reel).data,
            }
        )


class SerialHistoryView(APIView):
    """``/api/v1/stock/serials/{serial_number}/history`` (E2, T3.14).

    E2 calls this "the single most likely question from an operator audit", so it
    is addressed by the serial number itself rather than by a database id — the
    auditor has the number printed on the unit in front of them.
    """

    permission_classes = [IsAuthenticated, OrganizationIsActive]

    @extend_schema(responses={200: MovementSerializer(many=True)})
    def get(self, request, serial_number: str):  # type: ignore[no-untyped-def]
        unit = SerialUnit.objects.filter(serial_number__iexact=serial_number).first()
        if unit is None:
            from django.http import Http404

            raise Http404()

        movements = serial_history(unit.serial_number)
        # P4: which boxes the unit came in and when it left them, beside where
        # it went. Box events are a projection, not movements, so they are
        # listed apart rather than mixed into the ledger trail.
        box_events = (
            BoxEvent.objects.filter(serial_unit=unit)
            .select_related("box", "actor")
            .order_by("occurred_at", "id")
        )
        return Response(
            {
                "unit": SerialUnitSerializer(unit).data,
                "movements": MovementSerializer(movements, many=True).data,
                "box_events": [
                    {
                        "occurred_at": event.occurred_at,
                        "action": event.action,
                        "action_label": event.get_action_display(),
                        "box_code": event.box.code,
                        "actor": event.actor.get_full_name() if event.actor else "",
                        "document_type": event.document_type,
                        "document_id": event.document_id,
                        "document_number": event.document_number,
                        "note": event.note,
                    }
                    for event in box_events
                ],
            }
        )


class ReelHistoryView(APIView):
    """``/api/v1/stock/drums/{drum_number}/history`` (E3, T3.14)."""

    permission_classes = [IsAuthenticated, OrganizationIsActive]

    @extend_schema(responses={200: MovementSerializer(many=True)})
    def get(self, request, drum_number: str):  # type: ignore[no-untyped-def]
        reel = Reel.objects.filter(drum_number__iexact=drum_number).first()
        if reel is None:
            from django.http import Http404

            raise Http404()

        return Response(
            {
                "reel": ReelSerializer(reel).data,
                "movements": MovementSerializer(reel_history(reel.drum_number), many=True).data,
            }
        )


class LowStockView(APIView):
    """``/api/v1/stock/low`` (E6)."""

    permission_classes = [IsAuthenticated, OrganizationIsActive]

    @extend_schema(
        responses={
            200: inline_serializer(
                "LowStock", {"results": serializers.ListField(child=serializers.DictField())}
            )
        }
    )
    def get(self, request):  # type: ignore[no-untyped-def]
        rows = below_minimum_stock(request.user.organization_id)
        return Response({"results": [_stringify(row) for row in rows]})


class ClientPositionView(APIView):
    """``/api/v1/stock/client-position`` (M1, K3).

    What an operator audit opens with: how much of their material are we holding.
    """

    permission_classes = [IsAuthenticated, OrganizationIsActive]

    @extend_schema(
        parameters=[OpenApiParameter("client", required=False)],
        responses={200: StockBalanceSerializer(many=True)},
    )
    def get(self, request):  # type: ignore[no-untyped-def]
        balances = client_owned_position(
            client=_lookup(request.query_params.get("client"), "network.Client")
        )
        return Response({"results": StockBalanceSerializer(balances, many=True).data})


class InstalledBaseView(APIView):
    """``/api/v1/stock/installed`` (H2, M1)."""

    permission_classes = [IsAuthenticated, OrganizationIsActive]

    @extend_schema(
        parameters=[OpenApiParameter("site", required=False)],
        responses={200: StockBalanceSerializer(many=True)},
    )
    def get(self, request):  # type: ignore[no-untyped-def]
        balances = installed_base(
            site=_lookup(request.query_params.get("site"), "network.Site")
        )
        return Response({"results": StockBalanceSerializer(balances, many=True).data})


class CustodyStockView(APIView):
    """``/api/v1/stock/custody`` (I1, I4).

    Read from the ledger's PERSON nodes, which is why this and the custody
    screens can never disagree (§4.10).
    """

    permission_classes = [IsAuthenticated, OrganizationIsActive]

    @extend_schema(
        parameters=[OpenApiParameter("holder", required=False)],
        responses={200: StockBalanceSerializer(many=True)},
    )
    def get(self, request):  # type: ignore[no-untyped-def]
        holder = _lookup(request.query_params.get("holder"), "accounts.User")
        return Response(
            {"results": StockBalanceSerializer(custody_holdings(user=holder), many=True).data}
        )


class MovementViewSet(TenantScopedViewSet):
    """``/api/v1/movements`` — the ledger, read-only (§3.2, M1).

    Append-only, and written only by ``post_movement``. There is no endpoint that
    creates one directly: every movement belongs to a document, and one that
    belonged to nothing would be unexplainable to an auditor.
    """

    serializer_class = MovementSerializer
    model = StockMovement
    select_related = (
        "item_type",
        "from_node",
        "to_node",
        "owner_client",
        "posted_by",
        "serial_unit",
        "reel",
    )
    filterset_fields = [
        "movement_type",
        "item_type",
        "from_node",
        "to_node",
        "owner_client",
        "document_type",
    ]
    search_fields = ["document_number", "note"]
    ordering_fields = ["occurred_at"]
    pagination_class = OccurrenceCursorPagination

    http_method_names = ["get", "head", "options"]


class HolderFilterMixin:
    """``?holder=<user id>`` — what one person is carrying (I1).

    Serialized units and drums are located by ``current_node``, and a person's
    node is created on their first custody (§3.1) — so a technician's screen has
    no id to filter on until they have held something. Filtering by the *user*
    instead means "what am I carrying?" is one request that works from the first
    render, and returns nothing rather than failing when they hold nothing.
    """

    def get_queryset(self):  # type: ignore[no-untyped-def]
        queryset = super().get_queryset()  # type: ignore[misc]
        holder = self.request.query_params.get("holder")  # type: ignore[attr-defined]
        if holder == "me":
            holder = self.request.user.pk  # type: ignore[attr-defined]
        if holder:
            queryset = queryset.filter(
                current_node__type=NodeType.PERSON, current_node__user_id=holder
            )
        return queryset


class SerialUnitViewSet(HolderFilterMixin, TenantScopedViewSet):
    """``/api/v1/serials`` (D3, E2, I1)."""

    serializer_class = SerialUnitSerializer
    model = SerialUnit
    select_related = ("item_type", "current_node", "owner_client", "origin_site")
    filterset_fields = ["status", "condition", "item_type", "current_node", "owner_client"]
    search_fields = ["serial_number", "asset_tag"]
    ordering_fields = ["serial_number", "created_at"]

    http_method_names = ["get", "head", "options"]

    @extend_schema(parameters=[OpenApiParameter("holder", description="User id, or 'me'.")])
    def list(self, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        return super().list(request, *args, **kwargs)


class ReelViewSet(HolderFilterMixin, TenantScopedViewSet):
    """``/api/v1/drums`` (D4, E3, I1)."""

    serializer_class = ReelSerializer
    model = Reel
    select_related = ("item_type", "current_node", "owner_client")
    filterset_fields = ["status", "condition", "item_type", "current_node", "owner_client"]
    search_fields = ["drum_number"]
    ordering_fields = ["drum_number", "remaining_length", "created_at"]

    http_method_names = ["get", "head", "options"]

    @extend_schema(parameters=[OpenApiParameter("holder", description="User id, or 'me'.")])
    def list(self, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        return super().list(request, *args, **kwargs)


class TransferSerializer(serializers.Serializer):
    """E4: an internal move between two places inside the perimeter."""

    item_type = serializers.IntegerField()
    quantity = serializers.DecimalField(max_digits=14, decimal_places=3)
    from_location = serializers.IntegerField()
    to_location = serializers.IntegerField()
    owner_client = serializers.IntegerField(required=False, allow_null=True)
    condition = serializers.CharField(required=False, default=Condition.NEW)
    serial_unit = serializers.IntegerField(required=False, allow_null=True)
    reel = serializers.IntegerField(required=False, allow_null=True)
    note = serializers.CharField(required=False, allow_blank=True, default="")


class TransferView(APIView):
    """``/api/v1/stock/transfers`` (E4).

    A destination outside the yard is refused by the service with an explanation
    of what to do instead — raise a gate-out — because a refusal that only says
    "not allowed" gets worked around.
    """

    permission_classes = [IsAuthenticated, OrganizationIsActive]

    @extend_schema(request=TransferSerializer, responses={201: MovementSerializer})
    def post(self, request):  # type: ignore[no-untyped-def]
        serializer = TransferSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        from stock.models import OwnerType

        owner_client = _lookup(data.get("owner_client"), "network.Client")
        movement = transfer_stock(
            organization=request.user.organization,
            item_type=_lookup(data["item_type"], "catalogue.ItemType"),
            quantity=Decimal(str(data["quantity"])),
            from_location=_lookup(data["from_location"], "locations.Location"),
            to_location=_lookup(data["to_location"], "locations.Location"),
            owner_type=OwnerType.CLIENT if owner_client else OwnerType.OWN,
            owner_client=owner_client,
            condition=data.get("condition") or Condition.NEW,
            serial_unit=_lookup(data.get("serial_unit"), "stock.SerialUnit"),
            reel=_lookup(data.get("reel"), "stock.Reel"),
            performed_by=request.user,
            note=data.get("note", ""),
            request=request,
        )
        return Response(MovementSerializer(movement).data, status=201)


# --------------------------------------------------------------------------
# Boxes (design §4.15.9; P4, P6, P7, P8)
# --------------------------------------------------------------------------


class BoxRowSerializer(serializers.ModelSerializer):
    """A box in a list: counts are annotated by ``annotated_boxes``, not per row."""

    parent_code = serializers.CharField(source="parent.code", read_only=True, default=None)
    node = serializers.IntegerField(source="current_node_id", read_only=True)
    node_label = serializers.CharField(source="current_node.label", read_only=True)
    # Plain strings, not choice fields: a second "status" or "action" enum would
    # make the schema generator rename the existing ones.
    status = serializers.CharField(read_only=True)
    source = serializers.CharField(read_only=True)
    units_now = serializers.IntegerField(read_only=True)
    bulk_lines_now = serializers.IntegerField(read_only=True)

    class Meta:
        model = Box
        fields = (
            "id",
            "code",
            "status",
            "source",
            "depth",
            "parent_code",
            "node",
            "node_label",
            "units_now",
            "bulk_lines_now",
            "created_at",
            "closed_at",
        )
        read_only_fields = fields


class BoxFilter(django_filters.FilterSet):
    node = django_filters.NumberFilter(field_name="current_node")
    location = django_filters.NumberFilter(field_name="current_node__location")
    gate_in = django_filters.NumberFilter(field_name="gate_in")
    status = django_filters.ChoiceFilter(choices=BoxStatus.choices)

    class Meta:
        model = Box
        fields = ["node", "location", "status", "gate_in"]


class BoxEventSerializer(serializers.ModelSerializer):
    action = serializers.CharField(read_only=True)
    action_label = serializers.CharField(source="get_action_display", read_only=True)
    box_code = serializers.CharField(source="box.code", read_only=True)
    actor = serializers.CharField(source="actor.full_name", read_only=True, default="")
    serial_number = serializers.CharField(
        source="serial_unit.serial_number", read_only=True, default=""
    )
    child_box_code = serializers.CharField(source="child_box.code", read_only=True, default="")
    item_name = serializers.CharField(source="item_type.name", read_only=True, default="")
    owner_client = serializers.CharField(source="owner_client.name", read_only=True, default="")

    class Meta:
        model = BoxEvent
        fields = (
            "id",
            "occurred_at",
            "action",
            "action_label",
            "box_code",
            "actor",
            "serial_number",
            "child_box_code",
            "item_name",
            "owner_client",
            "condition",
            "quantity",
            "document_type",
            "document_id",
            "document_number",
            "note",
        )
        read_only_fields = fields


class BoxEventPagination(CursorPagination):
    """Newest first; ``id`` breaks ties, because one movement writes several
    events with the same ``occurred_at``."""

    ordering = ("-occurred_at", "-id")
    page_size_query_param = "page_size"
    max_page_size = 200


class BoxBulkTakeSerializer(serializers.Serializer):
    item_type = serializers.IntegerField()
    owner_client = serializers.IntegerField(required=False, allow_null=True, default=None)
    condition = serializers.ChoiceField(choices=Condition.choices)
    quantity = serializers.DecimalField(max_digits=14, decimal_places=3)


class BoxTakeOutSerializer(serializers.Serializer):
    units = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    bulk = BoxBulkTakeSerializer(many=True, required=False, default=list)
    boxes = serializers.ListField(child=serializers.CharField(), required=False, default=list)


class BoxMoveSerializer(serializers.Serializer):
    to_location = serializers.IntegerField()


_BoxDetail = inline_serializer(
    "BoxDetail",
    {
        "id": serializers.IntegerField(),
        "code": serializers.CharField(),
        "status": serializers.CharField(),
        "source": serializers.CharField(),
        "depth": serializers.IntegerField(),
        "parent_code": serializers.CharField(allow_null=True),
        "path": serializers.ListField(child=serializers.CharField()),
        "node_id": serializers.IntegerField(),
        "node_label": serializers.CharField(),
        "gate_in": serializers.IntegerField(allow_null=True),
        "gate_in_number": serializers.CharField(allow_null=True),
        "created_at": serializers.DateTimeField(),
        "closed_at": serializers.DateTimeField(allow_null=True),
        "units": serializers.ListField(child=serializers.DictField()),
        "bulk": serializers.ListField(child=serializers.DictField()),
        "children": serializers.ListField(child=serializers.DictField()),
        "counts": serializers.DictField(),
    },
)


class CanChangeBoxes(BasePermission):
    """``stock.adjust`` or ``gate_in.post`` (§4.15.9): the storekeeper who
    received the box and the one who corrects stock may both reorganise it."""

    message = "You do not have permission to change boxes."

    def has_permission(self, request, view) -> bool:  # type: ignore[no-untyped-def]
        granted = resolve_permissions(request.user)
        return granted.has(PERM.STOCK_ADJUST) or granted.has(PERM.GATE_IN_POST)


def box_detail(box: Box) -> dict:
    """The tree (P4) plus where the box sits, how it got here and its path."""
    box = Box.objects.select_related("current_node", "parent", "gate_in").get(pk=box.pk)
    payload = box_tree(box)
    payload.update(
        {
            "source": box.source,
            "parent_code": box.parent.code if box.parent else None,
            "path": _box_path(box, {}),
            "node_id": box.current_node_id,
            "node_label": box.current_node.label,
            "gate_in": box.gate_in_id,
            "gate_in_number": box.gate_in.number if box.gate_in else None,
            "created_at": box.created_at,
            "closed_at": box.closed_at,
        }
    )
    return payload


class BoxViewSet(TenantScopedViewSet):
    """``/api/v1/boxes`` (P4, P7, P8).

    Addressed by **code**, case-insensitively: the code is what is printed on
    the carton in the storekeeper's hands. ``lookup_value_regex`` accepts
    anything but ``/`` (a supplier code or a GS1 SSCC is letters, digits and
    punctuation, and may contain ``.``); a code containing ``/`` cannot be put in
    a path, so it is found through ``/stock/lookup?q=`` instead. A trailing
    ``.json`` is therefore part of the code, not a format suffix.

    Reads need only a signed-in user, as ``/stock`` does. Changing a box needs
    ``stock.adjust`` or ``gate_in.post``.
    """

    serializer_class = BoxRowSerializer
    model = Box
    filterset_class = BoxFilter
    search_fields = ["code"]
    ordering_fields = ["code", "created_at"]
    lookup_field = "code"
    lookup_value_regex = "[^/]+"

    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):  # type: ignore[no-untyped-def]
        return annotated_boxes()

    def get_permissions(self):  # type: ignore[no-untyped-def]
        if getattr(self, "action", None) in ("take_out", "empty", "move"):
            return [IsAuthenticated(), OrganizationIsActive(), CanChangeBoxes()]
        return [IsAuthenticated(), OrganizationIsActive(), HasPermission()]

    def get_object(self):  # type: ignore[no-untyped-def]
        box = get_object_or_404(self.get_queryset(), code__iexact=self.kwargs["code"])
        self.check_object_permissions(self.request, box)
        return box

    @extend_schema(exclude=True)
    def create(self, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise MethodNotAllowed(request.method)

    @extend_schema(responses={200: _BoxDetail})
    def retrieve(self, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        return Response(box_detail(self.get_object()))

    @extend_schema(responses={200: BoxEventSerializer(many=True)})
    @action(
        detail=True,
        methods=["get"],
        url_path="history",
        pagination_class=BoxEventPagination,
    )
    def history(self, request, code=None):  # type: ignore[no-untyped-def]
        """Events of this box **and every box inside it**, newest first (P8).

        A pallet's history includes its cartons' events; each row says which box
        it happened to (``box_code``).
        """
        box = self.get_object()
        ids = [b.pk for b in _subtree(box)]
        events = BoxEvent.objects.filter(box_id__in=ids).select_related(
            "box", "actor", "serial_unit", "child_box", "item_type", "owner_client"
        )
        page = self.paginate_queryset(events)
        return self.get_paginated_response(BoxEventSerializer(page, many=True).data)

    @extend_schema(request=BoxTakeOutSerializer, responses={200: _BoxDetail})
    @action(detail=True, methods=["post"], url_path="take-out")
    def take_out(self, request, code=None):  # type: ignore[no-untyped-def]
        """P7: contents leave the box and stay where they are."""
        box = self.get_object()
        serializer = BoxTakeOutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        units = _all_or_400(SerialUnit.objects.filter(pk__in=data["units"]), data["units"], "units")
        child_codes = {c.strip().lower() for c in data["boxes"]}
        children = [b for b in Box.objects.filter(parent=box) if b.code.lower() in child_codes]
        if {c.code.lower() for c in children} != child_codes:
            raise serializers.ValidationError(
                {"boxes": ["A box named is not inside this box."]}
            )

        bulk = []
        for index, entry in enumerate(data["bulk"]):
            item = _one_or_400("catalogue.ItemType", entry["item_type"], f"bulk.{index}.item_type")
            client = (
                _one_or_400("network.Client", entry["owner_client"], f"bulk.{index}.owner_client")
                if entry["owner_client"]
                else None
            )
            bulk.append(
                {
                    "item_type": item,
                    "owner_client": client,
                    "condition": entry["condition"],
                    "quantity": entry["quantity"],
                }
            )

        take_out(
            box,
            units=units,
            bulk=bulk,
            boxes=children,
            ctx=EventContext(actor=request.user),
        )
        return Response(box_detail(box))

    @extend_schema(request=None, responses={200: _BoxDetail})
    @action(detail=True, methods=["post"], url_path="empty")
    def empty(self, request, code=None):  # type: ignore[no-untyped-def]
        """P7: everything out, which closes the box."""
        box = self.get_object()
        empty_box(box, ctx=EventContext(actor=request.user))
        return Response(box_detail(box))

    @extend_schema(request=BoxMoveSerializer, responses={200: _BoxDetail})
    @action(detail=True, methods=["post"], url_path="move")
    def move(self, request, code=None):  # type: ignore[no-untyped-def]
        """P7, E4: the box and everything in it to another place in the yard."""
        box = self.get_object()
        serializer = BoxMoveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        location = _one_or_400(
            "locations.Location", serializer.validated_data["to_location"], "to_location"
        )
        move_box(box, location, actor=request.user, request=request)
        return Response(box_detail(box))


def _one_or_400(label, pk, field):  # type: ignore[no-untyped-def]
    """An id inside the tenant, or a 400 naming the field (a body is not a path)."""
    from django.apps import apps

    obj = apps.get_model(label).objects.filter(pk=pk).first()
    if obj is None:
        raise serializers.ValidationError({field: ["Not found."]})
    return obj


def _all_or_400(queryset, ids, field):  # type: ignore[no-untyped-def]
    found = list(queryset)
    if {obj.pk for obj in found} != set(ids):
        raise serializers.ValidationError({field: ["One of these is not found."]})
    return found


class BoxIssuableView(APIView):
    """``/api/v1/stock/boxes/{code}/issuable?from_location=`` (P6, P9, P10).

    What a gate-out for this box would carry from that place, and what could not
    go and why.
    """

    permission_classes = [IsAuthenticated, OrganizationIsActive, HasPermission]
    required_permissions = {"get": PERM.GATE_OUT_REQUEST}

    @extend_schema(
        parameters=[OpenApiParameter("from_location", required=True, type=int)],
        responses={
            200: inline_serializer(
                "BoxIssuable",
                {
                    "lines": serializers.ListField(child=serializers.DictField()),
                    "excluded": serializers.ListField(child=serializers.DictField()),
                },
            )
        },
    )
    def get(self, request, code: str):  # type: ignore[no-untyped-def]
        from locations.models import Location
        from locations.nodes import node_for_location

        box = get_object_or_404(Box.objects.all(), code__iexact=code)
        raw = request.query_params.get("from_location")
        location = None
        if raw and raw.isdigit():
            location = Location.objects.filter(pk=int(raw)).first()
        if location is None:
            raise serializers.ValidationError(
                {"from_location": ["Name a location of yours to issue from."]}
            )
        return Response(issuable_contents(box, from_node=node_for_location(location)))


class StockCountLineSerializer(serializers.Serializer):
    item_type = serializers.IntegerField()
    counted_quantity = serializers.DecimalField(max_digits=14, decimal_places=3)
    owner_client = serializers.IntegerField(required=False, allow_null=True)
    condition = serializers.CharField(required=False, default=Condition.NEW)
    reason = serializers.CharField(required=False, allow_blank=True, default="")


class StockCountSerializer(serializers.ModelSerializer):
    location_name = serializers.CharField(source="location.name", read_only=True)
    lines = serializers.SerializerMethodField()

    class Meta:
        model = StockCount
        fields = (
            "id",
            "number",
            "status",
            "location",
            "location_name",
            "counted_at",
            "counted_by",
            "posted_at",
            "posted_by",
            "notes",
            "lines",
            "created_at",
        )
        read_only_fields = ("number", "status", "posted_at", "posted_by")

    def get_lines(self, count) -> list[dict]:
        """Each line carries what the system expected when it was counted.

        E5: the variance is the point of a count, and recomputing "expected" at
        display time would show a difference against today's figure rather than
        against the one being disputed.
        """
        return [
            {
                "id": line.pk,
                "item_type": line.item_type_id,
                "item_name": line.item_type.name,
                "owner_client": line.owner_client_id,
                "owner_client_name": line.owner_client.name if line.owner_client_id else "",
                "condition": line.condition,
                "expected_quantity": str(line.expected_quantity),
                "counted_quantity": str(line.counted_quantity),
                "variance": str(line.variance),
                "uom": line.uom,
                "reason": line.reason,
            }
            for line in count.lines.select_related("item_type", "owner_client").all()
        ]


class StockCountViewSet(TenantScopedViewSet):
    """``/api/v1/stock-counts`` (E5)."""

    serializer_class = StockCountSerializer
    model = StockCount
    select_related = ("location", "counted_by", "posted_by")
    prefetch_related = ("lines", "lines__item_type", "lines__owner_client")
    filterset_fields = ["status", "location"]
    ordering_fields = ["created_at", "counted_at"]

    required_permissions = {
        "create": PERM.STOCK_ADJUST,
        "update": PERM.STOCK_ADJUST,
        "partial_update": PERM.STOCK_ADJUST,
        "add_line": PERM.STOCK_ADJUST,
        "post_document": PERM.STOCK_ADJUST,
    }

    http_method_names = ["get", "post", "patch", "head", "options"]

    def perform_create(self, serializer):  # type: ignore[no-untyped-def]
        serializer.save(created_by=self.request.user, counted_by=self.request.user)

    @extend_schema(request=StockCountLineSerializer, responses={201: StockCountSerializer})
    @action(detail=True, methods=["post"], url_path="lines")
    def add_line(self, request, pk=None):  # type: ignore[no-untyped-def]
        """E5: the expected figure is captured now, not at posting time."""
        count = self.get_object()
        if count.status != StockCountStatus.DRAFT:
            raise serializers.ValidationError(
                {"status": ["This count has been posted and cannot take new lines."]}
            )

        serializer = StockCountLineSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        add_count_line(
            count,
            _lookup(data["item_type"], "catalogue.ItemType"),
            Decimal(str(data["counted_quantity"])),
            owner_client=_lookup(data.get("owner_client"), "network.Client"),
            condition=data.get("condition") or Condition.NEW,
            reason=data.get("reason", ""),
        )
        count.refresh_from_db()
        return Response(self.get_serializer(count).data, status=201)

    @extend_schema(request=None, responses={200: StockCountSerializer})
    @action(detail=True, methods=["post"], url_path="post")
    def post_document(self, request, pk=None):  # type: ignore[no-untyped-def]
        """E5: posts an adjustment per variance, each with its reason.

        Client-owned variances always need an approval, whatever the rules say —
        adjusting somebody else's stock on our own say-so is the thing an operator
        audit is looking for.
        """
        count = post_stock_count(self.get_object(), posted_by=request.user, request=request)
        return Response(self.get_serializer(count).data)


def _stringify(row: dict) -> dict:
    """Decimals as strings, so no client loses precision to a float."""
    return {
        key: (str(value) if isinstance(value, Decimal) else value)
        for key, value in row.items()
    }
