"""Offline sync (design §8.2, §8.4; N1–N3).

Two tables, each closing a specific failure the field will otherwise produce.

**SyncSubmission** is idempotency (§8.2, N2). A phone with one bar retries; the
retry must not create a second gate-in. So the client generates a ``client_uuid``
per mutation and the server remembers what that uuid produced — a replay returns
the original document rather than another one.

**SyncException** is the honest answer to a conflict (§8.4, N3). When a queued
document is no longer valid — the stock moved, the serial was issued by somebody
else while the phone was offline — it is *neither* force-posted *nor* dropped.
Both of those corrupt something: force-posting corrupts the balance, dropping
loses the storekeeper's work and their trust with it. It lands here with its
payload and reason, and somebody resolves it.

§8.4 is emphatic about that, and it is the reason this app exists rather than the
sync endpoint simply returning 409 and forgetting.
"""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from core.models import TimeStampedModel
from core.tenancy import TenantModel


class SyncOperation(models.TextChoices):
    """What a queued mutation was trying to do (§8, N1).

    Deliberately short. §8 is explicit that offline capture is "**gate-in and
    gate-out capture only**" — a queue that accepted everything would be a second
    write path around every control in the system.
    """

    GATE_IN = "GATE_IN", "Receive a delivery"
    GATE_OUT_REQUEST = "GATE_OUT_REQUEST", "Request material"
    GATE_OUT_RELEASE = "GATE_OUT_RELEASE", "Release an already-approved pass"


class SubmissionStatus(models.TextChoices):
    APPLIED = "APPLIED", "Applied"
    #: Revalidated and refused. The reason is on the SyncException.
    REJECTED = "REJECTED", "Rejected"


class SyncSubmission(TenantModel, TimeStampedModel):
    """One offline mutation, and what it produced (§8.2, N2).

    The unique constraint is the whole mechanism: ``(organization, client_uuid)``.
    A second arrival of the same uuid cannot insert, so the endpoint looks the
    original up and returns it — which is why ten submissions of the same payload
    yield exactly one document (T8.4).
    """

    client_uuid = models.UUIDField()
    operation = models.CharField(max_length=30, choices=SyncOperation.choices)
    status = models.CharField(
        max_length=20, choices=SubmissionStatus.choices, default=SubmissionStatus.APPLIED
    )

    #: What it became, as ``(label, id)`` text — the same generic reference the
    #: approval and attachment tables use, so one submission table serves every
    #: kind of document without a column per type.
    document_type = models.CharField(max_length=100, blank=True)
    document_id = models.CharField(max_length=64, blank=True)
    document_number = models.CharField(max_length=50, blank=True)

    #: The payload as submitted, kept verbatim. When a conflict is resolved days
    #: later this is the only record of what the person on site actually said.
    payload = models.JSONField(default=dict, blank=True)

    submitted_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    #: When the phone captured it, as distinct from when it reached us. A
    #: delivery received at 08:00 and synced at 17:00 belongs to the morning.
    captured_at = models.DateTimeField(null=True, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "client_uuid"],
                name="uniq_sync_submission_per_org",
            ),
        ]
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=["organization", "status"]),
            models.Index(fields=["organization", "operation"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_operation_display()} {self.client_uuid}"

    @property
    def document(self):
        """The document this produced, or ``None`` if it was rejected."""
        from django.apps import apps

        if not self.document_type or not self.document_id:
            return None
        try:
            model = apps.get_model(self.document_type)
        except LookupError:
            return None
        return model.objects.filter(pk=self.document_id).first()


class ExceptionStatus(models.TextChoices):
    OPEN = "OPEN", "Open"
    #: Resolved by re-submitting a corrected version, or by deciding it was
    #: already handled. Either way somebody wrote down which.
    RESOLVED = "RESOLVED", "Resolved"
    #: Deliberately abandoned — the material was never received, the request is
    #: moot. Not a deletion: the record of the attempt stays.
    DISCARDED = "DISCARDED", "Discarded"


class SyncException(TenantModel, TimeStampedModel):
    """A queued document the server could not accept (§8.4, N3).

    §8.4: "**not** force-posted and **not** silently dropped." This row is what
    that third option looks like — the payload, the reason, and somebody's
    decision about it.
    """

    submission = models.OneToOneField(
        SyncSubmission, on_delete=models.CASCADE, related_name="exception"
    )
    status = models.CharField(
        max_length=20, choices=ExceptionStatus.choices, default=ExceptionStatus.OPEN, db_index=True
    )

    #: The stable code from the DomainError that refused it, so a client can
    #: switch on it and a screen can explain it in the storekeeper's terms.
    code = models.CharField(max_length=50, blank=True)
    reason = models.TextField()
    #: Whatever the refusal carried — which serial, which drum, how much was
    #: left. This is what makes an exception resolvable rather than merely
    #: reportable.
    details = models.JSONField(default=dict, blank=True)

    resolution = models.CharField(max_length=500, blank=True)
    resolved_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    #: Where a corrected re-submission ended up, when there was one.
    replacement_submission = models.ForeignKey(
        SyncSubmission,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="replaces",
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(status="OPEN", resolved_at__isnull=True)
                    | ~Q(status="OPEN")
                ),
                name="an_open_sync_exception_is_not_resolved",
            ),
        ]
        ordering = ("-created_at", "-id")
        indexes = [models.Index(fields=["organization", "status"])]

    def __str__(self) -> str:
        return f"{self.code or 'Conflict'}: {self.reason[:60]}"

    @property
    def is_open(self) -> bool:
        """M1: open exceptions are what the register lists."""
        return self.status == ExceptionStatus.OPEN
