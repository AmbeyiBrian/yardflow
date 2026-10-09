"""Money-out endpoints (§4.17.6, §4.17.7; R1-R5).

Every write goes through ``commercials.finance`` so an entry made here and one
replayed from a phone (R6) are judged by the same code. This module only
translates: HTTP in, a service call, a read payload out.

Read payloads are annotated in the query (evidence, routing, float spend) rather
than computed per row, so a page of a hundred entries costs the same handful of
queries as a page of one.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from django.db.models import (
    CharField,
    DecimalField,
    Exists,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
)
from django.db.models.functions import Cast, Coalesce
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import SAFE_METHODS, BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.api_permissions import HasPermission
from accounts.permissions_registry import PERM
from accounts.services import resolve_permissions
from approvals import engine
from approvals.addressing import open_requests_addressed_to
from approvals.models import ApprovalRequest
from commercials import finance, finance_rules
from commercials.finance import CasualLineInput
from commercials.models import (
    AllowanceRequest,
    AllowanceType,
    Casual,
    ExpenseCasualLine,
    ExpenseCategory,
    ExpenseStatus,
    ProjectExpense,
    TransportScope,
)
from commercials.services import reverse_expense
from core.api import TenantScopedViewSet
from core.api_permissions import OrganizationIsActive
from core.audit import record
from core.exceptions import PermissionDeniedError
from core.models import Attachment, AuditAction, Organization

# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _holds(user, codename: str) -> bool:  # type: ignore[no-untyped-def]
    return resolve_permissions(user).has(codename)


def _id_key() -> Any:
    """The row's id as text: ``Attachment.target_id`` and ``ApprovalRequest.document_id``
    are text, not foreign keys (§4.13)."""
    return Cast(OuterRef("pk"), output_field=CharField())


def _routing_annotations(label: str) -> dict[str, Any]:
    """Whether the entry was ever routed, and whether it had a PM level.

    ``pm_level_skipped`` is "routed, but never with a level-1 request" (§4.17.3):
    a PM's or Director's own entry. An entry with no requests at all (a
    reversal, which is born APPROVED) was not skipped, it was never routed.
    """
    requests = ApprovalRequest.objects.filter(document_type=label, document_id=_id_key())
    return {
        "has_requests": Exists(requests),
        "has_pm_request": Exists(requests.filter(level=engine.FINANCE_PM_LEVEL)),
    }


def _pm_level_skipped(entry) -> bool:  # type: ignore[no-untyped-def]
    routed = getattr(entry, "has_requests", None)
    pm = getattr(entry, "has_pm_request", None)
    if routed is None or pm is None:
        requests = ApprovalRequest.objects.filter(
            document_type=engine.document_type_of(entry), document_id=str(entry.pk)
        )
        routed = requests.exists()
        pm = requests.filter(level=engine.FINANCE_PM_LEVEL).exists()
    return bool(routed and not pm)


def _visible_to(queryset, user):  # type: ignore[no-untyped-def]
    """Money entries are not for every member to browse (R4).

    Somebody sees what they recorded, and the entries on projects they manage
    (the PM decides them). Finance, and those who see project cost (O14), see
    all of them. Without this, any storekeeper could read every colleague's
    allowances.
    """
    held = resolve_permissions(user)
    if held.has(PERM.FINANCE_APPROVE) or held.has(PERM.PROJECT_VIEW_COST):
        return queryset
    return queryset.filter(Q(recorded_by=user) | Q(project__manager=user))


def _require_finance(request) -> None:  # type: ignore[no-untyped-def]
    """``payable`` is the Finance queue; asking for it without the right is a 403,
    not an empty list that looks like "nothing to pay" (§4.17.7)."""
    if not _holds(request.user, PERM.FINANCE_APPROVE):
        raise PermissionDenied("Only Finance can see what is waiting to be paid.")


MINE = OpenApiParameter("mine", bool, description="Only entries I recorded.")
PAYABLE = OpenApiParameter(
    "payable", bool, description="Approved and unpaid (needs finance.approve)."
)


class CatalogueOrFinance(BasePermission):
    """Reads for any member; writes for ``catalogue.manage`` or ``finance.approve``.

    Finance owns the expense categories' ``kind`` (§4.17.2), so it may write them
    without also holding the whole catalogue.
    """

    message = "You do not have permission to perform this action."

    def has_permission(self, request, view) -> bool:  # type: ignore[no-untyped-def]
        if request.method in SAFE_METHODS:
            return True
        held = resolve_permissions(request.user)
        return held.has(PERM.CATALOGUE_MANAGE) or held.has(PERM.FINANCE_APPROVE)


# --------------------------------------------------------------------------
# Categories
# --------------------------------------------------------------------------


class ExpenseCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ExpenseCategory
        fields = ("id", "name", "code", "kind", "is_active")


class ExpenseCategoryViewSet(TenantScopedViewSet):
    """``/api/v1/expense-categories`` (O16, §4.17.2)."""

    serializer_class = ExpenseCategorySerializer
    model = ExpenseCategory
    permission_classes = [IsAuthenticated, OrganizationIsActive, CatalogueOrFinance]
    filterset_fields = ["is_active", "kind"]
    search_fields = ["name", "code"]


# --------------------------------------------------------------------------
# Casuals (R3)
# --------------------------------------------------------------------------


def mask_id_number(value: str) -> str:
    """Everything but the last three characters, hidden (§4.17.7)."""
    if len(value) <= 3:
        return "*" * len(value)
    return "*" * (len(value) - 3) + value[-3:]


class CasualSerializer(serializers.ModelSerializer):
    class Meta:
        model = Casual
        fields = ("id", "name", "id_number", "phone", "registered_by", "client_uuid")
        read_only_fields = ("registered_by",)
        # A replay of one client_uuid is answered with the row it made (R6), not
        # refused by a uniqueness validator.
        validators: list[Any] = []
        extra_kwargs = {"client_uuid": {"required": False, "allow_null": True}}

    def to_representation(self, instance):  # type: ignore[no-untyped-def]
        data = super().to_representation(instance)
        request = self.context.get("request")
        # Held directly or delegated: Finance needs the full number to pay a
        # casual, and a request-less context (a test, a command) sees it masked.
        if request is None or not _holds(request.user, PERM.FINANCE_APPROVE):
            data["id_number"] = mask_id_number(str(data["id_number"]))
        return data

    def create(self, validated_data):  # type: ignore[no-untyped-def]
        request = self.context["request"]
        return finance.register_casual(
            actor=request.user,
            name=validated_data["name"],
            id_number=validated_data["id_number"],
            phone=validated_data.get("phone", ""),
            client_uuid=validated_data.get("client_uuid"),
            request=request,
        )


class CasualUpdateSerializer(serializers.ModelSerializer):
    """Only the name and phone move; the ID number is what identifies the person."""

    class Meta:
        model = Casual
        fields = ("name", "phone")


class CasualViewSet(TenantScopedViewSet):
    """``/api/v1/casuals`` (R3, §4.17.6). Any member registers and reads."""

    serializer_class = CasualSerializer
    model = Casual
    @property
    def search_fields(self) -> list[str]:  # type: ignore[override]
        """Name and phone for everyone; the ID number only for Finance.

        ``id_number_key`` so "12 345-678" finds "12345678" (§4.17.2) — but a
        search that matches is itself an answer, so a member who sees only the
        last three digits must not be able to confirm the rest by searching.
        """
        request = getattr(self, "request", None)
        if request is not None and _holds(request.user, PERM.FINANCE_APPROVE):
            return ["name", "phone", "id_number_key"]
        return ["name", "phone"]
    http_method_names = ["get", "post", "patch", "head", "options"]

    def partial_update(self, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        casual = self.get_object()
        serializer = CasualUpdateSerializer(casual, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        record(
            AuditAction.DOCUMENT_AMENDED,
            actor=request.user,
            organization=casual.organization_id,
            target=casual,
            target_label=str(casual),
            request=request,
            note=f"Casual {casual.name} details changed.",
        )
        return Response(self.get_serializer(casual).data)


# --------------------------------------------------------------------------
# Expenses (R1, R4)
# --------------------------------------------------------------------------


class ExpenseCasualLineSerializer(serializers.ModelSerializer):
    casual_name = serializers.CharField(source="casual.name", read_only=True)

    class Meta:
        model = ExpenseCasualLine
        fields = ("id", "casual", "casual_name", "days", "amount")


class ProjectExpenseSerializer(serializers.ModelSerializer):
    project_reference = serializers.CharField(source="project.__str__", read_only=True)
    site_name = serializers.CharField(source="site.name", read_only=True, default="")
    category_name = serializers.CharField(source="category.name", read_only=True)
    category_kind = serializers.CharField(source="category.kind", read_only=True)
    recorded_by_name = serializers.CharField(source="recorded_by.full_name", read_only=True)
    is_reversal = serializers.SerializerMethodField()
    # O16: shown to the manager rather than used to refuse the record.
    is_evidenced = serializers.SerializerMethodField()
    #: 4.17.8: "ok" photos are in, "arriving" the phone said it would send some,
    #: "none" nobody did.
    evidence_state = serializers.SerializerMethodField()
    #: R4: the PM level was skipped, so this went straight to Finance.
    pm_level_skipped = serializers.SerializerMethodField()
    casual_lines = ExpenseCasualLineSerializer(many=True, required=False)

    class Meta:
        model = ProjectExpense
        fields = (
            "id",
            "project",
            "project_reference",
            "site",
            "site_name",
            "job",
            "category",
            "category_name",
            "category_kind",
            "amount",
            "incurred_on",
            "description",
            "scope_of_work",
            "vehicle_reg",
            "litres",
            "float_request",
            "photos_expected",
            "client_uuid",
            "casual_lines",
            "recorded_by",
            "recorded_by_name",
            "status",
            "decided_by",
            "decided_at",
            "decision_reason",
            "paid_at",
            "paid_by",
            "payment_reference",
            "reverses",
            "is_reversal",
            "is_evidenced",
            "evidence_state",
            "pm_level_skipped",
            "created_at",
        )
        read_only_fields = (
            "recorded_by",
            "status",
            "decided_by",
            "decided_at",
            "decision_reason",
            "paid_at",
            "paid_by",
            "payment_reference",
            "reverses",
        )
        # The service owns idempotency on client_uuid (R6).
        validators: list[Any] = []
        extra_kwargs = {
            # R1: a site is enough; the service resolves the project, and one
            # of the two is required there.
            "project": {"required": False, "allow_null": True},
            "site": {"required": False, "allow_null": True},
            "client_uuid": {"required": False, "allow_null": True},
        }

    def get_is_reversal(self, expense: ProjectExpense) -> bool:
        return bool(expense.reverses_id)

    def _has_evidence(self, expense: ProjectExpense) -> bool:
        annotated = getattr(expense, "has_evidence", None)
        return bool(expense.is_evidenced if annotated is None else annotated)

    def get_is_evidenced(self, expense: ProjectExpense) -> bool:
        return self._has_evidence(expense)

    def get_evidence_state(self, expense: ProjectExpense) -> str:
        if self._has_evidence(expense):
            return "ok"
        return "arriving" if expense.photos_expected else "none"

    def get_pm_level_skipped(self, expense: ProjectExpense) -> bool:
        return _pm_level_skipped(expense)

    def create(self, validated_data):  # type: ignore[no-untyped-def]
        request = self.context["request"]
        lines = [
            CasualLineInput(casual=item["casual"], days=item["days"], amount=item.get("amount"))
            for item in validated_data.pop("casual_lines", [])
        ]
        return finance.record_expense(
            actor=request.user,
            category=validated_data["category"],
            amount=validated_data["amount"],
            incurred_on=validated_data["incurred_on"],
            site=validated_data.get("site"),
            project=validated_data.get("project"),
            job=validated_data.get("job"),
            description=validated_data.get("description", ""),
            scope_of_work=validated_data.get("scope_of_work", ""),
            vehicle_reg=validated_data.get("vehicle_reg", ""),
            litres=validated_data.get("litres"),
            float_request=validated_data.get("float_request"),
            photos_expected=validated_data.get("photos_expected", 0),
            casual_lines=lines,
            client_uuid=validated_data.get("client_uuid"),
            request=request,
        )


class ExpenseEditSerializer(serializers.ModelSerializer):
    """What may still change on a pending expense: the words and the photo count.

    Money, project and category are not here: changing them after a PM has seen
    the entry would let an approved-looking number differ from the one approved.
    A wrong amount is rejected and sent again (§4.17.3).
    """

    class Meta:
        model = ProjectExpense
        fields = ("description", "scope_of_work", "photos_expected")


class DecideSerializer(serializers.Serializer):
    approved = serializers.BooleanField()
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True)


class ReverseSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=500)


class MarkPaidSerializer(serializers.Serializer):
    # Blank is accepted here so the service's own PAYMENT_REFERENCE_REQUIRED
    # (§4.17.12) is what the caller reads.
    payment_reference = serializers.CharField(
        max_length=100, required=False, allow_blank=True, default=""
    )
    paid_at = serializers.DateTimeField(required=False)


class CloseFloatSerializer(serializers.Serializer):
    returned_amount = serializers.DecimalField(max_digits=14, decimal_places=2)


class _EntryActions:
    """The actions the two entry viewsets share (decide, resubmit, mark-paid).

    A mixin rather than a base viewset so each keeps its own ``model``,
    serializer and permission map.
    """

    def _fresh(self, pk):  # type: ignore[no-untyped-def]
        """The row as the read payload wants it: annotated, not the service's copy."""
        return self.get_queryset().get(pk=pk)  # type: ignore[attr-defined]

    def _read(self, entry):  # type: ignore[no-untyped-def]
        return Response(self.get_serializer(self._fresh(entry.pk)).data)  # type: ignore[attr-defined]

    def _decide(self, request):  # type: ignore[no-untyped-def]
        serializer = DecideSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        entry = finance.decide(
            self.get_object(),  # type: ignore[attr-defined]
            actor=request.user,
            approved=serializer.validated_data["approved"],
            reason=serializer.validated_data.get("reason", ""),
            request=request,
        )
        return self._read(entry)

    def _resubmit(self, request):  # type: ignore[no-untyped-def]
        entry = finance.resubmit(
            self.get_object(),
            actor=request.user,
            request=request,  # type: ignore[attr-defined]
        )
        return self._read(entry)

    def _mark_paid(self, request):  # type: ignore[no-untyped-def]
        serializer = MarkPaidSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        entry = finance.mark_paid(
            self.get_object(),  # type: ignore[attr-defined]
            actor=request.user,
            reference=serializer.validated_data["payment_reference"],
            paid_at=serializer.validated_data.get("paid_at"),
            request=request,
        )
        return self._read(entry)

    def _pending(self, request, label: str):  # type: ignore[no-untyped-def]
        """Entries whose next open level is addressed to the caller (§4.17.6).

        The same definition as ``/approvals/pending``, so a PM sees what waits on
        them and Finance what waits on Finance, never each other's.
        """
        addressed = open_requests_addressed_to(
            request.user,
            ApprovalRequest.objects.filter(document_type=label, document_id=_id_key()),
        )
        queryset = self.filter_queryset(  # type: ignore[attr-defined]
            self.get_queryset().filter(Exists(addressed))  # type: ignore[attr-defined]
        )
        page = self.paginate_queryset(queryset)  # type: ignore[attr-defined]
        serializer = self.get_serializer(page if page is not None else queryset, many=True)  # type: ignore[attr-defined]
        if page is not None:
            return self.get_paginated_response(serializer.data)  # type: ignore[attr-defined]
        return Response(serializer.data)


class ProjectExpenseViewSet(_EntryActions, TenantScopedViewSet):
    """``/api/v1/project-expenses`` (O16, D29, R1, R4).

    Anyone may record one — a technician at a fuel station is closer to the fact
    than anybody back at the yard. Two levels decide it (§4.17.3).
    """

    serializer_class = ProjectExpenseSerializer
    model = ProjectExpense
    select_related = ("project", "site", "job", "category", "recorded_by")
    prefetch_related = ("casual_lines", "casual_lines__casual")
    required_permissions = {"mark_paid": PERM.FINANCE_APPROVE}
    filterset_fields = ["project", "job", "status", "category", "float_request"]
    search_fields = ["description", "scope_of_work", "category__name"]
    ordering_fields = ["incurred_on", "amount", "created_at"]

    # No destroy: an expense is rejected or reversed, never removed (O16). The
    # model refuses it too, so a route offering it would only produce a 500.
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):  # type: ignore[no-untyped-def]
        label = ProjectExpense._meta.label
        return (
            super()
            .get_queryset()
            .annotate(
                has_evidence=Exists(
                    Attachment.objects.filter(target_type=label, target_id=_id_key())
                ),
                **_routing_annotations(label),
            )
        )

    def filter_queryset(self, queryset):  # type: ignore[no-untyped-def]
        queryset = _visible_to(super().filter_queryset(queryset), self.request.user)
        params = self.request.query_params
        if _truthy(params.get("mine")):
            queryset = queryset.filter(recorded_by=self.request.user)
        if _truthy(params.get("payable")):
            _require_finance(self.request)
            # A float-backed expense was paid out of the float (§4.17.2), and a
            # reversal is a correction, not a debt.
            queryset = queryset.filter(
                status=ExpenseStatus.APPROVED,
                float_request__isnull=True,
                reverses__isnull=True,
            )
        return queryset

    @extend_schema(parameters=[MINE, PAYABLE], responses={200: ProjectExpenseSerializer(many=True)})
    def list(self, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        return super().list(request, *args, **kwargs)

    @extend_schema(request=ProjectExpenseSerializer, responses={201: ProjectExpenseSerializer})
    def create(self, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        serializer = ProjectExpenseSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        expense = serializer.save()
        return Response(self.get_serializer(self._fresh(expense.pk)).data, status=201)

    @extend_schema(request=ExpenseEditSerializer, responses={200: ProjectExpenseSerializer})
    def partial_update(self, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        """Only the recorder, and only until the PM has answered (§4.17.6)."""
        expense = self.get_object()
        if expense.recorded_by_id != request.user.pk:
            raise PermissionDeniedError("Only the person who recorded this can change it.")
        if expense.status != ExpenseStatus.PENDING_PM:
            raise finance.FinanceNotDecidable(
                "This can no longer be edited. Reject and send it again to change it."
            )
        serializer = ExpenseEditSerializer(expense, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return self._read(expense)

    @extend_schema(request=DecideSerializer, responses={200: ProjectExpenseSerializer})
    @action(detail=True, methods=["post"])
    def decide(self, request, pk=None):  # type: ignore[no-untyped-def]
        """O16: it reaches project cost only on Finance's approval (R4)."""
        return self._decide(request)

    @extend_schema(request=None, responses={200: ProjectExpenseSerializer})
    @action(detail=True, methods=["post"])
    def resubmit(self, request, pk=None):  # type: ignore[no-untyped-def]
        return self._resubmit(request)

    @extend_schema(request=MarkPaidSerializer, responses={200: ProjectExpenseSerializer})
    @action(detail=True, methods=["post"], url_path="mark-paid")
    def mark_paid(self, request, pk=None):  # type: ignore[no-untyped-def]
        return self._mark_paid(request)

    @extend_schema(request=ReverseSerializer, responses={201: ProjectExpenseSerializer})
    @action(detail=True, methods=["post"])
    def reverse(self, request, pk=None):  # type: ignore[no-untyped-def]
        """O16: corrected by its opposite, never by an edit."""
        serializer = ReverseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reversal = reverse_expense(
            self.get_object(),
            actor=request.user,
            reason=serializer.validated_data["reason"],
            request=request,
        )
        return Response(self.get_serializer(self._fresh(reversal.pk)).data, status=201)

    @extend_schema(parameters=[MINE], responses={200: ProjectExpenseSerializer(many=True)})
    @action(detail=False, methods=["get"])
    def pending(self, request):  # type: ignore[no-untyped-def]
        """What is waiting on this person, at their level (§4.17.6)."""
        return self._pending(request, ProjectExpense._meta.label)


# --------------------------------------------------------------------------
# Allowance requests (R2, R5)
# --------------------------------------------------------------------------

_MONEY: DecimalField = DecimalField(max_digits=14, decimal_places=2)


class AllowanceRequestSerializer(serializers.ModelSerializer):
    project_reference = serializers.CharField(source="project.__str__", read_only=True)
    site_name = serializers.CharField(source="site.name", read_only=True, default="")
    recorded_by_name = serializers.CharField(source="recorded_by.full_name", read_only=True)
    transport_scope = serializers.ChoiceField(
        choices=TransportScope.choices, required=False, allow_null=True, allow_blank=True
    )
    days = serializers.IntegerField(read_only=True)
    daily_amount = serializers.SerializerMethodField()
    returned_amount = serializers.SerializerMethodField()
    open_float_warning = serializers.SerializerMethodField()
    pm_level_skipped = serializers.SerializerMethodField()
    spent = serializers.SerializerMethodField()
    balance = serializers.SerializerMethodField()

    class Meta:
        model = AllowanceRequest
        fields = (
            "id",
            "number",
            "type",
            "transport_scope",
            "amount",
            "from_date",
            "to_date",
            "days",
            "daily_amount",
            "site",
            "site_name",
            "project",
            "project_reference",
            "reason",
            "recorded_by",
            "recorded_by_name",
            "status",
            "decided_by",
            "decided_at",
            "decision_reason",
            "paid_at",
            "paid_by",
            "payment_reference",
            "closed_at",
            "closed_by",
            "returned_amount",
            "client_uuid",
            "open_float_warning",
            "pm_level_skipped",
            "spent",
            "balance",
            "created_at",
        )
        read_only_fields = (
            "number",
            "recorded_by",
            "status",
            "decided_by",
            "decided_at",
            "decision_reason",
            "paid_at",
            "paid_by",
            "payment_reference",
            "closed_at",
            "closed_by",
        )
        validators: list[Any] = []
        extra_kwargs = {
            "project": {"required": False, "allow_null": True},
            "site": {"required": False, "allow_null": True},
            "client_uuid": {"required": False, "allow_null": True},
        }

    # -- reads ---------------------------------------------------------------

    def to_representation(self, instance):  # type: ignore[no-untyped-def]
        data = super().to_representation(instance)
        # "" is how the column says "no scope"; a client reads null.
        if not data.get("transport_scope"):
            data["transport_scope"] = None
        # Only a float has a ledger. Absent rather than null: the field means
        # something only when it is there.
        if instance.type != AllowanceType.FLOAT:
            data.pop("spent", None)
            data.pop("balance", None)
        return data

    def get_daily_amount(self, request: AllowanceRequest) -> str:
        daily = request.amount / Decimal(request.days)
        return str(daily.quantize(Decimal("0.01")))

    def get_returned_amount(self, request: AllowanceRequest) -> str | None:
        # The column defaults to 0.00; only a closed float has "returned" anything.
        return str(request.returned_amount) if request.closed_at is not None else None

    def _spent(self, request: AllowanceRequest) -> Decimal:
        annotated = getattr(request, "spent_total", None)
        return finance.float_spent(request) if annotated is None else annotated

    def get_spent(self, request: AllowanceRequest) -> str:
        return str(self._spent(request))

    def get_balance(self, request: AllowanceRequest) -> str:
        return str(
            finance_rules.float_balance(
                request.amount, self._spent(request), request.returned_amount
            )
        )

    def get_pm_level_skipped(self, request: AllowanceRequest) -> bool:
        return _pm_level_skipped(request)

    def get_open_float_warning(self, request: AllowanceRequest) -> dict[str, str] | None:
        """Another PAID, unclosed float of the same person (R2). Shown to
        approvers while the request is open; it never blocks."""
        if request.status not in finance.PENDING_STATUSES:
            return None
        cache: dict[int, list[AllowanceRequest]] = self.context.setdefault("_open_floats", {})
        if request.recorded_by_id not in cache:
            cache[request.recorded_by_id] = list(
                AllowanceRequest.objects.filter(
                    recorded_by_id=request.recorded_by_id,
                    type=AllowanceType.FLOAT,
                    status=ExpenseStatus.PAID,
                    closed_at__isnull=True,
                )
                .annotate(spent_total=_float_spent_expression())
                .order_by("number")
            )
        for other in cache[request.recorded_by_id]:
            if other.pk != request.pk:
                return {"number": other.number, "balance": self.get_balance(other)}
        return None

    # -- write ---------------------------------------------------------------

    def create(self, validated_data):  # type: ignore[no-untyped-def]
        request = self.context["request"]
        return finance.request_allowance(
            actor=request.user,
            type=validated_data["type"],
            amount=validated_data["amount"],
            from_date=validated_data["from_date"],
            to_date=validated_data["to_date"],
            site=validated_data.get("site"),
            project=validated_data.get("project"),
            reason=validated_data.get("reason", ""),
            transport_scope=validated_data.get("transport_scope") or "",
            client_uuid=validated_data.get("client_uuid"),
            request=request,
        )


def _float_spent_expression() -> Any:
    """Expenses charged to a float and not rejected, as a per-row subquery (R2)."""
    spent = (
        ProjectExpense.objects.filter(float_request=OuterRef("pk"))
        .exclude(status=ExpenseStatus.REJECTED)
        .order_by()
        .values("float_request")
        .annotate(total=Sum("amount"))
        .values("total")
    )
    return Coalesce(
        Subquery(spent, output_field=_MONEY),
        Value(Decimal("0.00"), output_field=_MONEY),
    )


class AllowanceRequestViewSet(_EntryActions, TenantScopedViewSet):
    """``/api/v1/allowance-requests`` (R2, R4, R5).

    No PATCH: a wrong amount or date is rejected and sent again, so the R5
    overlap and limit rules run on what is finally approved.
    """

    serializer_class = AllowanceRequestSerializer
    model = AllowanceRequest
    select_related = ("project", "site", "recorded_by")
    required_permissions = {
        "mark_paid": PERM.FINANCE_APPROVE,
        "close_float": PERM.FINANCE_APPROVE,
    }
    filterset_fields = ["type", "status", "project", "site"]
    search_fields = ["number", "reason"]
    ordering_fields = ["from_date", "amount", "created_at"]
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):  # type: ignore[no-untyped-def]
        return (
            super()
            .get_queryset()
            .annotate(
                spent_total=_float_spent_expression(),
                **_routing_annotations(AllowanceRequest._meta.label),
            )
        )

    def filter_queryset(self, queryset):  # type: ignore[no-untyped-def]
        queryset = _visible_to(super().filter_queryset(queryset), self.request.user)
        params = self.request.query_params
        if _truthy(params.get("mine")):
            queryset = queryset.filter(recorded_by=self.request.user)
        if _truthy(params.get("payable")):
            _require_finance(self.request)
            queryset = queryset.filter(status=ExpenseStatus.APPROVED)
        return queryset

    @extend_schema(
        parameters=[MINE, PAYABLE], responses={200: AllowanceRequestSerializer(many=True)}
    )
    def list(self, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        return super().list(request, *args, **kwargs)

    @extend_schema(request=AllowanceRequestSerializer, responses={201: AllowanceRequestSerializer})
    def create(self, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        serializer = AllowanceRequestSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        allowance = serializer.save()
        return Response(self.get_serializer(self._fresh(allowance.pk)).data, status=201)

    @extend_schema(request=DecideSerializer, responses={200: AllowanceRequestSerializer})
    @action(detail=True, methods=["post"])
    def decide(self, request, pk=None):  # type: ignore[no-untyped-def]
        return self._decide(request)

    @extend_schema(request=None, responses={200: AllowanceRequestSerializer})
    @action(detail=True, methods=["post"])
    def resubmit(self, request, pk=None):  # type: ignore[no-untyped-def]
        return self._resubmit(request)

    @extend_schema(request=MarkPaidSerializer, responses={200: AllowanceRequestSerializer})
    @action(detail=True, methods=["post"], url_path="mark-paid")
    def mark_paid(self, request, pk=None):  # type: ignore[no-untyped-def]
        return self._mark_paid(request)

    @extend_schema(request=CloseFloatSerializer, responses={200: AllowanceRequestSerializer})
    @action(detail=True, methods=["post"], url_path="close-float")
    def close_float(self, request, pk=None):  # type: ignore[no-untyped-def]
        serializer = CloseFloatSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        float_request = finance.close_float(
            self.get_object(),
            actor=request.user,
            returned_amount=serializer.validated_data["returned_amount"],
            request=request,
        )
        return self._read(float_request)

    @extend_schema(parameters=[MINE], responses={200: AllowanceRequestSerializer(many=True)})
    @action(detail=False, methods=["get"])
    def pending(self, request):  # type: ignore[no-untyped-def]
        return self._pending(request, AllowanceRequest._meta.label)


# --------------------------------------------------------------------------
# Settings (R4, R5)
# --------------------------------------------------------------------------

LIMIT_KEYS = (
    "TRANSPORT_WITHIN_NAIROBI",
    "TRANSPORT_OUTSIDE_NAIROBI",
    "NIGHT_OUT",
    "TEAM_ALLOWANCE",
)


def validate_allowance_limits(value: Any) -> dict[str, dict[str, str | None]]:
    """Exactly the four keys, each ``{min, max}`` of a decimal string or null,
    with ``min <= max`` (§4.17.2).

    Strict about shape because ``finance_rules`` reads this JSON on every
    request: a typo'd key would be silently "no limit".
    """
    if not isinstance(value, dict):
        raise serializers.ValidationError("Limits must be an object keyed by type.")
    unknown = set(value) - set(LIMIT_KEYS)
    missing = set(LIMIT_KEYS) - set(value)
    if unknown or missing:
        raise serializers.ValidationError(
            f"Limits need exactly these keys: {', '.join(LIMIT_KEYS)}."
            + (f" Unknown: {', '.join(sorted(unknown))}." if unknown else "")
            + (f" Missing: {', '.join(sorted(missing))}." if missing else "")
        )

    cleaned: dict[str, dict[str, str | None]] = {}
    for key in LIMIT_KEYS:
        entry = value[key]
        if not isinstance(entry, dict) or set(entry) != {"min", "max"}:
            raise serializers.ValidationError(f"{key} needs a min and a max (or null).")
        bounds: dict[str, Decimal | None] = {}
        for side in ("min", "max"):
            raw = entry[side]
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                bounds[side] = None
                continue
            if isinstance(raw, bool) or not isinstance(raw, (str, int)):
                raise serializers.ValidationError(
                    f"{key} {side} must be an amount in quotes, or null."
                )
            try:
                number = Decimal(str(raw).strip())
            except InvalidOperation:
                raise serializers.ValidationError(f"{key} {side} is not an amount.") from None
            if not number.is_finite() or number < 0:
                raise serializers.ValidationError(f"{key} {side} must be 0 or more.")
            bounds[side] = number
        low, high = bounds["min"], bounds["max"]
        if low is not None and high is not None and low > high:
            raise serializers.ValidationError(f"{key}: the minimum is above the maximum.")
        cleaned[key] = {
            side: None if number is None else format(number.normalize(), "f")
            for side, number in bounds.items()
        }
    return cleaned


def _settings_serializer(name: str):  # type: ignore[no-untyped-def]
    return inline_serializer(
        name,
        {
            "allowance_limits": serializers.DictField(),
            "finance_director_role": serializers.IntegerField(allow_null=True),
        },
    )


class FinanceSettingsView(APIView):
    """``/api/v1/finance/settings`` (R4, R5).

    Read by any member (the phone runs the same limits for early warnings,
    §4.17.8). Written by Finance or a settings manager.
    """

    permission_classes = [IsAuthenticated, OrganizationIsActive, HasPermission]

    @staticmethod
    def _settings(request):  # type: ignore[no-untyped-def]
        return Organization.objects.get(pk=request.user.organization_id).settings

    @staticmethod
    def _payload(settings_object) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        return {
            "allowance_limits": settings_object.allowance_limits,
            "finance_director_role": settings_object.finance_director_role_id,
        }

    @extend_schema(responses={200: _settings_serializer("FinanceSettings")})
    def get(self, request):  # type: ignore[no-untyped-def]
        return Response(self._payload(self._settings(request)))

    @extend_schema(
        request=inline_serializer(
            "FinanceSettingsUpdate",
            {
                "allowance_limits": serializers.DictField(required=False),
                "finance_director_role": serializers.IntegerField(required=False, allow_null=True),
            },
        ),
        responses={200: _settings_serializer("FinanceSettingsRead")},
    )
    def patch(self, request):  # type: ignore[no-untyped-def]
        from accounts.models import Role

        held = resolve_permissions(request.user)
        if not (held.has(PERM.FINANCE_APPROVE) or held.has(PERM.SETTINGS_MANAGE)):
            raise PermissionDenied("Only Finance or a settings manager can change these.")

        settings_object = self._settings(request)
        changes: dict[str, Any] = {}
        errors: dict[str, list[str]] = {}

        if "allowance_limits" in request.data:
            try:
                changes["allowance_limits"] = validate_allowance_limits(
                    request.data["allowance_limits"]
                )
            except serializers.ValidationError as exc:
                errors["allowance_limits"] = [str(item) for item in exc.detail]
        if "finance_director_role" in request.data:
            role_id = request.data["finance_director_role"]
            if role_id is None:
                changes["finance_director_role"] = None
            else:
                role = (
                    Role.objects.filter(pk=role_id).first()
                    if isinstance(role_id, int) and not isinstance(role_id, bool)
                    else None
                )
                if role is None:
                    errors["finance_director_role"] = ["Choose one of this organization's roles."]
                else:
                    changes["finance_director_role"] = role
        if errors:
            raise serializers.ValidationError(errors)

        for field, value in changes.items():
            setattr(settings_object, field, value)
        settings_object.save(update_fields=list(changes) or None)

        record(
            AuditAction.SETTINGS_CHANGED,
            actor=request.user,
            organization=request.user.organization_id,
            target=settings_object,
            target_label="Finance settings",
            request=request,
            after={
                "allowance_limits": settings_object.allowance_limits,
                "finance_director_role": settings_object.finance_director_role_id,
            },
            note="Finance settings changed.",
        )
        return Response(self._payload(settings_object))
