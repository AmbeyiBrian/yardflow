"""Subcontract endpoints (§4.19.10; R8).

``/subcontracts`` and ``/subcontract-payments``. Every write goes through
``commercials.contracts`` so the rules run once. This module only translates:
HTTP in, a service call, a read payload out.

Who reads: the project's PM, and anyone holding ``project.view_cost`` or
``finance.approve``. Who writes: the PM or Finance for a contract; Finance for a
payment, which only the project's PM decides (§4.19.4).
"""

from __future__ import annotations

from typing import Any

from django.db.models import CharField, Exists, OuterRef, Q
from django.db.models.functions import Cast
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_field
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.response import Response

from accounts.permissions_registry import PERM
from accounts.services import resolve_permissions
from approvals.addressing import open_requests_addressed_to
from approvals.models import ApprovalRequest
from commercials import contracts
from commercials.finance_api import DecideSerializer, ReverseSerializer
from commercials.models import (
    ExpenseStatus,
    Subcontract,
    SubcontractPayment,
    SubcontractStatus,
)
from core.api import TenantScopedViewSet
from network.models import Project, Site, Subcontractor

PAYMENT_LABEL = SubcontractPayment._meta.label


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _sees_everything(user) -> bool:  # type: ignore[no-untyped-def]
    held = resolve_permissions(user)
    return bool(held.has(PERM.FINANCE_APPROVE) or held.has(PERM.PROJECT_VIEW_COST))


def _one(model, pk, field: str):  # type: ignore[no-untyped-def]
    """A tenant-scoped row by id, or a field error. Never another tenant's."""
    row = model.objects.filter(pk=pk).first()
    if row is None:
        raise serializers.ValidationError({field: ["Not found."]})
    return row


# --------------------------------------------------------------------------
# Subcontracts
# --------------------------------------------------------------------------


class PositionSerializer(serializers.Serializer):
    contract_value = serializers.DecimalField(max_digits=14, decimal_places=2)
    work_done = serializers.DecimalField(max_digits=14, decimal_places=2)
    paid = serializers.DecimalField(max_digits=14, decimal_places=2)
    awaiting_approval = serializers.DecimalField(max_digits=14, decimal_places=2)
    owed = serializers.DecimalField(max_digits=14, decimal_places=2)
    committed = serializers.DecimalField(max_digits=14, decimal_places=2)
    paid_exceeds_work_done = serializers.BooleanField()
    paid_exceeds_contract_value = serializers.BooleanField()


class SubcontractJobSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    reference = serializers.CharField()
    description = serializers.CharField()
    status = serializers.CharField()
    site = serializers.IntegerField(source="site_id")
    site_name = serializers.CharField(source="site.name")
    agreed_price = serializers.DecimalField(max_digits=14, decimal_places=2, allow_null=True)
    over_contract_reason = serializers.CharField()


class SubcontractPaymentSerializer(serializers.ModelSerializer):
    """The payment as the PM's approval sheet and the contract's list read it."""

    amount = serializers.DecimalField(
        source="signed_amount", max_digits=14, decimal_places=2, read_only=True
    )
    subcontract_number = serializers.CharField(source="subcontract.number", read_only=True)
    subcontractor_name = serializers.CharField(
        source="subcontract.subcontractor.name", read_only=True
    )
    project_reference = serializers.CharField(
        source="subcontract.project.reference", read_only=True
    )
    recorded_by_name = serializers.SerializerMethodField()
    rejection_reason = serializers.SerializerMethodField()
    position = serializers.SerializerMethodField()
    would_exceed_work_done = serializers.SerializerMethodField()
    would_exceed_contract_value = serializers.SerializerMethodField()

    class Meta:
        model = SubcontractPayment
        fields = (
            "id",
            "subcontract",
            "subcontract_number",
            "subcontractor_name",
            "project_reference",
            "amount",
            "paid_on",
            "reference",
            "status",
            "recorded_by",
            "recorded_by_name",
            "decided_by",
            "decided_at",
            "decision_reason",
            "rejection_reason",
            "reverses",
            "client_uuid",
            "position",
            "would_exceed_work_done",
            "would_exceed_contract_value",
            "created_at",
        )
        read_only_fields = fields

    def get_recorded_by_name(self, payment) -> str:  # type: ignore[no-untyped-def]
        return payment.recorded_by.full_name or payment.recorded_by.email  # type: ignore[no-any-return]

    def get_rejection_reason(self, payment) -> str:  # type: ignore[no-untyped-def]
        return payment.decision_reason if payment.status == ExpenseStatus.REJECTED else ""

    def _position(self, payment):  # type: ignore[no-untyped-def]
        """Only where a screen decides on it: a list of a hundred would cost a hundred."""
        if not self.context.get("with_position"):
            return None
        cache = self.context.setdefault("_positions", {})
        if payment.subcontract_id not in cache:
            cache[payment.subcontract_id] = contracts.position(payment.subcontract)
        return cache[payment.subcontract_id]

    @extend_schema_field(PositionSerializer(allow_null=True))
    def get_position(self, payment):  # type: ignore[no-untyped-def]
        position = self._position(payment)
        return PositionSerializer(position.as_dict()).data if position else None

    def _would(self, payment, attribute: str):  # type: ignore[no-untyped-def]
        """Whether approving this would carry the payments past the figure (§4.19.4)."""
        position = self._position(payment)
        if position is None:
            return None
        paid = position.paid
        if payment.status == ExpenseStatus.PENDING_PM:
            paid += payment.signed_amount
        return bool(paid > getattr(position, attribute))

    def get_would_exceed_work_done(self, payment) -> bool | None:  # type: ignore[no-untyped-def]
        return self._would(payment, "work_done")

    def get_would_exceed_contract_value(self, payment) -> bool | None:  # type: ignore[no-untyped-def]
        return self._would(payment, "contract_value")


class SubcontractSerializer(serializers.ModelSerializer):
    reference = serializers.CharField(source="number", read_only=True)
    subcontractor_name = serializers.CharField(source="subcontractor.name", read_only=True)
    project_reference = serializers.CharField(source="project.reference", read_only=True)
    sites = serializers.PrimaryKeyRelatedField(many=True, read_only=True)
    position = serializers.SerializerMethodField()

    class Meta:
        model = Subcontract
        fields = (
            "id",
            "reference",
            "project",
            "project_reference",
            "subcontractor",
            "subcontractor_name",
            "sites",
            "contract_value",
            "payment_terms",
            "status",
            "position",
            "created_at",
        )
        read_only_fields = fields

    @extend_schema_field(PositionSerializer)
    def get_position(self, contract) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        return PositionSerializer(contracts.position_of(contract).as_dict()).data  # type: ignore[no-any-return]


class SubcontractDetailSerializer(SubcontractSerializer):
    jobs = serializers.SerializerMethodField()
    payments = serializers.SerializerMethodField()

    class Meta(SubcontractSerializer.Meta):
        fields = (*SubcontractSerializer.Meta.fields, "jobs", "payments")  # type: ignore[assignment]
        read_only_fields = fields  # type: ignore[assignment]

    @extend_schema_field(SubcontractJobSerializer(many=True))
    def get_jobs(self, contract) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
        jobs = contract.jobs.select_related("site").order_by("created_at", "id")
        return SubcontractJobSerializer(jobs, many=True).data  # type: ignore[no-any-return]

    @extend_schema_field(SubcontractPaymentSerializer(many=True))
    def get_payments(self, contract) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
        payments = contract.payments.select_related(
            "subcontract__project", "subcontract__subcontractor", "recorded_by"
        ).order_by("paid_on", "id")
        return SubcontractPaymentSerializer(  # type: ignore[no-any-return]
            payments, many=True, context=self.context
        ).data


class SubcontractCreateSerializer(serializers.Serializer):
    project = serializers.IntegerField()
    subcontractor = serializers.IntegerField()
    sites = serializers.ListField(child=serializers.IntegerField(), required=False)
    contract_value = serializers.DecimalField(max_digits=14, decimal_places=2)
    payment_terms = serializers.CharField(required=False, allow_blank=True, default="")


class SubcontractEditSerializer(serializers.Serializer):
    sites = serializers.ListField(child=serializers.IntegerField(), required=False)
    contract_value = serializers.DecimalField(max_digits=14, decimal_places=2, required=False)
    payment_terms = serializers.CharField(required=False, allow_blank=True)
    status = serializers.ChoiceField(choices=SubcontractStatus.choices, required=False)


def _sites_from(ids) -> list:  # type: ignore[no-untyped-def]
    ids = list(dict.fromkeys(ids))
    found = list(Site.objects.filter(pk__in=ids))
    if len(found) != len(ids):
        raise serializers.ValidationError({"sites": ["One of these sites was not found."]})
    return found


class SubcontractViewSet(TenantScopedViewSet):
    """``/api/v1/subcontracts`` (R8, §4.19.10).

    No destroy: a contract that has ended is closed, never removed.
    """

    serializer_class = SubcontractDetailSerializer
    model = Subcontract
    select_related = ("project", "subcontractor")
    prefetch_related = ("sites",)
    filterset_fields = ["project", "subcontractor", "status"]
    search_fields = ["number", "subcontractor__name", "project__reference"]
    ordering_fields = ["created_at", "contract_value"]
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):  # type: ignore[no-untyped-def]
        queryset = super().get_queryset().annotate(**contracts.position_annotations())
        if _sees_everything(self.request.user):
            return queryset
        return queryset.filter(project__manager=self.request.user)

    def get_serializer_class(self):  # type: ignore[no-untyped-def]
        if self.action == "list":
            return SubcontractSerializer
        return SubcontractDetailSerializer

    def _fresh(self, pk):  # type: ignore[no-untyped-def]
        return Response(self.get_serializer(self.get_queryset().get(pk=pk)).data)

    @extend_schema(
        request=SubcontractCreateSerializer, responses={201: SubcontractDetailSerializer}
    )
    def create(self, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        body = SubcontractCreateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        contract = contracts.create_subcontract(
            actor=request.user,
            project=_one(Project, data["project"], "project"),
            subcontractor=_one(Subcontractor, data["subcontractor"], "subcontractor"),
            contract_value=data["contract_value"],
            sites=_sites_from(data.get("sites", [])),
            payment_terms=data["payment_terms"],
            request=request,
        )
        response = self._fresh(contract.pk)
        response.status_code = 201
        return response

    @extend_schema(request=SubcontractEditSerializer, responses={200: SubcontractDetailSerializer})
    def partial_update(self, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        contract = self.get_object()
        body = SubcontractEditSerializer(data=request.data, partial=True)
        body.is_valid(raise_exception=True)
        changes = dict(body.validated_data)
        if "sites" in changes:
            changes["sites"] = _sites_from(changes["sites"])
        contracts.update_subcontract(contract, actor=request.user, changes=changes, request=request)
        return self._fresh(contract.pk)

    def update(self, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        return self.partial_update(request, *args, **kwargs)


# --------------------------------------------------------------------------
# Payments
# --------------------------------------------------------------------------


class SubcontractPaymentCreateSerializer(serializers.Serializer):
    subcontract = serializers.IntegerField()
    amount = serializers.DecimalField(max_digits=14, decimal_places=2)
    paid_on = serializers.DateField()
    # Blank is accepted here so the service's own PAYMENT_REFERENCE_REQUIRED is
    # what the caller reads.
    reference = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    client_uuid = serializers.UUIDField(required=False)


PENDING = OpenApiParameter(
    "pending", bool, description="Only payments waiting on the caller (the PM's list)."
)


class SubcontractPaymentViewSet(TenantScopedViewSet):
    """``/api/v1/subcontract-payments`` (R8, §4.19.4).

    Finance records; the project's PM decides. No PATCH: a wrong payment is
    rejected and sent again, or reversed once approved (O16).
    """

    serializer_class = SubcontractPaymentSerializer
    model = SubcontractPayment
    select_related = ("subcontract__project", "subcontract__subcontractor", "recorded_by")
    required_permissions = {"create": PERM.FINANCE_APPROVE}
    filterset_fields = ["subcontract", "status"]
    ordering_fields = ["paid_on", "created_at", "amount"]
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):  # type: ignore[no-untyped-def]
        queryset = super().get_queryset()
        if _sees_everything(self.request.user):
            return queryset
        user = self.request.user
        return queryset.filter(Q(subcontract__project__manager=user) | Q(recorded_by=user))

    def filter_queryset(self, queryset):  # type: ignore[no-untyped-def]
        queryset = super().filter_queryset(queryset)
        if _truthy(self.request.query_params.get("pending")):
            addressed = open_requests_addressed_to(
                self.request.user,
                ApprovalRequest.objects.filter(
                    document_type=PAYMENT_LABEL,
                    document_id=Cast(OuterRef("pk"), output_field=CharField()),
                ),
            )
            queryset = queryset.filter(Exists(addressed))
        return queryset

    def get_serializer_context(self):  # type: ignore[no-untyped-def]
        context = super().get_serializer_context()
        pending = _truthy(self.request.query_params.get("pending")) if self.request else False
        context["with_position"] = self.action != "list" or pending
        return context

    def _read(self, payment, status: int = 200):  # type: ignore[no-untyped-def]
        fresh = self.get_queryset().get(pk=payment.pk)
        return Response(self.get_serializer(fresh).data, status=status)

    @extend_schema(parameters=[PENDING], responses={200: SubcontractPaymentSerializer(many=True)})
    def list(self, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        return super().list(request, *args, **kwargs)

    @extend_schema(
        request=SubcontractPaymentCreateSerializer, responses={201: SubcontractPaymentSerializer}
    )
    def create(self, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        body = SubcontractPaymentCreateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        # Looked up through the visibility scope, so a contract the caller may
        # not read is "not found", not "forbidden".
        contract = (
            Subcontract.objects.select_related("project", "subcontractor")
            .filter(pk=data["subcontract"])
            .first()
        )
        if contract is None:
            raise serializers.ValidationError({"subcontract": ["Not found."]})
        payment = contracts.record_subcontract_payment(
            actor=request.user,
            subcontract=contract,
            amount=data["amount"],
            paid_on=data["paid_on"],
            reference=data["reference"],
            client_uuid=data.get("client_uuid"),
            request=request,
        )
        return self._read(payment, status=201)

    @extend_schema(request=DecideSerializer, responses={200: SubcontractPaymentSerializer})
    @action(detail=True, methods=["post"])
    def decide(self, request, pk=None):  # type: ignore[no-untyped-def]
        """The project's PM, and only the PM (§4.19.4)."""
        body = DecideSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        payment = contracts.decide(
            self.get_object(),
            actor=request.user,
            approved=body.validated_data["approved"],
            reason=body.validated_data.get("reason", ""),
            request=request,
        )
        return self._read(payment)

    @extend_schema(request=None, responses={200: SubcontractPaymentSerializer})
    @action(detail=True, methods=["post"])
    def resubmit(self, request, pk=None):  # type: ignore[no-untyped-def]
        payment = contracts.resubmit(self.get_object(), actor=request.user, request=request)
        return self._read(payment)

    @extend_schema(request=ReverseSerializer, responses={201: SubcontractPaymentSerializer})
    @action(detail=True, methods=["post"])
    def reverse(self, request, pk=None):  # type: ignore[no-untyped-def]
        """O16: corrected by its opposite, never by an edit."""
        body = ReverseSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        reversal = contracts.reverse_subcontract_payment(
            self.get_object(),
            actor=request.user,
            reason=body.validated_data["reason"],
            request=request,
        )
        return self._read(reversal, status=201)
