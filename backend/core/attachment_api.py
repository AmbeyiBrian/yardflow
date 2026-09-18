"""The attachment endpoint (design §6, §4.13; D6, G3, H2, K3, N-7).

§6 lists ``POST /attachments`` and ``GET /attachments/{id}/url``. Storage and
signed reads already existed (:mod:`core.attachments`); this is the way in.

Three decisions worth stating, because each closes a hole a simpler upload
endpoint would leave open:

**The target is an allow-list, not a free-text label.** ``Attachment`` carries
its owner as ``(target_type, target_id)`` text rather than a foreign key, so
without a whitelist a client could attach a file to *any* model — a user row, an
audit entry — and the listing on that record would then serve it back. Only the
documents the requirements actually attach to are accepted.

**The target is resolved through the tenant manager.** Ids are sequential per
table, so a caller could otherwise guess another tenant's closeout id and hang a
file off it. Resolving the row first means a foreign target is a 404 like
everywhere else (A3, §2.4).

**Attaching needs the permission that produces the document.** Photographing a
gate-in is part of receiving; if reading were enough, anyone who can see a
document could add evidence to it, which is exactly backwards for records D6 and
G3 exist to make trustworthy.

``POST /attachments/presign`` is deliberately absent. It exists in §6 for direct
browser-to-S3 uploads, which matter for large files; every attachment the
requirements describe is a phone photo or a delivery note, and routing those
through the API keeps one code path that validates size, type and target. It can
be added beside this without changing the read contract when something needs it.
"""

from __future__ import annotations

from django.apps import apps
from django.http import Http404
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions_registry import PERM
from accounts.services import resolve_permissions
from core.api import TenantScopedViewSet
from core.api_permissions import OrganizationIsActive
from core.attachments import max_upload_bytes, store_attachment
from core.audit import record
from core.models import Attachment, AttachmentKind, AuditAction

#: What may be attached to, and what a user must hold to attach to it.
#:
#: Keyed by ``Model._meta.label``, which is what :func:`store_attachment` writes
#: into ``target_type`` — so a record's own listing and this map cannot drift.
ATTACHABLE_TARGETS: dict[str, tuple[str, ...]] = {
    # D6: photos and the supplier or client delivery note at gate-in.
    "receiving.GateIn": (PERM.GATE_IN_POST,),
    # G3: the recipient's signature or a photo of the loaded vehicle. Both the
    # requester and the storekeeper releasing it have a reason to add one.
    "dispatch.GateOut": (PERM.GATE_OUT_REQUEST, PERM.GATE_OUT_RELEASE),
    # H2: site photos on a closeout — the evidence a job was actually done.
    "jobs.Job": (PERM.JOB_CLOSEOUT,),
    "jobs.JobCloseout": (PERM.JOB_CLOSEOUT,),
    # H3: a photo is often the only explanation a variance can carry.
    "jobs.Variance": (PERM.JOB_CLOSEOUT, PERM.GATE_OUT_APPROVE),
    "stock.StockCount": (PERM.STOCK_ADJUST,),
    "custody.CustodyTransfer": (PERM.CUSTODY_TRANSFER,),
    # O16: the receipt behind an expense. Open to anyone who may raise a
    # gate-out or close out a job, because the person who paid for the fuel is
    # the one holding the receipt — and O16 has anybody record an expense.
    "commercials.ProjectExpense": (
        PERM.GATE_OUT_REQUEST,
        PERM.JOB_CLOSEOUT,
        PERM.PROJECT_VIEW_COST,
    ),
}

#: N-7 keeps these unreadable without a signed link; this keeps the store to the
#: kinds of file the requirements describe. An upload endpoint that accepts
#: anything is a file host with an audit trail attached.
ALLOWED_CONTENT_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/heic",
        "image/heif",
        "application/pdf",
    }
)


def _resolve_target(target_type: str, target_id: str):
    """Return the record a file is being attached to, or 404.

    ``objects`` is the tenant manager, so this is also the isolation check: an
    id belonging to another organization simply is not there (§2.1).
    """
    if target_type not in ATTACHABLE_TARGETS:
        raise serializers.ValidationError(
            {
                "target_type": [
                    "Files cannot be attached to this kind of record. "
                    f"Accepted: {', '.join(sorted(ATTACHABLE_TARGETS))}."
                ]
            }
        )

    model = apps.get_model(target_type)
    target = model.objects.filter(pk=target_id).first()
    if target is None:
        raise Http404()
    return target


class AttachmentSerializer(serializers.ModelSerializer):
    """What a client reads back. The file itself is never inlined (N-7)."""

    download_url = serializers.SerializerMethodField()
    uploaded_by_name = serializers.CharField(
        source="uploaded_by.full_name", read_only=True, default=""
    )

    class Meta:
        model = Attachment
        fields = (
            "id",
            "target_type",
            "target_id",
            "filename",
            "content_type",
            "size",
            "kind",
            "uploaded_by",
            "uploaded_by_name",
            "download_url",
            "created_at",
        )
        read_only_fields = fields

    def get_download_url(self, attachment: Attachment) -> str:
        """A signed link that expires, in both storage backends (N-7)."""
        return attachment.download_url()


class AttachmentUploadSerializer(serializers.Serializer):
    """The multipart body. ``target_type`` is a model label, e.g. ``jobs.Job``."""

    target_type = serializers.CharField(max_length=100)
    target_id = serializers.CharField(max_length=64)
    file = serializers.FileField()
    kind = serializers.ChoiceField(
        choices=AttachmentKind.choices, default=AttachmentKind.PHOTO
    )

    def validate_file(self, value):  # type: ignore[no-untyped-def]
        if value.size > max_upload_bytes():
            megabytes = max_upload_bytes() / (1024 * 1024)
            raise serializers.ValidationError(
                f"This file is larger than the {megabytes:.0f} MB limit. "
                "A phone photo is normally well under it."
            )
        content_type = (getattr(value, "content_type", "") or "").lower()
        if content_type and content_type not in ALLOWED_CONTENT_TYPES:
            raise serializers.ValidationError(
                f"{content_type} is not an accepted file type. "
                "Photos and PDFs only."
            )
        return value


class AttachmentViewSet(TenantScopedViewSet):
    """``/api/v1/attachments`` (§6, §4.13).

    Listing is always by target: ``?target_type=jobs.JobCloseout&target_id=12``.
    An unfiltered list of every file in the tenant is not a question any screen
    asks, and answering it would put one organization's whole evidence trail
    behind a single request.
    """

    serializer_class = AttachmentSerializer
    model = Attachment
    select_related = ("uploaded_by",)
    parser_classes = [MultiPartParser, FormParser]
    ordering_fields = ["created_at"]

    # Attachments are evidence: added, read, and removed only by whoever added
    # them (below). Nothing edits one in place.
    http_method_names = ["get", "post", "delete", "head", "options"]

    def get_queryset(self):  # type: ignore[no-untyped-def]
        queryset = super().get_queryset()
        target_type = self.request.query_params.get("target_type")
        target_id = self.request.query_params.get("target_id")
        if target_type and target_id:
            return queryset.filter(target_type=target_type, target_id=str(target_id))
        if self.action == "list":
            # Deliberately empty rather than an error: a screen that has not yet
            # saved its document asks for its attachments with no id, and an
            # empty list is the truthful answer.
            return queryset.none()
        return queryset

    def _require_permission_for(self, target_type: str) -> None:
        """Attaching takes the permission that produces the document."""
        needed = ATTACHABLE_TARGETS[target_type]
        held = resolve_permissions(self.request.user)
        if not any(held.has(codename) for codename in needed):
            from rest_framework.exceptions import PermissionDenied

            raise PermissionDenied(
                "You do not have permission to attach files to this record."
            )

    @extend_schema(
        parameters=[
            OpenApiParameter("target_type", description="Model label, e.g. jobs.Job"),
            OpenApiParameter("target_id", description="Id of that record"),
        ],
        responses={200: AttachmentSerializer(many=True)},
    )
    def list(self, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        return super().list(request, *args, **kwargs)

    @extend_schema(
        request=AttachmentUploadSerializer, responses={201: AttachmentSerializer}
    )
    def create(self, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        upload = AttachmentUploadSerializer(data=request.data)
        upload.is_valid(raise_exception=True)
        data = upload.validated_data

        target = _resolve_target(data["target_type"], data["target_id"])
        self._require_permission_for(data["target_type"])

        attachment = store_attachment(
            target=target,
            uploaded_file=data["file"],
            kind=data["kind"],
            uploaded_by=request.user,
        )
        record(
            AuditAction.ATTACHMENT_ADDED,
            actor=request.user,
            target=attachment,
            target_label=attachment.filename,
            request=request,
            after={"target": f"{target._meta.label}:{target.pk}", "kind": attachment.kind},
            note=f"{attachment.get_kind_display()} attached to {target}.",
        )
        return Response(
            AttachmentSerializer(attachment).data, status=status.HTTP_201_CREATED
        )

    @extend_schema(
        responses={
            200: inline_serializer(
                "AttachmentUrl",
                {
                    "url": serializers.CharField(),
                    "expires_in": serializers.IntegerField(),
                },
            )
        }
    )
    @action(detail=True, methods=["get"])
    def url(self, request, pk=None):  # type: ignore[no-untyped-def]
        """A fresh signed link (§6, N-7).

        Separate from the serializer because links expire: a screen open for ten
        minutes needs to ask again rather than reload the whole document.
        """
        from core.attachments import DEFAULT_EXPIRY_SECONDS

        attachment = self.get_object()
        return Response(
            {
                "url": attachment.download_url(),
                "expires_in": DEFAULT_EXPIRY_SECONDS,
            }
        )

    def perform_destroy(self, instance):  # type: ignore[no-untyped-def]
        """Only whoever uploaded it, and it is recorded.

        A photo taken by accident has to be removable, or people stop taking
        them. Letting anyone remove *another* person's would make the evidence
        deniable, which is the opposite of what D6 and G3 are for.
        """
        if instance.uploaded_by_id != self.request.user.pk:
            from rest_framework.exceptions import PermissionDenied

            raise PermissionDenied(
                "Only the person who uploaded a file can remove it."
            )
        record(
            AuditAction.ATTACHMENT_REMOVED,
            actor=self.request.user,
            target_label=instance.filename,
            request=self.request,
            before={
                "target": f"{instance.target_type}:{instance.target_id}",
                "kind": instance.kind,
            },
            note=f"{instance.filename} removed from {instance.target_type}.",
        )
        instance.delete()


class AttachmentTargetsView(APIView):
    """``/api/v1/attachment-targets`` — what may be attached to, and by whom.

    The frontend needs to know whether to show a camera button before the user
    taps it. Deriving that from the same map the endpoint enforces means the
    button appears exactly when the upload would succeed.
    """

    permission_classes = [IsAuthenticated, OrganizationIsActive]

    @extend_schema(
        responses={
            200: inline_serializer(
                "AttachmentTargets",
                {"targets": serializers.DictField(child=serializers.ListField())},
            )
        }
    )
    def get(self, request):  # type: ignore[no-untyped-def]
        held = resolve_permissions(request.user)
        return Response(
            {
                "max_bytes": max_upload_bytes(),
                "content_types": sorted(ALLOWED_CONTENT_TYPES),
                "targets": {
                    label: any(held.has(codename) for codename in needed)
                    for label, needed in sorted(ATTACHABLE_TARGETS.items())
                },
            }
        )
