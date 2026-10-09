"""Client, site and project endpoints (design §6; C5, C6, C7)."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone
from django_filters import rest_framework as filters
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from accounts.permissions_registry import PERM
from accounts.services import resolve_permissions
from commercials.milestones_api import MilestoneInputSerializer, MilestoneSerializer
from commercials.visibility import may_see_project_cost, may_see_project_margin
from core.api import TenantScopedViewSet
from core.field_permissions import PermissionGatedFieldsMixin
from network import suppliers as supplier_services
from network.models import (
    Client,
    Project,
    ProjectSite,
    ProjectStatus,
    ProjectVariation,
    Site,
    SiteReference,
    Subcontractor,
    Supplier,
    SupplierStatus,
    VariationStatus,
)
from network.project_sites import (
    is_accepted,
    set_project_sites,
    with_site_facts,
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
            "radius_m",
            "area_history",
            "site_type",
            "status",
            "cell_id",
            "enodeb_id",
            "notes",
            "references",
        )
        read_only_fields = ("area_history",)


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
    #: R12: how long the project worked without a PO. Only for a project that has
    #: none, and only for those who hold ``project.view_margin`` (§4.19.8).
    days_without_po = serializers.SerializerMethodField()
    # DRF makes a many-to-many with a through model read-only. It is declared
    # here so the form keeps writing it; `ProjectSite` rows carry the tenant.
    # The manager itself, not `.all()`: evaluated per request, when a tenant exists.
    sites = serializers.PrimaryKeyRelatedField(
        many=True, queryset=Site.objects, required=False
    )

    class Meta:
        model = Project
        fields = (
            "id",
            "client",
            "client_name",
            "reference",
            "po_number",
            "po_issue_date",
            "payment_terms",
            "payment_terms_days",
            "po_recorded_at",
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
            "days_without_po",
        )
        # M6: the reference is allocated from the tenant's PROJECT series, not
        # typed. The client's own name for the work is `po_number`.
        read_only_fields = (
            "reference",
            "status",
            "opened_at",
            "closed_at",
            "closed_with_unreconciled",
            "po_recorded_at",
        )

    def create(self, validated_data: dict) -> Project:
        sites = validated_data.pop("sites", [])
        project = super().create(validated_data)
        if project.po_number:
            # §4.19.8: a project created with its PO had it from the start.
            project.po_recorded_at = project.opened_at
            project.save(update_fields=["po_recorded_at", "updated_at"])
        set_project_sites(project, sites)
        return project

    def update(self, instance: Project, validated_data: dict) -> Project:
        sites = validated_data.pop("sites", None)
        had_po = bool(instance.po_number)
        project = super().update(instance, validated_data)
        if project.po_number and not had_po and project.po_recorded_at is None:
            project.po_recorded_at = timezone.now()
            project.save(update_fields=["po_recorded_at", "updated_at"])
        if sites is not None:
            set_project_sites(project, sites)
        return project

    def get_site_count(self, project: Project) -> int:
        return project.sites.count()

    def get_days_without_po(self, project: Project) -> int | None:
        if project.po_number or project.opened_at is None:
            return None
        return (timezone.now() - project.opened_at).days

    def to_representation(self, instance):  # type: ignore[no-untyped-def]
        data = super().to_representation(instance)
        # O14 scopes a manager to **their own** projects, which a permission
        # alone cannot express — see `may_see_project_cost`.
        if not may_see_project_cost(self.context.get("request"), instance):
            data.pop("cost_budget", None)
            data.pop("current_cost_budget", None)
        if not may_see_project_margin(self.context.get("request")):
            data.pop("days_without_po", None)
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


class AttachPoSerializer(serializers.Serializer):
    """``POST /projects/{id}/attach-po`` (R12, §4.19.8)."""

    po_number = serializers.CharField(max_length=100)
    po_issue_date = serializers.DateField()
    contract_value = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=0
    )
    cost_budget = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=0)
    payment_terms = serializers.CharField(max_length=500, required=False, allow_blank=True)
    payment_terms_days = serializers.IntegerField(
        required=False, allow_null=True, min_value=0, max_value=32767
    )
    manager = serializers.PrimaryKeyRelatedField(
        queryset=get_user_model().objects, required=False, allow_null=True
    )


class ProjectSiteSerializer(serializers.ModelSerializer):
    """A project's site with its dates and what the yard did for it (R10, §4.19.6).

    ``is_accepted``, ``first_collection_at`` and ``last_dispatch_at`` are
    derived and read-only; only the two typed dates can be written.
    """

    site_ref = serializers.CharField(source="site.internal_ref", read_only=True)
    site_name = serializers.CharField(source="site.name", read_only=True)
    is_accepted = serializers.SerializerMethodField()
    first_collection_at = serializers.DateTimeField(read_only=True, allow_null=True)
    last_dispatch_at = serializers.DateTimeField(read_only=True, allow_null=True)

    class Meta:
        model = ProjectSite
        fields = (
            "id",
            "project",
            "site",
            "site_ref",
            "site_name",
            "mobilised_on",
            "accepted_on",
            "is_accepted",
            "first_collection_at",
            "last_dispatch_at",
        )
        read_only_fields = ("project", "site")

    def get_is_accepted(self, row: ProjectSite) -> bool:
        return is_accepted(row.accepted_on, getattr(row, "has_certificate", False))


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


class ProjectFilter(filters.FilterSet):
    """R1: a site's open projects, for the money forms to choose from (§4.17.4)."""

    client = filters.NumberFilter(field_name="client")
    status = filters.CharFilter(field_name="status")
    # A method, not ``field_name="sites"``: resolving a relation at import time
    # would query the tenant manager before any organization is in context.
    site = filters.NumberFilter(method="filter_site")
    # R12 (§4.19.8): ``po=none`` is the dashboard's "working without a PO" list,
    # oldest first in the screen's eyes; ``po=any`` its complement.
    po = filters.ChoiceFilter(
        choices=(("none", "No PO yet"), ("any", "Has a PO")), method="filter_po"
    )
    has_po = filters.BooleanFilter(method="filter_has_po")

    class Meta:
        model = Project
        fields = ["client", "status"]

    def filter_site(self, queryset, name, value):  # type: ignore[no-untyped-def]
        return queryset.filter(sites=value)

    def filter_po(self, queryset, name, value):  # type: ignore[no-untyped-def]
        if value == "none":
            # The list of projects still without a PO is the owner's, because it
            # sits next to figures only they see (§4.19.10).
            request = self.request
            if request is not None and not resolve_permissions(request.user).has(
                PERM.PROJECT_VIEW_MARGIN
            ):
                raise PermissionDenied("Only the owner sees which projects have no PO.")
            return queryset.filter(po_number="")
        return queryset.exclude(po_number="")

    def filter_has_po(self, queryset, name, value):  # type: ignore[no-untyped-def]
        return self.filter_po(queryset, name, "any" if value else "none")


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
    filterset_class = ProjectFilter
    # Both references, and the title. The list shows all three, so a person
    # searching for the one they happen to know — usually the client's PO
    # number, because that is what is on the paperwork in front of them — has
    # to be able to find it.
    search_fields = ["reference", "po_number", "title", "description"]
    ordering_fields = ["opened_at", "reference"]

    def perform_update(self, serializer):  # type: ignore[no-untyped-def]
        """R4, D28: a new manager inherits the approvals waiting on the old one.

        Done in the same transaction as the change, so there is no moment when
        the project has its new manager and the requests still name the old.
        """
        from django.db import transaction

        from approvals.engine import readdress_project_requests

        with transaction.atomic():
            old_manager_id = serializer.instance.manager_id
            project = serializer.save()
            if project.manager_id != old_manager_id:
                readdress_project_requests(
                    project,
                    old_manager_id=old_manager_id,
                    actor=self.request.user,
                    request=self.request,
                )

    # --- R12, R11: the PO that arrives late, and its milestones (§4.19.7-8) ---

    @extend_schema(request=AttachPoSerializer, responses={200: ProjectSerializer})
    @action(detail=True, methods=["post"], url_path="attach-po")
    def attach_po(self, request, pk=None):  # type: ignore[no-untyped-def]
        """Add the PO to a project working without one, on the same row."""
        from commercials.po import attach_po

        project = self.get_object()
        # "This project's PM" is a person, not a permission, so it is checked here.
        if project.manager_id != request.user.pk and not resolve_permissions(
            request.user
        ).has(PERM.CATALOGUE_MANAGE):
            raise PermissionDenied(
                "Only this project's manager, or someone who manages the catalogue, "
                "can attach its PO."
            )
        body = AttachPoSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        project = attach_po(
            project,
            po_number=data["po_number"],
            po_issue_date=data["po_issue_date"],
            contract_value=data["contract_value"],
            cost_budget=data["cost_budget"],
            payment_terms=data.get("payment_terms", ""),
            payment_terms_days=data.get("payment_terms_days"),
            manager=data.get("manager"),
            actor=request.user,
            request=request,
        )
        return Response(self.get_serializer(project).data)

    @extend_schema(responses={200: MilestoneSerializer(many=True)}, request=None)
    @action(detail=True, methods=["get", "post"], url_path="milestones")
    def milestones(self, request, pk=None):  # type: ignore[no-untyped-def]
        """``GET`` the project's milestones with their states; ``POST`` adds one."""
        from commercials import milestones_api

        project = self.get_object()
        if request.method == "POST":
            return milestones_api.add_project_milestone(request, project)
        return milestones_api.list_project_milestones(request, project)

    @extend_schema(request=MilestoneInputSerializer, responses={200: MilestoneSerializer})
    @action(
        detail=True,
        methods=["patch", "delete"],
        url_path=r"milestones/(?P<milestone_id>\d+)",
    )
    def milestone_change(self, request, pk=None, milestone_id=None):  # type: ignore[no-untyped-def]
        from commercials import milestones_api

        return milestones_api.change_project_milestone(
            request, self.get_object(), int(milestone_id)
        )

    @extend_schema(request=None, responses={200: MilestoneSerializer(many=True)})
    @action(detail=True, methods=["post"], url_path="milestones/defaults")
    def milestone_defaults(self, request, pk=None):  # type: ignore[no-untyped-def]
        """Finance's "Add default milestones" (M1 Deposit, M2, M3)."""
        from commercials import milestones_api

        return milestones_api.add_default_project_milestones(request, self.get_object())

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
    def budget(self, request, pk=None):  # type: ignore[no-untyped-def]
        """R9: committed, spent and remaining (§4.19.5). Cost viewers only."""
        from commercials.budget import budget_position

        project = self.get_object()
        if not may_see_project_cost(request, project):
            raise PermissionDenied("You may not see this project's cost.")
        return Response(budget_position(project).as_dict())

    @action(detail=True, methods=["post"], url_path="budget-check")
    def budget_check(self, request, pk=None):  # type: ignore[no-untyped-def]
        """R9's early warning: ``{amount}`` gives ``{over, over_by?}``.

        Anyone who can see the project may ask; the overrun itself is only
        named to those who may see its cost.
        """
        from decimal import Decimal, InvalidOperation

        from commercials.budget import would_exceed

        project = self.get_object()
        try:
            amount = Decimal(str(request.data.get("amount")))
        except InvalidOperation:
            raise serializers.ValidationError({"amount": "Enter an amount."}) from None
        if not amount.is_finite() or amount <= 0:
            raise serializers.ValidationError({"amount": "Enter an amount."})
        over_by = would_exceed(project, amount)
        data: dict = {"over": over_by is not None}
        if over_by is not None and may_see_project_cost(request, project):
            data["over_by"] = str(over_by)
        return Response(data)

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


# --------------------------------------------------------------------------
# Suppliers (R15, §4.20.6, §4.20.7)
# --------------------------------------------------------------------------

#: Seen only by Finance and the person who registered the supplier (§4.20.7).
SUPPLIER_PRIVATE_FIELDS = (
    "kra_pin",
    "email",
    "address",
    "bank_name",
    "account_number",
    "mpesa_type",
    "mpesa_number",
    "mpesa_account",
    "decision_reason",
)


def _holds_finance(user) -> bool:  # type: ignore[no-untyped-def]
    return resolve_permissions(user).has(PERM.FINANCE_APPROVE)


class SupplierSerializer(serializers.ModelSerializer):
    """Everyone sees name, contact and status; the PIN and payment details are
    omitted (absent, not blank) unless the reader is Finance or the registrar."""

    registered_by_name = serializers.CharField(source="registered_by.full_name", read_only=True)

    class Meta:
        model = Supplier
        fields = (
            "id",
            "name",
            "kra_pin",
            "contact_name",
            "phone",
            "email",
            "address",
            "bank_name",
            "account_number",
            "mpesa_type",
            "mpesa_number",
            "mpesa_account",
            "status",
            "is_active",
            "registered_by",
            "registered_by_name",
            "decision_reason",
            "client_uuid",
        )
        read_only_fields = ("status", "is_active", "registered_by", "decision_reason")
        # Duplicates are refused by the service, naming the existing row.
        validators: list = []
        extra_kwargs = {"client_uuid": {"required": False, "allow_null": True}}

    def to_representation(self, instance):  # type: ignore[no-untyped-def]
        data = super().to_representation(instance)
        request = self.context.get("request")
        if request is None:
            return data
        cache = self.context.setdefault("_finance", {})
        user = request.user
        if user.pk not in cache:
            cache[user.pk] = _holds_finance(user)
        if not cache[user.pk] and instance.registered_by_id != user.pk:
            for name in SUPPLIER_PRIVATE_FIELDS:
                data.pop(name, None)
        return data

    def create(self, validated_data):  # type: ignore[no-untyped-def]
        request = self.context["request"]
        return supplier_services.add_supplier(
            actor=request.user, request=request, **validated_data
        )


class SupplierDecideSerializer(serializers.Serializer):
    approved = serializers.BooleanField()
    reason = serializers.CharField(required=False, allow_blank=True, default="")


class SupplierFilter(filters.FilterSet):
    # Declared, not derived: building a filter from a model field queries the
    # tenant manager, which has no tenant at import time (as SiteFilter).
    status = filters.CharFilter(field_name="status")
    is_active = filters.BooleanFilter(field_name="is_active")
    payable = filters.BooleanFilter(method="filter_payable")

    class Meta:
        model = Supplier
        fields = ["status", "is_active"]

    def filter_payable(self, queryset, name, value):  # type: ignore[no-untyped-def]
        usable = Q(status=SupplierStatus.APPROVED, is_active=True)
        return queryset.filter(usable) if value else queryset.exclude(usable)


class SupplierViewSet(TenantScopedViewSet):
    """``/api/v1/suppliers`` (R15, §4.20.6). Every member reads and adds.

    Every write goes through :mod:`network.suppliers`. Never deleted:
    deactivating is how one leaves the pickers.
    """

    serializer_class = SupplierSerializer
    model = Supplier
    select_related = ("registered_by",)
    filterset_class = SupplierFilter
    http_method_names = ["get", "post", "patch", "head", "options"]
    required_permissions = {
        "decide": PERM.FINANCE_APPROVE,
        "deactivate": PERM.FINANCE_APPROVE,
        "reactivate": PERM.FINANCE_APPROVE,
        "link_history": PERM.FINANCE_APPROVE,
    }
    ordering_fields = ["name", "created_at"]

    def perform_create(self, serializer):  # type: ignore[no-untyped-def]
        # The service stamps the registrar; the base class's created_by is not a field.
        serializer.save()

    @property
    def search_fields(self) -> list[str]:  # type: ignore[override]
        """Searching the PIN is itself an answer, so only Finance may."""
        request = getattr(self, "request", None)
        if request is not None and _holds_finance(request.user):
            return ["name", "contact_name", "phone", "kra_pin_key"]
        return ["name", "contact_name", "phone"]

    def partial_update(self, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        supplier = self.get_object()
        registrar_may = supplier.registered_by_id == request.user.pk and supplier.status in (
            SupplierStatus.PENDING,
            SupplierStatus.REJECTED,
        )
        if not (registrar_may or _holds_finance(request.user)):
            raise PermissionDenied(
                "Only the person who added this can edit it while it waits or is "
                "rejected; after that Finance edits it."
            )
        serializer = self.get_serializer(supplier, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        changes = {
            k: v
            for k, v in serializer.validated_data.items()
            if k in supplier_services.EDITABLE_FIELDS
        }
        supplier = supplier_services.update_supplier(
            supplier, actor=request.user, changes=changes, request=request
        )
        return Response(self.get_serializer(supplier).data)

    @action(detail=True, methods=["post"])
    def decide(self, request, pk=None):  # type: ignore[no-untyped-def]
        body = SupplierDecideSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        supplier = supplier_services.decide_supplier(
            self.get_object(),
            actor=request.user,
            approved=body.validated_data["approved"],
            reason=body.validated_data["reason"],
            request=request,
        )
        return Response(self.get_serializer(supplier).data)

    @action(detail=True, methods=["post"])
    def resubmit(self, request, pk=None):  # type: ignore[no-untyped-def]
        supplier = supplier_services.resubmit(
            self.get_object(), actor=request.user, request=request
        )
        return Response(self.get_serializer(supplier).data)

    @action(detail=True, methods=["post"])
    def deactivate(self, request, pk=None):  # type: ignore[no-untyped-def]
        supplier = supplier_services.set_active(
            self.get_object(), actor=request.user, active=False, request=request
        )
        return Response(self.get_serializer(supplier).data)

    @action(detail=True, methods=["post"])
    def reactivate(self, request, pk=None):  # type: ignore[no-untyped-def]
        supplier = supplier_services.set_active(
            self.get_object(), actor=request.user, active=True, request=request
        )
        return Response(self.get_serializer(supplier).data)

    @action(detail=True, methods=["post"], url_path="link-history")
    def link_history(self, request, pk=None):  # type: ignore[no-untyped-def]
        linked = supplier_services.link_history(
            self.get_object(), actor=request.user, request=request
        )
        return Response({"linked": linked})


class ProjectSiteViewSet(TenantScopedViewSet):
    """``/api/v1/project-sites`` — a project's sites with their dates (R10, §4.19.6).

    Read for any member; the two dates are typed by the project's manager or
    someone holding ``catalogue.manage``. Linking and unlinking sites stays on
    the project (``sites``), so this offers no create or delete.
    """

    serializer_class = ProjectSiteSerializer
    model = ProjectSite
    select_related = ("site", "project")
    http_method_names = ["get", "patch", "head", "options"]
    filterset_fields = ["project", "site"]
    ordering_fields = ["id"]

    def get_queryset(self):  # type: ignore[no-untyped-def]
        return with_site_facts(super().get_queryset()).order_by("site__internal_ref", "id")

    def partial_update(self, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        from core.audit import record
        from core.models import AuditAction

        row = self.get_object()
        if row.project.manager_id != request.user.pk and not resolve_permissions(
            request.user
        ).has(PERM.CATALOGUE_MANAGE):
            raise PermissionDenied(
                "Only this project's manager, or someone who manages the catalogue, "
                "can set its site dates."
            )
        before = {"mobilised_on": str(row.mobilised_on), "accepted_on": str(row.accepted_on)}
        # Only the two dates are writable; anything else in the body is ignored.
        body = ProjectSiteSerializer(row, data=request.data, partial=True)
        body.is_valid(raise_exception=True)
        body.save()
        row = self.get_queryset().get(pk=row.pk)
        record(
            AuditAction.DOCUMENT_AMENDED,
            actor=request.user,
            target=row,
            request=request,
            before=before,
            after={"mobilised_on": str(row.mobilised_on), "accepted_on": str(row.accepted_on)},
            note=f"Site dates changed on {row.project} for {row.site}.",
        )
        return Response(ProjectSiteSerializer(row, context={"request": request}).data)
