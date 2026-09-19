"""Disposition, disposal and client-return endpoints (design §6, §4.11, §4.12).

§6's convention throughout: state changes are POST sub-resource actions, never a
PATCH on status. So a disposition is submitted, approved, rejected and posted;
nothing sets ``status`` directly, which is what makes "who authorised this
write-off?" answerable.

The one endpoint worth reading twice is the quarantine list. J2 exists because
quarantine becomes a graveyard — material sits there because nobody is looking at
it — so the API answers "what is in quarantine and how long has it been there"
rather than making a screen assemble that from balances and movements.
"""

from __future__ import annotations

from decimal import Decimal

from django.db.models import Q
from django.http import Http404, HttpResponse
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
    inline_serializer,
)
from rest_framework import serializers, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions_registry import PERM
from core.api import TenantScopedViewSet
from core.api_permissions import OrganizationIsActive
from disposition.models import (
    ClientReturnAck,
    Disposal,
    DisposalLine,
    Disposition,
    DispositionLine,
)
from disposition.services import (
    acknowledge_client_return,
    approve_disposal,
    approve_disposition,
    post_disposal,
    post_disposition,
    reject_disposal,
    reject_disposition,
    submit_disposal,
    submit_disposition,
)


class DispositionLineSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="item_type.name", read_only=True)
    serial_number = serializers.CharField(
        source="serial_unit.serial_number", read_only=True, default=""
    )
    drum_number = serializers.CharField(source="reel.drum_number", read_only=True, default="")
    owner_client_name = serializers.CharField(
        source="owner_client.name", read_only=True, default=""
    )

    class Meta:
        model = DispositionLine
        fields = (
            "id",
            "line_number",
            "item_type",
            "item_name",
            "serial_unit",
            "serial_number",
            "reel",
            "drum_number",
            "quantity",
            "uom",
            "condition",
            "to_condition",
            "owner_type",
            "owner_client",
            "owner_client_name",
            "notes",
        )


class DispositionSerializer(serializers.ModelSerializer):
    lines = DispositionLineSerializer(many=True)
    from_location_name = serializers.CharField(source="from_location.name", read_only=True)
    to_location_name = serializers.CharField(
        source="to_location.name", read_only=True, default=""
    )
    involves_client_owned_material = serializers.BooleanField(read_only=True)
    pending_approval = serializers.SerializerMethodField()

    class Meta:
        model = Disposition
        fields = (
            "id",
            "number",
            "status",
            "decision",
            "reason",
            "from_location",
            "from_location_name",
            "to_location",
            "to_location_name",
            "vendor_name",
            "expected_return_date",
            "decided_by",
            "posted_at",
            "posted_by",
            "disposal",
            "reject_reason",
            "cancel_reason",
            "notes",
            "client_uuid",
            "lines",
            "involves_client_owned_material",
            "pending_approval",
            "created_at",
        )
        # §6: status moves only through the actions below.
        read_only_fields = (
            "number",
            "status",
            "decided_by",
            "posted_at",
            "posted_by",
            "disposal",
            "reject_reason",
            "cancel_reason",
        )

    def get_pending_approval(self, disposition) -> dict | None:
        """What is being waited on, so a screen can say who to chase (F3)."""
        from approvals.engine import next_pending_request

        pending = next_pending_request(disposition)
        if pending is None:
            return None
        return {
            "id": pending.pk,
            "level": pending.level,
            "role": (
                pending.required_role.name if pending.required_role is not None else None
            ),
            "status": pending.status,
            "due_at": pending.due_at,
        }

    def create(self, validated_data):  # type: ignore[no-untyped-def]
        lines = validated_data.pop("lines")
        disposition = Disposition.objects.create(**validated_data)
        for index, line in enumerate(lines, start=1):
            line.setdefault("line_number", index)
            DispositionLine.objects.create(
                organization_id=disposition.organization_id,
                disposition=disposition,
                **line,
            )
        return disposition

    def update(self, instance, validated_data):  # type: ignore[no-untyped-def]
        lines = validated_data.pop("lines", None)
        disposition = super().update(instance, validated_data)
        if lines is not None:
            from disposition.models import DispositionStatus

            if disposition.status != DispositionStatus.DRAFT:
                raise serializers.ValidationError(
                    {
                        "lines": [
                            "This disposition has been submitted. Its lines are "
                            "what was approved, so they cannot be swapped now."
                        ]
                    }
                )
            disposition.lines.all().delete()
            for index, line in enumerate(lines, start=1):
                line.setdefault("line_number", index)
                DispositionLine.objects.create(
                    organization_id=disposition.organization_id,
                    disposition=disposition,
                    **line,
                )
        return disposition


class DisposalLineSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="item_type.name", read_only=True)
    serial_number = serializers.CharField(
        source="serial_unit.serial_number", read_only=True, default=""
    )
    drum_number = serializers.CharField(source="reel.drum_number", read_only=True, default="")
    owner_client_name = serializers.CharField(
        source="owner_client.name", read_only=True, default=""
    )

    class Meta:
        model = DisposalLine
        fields = (
            "id",
            "line_number",
            "item_type",
            "item_name",
            "serial_unit",
            "serial_number",
            "reel",
            "drum_number",
            "quantity",
            "uom",
            "condition",
            "owner_type",
            "owner_client",
            "owner_client_name",
            "written_off_value",
            "notes",
        )


class DisposalSerializer(serializers.ModelSerializer):
    lines = DisposalLineSerializer(many=True)
    from_location_name = serializers.CharField(source="from_location.name", read_only=True)
    involves_client_owned_material = serializers.BooleanField(read_only=True)
    pending_approval = serializers.SerializerMethodField()
    written_off_total = serializers.SerializerMethodField()

    class Meta:
        model = Disposal
        fields = (
            "id",
            "number",
            "status",
            "method",
            "handler_name",
            "handler_reference",
            "from_location",
            "from_location_name",
            "project",
            "requested_by",
            "submitted_at",
            "approved_at",
            "disposed_at",
            "disposed_by",
            "reject_reason",
            "cancel_reason",
            "notes",
            "client_uuid",
            "lines",
            "involves_client_owned_material",
            "pending_approval",
            "written_off_total",
            "created_at",
        )
        read_only_fields = (
            "number",
            "status",
            # A client naming somebody else as the requester could raise a
            # write-off in their name and then approve it (§5.3).
            "requested_by",
            "submitted_at",
            "approved_at",
            "disposed_at",
            "disposed_by",
            "reject_reason",
            "cancel_reason",
        )

    def get_pending_approval(self, disposal) -> dict | None:
        from approvals.engine import next_pending_request

        pending = next_pending_request(disposal)
        if pending is None:
            return None
        return {
            "id": pending.pk,
            "level": pending.level,
            "role": (
                pending.required_role.name if pending.required_role is not None else None
            ),
            "status": pending.status,
            "due_at": pending.due_at,
        }

    def get_written_off_total(self, disposal) -> str | None:
        """``None`` when the tenant does not track money (C8)."""
        from disposition.services import written_off_value

        total = written_off_value(disposal)
        return str(total) if total is not None else None

    def create(self, validated_data):  # type: ignore[no-untyped-def]
        lines = validated_data.pop("lines")
        disposal = Disposal.objects.create(**validated_data)
        for index, line in enumerate(lines, start=1):
            line.setdefault("line_number", index)
            DisposalLine.objects.create(
                organization_id=disposal.organization_id, disposal=disposal, **line
            )
        return disposal


class DecisionReasonSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True)


class DecisionRejectSerializer(serializers.Serializer):
    """F4: a rejection has to say why, which is what lets the requester fix it."""

    reason = serializers.CharField(max_length=500)


class DispositionViewSet(TenantScopedViewSet):
    """``/api/v1/dispositions`` (J2, §4.11)."""

    serializer_class = DispositionSerializer
    model = Disposition
    select_related = ("from_location", "to_location", "decided_by", "posted_by")
    prefetch_related = ("lines", "lines__item_type", "lines__owner_client")
    filterset_fields = ["status", "decision", "from_location"]
    search_fields = ["number", "reason", "vendor_name"]
    ordering_fields = ["created_at", "number", "status"]

    required_permissions = {
        "create": PERM.STOCK_ADJUST,
        "update": PERM.STOCK_ADJUST,
        "partial_update": PERM.STOCK_ADJUST,
        "submit": PERM.STOCK_ADJUST,
        "post_document": PERM.STOCK_ADJUST,
        # J2, §5.2: deciding is a different job from proposing.
        "approve": PERM.GATE_OUT_APPROVE,
        "reject": PERM.GATE_OUT_APPROVE,
    }

    # A disposition is cancelled or rejected, never deleted (M6).
    http_method_names = ["get", "post", "patch", "head", "options"]

    @extend_schema(request=None, responses={200: DispositionSerializer})
    @action(detail=True, methods=["post"])
    def submit(self, request, pk=None):  # type: ignore[no-untyped-def]
        disposition = submit_disposition(
            self.get_object(), submitted_by=request.user, request=request
        )
        return Response(self.get_serializer(disposition).data)

    @extend_schema(request=DecisionReasonSerializer, responses={200: DispositionSerializer})
    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):  # type: ignore[no-untyped-def]
        serializer = DecisionReasonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        disposition = approve_disposition(
            self.get_object(),
            actor=request.user,
            reason=serializer.validated_data.get("reason", ""),
            request=request,
        )
        return Response(self.get_serializer(disposition).data)

    @extend_schema(request=DecisionRejectSerializer, responses={200: DispositionSerializer})
    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):  # type: ignore[no-untyped-def]
        serializer = DecisionRejectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        disposition = reject_disposition(
            self.get_object(),
            actor=request.user,
            reason=serializer.validated_data["reason"],
            request=request,
        )
        return Response(self.get_serializer(disposition).data)

    @extend_schema(request=None, responses={200: DispositionSerializer})
    @action(detail=True, methods=["post"], url_path="post")
    def post_document(self, request, pk=None):  # type: ignore[no-untyped-def]
        """Move the material where the decision says (J2).

        Named ``post`` in the URL and ``post_document`` in Python, because a
        method called ``post`` on a viewset would shadow the HTTP verb.
        """
        disposition = post_disposition(
            self.get_object(), posted_by=request.user, request=request
        )
        return Response(self.get_serializer(disposition).data)


class DisposalViewSet(TenantScopedViewSet):
    """``/api/v1/disposals`` (J3, §4.11)."""

    serializer_class = DisposalSerializer
    model = Disposal
    select_related = ("from_location", "requested_by", "disposed_by")
    prefetch_related = ("lines", "lines__item_type", "lines__owner_client")
    filterset_fields = ["status", "method", "from_location"]
    search_fields = ["number", "handler_name", "handler_reference", "notes"]
    ordering_fields = ["created_at", "number", "status"]

    required_permissions = {
        "create": PERM.STOCK_ADJUST,
        "update": PERM.STOCK_ADJUST,
        "partial_update": PERM.STOCK_ADJUST,
        "submit": PERM.STOCK_ADJUST,
        "post_document": PERM.STOCK_ADJUST,
        # J3: disposal has its own approval permission, because signing off a
        # write-off is not the same authority as approving a gate pass.
        "approve": PERM.DISPOSAL_APPROVE,
        "reject": PERM.DISPOSAL_APPROVE,
    }

    http_method_names = ["get", "post", "patch", "head", "options"]

    def perform_create(self, serializer):  # type: ignore[no-untyped-def]
        serializer.save(created_by=self.request.user, requested_by=self.request.user)

    @extend_schema(request=None, responses={200: DisposalSerializer})
    @action(detail=True, methods=["post"])
    def submit(self, request, pk=None):  # type: ignore[no-untyped-def]
        disposal = submit_disposal(
            self.get_object(), submitted_by=request.user, request=request
        )
        return Response(self.get_serializer(disposal).data)

    @extend_schema(request=DecisionReasonSerializer, responses={200: DisposalSerializer})
    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):  # type: ignore[no-untyped-def]
        serializer = DecisionReasonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        disposal = approve_disposal(
            self.get_object(),
            actor=request.user,
            reason=serializer.validated_data.get("reason", ""),
            request=request,
        )
        return Response(self.get_serializer(disposal).data)

    @extend_schema(request=DecisionRejectSerializer, responses={200: DisposalSerializer})
    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):  # type: ignore[no-untyped-def]
        serializer = DecisionRejectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        disposal = reject_disposal(
            self.get_object(),
            actor=request.user,
            reason=serializer.validated_data["reason"],
            request=request,
        )
        return Response(self.get_serializer(disposal).data)

    @extend_schema(request=None, responses={200: DisposalSerializer})
    @action(detail=True, methods=["post"], url_path="post")
    def post_document(self, request, pk=None):  # type: ignore[no-untyped-def]
        disposal = post_disposal(
            self.get_object(), posted_by=request.user, request=request
        )
        return Response(self.get_serializer(disposal).data)

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "output",
                description="pdf (default) or html, where PDF rendering is unavailable.",
                required=False,
            )
        ],
        responses={
            200: OpenApiResponse(
                description=(
                    "The certificate, as a PDF — or as HTML where the host "
                    "cannot render PDFs (§11)."
                )
            )
        },
    )
    @action(detail=True, methods=["get"])
    def certificate(self, request, pk=None):  # type: ignore[no-untyped-def]
        """The certificate of disposal (J3, §11).

        Available before completion too, as a draft — somebody taking material to
        a licensed handler needs the paperwork in hand when the lorry arrives.
        """
        from dispatch.documents import render_disposal_certificate

        # ``output`` rather than ``format``: DRF reserves ``format`` for content
        # negotiation, so ``?format=html`` never reached this code — it 404'd
        # looking for a renderer of that name. The HTML fallback exists for hosts
        # without WeasyPrint, and a fallback that 404s is worse than none.
        wants_pdf = request.query_params.get("output", "pdf") != "html"
        content, content_type, filename = render_disposal_certificate(
            self.get_object(), as_pdf=wants_pdf
        )
        response = HttpResponse(content, content_type=content_type)
        response["Content-Disposition"] = f'inline; filename="{filename}"'
        return response


class ClientReturnAckSerializer(serializers.ModelSerializer):
    # Optional, because the common case is recording a signature as it happens
    # and the service stamps *now*. Required here would have made the ordinary
    # path a 400 while backdating — the rarer case — worked fine.
    acknowledged_at = serializers.DateTimeField(required=False)
    gate_out_number = serializers.CharField(source="gate_out.number", read_only=True)
    client_name = serializers.CharField(
        source="gate_out.client.name", read_only=True, default=""
    )
    recorded_by_name = serializers.CharField(
        source="recorded_by.full_name", read_only=True, default=""
    )

    class Meta:
        model = ClientReturnAck
        fields = (
            "id",
            "gate_out",
            "gate_out_number",
            "client_name",
            "acknowledged_ref",
            "acknowledged_at",
            "acknowledged_by_name",
            "recorded_by",
            "recorded_by_name",
            "notes",
            "created_at",
        )
        read_only_fields = ("recorded_by",)


class ClientReturnAckViewSet(TenantScopedViewSet):
    """``/api/v1/client-return-acks`` (K3, §4.12).

    Created through the service rather than by the serializer, because recording
    one ends liability on the record and the checks that guard that — released,
    a return at all, not already acknowledged — belong with the domain rather
    than with a form.
    """

    serializer_class = ClientReturnAckSerializer
    model = ClientReturnAck
    select_related = ("gate_out", "gate_out__client", "recorded_by")
    filterset_fields = ["gate_out"]
    search_fields = ["acknowledged_ref", "acknowledged_by_name"]
    ordering_fields = ["acknowledged_at", "created_at"]

    required_permissions = {"create": PERM.GATE_OUT_RELEASE}

    # An acknowledgement is a record of something that happened. Correcting one
    # is a new record with a note, not an edit (M6).
    http_method_names = ["get", "post", "head", "options"]

    def create(self, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        from dispatch.models import GateOut

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        gate_out = GateOut.objects.filter(
            pk=serializer.validated_data["gate_out"].pk
        ).first()
        if gate_out is None:
            raise Http404()

        ack = acknowledge_client_return(
            gate_out,
            acknowledged_ref=serializer.validated_data["acknowledged_ref"],
            acknowledged_at=serializer.validated_data.get("acknowledged_at"),
            acknowledged_by_name=serializer.validated_data.get(
                "acknowledged_by_name", ""
            ),
            notes=serializer.validated_data.get("notes", ""),
            recorded_by=request.user,
            request=request,
        )
        return Response(
            self.get_serializer(ack).data, status=status.HTTP_201_CREATED
        )


class QuarantineView(APIView):
    """``/api/v1/quarantine`` — what is in quarantine, and for how long (J1, J2).

    The age is the point. J2 exists because quarantine becomes a graveyard —
    material sits there because nobody is looking at it — so a list without "how
    long has this been here" would reproduce the problem it is meant to solve.
    """

    permission_classes = [IsAuthenticated, OrganizationIsActive]

    @extend_schema(
        parameters=[
            OpenApiParameter("location", required=False),
            OpenApiParameter("client", required=False),
            OpenApiParameter("search", required=False),
        ],
        responses={
            200: inline_serializer(
                "QuarantineList",
                {
                    "count": serializers.IntegerField(),
                    "items": serializers.ListField(child=serializers.DictField()),
                },
            )
        },
    )
    def get(self, request):  # type: ignore[no-untyped-def]
        from django.db.models import Max
        from django.utils import timezone

        from locations.models import LocationType, NodeType
        from stock.models import StockBalance, StockMovement

        balances = (
            StockBalance.objects.filter(
                node__type=NodeType.LOCATION,
                node__location__type=LocationType.QUARANTINE,
            )
            .nonzero()
            .select_related("node", "node__location", "item_type", "owner_client")
        )
        location_id = request.query_params.get("location")
        if location_id:
            balances = balances.filter(node__location_id=location_id)
        client_id = request.query_params.get("client")
        if client_id:
            balances = balances.filter(owner_client_id=client_id)

        # J2: a register nobody can find anything in becomes the graveyard the
        # requirement exists to prevent. Searched in the query rather than over
        # the rendered rows, so paging and counting stay honest.
        search = (request.query_params.get("search") or "").strip()
        if search:
            balances = balances.filter(
                Q(item_type__name__icontains=search)
                | Q(item_type__code__icontains=search)
                | Q(owner_client__name__icontains=search)
                | Q(node__location__name__icontains=search)
            )

        # When each item arrived in this quarantine, in one query rather than one
        # per row — the list is read on a phone, and N queries would show.
        arrivals = {
            (row["to_node"], row["item_type"], row["condition"]): row["latest"]
            for row in StockMovement.objects.filter(
                to_node__type=NodeType.LOCATION,
                to_node__location__type=LocationType.QUARANTINE,
            )
            .values("to_node", "item_type", "condition")
            .annotate(latest=Max("occurred_at"))
        }

        now = timezone.now()
        items = []
        for balance in balances:
            arrived = arrivals.get(
                (balance.node_id, balance.item_type_id, balance.condition)
            )
            items.append(
                {
                    "balance_id": balance.pk,
                    "location_id": balance.node.location_id,
                    "location": balance.node.location.name,
                    "item_type_id": balance.item_type_id,
                    "item_type": str(balance.item_type),
                    "tracking_mode": balance.item_type.default_tracking_mode,
                    "quantity": str(balance.quantity),
                    "uom": balance.uom,
                    "condition": balance.condition,
                    "owner_client_id": balance.owner_client_id,
                    "owner_client": (
                        str(balance.owner_client) if balance.owner_client_id else ""
                    ),
                    "since": arrived.isoformat() if arrived else None,
                    "days_in_quarantine": (now - arrived).days if arrived else None,
                }
            )

        # Oldest first: the thing that has been rotting longest is the thing
        # somebody has to decide about.
        items.sort(key=lambda entry: entry["days_in_quarantine"] or 0, reverse=True)
        return Response({"count": len(items), "items": items})


class ClientPositionView(APIView):
    """``/api/v1/client-position`` — held, in transit, acknowledged (K1, K3, M1).

    The report an operator audit opens with, and the reason the three states are
    separate: "we are holding 40 of yours, 12 are on their way back to you, and
    you signed for 8 last week" is a defensible answer. One number is not.
    """

    permission_classes = [IsAuthenticated, OrganizationIsActive]

    @extend_schema(
        parameters=[OpenApiParameter("client", required=False)],
        responses={
            200: inline_serializer(
                "ClientPosition",
                {
                    "rows": serializers.ListField(child=serializers.DictField()),
                    "exposure": serializers.CharField(),
                },
            )
        },
    )
    def get(self, request):  # type: ignore[no-untyped-def]
        from disposition.queries import client_position
        from network.models import Client

        client = None
        client_id = request.query_params.get("client")
        if client_id:
            client = Client.objects.filter(pk=client_id).first()
            if client is None:
                # Another tenant's id is a 404, as everywhere (A3).
                raise Http404()

        rows = client_position(client)
        exposure = sum(
            (
                Decimal(row["quantity"])
                for row in rows
                if row["state"] != "ACKNOWLEDGED"
            ),
            Decimal("0"),
        )
        return Response({"rows": rows, "exposure": str(exposure)})
