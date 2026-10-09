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
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
    extend_schema_field,
)
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.response import Response

from accounts.permissions_registry import PERM
from core.api import TenantScopedViewSet
from core.exceptions import DomainError
from core.idempotency import already_created
from network.models import SupplierStatus
from receiving.models import (
    DocumentStatus,
    GateIn,
    GateInBox,
    GateInLine,
    GateInReel,
    GateInSerial,
)
from receiving.services import post_gate_in, void_gate_in


def _check_for_site(site):  # type: ignore[no-untyped-def]
    """Q1: a delivery can only be for a live site. Another tenant's site is not
    visible at all, so the field's own "does not exist" covers that."""
    from network.models import SiteStatus

    if site is not None and site.status != SiteStatus.ACTIVE:
        raise serializers.ValidationError(
            f"{site.internal_ref} is decommissioned; material cannot be earmarked for it."
        )
    return site


def _earmarks_by_line(gate_in) -> dict[int, list[dict]]:  # type: ignore[no-untyped-def]
    """Q2: ``{line id: [{site, name}]}`` of where each line's material is earmarked now.

    Four queries however many lines: units by serial number, drums by drum number,
    the receipt movements (to find each bulk line's node) and the bulk claims.
    Bulk is approximate by design: a claim is on a lot, not on a delivery."""
    from catalogue.models import TrackingMode
    from stock.models import BulkEarmark, MovementType, Reel, SerialUnit, StockMovement

    lines = list(gate_in.lines.all())
    found: dict[int, dict[int, str]] = {line.pk: {} for line in lines}

    serial_line = {
        s.serial_number: line.pk for line in lines for s in line.serials.all()
    }
    if serial_line:
        for number, site_id, name in SerialUnit.objects.filter(
            serial_number__in=serial_line, earmark_site__isnull=False
        ).values_list("serial_number", "earmark_site_id", "earmark_site__name"):
            found[serial_line[number]][site_id] = name

    drum_line = {r.drum_number: line.pk for line in lines for r in line.reels.all()}
    if drum_line:
        for number, site_id, name in Reel.objects.filter(
            drum_number__in=drum_line, earmark_site__isnull=False
        ).values_list("drum_number", "earmark_site_id", "earmark_site__name"):
            found[drum_line[number]][site_id] = name

    bulk_lines = [line for line in lines if line.tracking_mode == TrackingMode.BULK]
    if bulk_lines and gate_in.status != DocumentStatus.DRAFT:
        node_by_line = dict(
            StockMovement.objects.filter(
                movement_type=MovementType.RECEIPT,
                document_type="receiving.GateIn",
                document_id=str(gate_in.pk),
                document_line_id__in=[str(line.pk) for line in bulk_lines],
            ).values_list("document_line_id", "to_node_id")
        )
        claims: dict[tuple, dict[int, str]] = {}
        for node_id, item_id, owner_id, condition, site_id, name in BulkEarmark.objects.filter(
            node_id__in=set(node_by_line.values()),
            item_type_id__in={line.item_type_id for line in bulk_lines},
        ).values_list(
            "node_id", "item_type_id", "owner_client_id", "condition", "site_id", "site__name"
        ):
            claims.setdefault((node_id, item_id, owner_id, condition), {})[site_id] = name
        for line in bulk_lines:
            node_id = node_by_line.get(str(line.pk))
            if node_id is not None:
                key = (node_id, line.item_type_id, line.owner_client_id, line.condition)
                found[line.pk].update(claims.get(key, {}))

    return {
        line_id: [
            {"site": site_id, "name": name}
            for site_id, name in sorted(sites.items(), key=lambda kv: (kv[1], kv[0]))
        ]
        for line_id, sites in found.items()
    }


class GateInSerialSerializer(serializers.ModelSerializer):
    class Meta:
        model = GateInSerial
        fields = ("id", "serial_number", "asset_tag", "source", "box_key")


class GateInBoxSerializer(serializers.ModelSerializer):
    """A box on the draft (P1, P10). ``key`` is the client's own, stable across edits;
    lines and serials refer to it by ``box_key`` and a child by ``parent_key``."""

    class Meta:
        model = GateInBox
        fields = ("key", "code", "parent_key", "label_text")
        extra_kwargs = {
            "code": {"required": False, "allow_blank": True},
            "parent_key": {"required": False, "allow_blank": True},
            "label_text": {"required": False, "allow_blank": True},
        }
        # Uniqueness of the key is a posting-time rule with a field error, not a 400 here.
        validators: list = []


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
    for_site_name = serializers.CharField(source="for_site.name", read_only=True, default="")
    # Q2: where this line's material is earmarked *now* (the delivery's own site is
    # `for_site`). Worked out once per document; the detail view only.
    earmarked_now = serializers.SerializerMethodField()

    def validate_for_site(self, site):  # type: ignore[no-untyped-def]
        return _check_for_site(site)

    @extend_schema_field(serializers.ListField(child=serializers.DictField()))
    def get_earmarked_now(self, line) -> list[dict]:  # type: ignore[no-untyped-def]
        view = self.context.get("view")
        if view is None or getattr(view, "action", None) != "retrieve":
            return []
        gate_in = line.gate_in
        cache = getattr(gate_in, "_earmarked_by_line", None)
        if cache is None:
            cache = _earmarks_by_line(gate_in)
            gate_in._earmarked_by_line = cache
        return cache.get(line.pk, [])

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
            "for_site",
            "for_site_name",
            "earmarked_now",
            "custom_field_values",
            "no_serial_reason",
            "declared_unit_value",
            "box_key",
            "notes",
            "serials",
            "reels",
        )


class SupplierNotUsableOnGateIn(DomainError):
    code = "SUPPLIER_NOT_USABLE_ON_GATE_IN"
    status_code = 400
    default_message = (
        "That supplier is inactive or was rejected, so it cannot be named on a delivery."
    )


def _check_supplier_usable(supplier) -> None:  # type: ignore[no-untyped-def]
    """Active and not REJECTED; PENDING is fine (R15). Not a payment check."""
    if not supplier.is_active or supplier.status == SupplierStatus.REJECTED:
        raise SupplierNotUsableOnGateIn(
            f"{supplier.name} is {'rejected' if supplier.is_active else 'inactive'}, "
            "so it cannot be named on a new delivery.",
            field_errors={"supplier": ["Pick a supplier that is active and not rejected."]},
        )


class GateInSerializer(serializers.ModelSerializer):
    lines = GateInLineSerializer(many=True, required=False)
    boxes = GateInBoxSerializer(many=True, required=False, source="gate_in_boxes")
    to_location_name = serializers.CharField(source="to_location.name", read_only=True)
    client_name = serializers.CharField(source="client.name", read_only=True, default="")
    returned_by_name = serializers.CharField(
        source="returned_by.full_name", read_only=True, default=""
    )
    origin_site_ref = serializers.CharField(
        source="origin_site.internal_ref", read_only=True, default=""
    )
    for_site_name = serializers.CharField(source="for_site.name", read_only=True, default="")
    #: R15: PENDING is said on the document, since it is not yet a checked business.
    supplier_status = serializers.CharField(source="supplier.status", read_only=True, default="")
    # R7: the site purchase that made this draft, when one did (§4.19.3).
    source_purchase_number = serializers.CharField(
        source="site_purchase.number", read_only=True, default=None
    )

    def validate_for_site(self, site):  # type: ignore[no-untyped-def]
        return _check_for_site(site)

    def validate(self, attrs):  # type: ignore[no-untyped-def]
        """R15 (4.20.5): a register supplier names the delivery.

        ``supplier_name`` is filled from it, so search, exports and old
        documents read as before. A supplier that is inactive or REJECTED is
        refused on a *new* gate-in; a draft being edited keeps one it already
        names. Text-only (``supplier_name`` alone) is still accepted.
        """
        attrs = super().validate(attrs)
        supplier = attrs.get("supplier")
        if supplier is not None:
            already = self.instance is not None and self.instance.supplier_id == supplier.pk
            if not already:
                _check_supplier_usable(supplier)
            attrs["supplier_name"] = supplier.name
        return attrs

    class Meta:
        model = GateIn
        fields = (
            "id",
            "number",
            "status",
            "source_type",
            "supplier",
            "supplier_status",
            "supplier_name",
            "source_purchase_number",
            "client",
            "client_name",
            "returned_by",
            "returned_by_name",
            "origin_site",
            "origin_site_ref",
            "for_site",
            "for_site_name",
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
            "boxes",
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

    def validate_boxes(self, boxes):  # type: ignore[no-untyped-def]
        # The key is what lines and serials point at, so two boxes sharing one is
        # ambiguous rather than merely untidy.
        keys = [box["key"] for box in boxes]
        for key in keys:
            if keys.count(key) > 1:
                raise serializers.ValidationError(f"Two boxes share the key {key}.")
        return boxes

    @transaction.atomic
    def create(self, validated_data):  # type: ignore[no-untyped-def]
        # A second press of the same button, or a retry of a request whose
        # response was lost, must not become a second document (N2).
        existing = already_created(GateIn, validated_data)
        if existing is not None:
            return existing

        lines = validated_data.pop("lines", [])
        boxes = validated_data.pop("gate_in_boxes", [])
        gate_in = GateIn.objects.create(**validated_data)
        self._write_boxes(gate_in, boxes)
        self._write_lines(gate_in, lines)
        return gate_in

    @transaction.atomic
    def update(self, instance, validated_data):  # type: ignore[no-untyped-def]
        lines = validated_data.pop("lines", None)
        boxes = validated_data.pop("gate_in_boxes", None)
        gate_in = super().update(instance, validated_data)
        if boxes is not None:
            # Wholesale, like the lines: lines refer to boxes by key, which survives.
            gate_in.gate_in_boxes.all().delete()
            self._write_boxes(gate_in, boxes)
        if lines is not None:
            # A draft's lines are replaced wholesale. Diffing them would mean
            # matching client-side ids that an offline device may not have yet
            # (N-1), and a draft has posted nothing, so replacing costs nothing.
            gate_in.lines.all().delete()
            self._write_lines(gate_in, lines)
        return gate_in

    def _write_boxes(self, gate_in, boxes) -> None:
        for box in boxes:
            GateInBox.objects.create(
                organization_id=gate_in.organization_id, gate_in=gate_in, **box
            )

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
        "for_site",
        "posted_by",
        "supplier",
        "site_purchase",
    )
    prefetch_related = (
        "lines",
        "lines__item_type",
        "lines__for_site",
        "lines__serials",
        "lines__reels",
        "gate_in_boxes",
    )
    filterset_fields = [
        "status",
        "source_type",
        "client",
        "supplier",
        "to_location",
        "origin_site",
    ]
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
        if wants_pdf and not content_type.startswith("application/pdf"):
            # A PDF was asked for and a web page came back. Say so in the
            # response rather than leaving it to be inferred from the extension.
            response["X-Document-Fallback"] = "html"
        return response
