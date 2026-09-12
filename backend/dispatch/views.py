"""Dispatch endpoints (design §6, §2.4; F1–F8, G1–G5).

§6's convention throughout: "collections are nouns; **state changes are explicit
sub-resource POST actions**, never a PATCH on status." So there is no way to set
``status`` through this API at all — submit, approve, reject, release, cancel and
close are each their own action, each with its own permission.
"""

from __future__ import annotations

from django.core import signing
from django.db import transaction
from django.http import Http404, HttpResponse
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions_registry import PERM
from core.api import TenantScopedViewSet
from core.api_permissions import OrganizationIsActive
from core.idempotency import already_created
from core.models import AuthMethod
from dispatch.documents import render_gate_pass, resolve_qr_token
from dispatch.models import (
    GateOut,
    GateOutLine,
    GateOutLineReel,
    GateOutLineSerial,
    GateOutPurpose,
    GateOutStatus,
    ReleaseVariance,
)
from dispatch.services import (
    acknowledge_variance,
    amend_gate_out,
    approve_gate_out,
    cancel_gate_out,
    close_gate_out,
    reject_gate_out,
    release_gate_out,
    submit_gate_out,
)


class GateOutLineSerialSerializer(serializers.ModelSerializer):
    serial_number = serializers.CharField(source="serial_unit.serial_number", read_only=True)

    class Meta:
        model = GateOutLineSerial
        fields = ("id", "serial_unit", "serial_number", "released")
        read_only_fields = ("released",)


class GateOutLineReelSerializer(serializers.ModelSerializer):
    drum_number = serializers.CharField(source="reel.drum_number", read_only=True)

    class Meta:
        model = GateOutLineReel
        fields = ("id", "reel", "drum_number", "length_requested", "length_released")
        read_only_fields = ("length_released",)


class GateOutLineSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="item_type.name", read_only=True)
    criticality = serializers.CharField(source="item_type.criticality", read_only=True)
    # F1: "serialized material is requested by identity, not by count" — so these
    # are writable, and `_write_lines` creates the child rows.
    #
    # They were read-only, and that was a hole rather than a restriction: the
    # request screen scans an RRU and posts its serial, DRF dropped it silently,
    # and the pass went out as an anonymous quantity of a serialized item. The
    # ledger then held an antenna with no identity — invisible to the serial
    # history, the installed base and the gate checklist alike.
    serials = GateOutLineSerialSerializer(many=True, required=False)
    reels = GateOutLineReelSerializer(many=True, required=False)
    outstanding_qty = serializers.DecimalField(max_digits=14, decimal_places=3, read_only=True)

    class Meta:
        model = GateOutLine
        fields = (
            "id",
            "line_number",
            "item_type",
            "item_name",
            "criticality",
            "tracking_mode",
            "requested_qty",
            "released_qty",
            "outstanding_qty",
            "uom",
            "condition",
            "owner_type",
            "owner_client",
            "is_returnable",
            "expected_return_date",
            "notes",
            "no_serial_reason",
            "serials",
            "reels",
        )
        read_only_fields = ("released_qty",)

    def validate(self, attrs):  # type: ignore[no-untyped-def]
        """A serialized line names as many units as it asks for (F1, D3).

        Asking for three RRUs and naming two is not a request anybody can fill:
        the gate would have nothing to check the third against, and it would
        reach a technician's custody with no identity. The escape hatch is the
        same as at gate-in — say why there is no serial, and it goes as a
        quantity (D3).
        """
        from catalogue.models import TrackingMode as CatalogueTrackingMode

        item_type = attrs.get("item_type") or getattr(self.instance, "item_type", None)
        mode = attrs.get("tracking_mode") or getattr(self.instance, "tracking_mode", "")
        serials = attrs.get("serials")
        quantity = attrs.get("requested_qty")

        if (
            item_type is not None
            and item_type.default_tracking_mode == CatalogueTrackingMode.SERIALIZED
            and mode != CatalogueTrackingMode.SERIALIZED
            and not attrs.get("no_serial_reason")
        ):
            raise serializers.ValidationError(
                {
                    "tracking_mode": [
                        f"{item_type} is tracked by serial number. Name the units "
                        f"going out, or say why there is no serial (D3)."
                    ]
                }
            )

        if mode == CatalogueTrackingMode.SERIALIZED and quantity is not None:
            named = len(serials or [])
            # Zero named is the case that produced the ghost unit, so it is the
            # same failure as naming too few — not a separate, softer one.
            if named != quantity:
                raise serializers.ValidationError(
                    {
                        "serials": [
                            f"{quantity} requested but {named} named. Scan the "
                            f"units that are going, or say why there is no serial "
                            f"(F1, D3)."
                        ]
                    }
                )
        return attrs


class GateOutSerializer(serializers.ModelSerializer):
    # Writable inline, like a gate-in's. A request assembled line by line could
    # be submitted half-built from a phone on a bad connection, and a pass
    # approved for two of the five things somebody meant to take is worse than
    # no pass at all (F1, T4.21).
    lines = GateOutLineSerializer(many=True, required=False)
    destination_label = serializers.CharField(read_only=True)
    is_releasable = serializers.BooleanField(read_only=True)
    is_expired = serializers.BooleanField(read_only=True)
    custody_holder_name = serializers.CharField(source="custody_holder.full_name", read_only=True)
    pending_approval = serializers.SerializerMethodField()

    class Meta:
        model = GateOut
        fields = (
            "id",
            "number",
            "status",
            "purpose_type",
            "site",
            "work_order",
            "client",
            "to_location",
            "from_location",
            "custody_holder",
            "custody_holder_name",
            "requested_by",
            "destination_label",
            "submitted_at",
            "approved_at",
            "expires_at",
            "vehicle_reg",
            "driver_name",
            "released_by",
            "released_at",
            "version",
            "client_uuid",
            "reject_reason",
            "cancel_reason",
            "close_reason",
            "notes",
            "lines",
            "is_releasable",
            "is_expired",
            "pending_approval",
        )
        # §6: status is never set through the API. Only the named actions move it.
        #
        # ``requested_by`` is server-set for a different reason: a client that
        # could name somebody else as the requester could raise a request in
        # their name and then approve it, which is exactly the separation §5.3
        # exists to enforce.
        read_only_fields = (
            "number",
            "status",
            "requested_by",
            "submitted_at",
            "approved_at",
            "expires_at",
            "released_by",
            "released_at",
            "version",
            "reject_reason",
            "cancel_reason",
            "close_reason",
        )

    def get_pending_approval(self, gate_out) -> dict | None:
        """What is currently being waited on, so the client can say who to chase."""
        from approvals.engine import next_pending_request

        pending = next_pending_request(gate_out)
        if pending is None:
            return None
        return {
            "id": pending.pk,
            "level": pending.level,
            "role": (pending.required_role.name if pending.required_role is not None else None),
            "status": pending.status,
            "due_at": pending.due_at,
        }

    @transaction.atomic
    def create(self, validated_data):  # type: ignore[no-untyped-def]
        # A second press of the same button, or a retry of a request whose
        # response was lost, must not become a second document (N2).
        existing = already_created(GateOut, validated_data)
        if existing is not None:
            return existing

        lines = validated_data.pop("lines", [])
        gate_out = GateOut.objects.create(**validated_data)
        self._write_lines(gate_out, lines)
        return gate_out

    @transaction.atomic
    def update(self, instance, validated_data):  # type: ignore[no-untyped-def]
        """Lines are replaced wholesale while the pass is still a draft.

        Editing an approved pass is F6's amendment, which voids the approval and
        routes again — so it goes through the ``amend`` action rather than here.
        """
        lines = validated_data.pop("lines", None)
        gate_out = super().update(instance, validated_data)
        if lines is not None:
            if gate_out.status != GateOutStatus.DRAFT:
                raise serializers.ValidationError(
                    {
                        "lines": [
                            "This request has been submitted. Amend it instead, "
                            "which voids the approval and routes again (F6)."
                        ]
                    }
                )
            gate_out.lines.all().delete()
            self._write_lines(gate_out, lines)
        return gate_out

    def _write_lines(self, gate_out, lines) -> None:
        for index, line_data in enumerate(lines, start=1):
            serials = line_data.pop("serials", [])
            reels = line_data.pop("reels", [])
            line_data.setdefault("line_number", index)
            line = GateOutLine.objects.create(
                organization_id=gate_out.organization_id, gate_out=gate_out, **line_data
            )
            for serial in serials:
                GateOutLineSerial.objects.create(
                    organization_id=gate_out.organization_id, line=line, **serial
                )
            for reel in reels:
                GateOutLineReel.objects.create(
                    organization_id=gate_out.organization_id, line=line, **reel
                )


class ApproveSerializer(serializers.Serializer):
    """An approval, optionally signed with a fingerprint (F4, M3, T8.9).

    The assertion is optional because §5.3 makes the step-up a *step-up*: a
    tenant with no enrolled devices, or an owner on a laptop with no sensor, still
    has to be able to approve. What changes is what ``ApprovalAction`` records —
    and an auditor can tell the two apart, which is the point.
    """

    reason = serializers.CharField(max_length=500, required=False, allow_blank=True)
    assertion = serializers.DictField(required=False)


class ReasonSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=500, allow_blank=True, required=False)


class ReleaseSerializer(serializers.Serializer):
    """G1, G2: the actual load, the vehicle and the driver."""

    vehicle_reg = serializers.CharField(max_length=30, required=False, allow_blank=True)
    driver_name = serializers.CharField(max_length=200, required=False, allow_blank=True)
    #: line id -> quantity actually loaded. Omitted lines release in full.
    released_lines = serializers.DictField(
        child=serializers.DecimalField(max_digits=14, decimal_places=3), required=False
    )
    #: line id -> why it was short. G1's edge case.
    variance_reasons = serializers.DictField(
        child=serializers.CharField(max_length=500), required=False
    )


class GateOutViewSet(TenantScopedViewSet):
    """``/api/v1/gate-outs`` (F1–F8, G1–G4)."""

    serializer_class = GateOutSerializer
    model = GateOut
    select_related = (
        "site",
        "work_order",
        "client",
        "to_location",
        "from_location",
        "custody_holder",
    )
    prefetch_related = ("lines", "lines__item_type", "lines__serials", "lines__reels")
    filterset_fields = ["status", "purpose_type", "custody_holder", "site", "client"]
    search_fields = ["number", "notes", "vehicle_reg", "driver_name"]
    ordering_fields = ["created_at", "number", "expires_at"]

    # Each action carries the permission its requirement names. G1 and Q7 keep
    # release separate from approval, so a dedicated gate guard is possible.
    required_permissions = {
        "create": PERM.GATE_OUT_REQUEST,
        "update": PERM.GATE_OUT_REQUEST,
        "partial_update": PERM.GATE_OUT_REQUEST,
        "submit": PERM.GATE_OUT_REQUEST,
        "amend": PERM.GATE_OUT_REQUEST,
        "cancel": PERM.GATE_OUT_REQUEST,
        "approve": PERM.GATE_OUT_APPROVE,
        "reject": PERM.GATE_OUT_APPROVE,
        "release": PERM.GATE_OUT_RELEASE,
        "close": PERM.GATE_OUT_RELEASE,
    }

    # A gate pass is never deleted: it is cancelled, with a reason (F8, M6).
    http_method_names = ["get", "post", "patch", "head", "options"]

    def perform_create(self, serializer):  # type: ignore[no-untyped-def]
        serializer.save(created_by=self.request.user, requested_by=self.request.user)

    @extend_schema(request=None, responses={200: GateOutSerializer})
    @action(detail=True, methods=["post"])
    def submit(self, request, pk=None):  # type: ignore[no-untyped-def]
        """F1, F2, F3: route the request to its approvers, or auto-approve."""
        gate_out = submit_gate_out(self.get_object(), submitted_by=request.user, request=request)
        return Response(self.get_serializer(gate_out).data)

    @extend_schema(request=ApproveSerializer, responses={200: GateOutSerializer})
    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):  # type: ignore[no-untyped-def]
        """F4, F5, §5.3.

        WebAuthn step-up (D9, T8.9) supplies ``auth_method`` here once enrolled;
        until then the authenticated session is the password path.
        """
        serializer = ApproveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        gate_out = self.get_object()
        auth_method = AuthMethod.PASSWORD
        credential = None

        assertion = serializer.validated_data.get("assertion")
        if assertion:
            # T8.9: the assertion is verified against *this* approval's
            # challenge. A signature obtained for another document does not
            # authorise this one — that binding is the whole point of the
            # step-up, and it is checked here rather than trusted.
            from accounts.webauthn_service import complete_assertion
            from approvals.engine import next_pending_request

            pending = next_pending_request(gate_out)
            if pending is None:
                raise serializers.ValidationError(
                    {"assertion": ["There is nothing outstanding to approve."]}
                )
            credential = complete_assertion(request.user, pending, credential=assertion)
            auth_method = AuthMethod.WEBAUTHN

        gate_out = approve_gate_out(
            gate_out,
            actor=request.user,
            reason=serializer.validated_data.get("reason", ""),
            auth_method=auth_method,
            webauthn_credential=credential,
            request=request,
        )
        return Response(self.get_serializer(gate_out).data)

    @extend_schema(request=ReasonSerializer, responses={200: GateOutSerializer})
    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):  # type: ignore[no-untyped-def]
        """F4: rejection requires a reason."""
        serializer = ReasonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reason = serializer.validated_data.get("reason", "")
        if not reason:
            raise serializers.ValidationError({"reason": ["A reason is required."]})

        gate_out = reject_gate_out(
            self.get_object(), actor=request.user, reason=reason, request=request
        )
        return Response(self.get_serializer(gate_out).data)

    @extend_schema(request=ReleaseSerializer, responses={200: GateOutSerializer})
    @action(detail=True, methods=["post"])
    def release(self, request, pk=None):  # type: ignore[no-untyped-def]
        """G1, G2, F7: release at the gate, recording any variance."""
        serializer = ReleaseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        gate_out = release_gate_out(
            self.get_object(),
            released_by=request.user,
            released_lines=data.get("released_lines"),
            vehicle_reg=data.get("vehicle_reg", ""),
            driver_name=data.get("driver_name", ""),
            variance_reasons=data.get("variance_reasons"),
            request=request,
        )
        return Response(self.get_serializer(gate_out).data)

    @extend_schema(request=None, responses={200: GateOutSerializer})
    @action(detail=True, methods=["post"])
    def amend(self, request, pk=None):  # type: ignore[no-untyped-def]
        """F6: voids the approval and re-triggers routing."""
        gate_out = amend_gate_out(self.get_object(), amended_by=request.user, request=request)
        return Response(self.get_serializer(gate_out).data)

    @extend_schema(request=ReasonSerializer, responses={200: GateOutSerializer})
    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):  # type: ignore[no-untyped-def]
        """F8: a reason is required, and never after a release."""
        serializer = ReasonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reason = serializer.validated_data.get("reason", "")
        if not reason:
            raise serializers.ValidationError({"reason": ["A reason is required."]})

        gate_out = cancel_gate_out(
            self.get_object(), reason=reason, cancelled_by=request.user, request=request
        )
        return Response(self.get_serializer(gate_out).data)

    @extend_schema(request=ReasonSerializer, responses={200: GateOutSerializer})
    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):  # type: ignore[no-untyped-def]
        """F7: close a partially released pass, writing off the balance."""
        serializer = ReasonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        gate_out = close_gate_out(
            self.get_object(),
            reason=serializer.validated_data.get("reason", ""),
            closed_by=request.user,
            request=request,
        )
        return Response(self.get_serializer(gate_out).data)

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "format",
                str,
                description="'pdf' (default) or 'html' where PDF rendering is unavailable.",
            )
        ],
        responses={(200, "application/pdf"): bytes},
    )
    @action(detail=True, methods=["get"])
    def pdf(self, request, pk=None):  # type: ignore[no-untyped-def]
        """G4: the document the driver carries."""
        gate_out = self.get_object()
        # ``output`` rather than ``format``: DRF reserves ``format`` for content
        # negotiation, so ``?format=html`` never reached this code — it 404'd
        # looking for a renderer of that name. The HTML fallback exists for hosts
        # without WeasyPrint, and a fallback that 404s is worse than none.
        wants_pdf = request.query_params.get("output", "pdf") != "html"

        content, content_type, filename = render_gate_pass(gate_out, as_pdf=wants_pdf)

        response = HttpResponse(content, content_type=content_type)
        response["Content-Disposition"] = f'inline; filename="{filename}"'
        return response

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
    def waybill(self, request, pk=None):  # type: ignore[no-untyped-def]
        """The return waybill the client's store signs (K2, T6.5).

        Two refusals rather than one blank document:

        * a pass that is not a return to client has no waybill to print
        * a tenant with ``client_waybill_enabled`` off does not use ours (C8),
          and printing it anyway invites two references for one delivery
        """
        from dispatch.documents import render_return_waybill

        gate_out = self.get_object()

        if gate_out.purpose_type != GateOutPurpose.RETURN_TO_CLIENT:
            raise serializers.ValidationError(
                {
                    "purpose_type": [
                        "A waybill is for a return to client. This pass is a "
                        f"{gate_out.get_purpose_type_display().lower()} (K2)."
                    ]
                }
            )
        if not gate_out.organization.settings.client_waybill_enabled:
            raise serializers.ValidationError(
                {
                    "client_waybill_enabled": [
                        "This organization does not issue its own return "
                        "waybills. An administrator can turn them on in "
                        "settings (C8, K2)."
                    ]
                }
            )

        # ``output`` rather than ``format``: DRF reserves ``format`` for content
        # negotiation, so ``?format=html`` never reached this code — it 404'd
        # looking for a renderer of that name. The HTML fallback exists for hosts
        # without WeasyPrint, and a fallback that 404s is worse than none.
        wants_pdf = request.query_params.get("output", "pdf") != "html"
        content, content_type, filename = render_return_waybill(gate_out, as_pdf=wants_pdf)
        response = HttpResponse(content, content_type=content_type)
        response["Content-Disposition"] = f'inline; filename="{filename}"'
        return response


class ReleaseVarianceSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="gate_out_line.item_type.name", read_only=True)
    gate_out_number = serializers.CharField(source="gate_out_line.gate_out.number", read_only=True)
    is_open = serializers.BooleanField(read_only=True)
    difference = serializers.DecimalField(max_digits=14, decimal_places=3, read_only=True)

    class Meta:
        model = ReleaseVariance
        fields = (
            "id",
            "gate_out_line",
            "gate_out_number",
            "item_name",
            "approved_qty",
            "released_qty",
            "difference",
            "reason",
            "recorded_by",
            "acknowledged_by",
            "acknowledged_at",
            "is_open",
        )
        read_only_fields = ("acknowledged_by", "acknowledged_at")


class ReleaseVarianceViewSet(TenantScopedViewSet):
    """``/api/v1/release-variances`` (G1).

    Part of the exceptions register (M1): open variances stay listed until an
    approver acknowledges them.
    """

    serializer_class = ReleaseVarianceSerializer
    model = ReleaseVariance
    select_related = ("gate_out_line", "gate_out_line__item_type", "gate_out_line__gate_out")
    filterset_fields = ["acknowledged_at"]
    http_method_names = ["get", "post", "head", "options"]
    required_permissions = {"acknowledge": PERM.GATE_OUT_APPROVE}

    @extend_schema(request=None, responses={200: ReleaseVarianceSerializer})
    @action(detail=True, methods=["post"])
    def acknowledge(self, request, pk=None):  # type: ignore[no-untyped-def]
        variance = acknowledge_variance(self.get_object(), actor=request.user, request=request)
        return Response(self.get_serializer(variance).data)

    @extend_schema(responses={200: ReleaseVarianceSerializer(many=True)})
    @action(detail=False, methods=["get"])
    def open(self, request):  # type: ignore[no-untyped-def]
        """What is currently unresolved (M1)."""
        variances = self.filter_queryset(self.get_queryset()).filter(acknowledged_at__isnull=True)
        page = self.paginate_queryset(variances)
        if page is not None:
            return self.get_paginated_response(self.get_serializer(page, many=True).data)
        return Response(self.get_serializer(variances, many=True).data)


class QrScanView(APIView):
    """``GET /api/v1/qr/scan?token=...`` — resolve a scanned code (G5, T4.20).

    The token identifies the document; it authorises nothing. The caller must be
    authenticated and permitted, and a token for another tenant resolves to
    nothing at all (A3) — so a photographed gate pass is not a way in.
    """

    permission_classes = [IsAuthenticated, OrganizationIsActive]

    @extend_schema(
        parameters=[OpenApiParameter("token", str, required=True)],
        responses={200: dict},
    )
    def get(self, request):  # type: ignore[no-untyped-def]
        token = request.query_params.get("token", "")
        if not token:
            raise Http404("No code supplied.")

        try:
            document = resolve_qr_token(token)
        except signing.BadSignature as exc:
            # 404, not 400: a tampered code should look exactly like an unknown
            # one (§2.4).
            raise Http404("That code is not recognised.") from exc

        if document is None:
            raise Http404("That code does not match any document.")

        # The scanned document must belong to the caller's tenant.
        caller_organization = getattr(request, "organization", None) or getattr(
            request.user, "organization", None
        )
        if caller_organization is None or document.organization_id != caller_organization.pk:
            raise Http404("That code does not match any document.")

        return Response(
            {
                "type": document._meta.label,
                "id": document.pk,
                "number": getattr(document, "number", ""),
                "status": getattr(document, "status", ""),
                # What the client should open. A gate pass scanned at the gate
                # goes straight to the release screen (T4.20).
                "resource": _resource_path(document),
            },
            status=status.HTTP_200_OK,
        )


def _resource_path(document) -> str:
    label = document._meta.label
    if label == "dispatch.GateOut":
        return f"/gate-out/{document.pk}"
    if label == "receiving.GateIn":
        return f"/gate-in/{document.pk}"
    return ""
