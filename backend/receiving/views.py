"""Receiving endpoints (design §6, §4.6; D1–D8, D11, J1, M4).

§6's convention: a gate-in is created as a draft and **posted** by an explicit
action. Nothing about posting is a field the client can set — `status`, `number`,
`posted_at` are all read-only, because posting is what moves stock (D8) and it
has to run the validation in one place.

Lines, serials and drums are writable inline. A ten-line mixed delivery entered
on a phone (T3.19) has to arrive as one document: half a delivery posted because
the connection dropped between lines is worse than no delivery at all.
"""

from __future__ import annotations

from django.db import transaction
from django.http import HttpResponse
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.response import Response

from accounts.permissions_registry import PERM
from core.api import TenantScopedViewSet
from core.idempotency import already_created
from receiving.models import (
    DocumentStatus,
    GateIn,
    GateInLine,
    GateInReel,
    GateInSerial,
)
from receiving.services import post_gate_in, void_gate_in


class GateInSerialSerializer(serializers.ModelSerializer):
    class Meta:
        model = GateInSerial
        fields = ("id", "serial_number", "asset_tag", "source")


class GateInReelSerializer(serializers.ModelSerializer):
    class Meta:
        model = GateInReel
        fields = ("id", "drum_number", "length")


class GateInLineSerializer(serializers.ModelSerializer):
    serials = GateInSerialSerializer(many=True, required=False)
    reels = GateInReelSerializer(many=True, required=False)
    item_name = serializers.CharField(source="item_type.name", read_only=True)
    is_unserviceable = serializers.BooleanField(read_only=True)
    # "Client owned" on its own raises the question it is meant to answer.
    owner_client_name = serializers.CharField(
        source="owner_client.name", read_only=True, default=""
    )

    class Meta:
        model = GateInLine
        fields = (
            "id",
            "line_number",
            "item_type",
            "item_name",
            "tracking_mode",
            "quantity",
            "uom",
            "condition",
            "is_unserviceable",
            "owner_type",
            "owner_client",
            "owner_client_name",
            "custom_field_values",
            "no_serial_reason",
            "notes",
            "serials",
            "reels",
        )


class GateInSerializer(serializers.ModelSerializer):
    lines = GateInLineSerializer(many=True, required=False)
    to_location_name = serializers.CharField(source="to_location.name", read_only=True)
    client_name = serializers.CharField(source="client.name", read_only=True, default="")
    returned_by_name = serializers.CharField(
        source="returned_by.full_name", read_only=True, default=""
    )
    origin_site_ref = serializers.CharField(
        source="origin_site.internal_ref", read_only=True, default=""
    )

    class Meta:
        model = GateIn
        fields = (
            "id",
            "number",
            "status",
            "source_type",
            "supplier_name",
            "client",
            "client_name",
            "returned_by",
            "returned_by_name",
            "origin_site",
            "origin_site_ref",
            "to_location",
            "to_location_name",
            "received_at",
            "posted_at",
            "posted_by",
            "client_delivery_note_ref",
            "client_uuid",
            "void_reason",
            "voided_at",
            "notes",
            "lines",
            "created_at",
        )
        # D8: posting is what affects stock, and it is an action with its own
        # permission. None of its record is writable.
        read_only_fields = (
            "number",
            "status",
            "posted_at",
            "posted_by",
            "void_reason",
            "voided_at",
        )

    @transaction.atomic
    def create(self, validated_data):  # type: ignore[no-untyped-def]
        # A second press of the same button, or a retry of a request whose
        # response was lost, must not become a second document (N2).
        existing = already_created(GateIn, validated_data)
        if existing is not None:
            return existing

        lines = validated_data.pop("lines", [])
        gate_in = GateIn.objects.create(**validated_data)
        self._write_lines(gate_in, lines)
        return gate_in

    @transaction.atomic
    def update(self, instance, validated_data):  # type: ignore[no-untyped-def]
        lines = validated_data.pop("lines", None)
        gate_in = super().update(instance, validated_data)
        if lines is not None:
            # A draft's lines are replaced wholesale. Diffing them would mean
            # matching client-side ids that an offline device may not have yet
            # (N-1), and a draft has posted nothing, so replacing costs nothing.
            gate_in.lines.all().delete()
            self._write_lines(gate_in, lines)
        return gate_in

    def _write_lines(self, gate_in, lines) -> None:
        for index, line_data in enumerate(lines, start=1):
            serials = line_data.pop("serials", [])
            reels = line_data.pop("reels", [])
            line_data.setdefault("line_number", index)
            line = GateInLine.objects.create(
                organization_id=gate_in.organization_id, gate_in=gate_in, **line_data
            )
            for serial in serials:
                GateInSerial.objects.create(
                    organization_id=gate_in.organization_id, line=line, **serial
                )
            for reel in reels:
                GateInReel.objects.create(
                    organization_id=gate_in.organization_id, line=line, **reel
                )


class VoidReasonSerializer(serializers.Serializer):
    """Named for this endpoint: the schema keys components by class name, and two
    different ``ReasonSerializer`` classes would collide into one wrong shape."""

    reason = serializers.CharField(max_length=500)


class GateInViewSet(TenantScopedViewSet):
    """``/api/v1/gate-ins`` (D1–D8, M4)."""

    serializer_class = GateInSerializer
    model = GateIn
    select_related = (
        "to_location",
        "client",
        "returned_by",
        "origin_site",
        "posted_by",
    )
    prefetch_related = (
        "lines",
        "lines__item_type",
        "lines__serials",
        "lines__reels",
    )
    filterset_fields = ["status", "source_type", "client", "to_location", "origin_site"]
    search_fields = [
        "number",
        "supplier_name",
        "client_delivery_note_ref",
        "notes",
    ]
    ordering_fields = ["created_at", "received_at", "number"]

    required_permissions = {
        "post_document": PERM.GATE_IN_POST,
        "void": PERM.GATE_IN_POST,
        "destroy": PERM.GATE_IN_POST,
    }

    # M6: a *posted* gate-in is voided, never deleted — its number stays used,
    # so the sequence cannot have a hole in it (D15). A draft is a different
    # thing: it holds no number and has moved no stock, and leaving one in the
    # list for ever because it was started by mistake is how a list stops being
    # read. `perform_destroy` keeps the two apart.
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def perform_update(self, serializer):  # type: ignore[no-untyped-def]
        """Only a draft can be edited.

        D8 makes posting the moment stock changes. Editing a posted document
        afterwards would change what the ledger says was received without
        changing the ledger.
        """
        if serializer.instance.status != DocumentStatus.DRAFT:
            raise serializers.ValidationError(
                {
                    "status": [
                        "This gate-in has been posted. Void it and receive again if it was wrong."
                    ]
                }
            )
        serializer.save()

    def perform_destroy(self, instance):  # type: ignore[no-untyped-def]
        """Only a draft can be discarded.

        Anything posted is in the ledger and in the numbered sequence, and is
        voided instead — a delete there would leave the stock it created with
        nothing explaining where it came from.
        """
        if instance.status != DocumentStatus.DRAFT:
            raise serializers.ValidationError(
                {
                    "status": [
                        "This gate-in has been posted, so it cannot be deleted. "
                        "Void it instead — the movements are reversed and the "
                        "record of what happened stays."
                    ]
                }
            )
        instance.delete()

    @extend_schema(request=None, responses={200: GateInSerializer})
    @action(detail=True, methods=["post"], url_path="post")
    def post_document(self, request, pk=None):  # type: ignore[no-untyped-def]
        """D1–D4, J1: turn a draft into stock, in one transaction."""
        gate_in = post_gate_in(self.get_object(), posted_by=request.user, request=request)
        return Response(self.get_serializer(gate_in).data)

    @extend_schema(request=VoidReasonSerializer, responses={200: GateInSerializer})
    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):  # type: ignore[no-untyped-def]
        """M4: reversed rather than deleted, and the reason is required."""
        serializer = VoidReasonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        gate_in = void_gate_in(
            self.get_object(),
            reason=serializer.validated_data["reason"],
            voided_by=request.user,
            request=request,
        )
        return Response(self.get_serializer(gate_in).data)

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "output",
                description="pdf (default) or html where PDF rendering is unavailable.",
                required=False,
            )
        ],
        responses={
            200: OpenApiResponse(
                description=(
                    "The document, as a PDF — or as HTML where the host cannot render PDFs (§11)."
                )
            )
        },
    )
    @action(detail=True, methods=["get"])
    def grn(self, request, pk=None):  # type: ignore[no-untyped-def]
        """The goods received note (D1, A4, §11).

        T4.19 built the template and the renderer; nothing served them. A GRN
        that exists only as a Python function is a GRN the storekeeper replacing
        their paper book cannot print — and replacing that book is what M3 is.
        """
        from dispatch.documents import render_grn

        wants_pdf = request.query_params.get("output", "pdf") != "html"
        content, content_type, filename = render_grn(self.get_object(), as_pdf=wants_pdf)
        response = HttpResponse(content, content_type=content_type)
        response["Content-Disposition"] = f'inline; filename="{filename}"'
        return response
