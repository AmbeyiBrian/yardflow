"""Platform-level and cross-cutting models.

Design §4.1 (Organization, OrganizationSettings). Requirements A1, A2, A4, C8.

``Organization`` is deliberately *not* a :class:`~core.tenancy.TenantModel` — it
is the tenant, so it cannot be scoped to one.
"""

from __future__ import annotations

import uuid

from django.core.validators import RegexValidator
from django.db import models

from core.tenancy import TenantModel


class TimeStampedModel(models.Model):
    """Audit columns carried by every model (design §4)."""

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        editable=False,
        related_name="+",
    )

    class Meta:
        abstract = True


# A subdomain: lowercase letters, digits and hyphens, not starting or ending
# with a hyphen. Immutable once set (A1).
subdomain_validator = RegexValidator(
    regex=r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$",
    message=(
        "A subdomain may contain only lowercase letters, digits and hyphens, "
        "and may not begin or end with a hyphen."
    ),
)

# Subdomains that must never be handed to a tenant, because they address the
# platform itself (§2.2).
RESERVED_SUBDOMAINS = frozenset(
    {"admin", "api", "www", "app", "static", "media", "mail", "internal", "health"}
)


class Organization(TimeStampedModel):  # type: ignore[django-manager-missing]
    """One customer company. Silvertech is tenant #1 (A1).

    The primary key is a UUID because it is written into the Postgres session
    setting ``app.current_org`` and compared as a uuid by every row-level
    security policy (§2.3).
    """

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        SUSPENDED = "SUSPENDED", "Suspended"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    name = models.CharField(max_length=200)

    # A2: suspension never deletes data. A suspended tenant's users can still
    # log in and read; every write is refused with ORGANIZATION_SUSPENDED.
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.ACTIVE, db_index=True
    )

    # A1: unique across the platform and immutable after creation. Enforced in
    # `save()` because a database constraint cannot express "unchanged".
    slug = models.SlugField(
        max_length=63,  # a DNS label cannot exceed 63 octets
        unique=True,
        validators=[subdomain_validator],
        help_text="Subdomain addressing this tenant, e.g. 'silvertech'. Immutable.",
    )

    # A4: branding on printed gate passes, GRNs and waybills.
    logo = models.FileField(upload_to="organizations/logos/", blank=True, null=True)
    legal_name = models.CharField(max_length=255, blank=True)
    tax_pin = models.CharField(max_length=50, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)

    class Meta:
        ordering = ("name",)

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.slug = (self.slug or "").lower()

        if self.slug in RESERVED_SUBDOMAINS:
            raise ValueError(
                f"'{self.slug}' is reserved for the platform and cannot be used "
                f"as a tenant subdomain."
            )

        # A1: immutable after creation. Changing it would silently break every
        # bookmark, deep link and QR code already in the field.
        if self.pk:
            previous_slug = (
                Organization.objects.filter(pk=self.pk)
                .values_list("slug", flat=True)
                .first()
            )
            if previous_slug is not None and previous_slug != self.slug:
                raise ValueError(
                    f"An organization subdomain is immutable: cannot change "
                    f"'{previous_slug}' to '{self.slug}' (A1)."
                )

        return super().save(*args, **kwargs)

    @property
    def is_suspended(self) -> bool:
        return self.status == self.Status.SUSPENDED


class OrganizationSettings(models.Model):
    """Per-tenant configuration. Covers all of requirement C8 (design §4.1).

    Every default here is the one the requirements specify. The conservative
    ones — money tracking, self-approval and document amendment all **off** —
    are load bearing: a tenant that never opens the settings screen still gets
    the controlled behaviour the system exists to provide.
    """

    organization = models.OneToOneField(
        Organization, on_delete=models.CASCADE, related_name="settings", primary_key=True
    )

    # --- Money (D16) ------------------------------------------------------
    # Quantities are the primary currency. Costs stay hidden until asked for.
    money_tracking_enabled = models.BooleanField(default=False)

    # --- Minimum stock (E6) ----------------------------------------------
    min_stock_enabled = models.BooleanField(default=False)

    # --- Labels and asset tags (C8) --------------------------------------
    qr_labels_enabled = models.BooleanField(default=False)
    asset_tag_enabled = models.BooleanField(default=False)
    asset_tag_prefix_format = models.CharField(
        max_length=100,
        default="{org}-{category}-{seq:06d}",
        help_text="Format for generated internal asset tags, e.g. SLV-RRU-000123.",
    )

    # --- Client returns (K2) ---------------------------------------------
    client_waybill_enabled = models.BooleanField(default=False)

    # --- Attachments (D6, G3) --------------------------------------------
    attachments_enabled = models.BooleanField(default=True)
    attachments_required_gate_in = models.BooleanField(default=False)
    attachments_required_gate_out = models.BooleanField(default=False)
    signature_required_on_release = models.BooleanField(default=False)

    # --- Approvals -------------------------------------------------------
    # Q3: an approved gate pass not released within this window expires.
    gate_pass_expiry_hours = models.PositiveIntegerField(default=24)

    # F3: a requester must not approve their own request. Default off, and
    # turning it on is a deliberate act.
    allow_self_approval = models.BooleanField(default=False)

    # F5: unanswered approvals escalate to the configured fallback.
    approval_escalation_hours = models.PositiveIntegerField(default=24)

    # O7: single-signature approval on project material (D22) should not also be
    # unwatched. Above this, the owner is told after the fact — it blocks
    # nothing, because the point is visibility, not a second gate.
    project_release_notify_above = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=(
            "Tell the owner when project material above this value is approved, "
            "excluding VAT. Empty means never."
        ),
    )

    # --- Documents and retention -----------------------------------------
    # M4: posted documents are immutable. Corrections are reversals, not edits.
    allow_document_amendment = models.BooleanField(default=False)

    # M5: retention never silently deletes; expiry flags records for review.
    retention_months = models.PositiveIntegerField(default=84)  # 7 years

    # --- Locale (N-9) ----------------------------------------------------
    timezone = models.CharField(max_length=64, default="Africa/Nairobi")
    currency = models.CharField(max_length=3, default="KES")

    # --- SMS credits (L4) -------------------------------------------------
    # A cache of `SUM(SmsCreditEntry.quantity)`, kept in step inside the same
    # transaction as the entry that changed it. The ledger is the truth; this
    # exists so a screen can show the balance without summing a table, exactly
    # as StockBalance does for the stock ledger (§3.3).
    sms_credit_balance = models.IntegerField(default=0)
    #: Below this, the notifications screen warns. The failure being prevented is
    #: silent — messages that simply stop arriving.
    sms_credit_low_threshold = models.PositiveIntegerField(default=50)

    # --- Notifications (L1, L2) ------------------------------------------
    # Channels active for this tenant. WhatsApp ships written but disabled
    # pending Meta sender approval (Q1, §9.2).
    notification_channels = models.JSONField(default=dict, blank=True)
    # Event -> {recipients, channels}. Seeded from the L2 default matrix.
    notification_matrix = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name_plural = "organization settings"

    def __str__(self) -> str:
        return f"Settings for {self.organization.name}"


# --------------------------------------------------------------------------
# Audit trail (§4.2, M3, B6)
# --------------------------------------------------------------------------


class AuditAction(models.TextChoices):
    """What happened. Extended as later phases add events."""

    LOGIN_SUCCEEDED = "LOGIN_SUCCEEDED", "Login succeeded"
    LOGIN_FAILED = "LOGIN_FAILED", "Login failed"
    LOGOUT = "LOGOUT", "Logout"
    PASSWORD_RESET_REQUESTED = "PASSWORD_RESET_REQUESTED", "Password reset requested"
    PASSWORD_CHANGED = "PASSWORD_CHANGED", "Password changed"

    USER_CREATED = "USER_CREATED", "User created"
    USER_UPDATED = "USER_UPDATED", "User updated"
    USER_DEACTIVATED = "USER_DEACTIVATED", "User deactivated"
    PERMISSION_CHANGED = "PERMISSION_CHANGED", "Permission changed"
    ROLE_CREATED = "ROLE_CREATED", "Role created"
    ROLE_UPDATED = "ROLE_UPDATED", "Role updated"
    ROLE_DELETED = "ROLE_DELETED", "Role deleted"

    SETTINGS_CHANGED = "SETTINGS_CHANGED", "Settings changed"
    ORGANIZATION_SUSPENDED = "ORGANIZATION_SUSPENDED", "Organization suspended"
    ORGANIZATION_REINSTATED = "ORGANIZATION_REINSTATED", "Organization reinstated"

    DOCUMENT_POSTED = "DOCUMENT_POSTED", "Document posted"
    DOCUMENT_VOIDED = "DOCUMENT_VOIDED", "Document voided"
    DOCUMENT_AMENDED = "DOCUMENT_AMENDED", "Document amended"
    STATUS_CHANGED = "STATUS_CHANGED", "Status changed"

    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"

    CUSTODY_TRANSFERRED = "CUSTODY_TRANSFERRED", "Custody transferred"
    VARIANCE_RAISED = "VARIANCE_RAISED", "Variance raised"
    VARIANCE_RESOLVED = "VARIANCE_RESOLVED", "Variance resolved"

    # D6, G3, H2: a photo is evidence, so adding or removing one is an event in
    # its own right. Without this, a site photo could disappear from a closeout
    # and the trail would show nothing at all.
    ATTACHMENT_ADDED = "ATTACHMENT_ADDED", "Attachment added"
    ATTACHMENT_REMOVED = "ATTACHMENT_REMOVED", "Attachment removed"


class AuthMethod(models.TextChoices):
    """How the actor proved who they were.

    Recorded on approvals because it is the non-repudiation evidence an ISO
    auditor asks for (F4, M3).
    """

    PASSWORD = "PASSWORD", "Password"
    WEBAUTHN = "WEBAUTHN", "Fingerprint or platform authenticator"
    SYSTEM = "SYSTEM", "Automatic, no human actor"


class AuditLog(TenantModel):
    """Append-only record of who did what, when (M3, B6).

    Append-only **in the database** (T1.14), not merely in Python: an audit trail
    the application can rewrite is not an audit trail. No tenant user has any
    path to change or delete a row here, whatever their permissions, and
    regardless of the ``allow_document_amendment`` setting (M4).

    Failed logins that cannot be attributed to an organization — an unknown
    subdomain, say — are written to the structured application log instead
    (N-11), because a tenant's audit trail must not contain another tenant's
    events.
    """

    # Not TimeStampedModel: an audit row is never updated, so `updated_at` would
    # be a field that must always equal `created_at`.
    occurred_at = models.DateTimeField(auto_now_add=True, db_index=True)

    actor = models.ForeignKey(
        "accounts.User",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="audit_entries",
        help_text=(
            "Null for automatic actions, and for failed logins with an "
            "unknown identifier."
        ),
    )
    # Kept as text as well as a foreign key: the FK explains who they are now,
    # this records how they identified themselves at the time.
    actor_identifier = models.CharField(max_length=254, blank=True)

    action = models.CharField(max_length=40, choices=AuditAction.choices, db_index=True)

    # Generic target, so any model can be audited without a schema change.
    target_type = models.CharField(max_length=100, blank=True, db_index=True)
    target_id = models.CharField(max_length=64, blank=True, db_index=True)
    target_label = models.CharField(
        max_length=255,
        blank=True,
        help_text="How the target read at the time, e.g. a document number.",
    )

    # M3: before/after values.
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)

    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=400, blank=True)
    auth_method = models.CharField(
        max_length=20, choices=AuthMethod.choices, blank=True
    )

    note = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ("-occurred_at",)
        indexes = [
            models.Index(fields=["organization", "-occurred_at"]),
            models.Index(fields=["organization", "target_type", "target_id"]),
            models.Index(fields=["organization", "action", "-occurred_at"]),
        ]

    def __str__(self) -> str:
        who = self.actor_identifier or "system"
        return f"{self.occurred_at:%Y-%m-%d %H:%M} {self.action} by {who}"

    def save(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self.pk is not None and not self._state.adding:
            raise ValueError(
                "The audit trail is append-only and cannot be edited by any user (M3)."
            )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise ValueError("The audit trail is append-only and cannot be deleted.")


# --------------------------------------------------------------------------
# Document numbering (§4.13, M6)
# --------------------------------------------------------------------------
# The model lives in core.numbering alongside the allocation logic, and is
# re-exported here so `core.models` remains the single place to look for a
# model. See core/numbering.py for why a locked counter row rather than a
# Postgres sequence.

from core.numbering import DocumentSequence, DocumentType  # noqa: E402, F401

# --------------------------------------------------------------------------
# Attachments (§4.13, D6, G3, N-7)
# --------------------------------------------------------------------------


class AttachmentKind(models.TextChoices):
    PHOTO = "PHOTO", "Photo"
    DOCUMENT = "DOCUMENT", "Document"
    SIGNATURE = "SIGNATURE", "Signature"


def attachment_upload_path(instance, filename: str) -> str:
    """Partition by organization, so one tenant's files never sit among another's."""
    return f"attachments/{instance.organization_id}/{instance.kind.lower()}/{filename}"


class Attachment(TenantModel, TimeStampedModel):
    """A photo, document or signature attached to any record (§4.13).

    D6 (delivery notes and photos at gate-in), G3 (signature or photo at
    release), K3 (the client's signed acknowledgement).

    N-7: **never publicly readable.** Every read goes through a signed,
    time-limited URL. Locally that is a signed Django view rather than a raw
    ``/media/`` path, so the contract is identical in development and
    production and a missing-authorisation bug cannot hide until deployment.
    """

    # Generic owner as (label, id) text rather than Django's contenttypes
    # framework: it avoids a join and a migration dependency for something only
    # ever read back by the owning record.
    target_type = models.CharField(max_length=100, db_index=True)
    target_id = models.CharField(max_length=64, db_index=True)

    file = models.FileField(upload_to=attachment_upload_path)
    filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100, blank=True)
    size = models.PositiveBigIntegerField(default=0)
    kind = models.CharField(
        max_length=20, choices=AttachmentKind.choices, default=AttachmentKind.PHOTO
    )
    uploaded_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        indexes = [models.Index(fields=["organization", "target_type", "target_id"])]
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return self.filename

    def download_url(self, *, expires_in: int | None = None) -> str:
        """A signed, expiring URL for this attachment (N-7).

        Same contract under both storage backends: S3 issues a pre-signed GET,
        local storage issues a signed view URL. Neither is guessable and neither
        lasts.
        """
        from core.attachments import signed_download_url

        return signed_download_url(self, expires_in=expires_in)
