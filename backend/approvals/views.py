"""Approval endpoints (design §6, §5.3; F3, F4, F5, M3)."""

from __future__ import annotations

from django.core import signing
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.response import Response

from accounts.permissions_registry import PERM
from approvals.models import (
    ApprovalAction,
    ApprovalRequest,
    ApprovalRequestStatus,
    ApprovalRule,
)
from core.api import TenantScopedViewSet

#: Deep links are short-lived and signed, and authorise nothing on their own
#: (§9.2, F4, T4.18). Two hours is long enough to read a notification and act,
#: short enough that a forwarded message is useless the next day.
DEEP_LINK_MAX_AGE = 2 * 60 * 60
DEEP_LINK_SALT = "approvals.deep_link"


class ApprovalRuleSerializer(serializers.ModelSerializer):
    role_name = serializers.CharField(source="required_role.name", read_only=True)
    category_name = serializers.CharField(source="category.name", read_only=True)

    class Meta:
        model = ApprovalRule
        fields = (
            "id",
            "category",
            "category_name",
            "criticality",
            "required_role",
            "role_name",
            "sequence",
            "conditions",
            "is_active",
            "description",
        )

    def validate_conditions(self, value):
        """F3: conditions are empty in v1.

        The field exists so a future dimension needs no migration. Accepting an
        arbitrary condition today would mean a tenant could write a rule whose
        effect nothing has tested — and because unknown keys fail closed, it
        would silently *raise* the approval required rather than lower it. Better
        to refuse it plainly.
        """
        if not value:
            return value

        from approvals.engine import KNOWN_CONDITION_KEYS

        unknown = set(value) - KNOWN_CONDITION_KEYS
        if unknown:
            raise serializers.ValidationError(
                f"Unknown condition(s): {', '.join(sorted(unknown))}. "
                f"Supported: {', '.join(sorted(KNOWN_CONDITION_KEYS))}."
            )
        return value


class ApprovalActionSerializer(serializers.ModelSerializer):
    attribution = serializers.CharField(read_only=True)
    actor_name = serializers.CharField(source="actor.full_name", read_only=True)

    class Meta:
        model = ApprovalAction
        fields = (
            "id",
            "decision",
            "reason",
            "actor",
            "actor_name",
            "on_behalf_of",
            "attribution",
            "auth_method",
            "decided_at",
        )
        read_only_fields = fields


class ApprovalRequestSerializer(serializers.ModelSerializer):
    role_name = serializers.SerializerMethodField()
    actions = ApprovalActionSerializer(many=True, read_only=True)
    document = serializers.SerializerMethodField()

    class Meta:
        model = ApprovalRequest
        fields = (
            "id",
            "document_type",
            "document_id",
            "document_number",
            "document",
            "level",
            "required_role",
            "role_name",
            "status",
            "due_at",
            "escalated_at",
            "resolved_at",
            "actions",
        )
        read_only_fields = fields

    def get_role_name(self, request: ApprovalRequest) -> str | None:
        role = request.required_role
        return role.name if role is not None else None

    def get_document(self, approval_request: ApprovalRequest) -> dict:
        """Enough of the document to decide without a second request.

        F4 wants approval possible "from my phone" — so the pending list has to
        carry what an approver needs to judge, not just a reference.
        """
        from dispatch.models import GateOut

        if approval_request.document_type != "dispatch.GateOut":
            return {"label": approval_request.document_number}

        gate_out = (
            GateOut.objects.filter(pk=approval_request.document_id)
            .select_related("custody_holder", "site", "client", "from_location")
            .prefetch_related("lines", "lines__item_type", "lines__owner_client")
            .first()
        )
        if gate_out is None:
            return {"label": approval_request.document_number}

        return {
            "id": gate_out.pk,
            "number": gate_out.number,
            "purpose": gate_out.get_purpose_type_display(),
            "destination": gate_out.destination_label,
            "custody_holder": str(gate_out.custody_holder),
            "requested_by": str(gate_out.requested_by),
            # So the screen can say "you raised this" before the approver taps
            # approve and gets refused by §5.3.
            "requested_by_id": gate_out.requested_by_id,
            "notes": gate_out.notes,
            "lines": [
                {
                    "item": str(line.item_type),
                    "quantity": str(line.requested_qty),
                    "uom": line.uom,
                    # E1: client ownership is visible on the approval screen, not
                    # buried a tap away — it changes whether you approve.
                    "owner": str(line.owner_client) if line.owner_client_id else "Own stock",
                    "is_client_owned": bool(line.owner_client_id),
                    "criticality": line.item_type.criticality,
                }
                for line in gate_out.lines.all()
            ],
        }


class ApprovalRuleViewSet(TenantScopedViewSet):
    """``/api/v1/approval-rules`` (F3, T4.3)."""

    serializer_class = ApprovalRuleSerializer
    model = ApprovalRule
    select_related = ("required_role", "category")
    filterset_fields = ["criticality", "required_role", "is_active"]
    ordering_fields = ["sequence", "criticality"]
    required_permissions = {
        "create": PERM.SETTINGS_MANAGE,
        "update": PERM.SETTINGS_MANAGE,
        "partial_update": PERM.SETTINGS_MANAGE,
        "destroy": PERM.SETTINGS_MANAGE,
    }


class ApprovalRequestViewSet(TenantScopedViewSet):
    """``/api/v1/approvals`` (F4).

    Read-only: a decision is made through the document's own action
    (``/gate-outs/{id}/approve``), so there is exactly one code path per
    decision and the audit trail cannot be written from two places.
    """

    serializer_class = ApprovalRequestSerializer
    model = ApprovalRequest
    select_related = ("required_role",)
    prefetch_related = ("actions", "actions__actor", "actions__on_behalf_of")
    filterset_fields = ["status", "document_type", "level"]
    ordering_fields = ["due_at", "created_at"]
    http_method_names = ["get", "head", "options"]

    @extend_schema(responses={200: ApprovalRequestSerializer(many=True)})
    @action(detail=False, methods=["get"])
    def pending(self, request):  # type: ignore[no-untyped-def]
        """What this user can act on now (F4).

        Filtered to the caller's own roles and delegations: a list showing
        approvals somebody else must make is noise, and noise is what stops
        people reading the list at all.
        """
        from django.db.models import Q

        from accounts.services import resolve_permissions

        outstanding = self.filter_queryset(self.get_queryset()).filter(
            status__in=(
                ApprovalRequestStatus.PENDING,
                ApprovalRequestStatus.ESCALATED,
            )
        )

        # Narrowed in the database rather than in Python, because the result has
        # to stay a queryset: cursor pagination orders by a column, and a list
        # cannot be ordered by one (§6, N-2).
        if not resolve_permissions(request.user).has(PERM.GATE_OUT_APPROVE):
            now = timezone.now()
            role_ids = set(request.user.user_roles.values_list("role_id", flat=True))
            # F5: a delegation confers the role for a period.
            role_ids.update(
                request.user.delegations_received.filter(
                    is_revoked=False, starts_at__lte=now, ends_at__gte=now
                ).values_list("role_id", flat=True)
            )
            outstanding = outstanding.filter(
                Q(required_role_id__in=role_ids) | Q(required_role__isnull=True)
            )

        # Self-approval is deliberately *not* filtered out here. §5.3 refuses it
        # at the moment of approving, and seeing your own request in the queue —
        # with the screen saying somebody else must sign it — is more useful than
        # a request that appears to have vanished.
        page = self.paginate_queryset(outstanding.order_by("due_at", "level", "id"))
        if page is not None:
            return self.get_paginated_response(self.get_serializer(page, many=True).data)
        return Response(self.get_serializer(outstanding, many=True).data)

    @extend_schema(responses={200: dict})
    @action(detail=True, methods=["get"], url_path="deep-link")
    def deep_link(self, request, pk=None):  # type: ignore[no-untyped-def]
        """A short-lived signed link to this approval (F4, §9.2, T4.18).

        §9.2: "approval deep links are short-lived signed URLs that land on the
        approval screen. **They authenticate nothing by themselves** — the user
        still logs in or presents a fingerprint."
        """
        approval_request = self.get_object()
        token = signing.dumps(
            {"id": approval_request.pk, "org": str(approval_request.organization_id)},
            salt=DEEP_LINK_SALT,
        )
        return Response(
            {
                "path": f"/approvals/{approval_request.pk}?token={token}",
                "expires_in_seconds": DEEP_LINK_MAX_AGE,
                "authenticates": False,
            }
        )


def resolve_deep_link(token: str) -> ApprovalRequest | None:
    """Resolve a deep-link token (T4.18).

    Expired or tampered tokens raise; the caller turns that into a 404. The
    organization is inside the signed payload, so a link cannot be replayed
    against another tenant (A3).
    """
    from core.tenancy import tenant_context

    payload = signing.loads(token, salt=DEEP_LINK_SALT, max_age=DEEP_LINK_MAX_AGE)

    with tenant_context(payload["org"]):
        return ApprovalRequest.objects.filter(pk=payload["id"]).first()


def _document_for(approval_request: ApprovalRequest):
    from django.apps import apps

    try:
        model = apps.get_model(approval_request.document_type)
    except LookupError:
        return None
    return model.objects.filter(pk=approval_request.document_id).first()
