"""Client, site and project endpoints (design §6; C5, C6, C7)."""

from __future__ import annotations

from django.db.models import Q
from django_filters import rest_framework as filters
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.response import Response

from accounts.permissions_registry import PERM
from commercials.visibility import may_see_project_cost
from core.api import TenantScopedViewSet
from core.field_permissions import PermissionGatedFieldsMixin
from network.models import (
    Client,
    Project,
    ProjectStatus,
    ProjectVariation,
    Site,
    SiteReference,
    Subcontractor,
    VariationStatus,
)


class ClientSerializer(serializers.ModelSerializer):
    site_count = serializers.SerializerMethodField()

    class Meta:
        model = Client
        fields = (
            "id",
            "name",
            "code",
            "contact_name",
            "contact_email",
            "contact_phone",
            "site_code_pattern",
            "is_active",
            "site_count",
        )

    def get_site_count(self, client: Client) -> int:
        return client.sites.count()


class SiteReferenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = SiteReference
        fields = ("id", "site", "label", "value")


class SiteSerializer(serializers.ModelSerializer):
    references = SiteReferenceSerializer(many=True, read_only=True)
    client_name = serializers.CharField(source="client.name", read_only=True)

    class Meta:
        model = Site
        fields = (
            "id",
            "client",
            "client_name",
            "internal_ref",
            "name",
            "region",
            "county",
            "latitude",
            "longitude",
            "site_type",
            "status",
            "cell_id",
            "enodeb_id",
            "notes",
            "references",
        )


class ProjectSerializer(PermissionGatedFieldsMixin, serializers.ModelSerializer):
    # O14: two permissions, two different audiences. A manager works to a
    # budget and needs to see what they have spent against it; the contract
    # value behind it, and therefore the margin, is the owner's business.
    #
    # Withheld at the serializer, not hidden in the interface: a field the API
    # still returns is not restricted, it is merely out of sight (§10).
    permission_gated_fields = {
        PERM.PROJECT_VIEW_COST: ("cost_budget", "current_cost_budget"),
        PERM.PROJECT_VIEW_MARGIN: ("contract_value", "current_contract_value"),
    }

    client_name = serializers.CharField(source="client.name", read_only=True)
    manager_name = serializers.CharField(source="manager.get_full_name", read_only=True)
    # O2: what the award is worth now. `contract_value` keeps saying what was
    # first agreed, which is what a dispute is usually about (D21).
    current_contract_value = serializers.DecimalField(
        max_digits=14, decimal_places=2, read_only=True
    )
    current_cost_budget = serializers.DecimalField(
        max_digits=14, decimal_places=2, read_only=True
    )
    site_count = serializers.SerializerMethodField()

    class Meta:
        model = Project
        fields = (
            "id",
            "client",
            "client_name",
            "reference",
            "po_number",
            "title",
            "description",
            "manager",
            "manager_name",
            "contract_value",
            "current_contract_value",
            "cost_budget",
            "current_cost_budget",
            "starts_on",
            "target_completion_on",
            "sites",
            "site_count",
            "status",
            "opened_at",
            "closed_at",
            "closed_with_unreconciled",
            "close_reason",
        )
        read_only_fields = ("status", "opened_at", "closed_at", "closed_with_unreconciled")

    def get_site_count(self, project: Project) -> int:
        return project.sites.count()

    def to_representation(self, instance):  # type: ignore[no-untyped-def]
        data = super().to_representation(instance)
        # O14 scopes a manager to **their own** projects, which a permission
        # alone cannot express — see `may_see_project_cost`.
        if not may_see_project_cost(self.context.get("request"), instance):
            data.pop("cost_budget", None)
            data.pop("current_cost_budget", None)
        return data

    def validate(self, attrs: dict) -> dict:
        """Say what the check constraint would say, before it says it (O1).

        The database refuses a half-specified PO either way (``Meta.constraints``).
        Reaching it produces an IntegrityError and a 500; this turns the same
        refusal into a field error the form can point at.
        """
        merged = {**({} if self.instance is None else {
            "po_number": self.instance.po_number,
            "manager": self.instance.manager,
            "contract_value": self.instance.contract_value,
            "cost_budget": self.instance.cost_budget,
        }), **attrs}
        if not merged.get("po_number"):
            return attrs
        missing = {
            name: "Required once the project carries a PO number."
            for name in ("manager", "contract_value", "cost_budget")
            if merged.get(name) is None
        }
        if missing:
            raise serializers.ValidationError(missing)
        return attrs


class ProjectVariationSerializer(serializers.ModelSerializer):
    project_reference = serializers.CharField(source="project.__str__", read_only=True)
    raised_by_name = serializers.CharField(source="raised_by.get_full_name", read_only=True)

    class Meta:
        model = ProjectVariation
        fields = (
            "id",
            "project",
            "project_reference",
            "reference",
            "description",
            "value_delta",
            "budget_delta",
            "effective_on",
            "raised_by",
            "raised_by_name",
            "status",
            "decided_by",
            "decided_at",
            "decision_reason",
        )
        read_only_fields = ("raised_by", "status", "decided_by", "decided_at")

    def validate_project(self, project: Project) -> Project:
        """O2: a variation amends a live award, not a finished one (O13)."""
        if project.status != ProjectStatus.OPEN:
            raise serializers.ValidationError(
                "This project is closed. Its figures are final (O13)."
            )
        if not project.is_po:
            raise serializers.ValidationError(
                "Only a project carrying a purchase order has a value to vary (O1)."
            )
        return project


class SubcontractorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Subcontractor
        fields = (
            "id",
            "name",
            "code",
            "contact_name",
            "contact_email",
            "contact_phone",
            "notes",
            "is_active",
        )


class ClientViewSet(TenantScopedViewSet):
    """``/api/v1/clients`` (C5)."""

    serializer_class = ClientSerializer
    model = Client
    required_permissions = {
        "create": PERM.CATALOGUE_MANAGE,
        "update": PERM.CATALOGUE_MANAGE,
        "partial_update": PERM.CATALOGUE_MANAGE,
        "destroy": PERM.CATALOGUE_MANAGE,
    }
    filterset_fields = ["is_active"]
    search_fields = ["name", "code"]
    ordering_fields = ["name"]


class SiteFilter(filters.FilterSet):
    """C6: one search box that resolves any of a site's references."""

    client = filters.NumberFilter(field_name="client")
    status = filters.CharFilter(field_name="status")
    region = filters.CharFilter(field_name="region", lookup_expr="iexact")
    # The search a storekeeper actually performs: they have a code off a work
    # order and no idea which system it came from.
    reference = filters.CharFilter(method="filter_any_reference")

    class Meta:
        model = Site
        fields = ["client", "status", "region"]

    def filter_any_reference(self, queryset, name, value):  # type: ignore[no-untyped-def]
        return queryset.filter(
            Q(internal_ref__iexact=value) | Q(references__value__iexact=value)
        ).distinct()


class SiteViewSet(TenantScopedViewSet):
    """``/api/v1/sites`` (C6).

    Deletion is not offered: C6 keeps decommissioned sites permanently, because
    recoveries originate from them (D5).
    """

    serializer_class = SiteSerializer
    model = Site
    select_related = ("client",)
    prefetch_related = ("references",)
    filterset_class = SiteFilter
    search_fields = ["internal_ref", "name", "references__value", "cell_id", "enodeb_id"]
    ordering_fields = ["internal_ref", "name", "created_at"]
    http_method_names = ["get", "post", "patch", "head", "options"]
    required_permissions = {
        "create": PERM.CATALOGUE_MANAGE,
        "update": PERM.CATALOGUE_MANAGE,
        "partial_update": PERM.CATALOGUE_MANAGE,
        "add_reference": PERM.CATALOGUE_MANAGE,
        "decommission": PERM.CATALOGUE_MANAGE,
    }

    @action(detail=True, methods=["get", "post"], url_path="references")
    def add_reference(self, request, pk=None):  # type: ignore[no-untyped-def]
        """List or add labelled references for a site (C6)."""
        site = self.get_object()

        if request.method == "GET":
            return Response(SiteReferenceSerializer(site.references.all(), many=True).data)

        serializer = SiteReferenceSerializer(data={**request.data, "site": site.pk})
        serializer.is_valid(raise_exception=True)
        serializer.save(organization_id=site.organization_id)
        return Response(serializer.data, status=201)

    @action(detail=True, methods=["post"])
    def decommission(self, request, pk=None):  # type: ignore[no-untyped-def]
        """C6: mark a site decommissioned. It stays in the register."""
        from network.models import SiteStatus

        site = self.get_object()
        site.status = SiteStatus.DECOMMISSIONED
        site.save(update_fields=["status"])
        return Response(self.get_serializer(site).data)


class ProjectVariationViewSet(TenantScopedViewSet):
    """``/api/v1/project-variations`` (O2).

    Decided variations are append-only, so there is no update action worth
    offering once the owner has ruled: the model refuses it, and a route that
    looked writable would only produce a confusing 400.
    """

    serializer_class = ProjectVariationSerializer
    model = ProjectVariation
    select_related = ("project", "raised_by")
    required_permissions = {
        "create": PERM.CATALOGUE_MANAGE,
        "update": PERM.CATALOGUE_MANAGE,
        "partial_update": PERM.CATALOGUE_MANAGE,
        "destroy": PERM.CATALOGUE_MANAGE,
        "approve": PERM.PROJECT_VARIATION_APPROVE,
        "reject": PERM.PROJECT_VARIATION_APPROVE,
    }
    filterset_fields = ["project", "status"]
    search_fields = ["reference", "description"]
    ordering_fields = ["effective_on", "created_at"]

    def perform_create(self, serializer):  # type: ignore[no-untyped-def]
        serializer.save(raised_by=self.request.user)

    def _decide(self, request, decision: str, reason_required: bool):  # type: ignore[no-untyped-def]
        from django.utils import timezone

        variation = self.get_object()
        if variation.status != VariationStatus.PENDING:
            return Response(
                {"detail": "This variation has already been decided (O2)."}, status=409
            )

        reason = request.data.get("reason", "")
        if reason_required and not reason:
            return Response({"reason": "A reason is required."}, status=400)

        variation.status = decision
        variation.decided_by = request.user
        variation.decided_at = timezone.now()
        variation.decision_reason = reason
        variation.save()
        return Response(self.get_serializer(variation).data)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):  # type: ignore[no-untyped-def]
        """O2: the owner's decision. It moves the project's current value."""
        return self._decide(request, VariationStatus.APPROVED, reason_required=False)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):  # type: ignore[no-untyped-def]
        """Rejection needs a reason, as every other rejection here does (F4)."""
        return self._decide(request, VariationStatus.REJECTED, reason_required=True)


class SubcontractorViewSet(TenantScopedViewSet):
    """``/api/v1/subcontractors`` (O4).

    No destroy action. A contractor referenced by a job is part of that
    project's cost for as long as the record is worth anything, and the
    database refuses the delete anyway (``PROTECT`` on ``Job.subcontractor``).
    Deactivating is how one leaves the list.
    """

    serializer_class = SubcontractorSerializer
    model = Subcontractor
    required_permissions = {
        "create": PERM.CATALOGUE_MANAGE,
        "update": PERM.CATALOGUE_MANAGE,
        "partial_update": PERM.CATALOGUE_MANAGE,
        "destroy": PERM.CATALOGUE_MANAGE,
    }
    filterset_fields = ["is_active"]
    search_fields = ["name", "code", "contact_name"]
    ordering_fields = ["name"]


class SiteReferenceViewSet(TenantScopedViewSet):
    """``/api/v1/site-references`` (C6)."""

    serializer_class = SiteReferenceSerializer
    model = SiteReference
    select_related = ("site", "site__client")
    required_permissions = {
        "create": PERM.CATALOGUE_MANAGE,
        "update": PERM.CATALOGUE_MANAGE,
        "partial_update": PERM.CATALOGUE_MANAGE,
        "destroy": PERM.CATALOGUE_MANAGE,
    }
    filterset_fields = ["site", "label"]
    search_fields = ["value", "label"]


class ProjectViewSet(TenantScopedViewSet):
    """``/api/v1/projects`` (C7, D14 — optional throughout)."""

    serializer_class = ProjectSerializer
    model = Project
    select_related = ("client",)
    prefetch_related = ("sites",)
    required_permissions = {
        "create": PERM.CATALOGUE_MANAGE,
        "update": PERM.CATALOGUE_MANAGE,
        "partial_update": PERM.CATALOGUE_MANAGE,
        "destroy": PERM.CATALOGUE_MANAGE,
        # O13: closing is the manager's — `close_project` checks that it is
        # *their* project. Reopening is the owner's.
        "close": PERM.PROJECT_VIEW_COST,
        "reopen": PERM.PROJECT_VIEW_MARGIN,
    }
    filterset_fields = ["client", "status"]
    search_fields = ["reference", "description"]
    ordering_fields = ["opened_at", "reference"]

    @action(detail=True, methods=["get"])
    def performance(self, request, pk=None):  # type: ignore[no-untyped-def]
        """O12: cost against value. Figures the caller may not see are absent."""
        from commercials.costing import performance_for
        from commercials.serializers import ProjectPerformanceSerializer

        result = performance_for(self.get_object())
        return Response(
            ProjectPerformanceSerializer(result, context={"request": request}).data
        )

    @action(detail=True, methods=["get"])
    def unreconciled(self, request, pk=None):  # type: ignore[no-untyped-def]
        """What remains unaccounted for (C7). Real figures arrive with T5.7."""
        return Response(self.get_object().unreconciled_summary())

    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):  # type: ignore[no-untyped-def]
        """Close a project and freeze what it reported (C7, O13).

        Still a warning rather than a block on unreconciled material — C7 says
        "closing it warns", and blocking is H5's rule for jobs. What O13 adds is
        that closing with open jobs or unreconciled material needs a **reason**,
        and that the figures are snapshotted so a later reversal cannot move
        them under the people who signed them off.
        """
        from commercials.closing import close_project

        project, summary = close_project(
            self.get_object(),
            actor=request.user,
            reason=request.data.get("reason", ""),
            request=request,
        )
        return Response(
            {**self.get_serializer(project).data, "unreconciled_warning": summary}
        )

    @action(detail=True, methods=["post"])
    def reopen(self, request, pk=None):  # type: ignore[no-untyped-def]
        """O13: an owner's action, and recorded."""
        from commercials.closing import reopen_project

        project = reopen_project(
            self.get_object(),
            actor=request.user,
            reason=request.data.get("reason", ""),
            request=request,
        )
        return Response(self.get_serializer(project).data)
