"""PO milestone, invoice and receipt endpoints (R11; design §4.19.7, §4.19.10).

Every write goes through ``commercials.milestones`` so the API, the report and
the sweep judge a milestone with the same code. This module only translates.

Routes:

* ``/projects/{id}/milestones`` (GET, POST), ``.../{milestone}`` (PATCH, DELETE)
  and ``.../defaults`` (POST) live as actions on ``ProjectViewSet``, which calls
  the helpers here. They are not a nested router: the isolation and listing
  suites need every collection to answer without a parent id, and a milestone
  is only ever meaningful inside its project.
* ``/milestones`` (read), ``POST /milestones/{id}/invoices`` and ``/receipts``,
  ``/milestone-invoices`` and ``/milestone-receipts`` (read, and ``void``).

Money is shown to ``finance.approve`` and ``project.view_margin`` holders. The
project's manager sees the states only (O14).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from django.utils import timezone
from drf_spectacular.utils import extend_schema, extend_schema_field
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.exceptions import MethodNotAllowed, PermissionDenied
from rest_framework.response import Response

from accounts.permissions_registry import PERM
from accounts.services import resolve_permissions
from commercials import milestones as service
from commercials.models import (
    MilestoneCondition,
    MilestoneInvoice,
    MilestoneReceipt,
    MilestoneShare,
    ProjectMilestone,
)
from core.api import TenantScopedViewSet

MONEY = {"max_digits": 14, "decimal_places": 2}


def _held(user):  # type: ignore[no-untyped-def]
    return resolve_permissions(user)


def can_see_milestone_money(user) -> bool:  # type: ignore[no-untyped-def]
    held = _held(user)
    return held.has(PERM.FINANCE_APPROVE) or held.has(PERM.PROJECT_VIEW_MARGIN)


def can_read_milestones(user, project) -> bool:  # type: ignore[no-untyped-def]
    """Finance, the owner, the catalogue admin and the project's own manager."""
    held = _held(user)
    return (
        held.has(PERM.FINANCE_APPROVE)
        or held.has(PERM.PROJECT_VIEW_MARGIN)
        or held.has(PERM.CATALOGUE_MANAGE)
        or project.manager_id == user.pk
    )


def require_finance(user) -> None:  # type: ignore[no-untyped-def]
    if not _held(user).has(PERM.FINANCE_APPROVE):
        raise PermissionDenied("Only Finance can change milestones, invoices and receipts.")


# --------------------------------------------------------------------------
# Serializers
# --------------------------------------------------------------------------


class MilestoneInvoiceSerializer(serializers.ModelSerializer):
    class Meta:
        model = MilestoneInvoice
        fields = (
            "id",
            "milestone",
            "invoice_number",
            "invoice_date",
            "amount",
            "recorded_by",
            "voided_at",
            "void_reason",
        )
        read_only_fields = fields


class MilestoneReceiptSerializer(serializers.ModelSerializer):
    class Meta:
        model = MilestoneReceipt
        fields = (
            "id",
            "milestone",
            "received_on",
            "amount",
            "reference",
            "recorded_by",
            "voided_at",
            "void_reason",
        )
        read_only_fields = fields


class MilestoneSerializer(serializers.ModelSerializer):
    """A milestone as the screen reads it: share, condition and the server's verdict."""

    state = serializers.SerializerMethodField()
    met_on = serializers.SerializerMethodField()
    amount = serializers.SerializerMethodField()
    invoiced = serializers.SerializerMethodField()
    received = serializers.SerializerMethodField()
    invoices = serializers.SerializerMethodField()
    receipts = serializers.SerializerMethodField()

    class Meta:
        model = ProjectMilestone
        fields = (
            "id",
            "project",
            "sequence",
            "name",
            "share_type",
            "share_value",
            "condition",
            "condition_date",
            "amount",
            "state",
            "met_on",
            "invoiced",
            "received",
            "invoices",
            "receipts",
        )
        read_only_fields = fields

    def _state(self, milestone: ProjectMilestone) -> service.MilestoneState:
        from network.project_sites import accepted_dates_of

        cache = self.context.setdefault("_milestone_states", {})
        if milestone.pk not in cache:
            per_project = self.context.setdefault("_project_facts", {})
            if milestone.project_id not in per_project:
                project = milestone.project
                per_project[project.pk] = (
                    accepted_dates_of(project),
                    project.current_contract_value,
                )
            accepted, value = per_project[milestone.project_id]
            cache[milestone.pk] = service.milestone_state(
                milestone, timezone.localdate(), accepted, contract_value=value
            )
        return cache[milestone.pk]  # type: ignore[no-any-return]

    def get_state(self, milestone: ProjectMilestone) -> str:
        return self._state(milestone).state

    def get_met_on(self, milestone: ProjectMilestone) -> date | None:
        return self._state(milestone).met_on

    @extend_schema_field(serializers.DecimalField(**MONEY, allow_null=True))
    def get_amount(self, milestone: ProjectMilestone) -> str | None:
        amount = self._state(milestone).amount
        return None if amount is None else str(amount)

    @extend_schema_field(serializers.DecimalField(**MONEY))
    def get_invoiced(self, milestone: ProjectMilestone) -> str:
        return str(self._state(milestone).invoiced)

    @extend_schema_field(serializers.DecimalField(**MONEY))
    def get_received(self, milestone: ProjectMilestone) -> str:
        return str(self._state(milestone).received)

    def get_invoices(self, milestone: ProjectMilestone) -> list[dict[str, Any]]:
        return MilestoneInvoiceSerializer(  # type: ignore[no-any-return]
            milestone.invoices.all(), many=True
        ).data

    def get_receipts(self, milestone: ProjectMilestone) -> list[dict[str, Any]]:
        return MilestoneReceiptSerializer(  # type: ignore[no-any-return]
            milestone.receipts.all(), many=True
        ).data

    def to_representation(self, instance):  # type: ignore[no-untyped-def]
        data = super().to_representation(instance)
        request = self.context.get("request")
        if request is not None and not can_see_milestone_money(request.user):
            # Withheld, not nulled (O14): "you were not told", not "there is none".
            for key in ("amount", "invoiced", "received", "invoices", "receipts"):
                data.pop(key, None)
            data.pop("share_value", None)
        return data


class MilestoneInputSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200)
    share_type = serializers.ChoiceField(choices=MilestoneShare.choices)
    share_value = serializers.DecimalField(
        **MONEY, required=False, allow_null=True, min_value=Decimal("0.01")
    )
    condition = serializers.ChoiceField(choices=MilestoneCondition.choices)
    condition_date = serializers.DateField(required=False, allow_null=True)

    def validate(self, attrs: dict) -> dict:
        merged = {
            **(
                {}
                if self.instance is None
                else {
                    "share_type": self.instance.share_type,
                    "condition": self.instance.condition,
                    "condition_date": self.instance.condition_date,
                }
            ),
            **attrs,
        }
        errors: dict[str, str] = {}
        share = attrs.get("share_value")
        if (
            share is not None
            and merged.get("share_type") == MilestoneShare.PERCENT
            and share > 100
        ):
            errors["share_value"] = "A percent share is at most 100."
        if merged.get("condition") == MilestoneCondition.DATE:
            if merged.get("condition_date") is None:
                errors["condition_date"] = "Pick the date."
        else:
            attrs["condition_date"] = None
        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class InvoiceInputSerializer(serializers.Serializer):
    milestone = serializers.IntegerField(required=False)
    invoice_number = serializers.CharField(max_length=100)
    invoice_date = serializers.DateField()
    amount = serializers.DecimalField(**MONEY, min_value=Decimal("0.01"))


class ReceiptInputSerializer(serializers.Serializer):
    milestone = serializers.IntegerField(required=False)
    received_on = serializers.DateField()
    amount = serializers.DecimalField(**MONEY, min_value=Decimal("0.01"))
    reference = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")


class VoidSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=500)


# --------------------------------------------------------------------------
# The project routes (called from ProjectViewSet)
# --------------------------------------------------------------------------


def list_project_milestones(request, project) -> Response:  # type: ignore[no-untyped-def]
    if not can_read_milestones(request.user, project):
        raise PermissionDenied("You may not see this project's milestones.")
    rows = service.project_milestones(project)
    data = MilestoneSerializer(rows, many=True, context={"request": request}).data
    # The same envelope as every list, so the screen's list hook reads it as is.
    return Response({"next": None, "previous": None, "results": data})


def add_project_milestone(request, project) -> Response:  # type: ignore[no-untyped-def]
    require_finance(request.user)
    body = MilestoneInputSerializer(data=request.data)
    body.is_valid(raise_exception=True)
    data = {"share_value": None, "condition_date": None, **body.validated_data}
    milestone = service.create_milestone(project, data, actor=request.user, request=request)
    return Response(_one(request, milestone), status=201)


def change_project_milestone(request, project, milestone_id: int) -> Response:  # type: ignore[no-untyped-def]
    require_finance(request.user)
    milestone = _milestone_of(project, milestone_id)
    if request.method == "DELETE":
        service.delete_milestone(milestone, actor=request.user, request=request)
        return Response(status=204)
    body = MilestoneInputSerializer(milestone, data=request.data, partial=True)
    body.is_valid(raise_exception=True)
    milestone = service.update_milestone(
        milestone, dict(body.validated_data), actor=request.user, request=request
    )
    return Response(_one(request, milestone))


def add_default_project_milestones(request, project) -> Response:  # type: ignore[no-untyped-def]
    require_finance(request.user)
    service.add_default_milestones(project, actor=request.user, request=request)
    return list_project_milestones(request, project)


def _milestone_of(project, milestone_id: int) -> ProjectMilestone:  # type: ignore[no-untyped-def]
    from django.http import Http404

    milestone = ProjectMilestone.objects.filter(project=project, pk=milestone_id).first()
    if milestone is None:
        raise Http404()
    return milestone


def _one(request, milestone: ProjectMilestone) -> dict:  # type: ignore[no-untyped-def]
    fresh = (
        ProjectMilestone.objects.select_related("project")
        .prefetch_related("invoices", "receipts")
        .get(pk=milestone.pk)
    )
    return MilestoneSerializer(fresh, context={"request": request}).data  # type: ignore[no-any-return]


# --------------------------------------------------------------------------
# Flat viewsets
# --------------------------------------------------------------------------


class _ReadOnlyFinanceViewSet(TenantScopedViewSet):
    """Read for Finance and the owner; changes only through named actions."""

    http_method_names = ["get", "post", "head", "options"]
    required_permission = PERM.FINANCE_APPROVE

    def create(self, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise MethodNotAllowed("POST")

    def update(self, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise MethodNotAllowed(request.method)

    def destroy(self, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise MethodNotAllowed("DELETE")


class MilestoneViewSet(_ReadOnlyFinanceViewSet):
    """``/api/v1/milestones`` and ``POST /milestones/{id}/invoices`` · ``/receipts``."""

    serializer_class = MilestoneSerializer
    model = ProjectMilestone
    select_related = ("project",)
    prefetch_related = ("invoices", "receipts")
    filterset_fields = ["project"]

    @extend_schema(request=InvoiceInputSerializer, responses={201: MilestoneInvoiceSerializer})
    @action(detail=True, methods=["post"], url_path="invoices")
    def invoices(self, request, pk=None):  # type: ignore[no-untyped-def]
        body = InvoiceInputSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        invoice, warning = service.record_invoice(
            self.get_object(),
            invoice_number=body.validated_data["invoice_number"],
            invoice_date=body.validated_data["invoice_date"],
            amount=body.validated_data["amount"],
            actor=request.user,
            request=request,
        )
        payload = MilestoneInvoiceSerializer(invoice).data
        if warning:
            payload = {**payload, "warning": warning}
        return Response(payload, status=201)

    @extend_schema(request=ReceiptInputSerializer, responses={201: MilestoneReceiptSerializer})
    @action(detail=True, methods=["post"], url_path="receipts")
    def receipts(self, request, pk=None):  # type: ignore[no-untyped-def]
        body = ReceiptInputSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        receipt = service.record_receipt(
            self.get_object(),
            received_on=body.validated_data["received_on"],
            amount=body.validated_data["amount"],
            reference=body.validated_data.get("reference", ""),
            actor=request.user,
            request=request,
        )
        return Response(MilestoneReceiptSerializer(receipt).data, status=201)


class MilestoneInvoiceViewSet(_ReadOnlyFinanceViewSet):
    """``/api/v1/milestone-invoices``; a mistake is voided, never edited."""

    serializer_class = MilestoneInvoiceSerializer
    model = MilestoneInvoice
    select_related = ("milestone",)
    filterset_fields = ["milestone"]

    @extend_schema(request=VoidSerializer, responses={200: MilestoneInvoiceSerializer})
    @action(detail=True, methods=["post"], url_path="void")
    def void(self, request, pk=None):  # type: ignore[no-untyped-def]
        invoice = service.void_invoice(
            self.get_object(),
            reason=request.data.get("reason", ""),
            actor=request.user,
            request=request,
        )
        return Response(MilestoneInvoiceSerializer(invoice).data)


class MilestoneReceiptViewSet(_ReadOnlyFinanceViewSet):
    """``/api/v1/milestone-receipts``."""

    serializer_class = MilestoneReceiptSerializer
    model = MilestoneReceipt
    select_related = ("milestone",)
    filterset_fields = ["milestone"]

    @extend_schema(request=VoidSerializer, responses={200: MilestoneReceiptSerializer})
    @action(detail=True, methods=["post"], url_path="void")
    def void(self, request, pk=None):  # type: ignore[no-untyped-def]
        receipt = service.void_receipt(
            self.get_object(),
            reason=request.data.get("reason", ""),
            actor=request.user,
            request=request,
        )
        return Response(MilestoneReceiptSerializer(receipt).data)
