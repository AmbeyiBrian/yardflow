"""Expense endpoints (§6; O16, D29)."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.response import Response

from accounts.permissions_registry import PERM
from commercials.models import ExpenseCategory, ExpenseStatus, ProjectExpense
from commercials.services import decide_expense, reverse_expense
from core.api import TenantScopedViewSet


class ExpenseCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ExpenseCategory
        fields = ("id", "name", "code", "is_active")


class ProjectExpenseSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source="category.name", read_only=True)
    project_reference = serializers.CharField(source="project.__str__", read_only=True)
    recorded_by_name = serializers.CharField(
        source="recorded_by.full_name", read_only=True
    )
    is_reversal = serializers.SerializerMethodField()

    class Meta:
        model = ProjectExpense
        fields = (
            "id",
            "project",
            "project_reference",
            "job",
            "category",
            "category_name",
            "amount",
            "incurred_on",
            "description",
            "recorded_by",
            "recorded_by_name",
            "status",
            "decided_by",
            "decided_at",
            "decision_reason",
            "reverses",
            "is_reversal",
            "created_at",
        )
        read_only_fields = (
            "recorded_by",
            "status",
            "decided_by",
            "decided_at",
            "decision_reason",
            "reverses",
        )

    def get_is_reversal(self, expense: ProjectExpense) -> bool:
        return bool(expense.reverses_id)

    def validate_project(self, project):  # type: ignore[no-untyped-def]
        from network.models import ProjectStatus

        if project.status != ProjectStatus.OPEN:
            raise serializers.ValidationError(
                "This project is closed. Its figures are final (O13)."
            )
        if project.manager_id is None:
            raise serializers.ValidationError(
                "This project has no manager, so there is nobody to approve an "
                "expense against it (O16)."
            )
        return project

    def validate(self, attrs: dict) -> dict:
        job = attrs.get("job")
        project = attrs.get("project") or getattr(self.instance, "project", None)
        if job is not None and project is not None and job.project_id != project.pk:
            raise serializers.ValidationError(
                {"job": "That job belongs to a different project."}
            )
        return attrs


class DecideExpenseSerializer(serializers.Serializer):
    approved = serializers.BooleanField()
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True)


class ReverseExpenseSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=500)


class ExpenseCategoryViewSet(TenantScopedViewSet):
    """``/api/v1/expense-categories`` (O16)."""

    serializer_class = ExpenseCategorySerializer
    model = ExpenseCategory
    required_permissions = {
        "create": PERM.CATALOGUE_MANAGE,
        "update": PERM.CATALOGUE_MANAGE,
        "partial_update": PERM.CATALOGUE_MANAGE,
        "destroy": PERM.CATALOGUE_MANAGE,
    }
    filterset_fields = ["is_active"]
    search_fields = ["name", "code"]


class ProjectExpenseViewSet(TenantScopedViewSet):
    """``/api/v1/project-expenses`` (O16, D29).

    Anyone may record one — a technician at a fuel station is closer to the fact
    than anybody back at the yard. The manager decides.
    """

    serializer_class = ProjectExpenseSerializer
    model = ProjectExpense
    select_related = ("project", "job", "category", "recorded_by")
    filterset_fields = ["project", "job", "status", "category"]
    search_fields = ["description"]
    ordering_fields = ["incurred_on", "amount", "created_at"]

    # No destroy: an expense is rejected or reversed, never removed (O16). The
    # model refuses it too, so a route offering it would only produce a 500.
    http_method_names = ["get", "post", "patch", "head", "options"]

    def perform_create(self, serializer):  # type: ignore[no-untyped-def]
        serializer.save(recorded_by=self.request.user)

    @extend_schema(
        request=DecideExpenseSerializer, responses={200: ProjectExpenseSerializer}
    )
    @action(detail=True, methods=["post"])
    def decide(self, request, pk=None):  # type: ignore[no-untyped-def]
        """O16: it reaches project cost only on approval."""
        serializer = DecideExpenseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        expense = decide_expense(
            self.get_object(),
            actor=request.user,
            approved=serializer.validated_data["approved"],
            reason=serializer.validated_data.get("reason", ""),
            request=request,
        )
        return Response(self.get_serializer(expense).data)

    @extend_schema(
        request=ReverseExpenseSerializer, responses={201: ProjectExpenseSerializer}
    )
    @action(detail=True, methods=["post"])
    def reverse(self, request, pk=None):  # type: ignore[no-untyped-def]
        """O16: corrected by its opposite, never by an edit."""
        serializer = ReverseExpenseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        reversal = reverse_expense(
            self.get_object(),
            actor=request.user,
            reason=serializer.validated_data["reason"],
            request=request,
        )
        return Response(self.get_serializer(reversal).data, status=201)

    @action(detail=False, methods=["get"])
    def pending(self, request):  # type: ignore[no-untyped-def]
        """What this manager has waiting on them (O16)."""
        queryset = self.filter_queryset(
            self.get_queryset().filter(
                status=ExpenseStatus.SUBMITTED, project__manager=request.user
            )
        )
        page = self.paginate_queryset(queryset)
        serializer = self.get_serializer(page or queryset, many=True)
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)
