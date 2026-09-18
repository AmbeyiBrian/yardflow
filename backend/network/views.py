"""Client, site and project endpoints (design §6; C5, C6, C7)."""

from __future__ import annotations

from django.db.models import Q
from django_filters import rest_framework as filters
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.response import Response

from accounts.permissions_registry import PERM
from core.api import TenantScopedViewSet
from network.models import Client, Project, ProjectStatus, Site, SiteReference


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


class ProjectSerializer(serializers.ModelSerializer):
    client_name = serializers.CharField(source="client.name", read_only=True)
    manager_name = serializers.CharField(source="manager.get_full_name", read_only=True)
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
            "cost_budget",
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
        "close": PERM.CATALOGUE_MANAGE,
    }
    filterset_fields = ["client", "status"]
    search_fields = ["reference", "description"]
    ordering_fields = ["opened_at", "reference"]

    @action(detail=True, methods=["get"])
    def unreconciled(self, request, pk=None):  # type: ignore[no-untyped-def]
        """What remains unaccounted for (C7). Real figures arrive with T5.7."""
        return Response(self.get_object().unreconciled_summary())

    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):  # type: ignore[no-untyped-def]
        """Close a project, warning on unreconciled material (C7).

        A warning, not a block: C7 says "closing it warns". Blocking would be
        H5's rule, and that applies to jobs, not projects.
        """
        from django.utils import timezone

        project = self.get_object()
        summary = project.unreconciled_summary()

        project.status = ProjectStatus.CLOSED
        project.closed_at = timezone.now()
        project.close_reason = request.data.get("reason", "")
        project.closed_with_unreconciled = bool(summary.get("unreconciled"))
        project.save(
            update_fields=["status", "closed_at", "close_reason", "closed_with_unreconciled"]
        )

        return Response(
            {**self.get_serializer(project).data, "unreconciled_warning": summary}
        )
