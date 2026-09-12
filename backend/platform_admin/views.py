"""The cross-tenant platform admin console (design §6, requirements A1, A2).

D7: no self-serve signup in v1 — tenants are onboarded manually here.

This is the **only** app allowed to reach across tenants. It is also the only
place ``all_objects`` and ``rls_bypass`` may appear; CI fails the build if they
show up anywhere else (T1.20).

Note that most of what this console does needs neither: ``Organization`` is not
itself a tenant-scoped model — it *is* the tenant — so listing, creating and
suspending organizations are ordinary queries.
"""

from __future__ import annotations

from django.db import transaction
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from accounts.api_permissions import IsPlatformAdmin
from core.models import AuditAction, Organization
from core.provisioning import provision_tenant


class OrganizationSerializer(serializers.ModelSerializer):
    """A tenant as the console sees it."""

    user_count = serializers.SerializerMethodField()
    owner_identifier = serializers.SerializerMethodField()

    class Meta:
        model = Organization
        fields = (
            "id",
            "name",
            "slug",
            "status",
            "legal_name",
            "tax_pin",
            "phone",
            "email",
            "address",
            "created_at",
            "user_count",
            "owner_identifier",
        )
        # A1: the subdomain is immutable after creation.
        read_only_fields = ("id", "slug", "status", "created_at")

    def get_user_count(self, organization: Organization) -> int:
        return organization.users.count()

    def get_owner_identifier(self, organization: Organization) -> str | None:
        owner = (
            organization.users.filter(user_roles__role__name="Owner")
            .order_by("pk")
            .first()
        )
        return str(owner) if owner else None


class ProvisionTenantSerializer(serializers.Serializer):
    """A1: name, subdomain and initial owner account."""

    name = serializers.CharField(max_length=200)
    slug = serializers.SlugField(max_length=63)
    owner_email = serializers.EmailField(required=False, allow_null=True)
    owner_phone = serializers.CharField(max_length=20, required=False, allow_null=True)
    owner_full_name = serializers.CharField(max_length=200, required=False, allow_blank=True)

    def validate_slug(self, value: str) -> str:
        from core.models import RESERVED_SUBDOMAINS

        value = value.lower()
        if value in RESERVED_SUBDOMAINS:
            raise serializers.ValidationError(
                f"'{value}' is reserved for the platform and cannot be a tenant subdomain."
            )
        if Organization.objects.filter(slug=value).exists():
            raise serializers.ValidationError("That subdomain is already taken.")
        return value

    def validate(self, attrs):  # type: ignore[no-untyped-def]
        if not attrs.get("owner_email") and not attrs.get("owner_phone"):
            raise serializers.ValidationError(
                "The owner needs an email address or a phone number, so they can "
                "be invited to set a password (A1, B1)."
            )
        return attrs

    def create(self, validated_data):  # type: ignore[no-untyped-def]
        return provision_tenant(
            name=validated_data["name"],
            slug=validated_data["slug"],
            owner_email=validated_data.get("owner_email"),
            owner_phone=validated_data.get("owner_phone"),
            owner_full_name=validated_data.get("owner_full_name", ""),
            request=self.context.get("request"),
        )


class SuspensionSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=255, required=False, allow_blank=True)


class OrganizationViewSet(viewsets.ModelViewSet):
    """``/admin-api/organizations`` — list, create and suspend tenants (A1, A2)."""

    serializer_class = OrganizationSerializer
    permission_classes = [IsPlatformAdmin]
    queryset = Organization.objects.all().order_by("name")
    filterset_fields = ["status"]
    search_fields = ["name", "slug", "legal_name"]

    # Deleting a tenant is not an operation this console offers. A2 is explicit
    # that suspension never deletes data, and a customer's records may be needed
    # to answer an audit long after they stop paying.
    http_method_names = ["get", "post", "patch", "head", "options"]

    @extend_schema(request=ProvisionTenantSerializer, responses={201: OrganizationSerializer})
    def create(self, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        serializer = ProvisionTenantSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        result = serializer.save()

        return Response(
            OrganizationSerializer(result["organization"]).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(request=SuspensionSerializer, responses={200: OrganizationSerializer})
    @action(detail=True, methods=["post"])
    def suspend(self, request, pk=None):  # type: ignore[no-untyped-def]
        """A2: suspend a non-paying or dormant customer. Never deletes data."""
        organization = self.get_object()
        serializer = SuspensionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            before = organization.status
            organization.status = Organization.Status.SUSPENDED
            organization.save(update_fields=["status"])
            self._audit(
                organization,
                AuditAction.ORGANIZATION_SUSPENDED,
                request,
                before,
                serializer.validated_data.get("reason", ""),
            )

        return Response(OrganizationSerializer(organization).data)

    @extend_schema(request=SuspensionSerializer, responses={200: OrganizationSerializer})
    @action(detail=True, methods=["post"])
    def reinstate(self, request, pk=None):  # type: ignore[no-untyped-def]
        """Undo a suspension. A2 says data survives, so this is a status change."""
        organization = self.get_object()
        serializer = SuspensionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            before = organization.status
            organization.status = Organization.Status.ACTIVE
            organization.save(update_fields=["status"])
            self._audit(
                organization,
                AuditAction.ORGANIZATION_REINSTATED,
                request,
                before,
                serializer.validated_data.get("reason", ""),
            )

        return Response(OrganizationSerializer(organization).data)

    @staticmethod
    def _audit(organization, action_name, request, before_status, reason):  # type: ignore[no-untyped-def]
        """Record the change in the tenant's own trail (M3).

        Filed under the affected tenant rather than centrally, so their admin can
        see what happened to them and when.
        """
        from core.audit import record

        record(
            action_name,
            actor=request.user,
            organization=organization,
            target=organization,
            target_label=organization.name,
            before={"status": before_status},
            after={"status": organization.status},
            request=request,
            note=reason,
        )
