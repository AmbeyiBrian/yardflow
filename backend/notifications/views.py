"""In-app notifications (design §6, §9.1; L1).

L1: "in-app, email and SMS ... **in-app always on**." So this endpoint is the one
channel that cannot be switched off, and it is scoped to the caller: a
notification is addressed to a person, and showing one person another's messages
would leak who is being asked to approve what.

The deep link matters as much as the message. A notification saying "GP-000412
needs your approval" with nowhere to tap is a notification that gets ignored, so
every row carries the resource the client should open (T4.18, §9.2).
"""

from __future__ import annotations

from django.db.models import Q
from django.utils import timezone
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.api import TenantScopedViewSet
from core.api_permissions import OrganizationIsActive
from notifications.models import NotificationDelivery

#: Where each document type lives in the client's routing (§9.2, T4.18).
RESOURCE_ROUTES = {
    "dispatch.GateOut": "/gate-out/{id}",
    "receiving.GateIn": "/gate-in/{id}",
    "approvals.ApprovalRequest": "/approvals/{id}",
    "jobs.Job": "/jobs/{id}",
    "jobs.Variance": "/exceptions",
    "custody.CustodyExpectation": "/custody",
    "custody.CustodyTransfer": "/custody/handovers",
    "stock.StockCount": "/stock/counts/{id}",
}


def resource_for(target_type: str, target_id: str) -> str:
    """The client route for a notification's subject.

    Computed here rather than on the client so a new document type needs no
    frontend release to become tappable.
    """
    template = RESOURCE_ROUTES.get(target_type)
    if not template or not target_id:
        return ""
    return template.format(id=target_id)


class NotificationSerializer(serializers.ModelSerializer):
    event_key = serializers.CharField(source="event.event_key", read_only=True)
    target_label = serializers.CharField(source="event.target_label", read_only=True)
    target_type = serializers.CharField(source="event.target_type", read_only=True)
    target_id = serializers.CharField(source="event.target_id", read_only=True)
    payload = serializers.JSONField(source="event.payload", read_only=True)
    occurred_at = serializers.DateTimeField(source="event.occurred_at", read_only=True)
    resource = serializers.SerializerMethodField()
    is_unread = serializers.BooleanField(read_only=True)

    class Meta:
        model = NotificationDelivery
        fields = (
            "id",
            "event_key",
            "subject",
            "body",
            "target_label",
            "target_type",
            "target_id",
            "resource",
            "payload",
            "occurred_at",
            "read_at",
            "is_unread",
        )
        read_only_fields = fields

    def get_resource(self, delivery) -> str:
        return resource_for(delivery.event.target_type, delivery.event.target_id)


class NotificationViewSet(TenantScopedViewSet):
    """``/api/v1/notifications`` (L1).

    In-app deliveries addressed to the caller. Other channels — email, SMS — have
    their own delivery rows and are not shown here: an SMS that failed is an
    administrator's problem, not something to put in the recipient's inbox twice.
    """

    serializer_class = NotificationSerializer
    model = NotificationDelivery
    select_related = ("event", "recipient")
    filterset_fields = ["status"]
    ordering_fields = ["created_at"]

    # POST is here for `read` and `read-all` only; `create` is refused below.
    http_method_names = ["get", "post", "head", "options"]

    def create(self, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        """A notification is emitted by the system, never posted by a client.

        Same rule as a movement (§3.2) or a variance (H3): if a client could
        write one, the inbox would be something anyone could put words in — and
        "the yard says your gate pass was approved" is exactly the message worth
        forging. Refused explicitly rather than left to fail on a missing
        recipient, which is a 500 dressed up as a validation error.
        """
        from rest_framework import status as http_status

        return Response(
            {
                "error": {
                    "code": "METHOD_NOT_ALLOWED",
                    "message": (
                        "Notifications are raised by the system when something "
                        "happens, and cannot be created directly (L1)."
                    ),
                }
            },
            status=http_status.HTTP_405_METHOD_NOT_ALLOWED,
        )

    def get_queryset(self):  # type: ignore[no-untyped-def]
        """Scoped to the caller as well as the tenant.

        The tenant manager already limits this to the organization; the recipient
        filter is what stops one colleague reading another's approval requests.
        """
        return (
            NotificationDelivery.objects.filter(recipient=self.request.user, channel="in_app")
            .select_related(*self.select_related)
            .order_by("-created_at")
        )

    @extend_schema(
        request=None,
        responses={200: inline_serializer("UnreadCount", {"unread": serializers.IntegerField()})},
    )
    @action(detail=False, methods=["get"])
    def unread(self, request):  # type: ignore[no-untyped-def]
        """The badge count. Cheap enough to poll (T4.24)."""
        return Response({"unread": self.get_queryset().filter(read_at__isnull=True).count()})

    @extend_schema(request=None, responses={200: NotificationSerializer})
    @action(detail=True, methods=["post"], url_path="read")
    def mark_read(self, request, pk=None):  # type: ignore[no-untyped-def]
        delivery = self.get_object()
        if delivery.read_at is None:
            delivery.read_at = timezone.now()
            delivery.save(update_fields=["read_at", "updated_at"])
        return Response(self.get_serializer(delivery).data)

    @extend_schema(
        request=None,
        responses={200: inline_serializer("MarkedRead", {"marked": serializers.IntegerField()})},
    )
    @action(detail=False, methods=["post"], url_path="read-all")
    def mark_all_read(self, request):  # type: ignore[no-untyped-def]
        marked = self.get_queryset().filter(read_at__isnull=True).update(read_at=timezone.now())
        return Response({"marked": marked})


class NotificationPreferencesView(APIView):
    """``/api/v1/notifications/preferences`` (L1, L2).

    What this tenant will actually be told, and how — for any member to read,
    because a notification nobody expected reads as spam.

    Editing happens on ``/settings`` behind ``settings.manage``. This endpoint
    stays read-only because it is the *resolved* view: the tenant's matrix after
    channel switches have been applied, which is a different question from what
    is stored. It also answers which events carry a control, so the settings
    screen can say plainly what silence would cost before somebody chooses it.
    """

    permission_classes = [IsAuthenticated, OrganizationIsActive]

    @extend_schema(
        responses={
            200: inline_serializer(
                "NotificationMatrix",
                {"events": serializers.ListField(child=serializers.DictField())},
            )
        }
    )
    def get(self, request):  # type: ignore[no-untyped-def]
        from accounts.permissions_registry import PERM
        from accounts.services import resolve_permissions
        from notifications import credits
        from notifications.matrix import DEFAULT_MATRIX, channels_for, is_enabled

        organization = request.user.organization

        # L4: what SMS costs and what is left — omitted for anyone who cannot
        # change the settings. Everybody may see *what they will be told*, which
        # is about them; the balance is the company's commercial position, and a
        # storekeeper reading how many credits are left is a leak rather than a
        # feature. Withheld here rather than hidden on the screen, because a
        # field the API still returns is not withheld at all.
        may_manage = resolve_permissions(request.user).has(PERM.SETTINGS_MANAGE)

        payload: dict = {}
        if may_manage:
            payload["sms"] = {
                "credit_balance": credits.balance(organization),
                "credit_price_kes": credits.CREDIT_PRICE_KES,
                "is_low": credits.is_low(organization),
            }

        payload.update(
            {
                "events": [
                    {
                        "event": spec.key,
                        "label": spec.label,
                        "recipients": list(spec.recipients),
                        # What this tenant will actually use, not the default —
                        # WhatsApp ships disabled (Q1), and showing it as active
                        # would promise a message nobody receives.
                        "channels": channels_for(organization, spec.key),
                        # Whether silence here lets something go unnoticed. A
                        # warning for the settings screen, never a block: the
                        # work stays visible in the app either way.
                        "carries_a_control": spec.carries_a_control,
                        "enabled": is_enabled(organization, spec.key),
                        # Who that actually is, by name. "Approvers" is the rule;
                        # an owner deciding whether to mute a message wants to
                        # know which people stop hearing it. Groups that depend
                        # on the document — whoever raised it, whoever is
                        # carrying it — cannot be named in advance and say so.
                        "people": _named_recipients(organization, spec.recipients),
                    }
                    for spec in DEFAULT_MATRIX
                ]
            }
        )
        return Response(payload)


#: Groups that resolve to a role, and can therefore be named before an event
#: happens. The rest depend on the document — the person who raised *this*
#: request — and are only known when there is one.
_NAMEABLE = {
    "approvers",
    "storekeepers",
    "owner",
    "supervisor",
    "fallback_approver",
}


def _named_recipients(organization, groups) -> list[dict]:
    """Turn recipient groups into the people currently in them (L2).

    Read live rather than stored: roles change, and a screen showing who *was*
    told is worse than one showing nobody.
    """
    from notifications.events import _resolve_group

    named: list[dict] = []
    for group in groups:
        if group not in _NAMEABLE:
            named.append({"group": group, "people": None})
            continue

        people = sorted(
            {
                user.full_name or user.email or user.phone
                for user in _resolve_group(organization, group, None)
                if user is not None and user.is_active
            }
        )
        named.append({"group": group, "people": people})
    return named


def unread_for(user) -> int:
    """Used by the dashboards (T7.8)."""
    return NotificationDelivery.objects.filter(
        Q(recipient=user), channel="in_app", read_at__isnull=True
    ).count()
