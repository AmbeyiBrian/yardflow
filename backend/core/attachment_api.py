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
from django.db import IntegrityError, transaction
from django.db.models import CharField, Q
from django.db.models.functions import Cast
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
from core.exceptions import DomainError
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
    #
    # R1, §4.17.7: recording is open to every member, so a permission cannot be
    # the test; the owner rule below is (see ``OWNER_RULED_TARGETS``).
    "commercials.ProjectExpense": (),
    # R3: the casual's ID photo, taken by whoever registers them.
    "commercials.Casual": (),
    # R15, §4.20.7: a supplier's documents (PIN certificate, bank letter). The
    # registrar adds them while it is PENDING or REJECTED; Finance always.
    "network.Supplier": (),
    # R7-R12, §4.19.9. The three the project's own manager decides are in
    # ``PROJECT_RULED_TARGETS`` (empty here for the same reason as the owner
    # rules): the PO, the acceptance certificate and the subcontract.
    "network.Project": (),
    "network.ProjectSite": (),
    "commercials.Subcontract": (),
    # The invoice behind a subcontract payment and the receipt behind a site
    # purchase are the recorder's, while the entry can still change.
    "commercials.SubcontractPayment": (),
    "commercials.SitePurchase": (),
    # Finance attaches the invoice document to a milestone invoice it recorded.
    "commercials.MilestoneInvoice": (PERM.FINANCE_APPROVE,),
}

#: Targets where the **record's own owner** decides, not a permission (§4.17.7).
#: Their entry in ``ATTACHABLE_TARGETS`` is empty on purpose: any member may
#: attach, but only to what they recorded, and only while it can still change.
OWNER_RULED_TARGETS = frozenset(
    {
        "commercials.ProjectExpense",
        "commercials.Casual",
        "network.Supplier",
        "commercials.SubcontractPayment",
        "commercials.SitePurchase",
    }
)

#: Targets decided by **the project's manager** (§4.19.9): "this project's PM"
#: is a person, not a permission, the same reason ``OWNER_RULED_TARGETS``
#: exists. Each names the permissions that may also attach, besides the PM.
PROJECT_RULED_TARGETS: dict[str, tuple[str, ...]] = {
    "network.Project": (PERM.CATALOGUE_MANAGE, PERM.FINANCE_APPROVE),
    "network.ProjectSite": (PERM.CATALOGUE_MANAGE,),
    "commercials.Subcontract": (PERM.FINANCE_APPROVE,),
}

#: Statuses in which photos may still be added or removed, by kind of entry.
#: After final approval the evidence is fixed: it is what the approval was given
#: on. A subcontract payment has no second level, so it has no PENDING_FINANCE.
_EVIDENCE_OPEN_STATUSES = frozenset({"PENDING_PM", "PENDING_FINANCE", "REJECTED"})
_EVIDENCE_OPEN_BY_TARGET = {
    "commercials.ProjectExpense": _EVIDENCE_OPEN_STATUSES,
    "commercials.SitePurchase": _EVIDENCE_OPEN_STATUSES,
    "commercials.SubcontractPayment": frozenset({"PENDING_PM", "REJECTED"}),
}


class AttachmentLocked(DomainError):
    """The entry was approved, so what it was approved on can no longer change."""

    code = "ATTACHMENT_LOCKED"
    status_code = 409
    default_message = (
        "This entry is approved, so its photos can no longer be added or removed."
    )


def _can_attach_to(held, target_type: str) -> bool:  # type: ignore[no-untyped-def]
    """Whether a user's permissions allow attaching to this kind of record at all."""
    if target_type in OWNER_RULED_TARGETS or target_type in PROJECT_RULED_TARGETS:
        return True
    return any(held.has(codename) for codename in ATTACHABLE_TARGETS[target_type])


def _owner_of(target) -> int | None:  # type: ignore[no-untyped-def]
    """Who recorded or registered an owner-ruled record."""
    if target._meta.label in ("commercials.Casual", "network.Supplier"):
        return target.registered_by_id  # type: ignore[no-any-return]
    return target.recorded_by_id  # type: ignore[no-any-return]


def _require_supplier_documents_open(target, user) -> None:  # type: ignore[no-untyped-def]
    """A supplier's documents are fixed once APPROVED, except by Finance (§4.20.7)."""
    if resolve_permissions(user).has(PERM.FINANCE_APPROVE):
        return
    if target.status not in ("PENDING", "REJECTED"):
        raise AttachmentLocked(
            "This supplier is approved, so its documents can only be changed by Finance."
        )


def _require_evidence_open(target) -> None:  # type: ignore[no-untyped-def]
    """Photos on an expense, purchase or payment are fixed once it is approved or
    paid (§4.17.7, §4.19.9), and a voided milestone invoice's document with it."""
    label = target._meta.label
    open_statuses = _EVIDENCE_OPEN_BY_TARGET.get(label)
    if open_statuses is not None and target.status not in open_statuses:
        raise AttachmentLocked()
    if label == "commercials.MilestoneInvoice" and target.voided_at is not None:
        raise AttachmentLocked("This invoice was voided, so its document is fixed.")


def _project_of_target(target):  # type: ignore[no-untyped-def]
    """The project whose manager rules a project-ruled record."""
    return target if target._meta.label == "network.Project" else target.project

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
            "caption",
            "client_uuid",
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
    # R6: what the photo shows, chosen on the phone (Receipt / Fuel pump / ...).
    caption = serializers.CharField(max_length=60, required=False, allow_blank=True)
    # R6: a repeat of one client_uuid returns the attachment it made, so a photo
    # whose response was lost on a bad connection lands once, not twice.
    client_uuid = serializers.UUIDField(required=False)

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
        queryset = self._without_unseen_id_photos(queryset)
        if target_type and target_id:
            return queryset.filter(target_type=target_type, target_id=str(target_id))
        if self.action == "list":
            # Deliberately empty rather than an error: a screen that has not yet
            # saved its document asks for its attachments with no id, and an
            # empty list is the truthful answer.
            return queryset.none()
        return queryset

    def _without_unseen_id_photos(self, queryset):  # type: ignore[no-untyped-def]
        """A casual's ID photo is for Finance and whoever registered them (§4.17.7).

        Applied to every route (list, retrieve, ``url``), so a photo that is
        filtered out of the list cannot be fetched by id either: it is a 404.
        """
        if resolve_permissions(self.request.user).has(PERM.FINANCE_APPROVE):
            return queryset
        from commercials.models import Casual

        registered = (
            Casual.objects.filter(registered_by=self.request.user)
            .annotate(key=Cast("pk", output_field=CharField()))
            .values("key")
        )
        from network.models import Supplier

        added = (
            Supplier.objects.filter(registered_by=self.request.user)
            .annotate(key=Cast("pk", output_field=CharField()))
            .values("key")
        )
        return queryset.exclude(
            Q(target_type="commercials.Casual") & ~Q(target_id__in=registered)
        ).exclude(Q(target_type="network.Supplier") & ~Q(target_id__in=added))

    def _require_project_rule(self, target) -> None:  # type: ignore[no-untyped-def]
        """The project's manager, or a holder of one of the named permissions."""
        permissions = PROJECT_RULED_TARGETS.get(target._meta.label)
        if permissions is None:
            return
        if _project_of_target(target).manager_id == self.request.user.pk:
            return
        held = resolve_permissions(self.request.user)
        if any(held.has(codename) for codename in permissions):
            return
        from rest_framework.exceptions import PermissionDenied

        raise PermissionDenied(
            "Only this project's manager, or someone with the right to, can attach "
            "documents to it."
        )

    def _require_owner(self, target) -> None:  # type: ignore[no-untyped-def]
        """The recorder (or registrar) only, and only while the entry can change."""
        self._require_project_rule(target)
        if target._meta.label not in OWNER_RULED_TARGETS:
            # Not the recorder's to decide, but a voided invoice's document is
            # still fixed.
            _require_evidence_open(target)
            return
        if target._meta.label == "network.Supplier":
            if resolve_permissions(self.request.user).has(PERM.FINANCE_APPROVE):
                return
            if _owner_of(target) != self.request.user.pk:
                from rest_framework.exceptions import PermissionDenied

                raise PermissionDenied(
                    "Only the person who added this supplier, or Finance, can add documents."
                )
            _require_supplier_documents_open(target, self.request.user)
            return
        if _owner_of(target) != self.request.user.pk:
            from rest_framework.exceptions import PermissionDenied

            raise PermissionDenied(
                "Only the person who recorded this can add photos to it."
            )
        _require_evidence_open(target)

    def _require_permission_for(self, target_type: str) -> None:
        """Attaching takes the permission that produces the document."""
        held = resolve_permissions(self.request.user)
        if not _can_attach_to(held, target_type):
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

        client_uuid = data.get("client_uuid")
        if client_uuid is not None:
            replay = self._replay(client_uuid, target, request.user)
            if replay is not None:
                return Response(AttachmentSerializer(replay).data, status=status.HTTP_200_OK)

        self._require_owner(target)

        try:
            with transaction.atomic():
                attachment = store_attachment(
                    target=target,
                    uploaded_file=data["file"],
                    kind=data["kind"],
                    uploaded_by=request.user,
                )
                attachment.caption = data.get("caption", "")
                attachment.client_uuid = client_uuid
                attachment.save(update_fields=["caption", "client_uuid"])
        except IntegrityError:
            # Two sends of one client_uuid raced past the check above; the unique
            # constraint kept one, and the other is simply a replay.
            replay = self._replay(client_uuid, target, request.user) if client_uuid else None
            if replay is None:
                raise
            return Response(AttachmentSerializer(replay).data, status=status.HTTP_200_OK)
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

    @staticmethod
    def _replay(client_uuid, target, user):  # type: ignore[no-untyped-def]
        """The attachment this ``client_uuid`` already made, if it is the same upload.

        The same uuid on a different record or from a different person is not a
        replay but a clash, and is refused rather than returning somebody else's
        file.
        """
        existing = Attachment.objects.filter(client_uuid=client_uuid).first()
        if existing is None:
            return None
        if (
            existing.target_type != target._meta.label
            or existing.target_id != str(target.pk)
            or existing.uploaded_by_id != user.pk
        ):
            raise serializers.ValidationError(
                {"client_uuid": ["This upload id was already used for another file."]}
            )
        return existing

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
        # §4.17.7: nobody removes evidence from an approved or paid expense.
        # §4.19.9: and likewise on a purchase, a subcontract payment or a voided
        # milestone invoice. The PO, certificate and contract stay removable.
        if (
            instance.target_type in _EVIDENCE_OPEN_BY_TARGET
            or instance.target_type == "commercials.MilestoneInvoice"
        ):
            held_target = (
                apps.get_model(instance.target_type)
                .objects.filter(pk=instance.target_id)
                .first()
            )
            if held_target is not None:
                _require_evidence_open(held_target)
        if instance.target_type == "network.Supplier":
            from network.models import Supplier

            supplier = Supplier.objects.filter(pk=instance.target_id).first()
            if supplier is not None:
                _require_supplier_documents_open(supplier, self.request.user)
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
                    label: _can_attach_to(held, label)
                    for label in sorted(ATTACHABLE_TARGETS)
                },
            }
        )
