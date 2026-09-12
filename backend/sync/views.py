"""Sync endpoints (design §6, §8; N1–N3).

Three, matching §6's table:

* ``POST /sync/submissions`` — the queue drains here
* ``GET /sync/exceptions`` — what could not be applied (§8.4)
* ``POST /sync/exceptions/{id}/resolve`` — somebody's decision about one

The submissions endpoint takes a **batch**, because a phone that has been offline
for a shift has ten things to send and ten round trips over a bad connection is
how a sync gets abandoned half way. Each item is applied independently: one
conflict must not stop the rest, or a single stale gate-in would strand a whole
day's capture.
"""

from __future__ import annotations

from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions_registry import PERM
from core.api import TenantScopedViewSet
from core.api_permissions import OrganizationIsActive
from core.exceptions import DomainError
from sync.models import SyncException, SyncOperation, SyncSubmission
from sync.services import apply_submission, resolve_exception


class SubmissionItemSerializer(serializers.Serializer):
    """One queued mutation as the device recorded it (§8.1)."""

    client_uuid = serializers.UUIDField()
    operation = serializers.ChoiceField(choices=SyncOperation.choices)
    payload = serializers.DictField()
    #: When the phone captured it. A delivery received at 08:00 and synced at
    #: 17:00 belongs to the morning, and the ledger should say so.
    captured_at = serializers.DateTimeField(required=False)


class SubmissionBatchSerializer(serializers.Serializer):
    submissions = SubmissionItemSerializer(many=True)


class SyncSubmissionSerializer(serializers.ModelSerializer):
    submitted_by_name = serializers.CharField(
        source="submitted_by.full_name", read_only=True, default=""
    )
    exception = serializers.SerializerMethodField()

    class Meta:
        model = SyncSubmission
        fields = (
            "id",
            "client_uuid",
            "operation",
            "status",
            "document_type",
            "document_id",
            "document_number",
            "submitted_by",
            "submitted_by_name",
            "captured_at",
            "applied_at",
            "created_at",
            "exception",
        )
        read_only_fields = fields

    def get_exception(self, submission) -> dict | None:
        exception = getattr(submission, "exception", None)
        if exception is None:
            return None
        return {
            "id": exception.pk,
            "code": exception.code,
            "reason": exception.reason,
            "details": exception.details,
            "status": exception.status,
        }


class SyncExceptionSerializer(serializers.ModelSerializer):
    operation = serializers.CharField(source="submission.operation", read_only=True)
    client_uuid = serializers.UUIDField(source="submission.client_uuid", read_only=True)
    captured_at = serializers.DateTimeField(
        source="submission.captured_at", read_only=True
    )
    captured_by = serializers.CharField(
        source="submission.submitted_by.full_name", read_only=True, default=""
    )
    #: The payload, verbatim. When a conflict is resolved days later this is the
    #: only record of what the person on site actually said (§8.4).
    payload = serializers.JSONField(source="submission.payload", read_only=True)
    is_open = serializers.BooleanField(read_only=True)
    resolved_by_name = serializers.CharField(
        source="resolved_by.full_name", read_only=True, default=""
    )

    class Meta:
        model = SyncException
        fields = (
            "id",
            "status",
            "code",
            "reason",
            "details",
            "operation",
            "client_uuid",
            "captured_at",
            "captured_by",
            "payload",
            "resolution",
            "resolved_by",
            "resolved_by_name",
            "resolved_at",
            "is_open",
            "created_at",
        )
        read_only_fields = fields


class ResolveExceptionSerializer(serializers.Serializer):
    resolution = serializers.CharField(max_length=500)
    #: True when the right answer is "this never happened" — the material was
    #: never received, the request is moot. Still recorded, never deleted.
    discard = serializers.BooleanField(default=False)


class SyncSubmissionsView(APIView):
    """``POST /api/v1/sync/submissions`` (§8.2, N1, N2).

    Always 200 for a well-formed batch, even when items were refused. The queue
    needs to know the outcome per item so it can clear the ones that landed and
    surface the ones that did not — a 400 for the whole batch would leave the
    device unable to tell which was which, and it would send everything again.
    """

    permission_classes = [IsAuthenticated, OrganizationIsActive]

    @extend_schema(
        request=SubmissionBatchSerializer,
        responses={
            200: inline_serializer(
                "SyncResult",
                {
                    "applied": serializers.IntegerField(),
                    "replayed": serializers.IntegerField(),
                    "rejected": serializers.IntegerField(),
                    "results": serializers.ListField(child=serializers.DictField()),
                },
            )
        },
    )
    def post(self, request):  # type: ignore[no-untyped-def]
        batch = SubmissionBatchSerializer(data=request.data)
        batch.is_valid(raise_exception=True)

        results = []
        applied = replayed = rejected = 0

        for item in batch.validated_data["submissions"]:
            try:
                submission, was_replay = apply_submission(
                    organization=request.user.organization,
                    client_uuid=item["client_uuid"],
                    operation=item["operation"],
                    payload=item["payload"],
                    submitted_by=request.user,
                    captured_at=item.get("captured_at"),
                    request=request,
                )
            except DomainError as refusal:
                # A refusal the queue should stop retrying — an operation that
                # cannot be captured offline at all. Reported per item so the
                # rest of the batch still applies.
                rejected += 1
                results.append(
                    {
                        "client_uuid": str(item["client_uuid"]),
                        "status": "REFUSED",
                        "code": refusal.code,
                        "reason": str(refusal),
                        "details": refusal.details or {},
                    }
                )
                continue

            payload = SyncSubmissionSerializer(submission).data
            payload["replayed"] = was_replay
            results.append(payload)

            if was_replay:
                replayed += 1
            elif submission.status == "APPLIED":
                applied += 1
            else:
                rejected += 1

        return Response(
            {
                "applied": applied,
                "replayed": replayed,
                "rejected": rejected,
                "results": results,
            }
        )


class SyncSubmissionViewSet(TenantScopedViewSet):
    """``/api/v1/sync-submissions`` — what this device has sent (N1).

    Read-only. Submitting goes through the batch endpoint above, which is where
    the idempotency lives.
    """

    serializer_class = SyncSubmissionSerializer
    model = SyncSubmission
    select_related = ("submitted_by", "exception")
    filterset_fields = ["operation", "status", "submitted_by"]
    search_fields = ["document_number"]
    ordering_fields = ["created_at", "captured_at"]

    http_method_names = ["get", "head", "options"]


class SyncExceptionViewSet(TenantScopedViewSet):
    """``/api/v1/sync-exceptions`` (§8.4, N3, T8.7).

    T8.7's criterion is "every synced conflict is resolvable **without developer
    intervention**", which is why the payload and the refusal details are on the
    serializer rather than in a log line somebody would have to be shown.
    """

    serializer_class = SyncExceptionSerializer
    model = SyncException
    select_related = ("submission", "submission__submitted_by", "resolved_by")
    filterset_fields = ["status", "code"]
    search_fields = ["reason", "resolution"]
    ordering_fields = ["created_at", "status"]

    required_permissions = {
        # Whoever posts stock resolves the conflicts about stock.
        "resolve": PERM.GATE_IN_POST,
    }

    # Raised by the server, resolved by a person. Never created by hand.
    http_method_names = ["get", "post", "head", "options"]

    def create(self, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        return Response(
            {
                "error": {
                    "code": "METHOD_NOT_ALLOWED",
                    "message": (
                        "A sync exception is raised when the server refuses a "
                        "queued document, and cannot be created directly (§8.4)."
                    ),
                }
            },
            status=status.HTTP_405_METHOD_NOT_ALLOWED,
        )

    @extend_schema(
        request=ResolveExceptionSerializer, responses={200: SyncExceptionSerializer}
    )
    @action(detail=True, methods=["post"])
    def resolve(self, request, pk=None):  # type: ignore[no-untyped-def]
        serializer = ResolveExceptionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        exception = resolve_exception(
            self.get_object(),
            resolution=serializer.validated_data["resolution"],
            discard=serializer.validated_data["discard"],
            resolved_by=request.user,
            request=request,
        )
        return Response(self.get_serializer(exception).data)


class OfflineBundleView(APIView):
    """``GET /api/v1/sync/bundle`` — what a device needs to work offline (§8.1, N1, N3).

    Two halves, and the second is the one that matters:

    * **reference data** — item types, locations, clients, sites, people. Enough
      to build a line without asking the server anything (T8.2).
    * **releasable passes** — gate-outs that are *already approved*. §8.3 allows
      offline release only of these, and only because they are already
      authorised. A device that downloaded unapproved passes could release them
      with nobody's authority, which is the hole the whole offline design exists
      to keep shut.

    One request rather than eight, because this is the last thing a phone does
    before losing signal and each extra round trip is a chance to lose half of it.
    """

    permission_classes = [IsAuthenticated, OrganizationIsActive]

    @extend_schema(
        responses={
            200: inline_serializer(
                "OfflineBundle",
                {
                    "generated_at": serializers.DateTimeField(),
                    "item_types": serializers.ListField(child=serializers.DictField()),
                    "locations": serializers.ListField(child=serializers.DictField()),
                    "clients": serializers.ListField(child=serializers.DictField()),
                    "sites": serializers.ListField(child=serializers.DictField()),
                    "people": serializers.ListField(child=serializers.DictField()),
                    "releasable_gate_outs": serializers.ListField(
                        child=serializers.DictField()
                    ),
                },
            )
        }
    )
    def get(self, request):  # type: ignore[no-untyped-def]
        from django.utils import timezone

        from accounts.models import User
        from catalogue.models import ItemType
        from dispatch.models import RELEASABLE_STATUSES, GateOut
        from locations.models import Location
        from network.models import Client, Site

        item_types = [
            {
                "id": item.pk,
                "name": item.name,
                "code": item.code,
                "uom": item.uom,
                "tracking_mode": item.default_tracking_mode,
                "category": item.category_id,
                "is_returnable": item.is_returnable,
            }
            for item in ItemType.objects.filter(is_archived=False).select_related("category")
        ]

        locations = [
            {"id": row.pk, "name": row.name, "type": row.type}
            for row in Location.objects.filter(is_active=True)
        ]
        clients = [{"id": row.pk, "name": row.name} for row in Client.objects.all()]
        sites = [
            {
                "id": row.pk,
                "name": row.name,
                "internal_ref": row.internal_ref,
                "client": row.client_id,
            }
            for row in Site.objects.all()
        ]
        people = [
            {"id": row.pk, "full_name": row.full_name}
            for row in User.objects.filter(is_active=True)
        ]

        releasable = []
        for gate_out in (
            GateOut.objects.filter(status__in=RELEASABLE_STATUSES)
            .select_related("site", "client", "to_location", "custody_holder")
            .prefetch_related("lines", "lines__item_type", "lines__serials", "lines__reels")
        ):
            if gate_out.is_expired:
                # Q3: an expired approval is not an approval. Sending it to a
                # device would be sending a licence to release stale stock.
                continue
            releasable.append(
                {
                    "id": gate_out.pk,
                    "number": gate_out.number,
                    "status": gate_out.status,
                    "destination": gate_out.destination_label,
                    "custody_holder": str(gate_out.custody_holder),
                    "expires_at": gate_out.expires_at,
                    "lines": [
                        {
                            "id": line.pk,
                            "item_type": line.item_type_id,
                            "item_name": str(line.item_type),
                            "requested_qty": str(line.requested_qty),
                            "released_qty": str(line.released_qty),
                            "uom": line.uom,
                            "tracking_mode": line.tracking_mode,
                            "serials": [
                                {
                                    "serial_unit": entry.serial_unit_id,
                                    "serial_number": entry.serial_unit.serial_number,
                                }
                                for entry in line.serials.select_related("serial_unit")
                            ],
                            "reels": [
                                {
                                    "reel": entry.reel_id,
                                    "drum_number": entry.reel.drum_number,
                                    "length_requested": str(entry.length_requested),
                                }
                                for entry in line.reels.select_related("reel")
                            ],
                        }
                        for line in gate_out.lines.all()
                    ],
                }
            )

        return Response(
            {
                "generated_at": timezone.now(),
                "item_types": item_types,
                "locations": locations,
                "clients": clients,
                "sites": sites,
                "people": people,
                "releasable_gate_outs": releasable,
            }
        )
