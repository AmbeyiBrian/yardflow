"""Notification records (design §9; L1, L2, L3).

L3 is the requirement that shapes this: "**failure of a notification never blocks
the underlying transaction.**" So a notification is a *record* that something
should be sent, written and then dispatched after commit — never an outbound call
in the middle of approving a gate pass.
"""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from core.models import TimeStampedModel
from core.tenancy import TenantModel


class DeliveryStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    SENT = "SENT", "Sent"
    FAILED = "FAILED", "Failed"
    # L3: "delivery status is visible to admins." A permanent failure is a
    # distinct state from one still being retried, because only one of them needs
    # a human.
    ABANDONED = "ABANDONED", "Abandoned after retries"
    #: The channel was switched off between queueing and sending.
    SKIPPED = "SKIPPED", "Skipped — channel not active"


class NotificationEvent(TenantModel, TimeStampedModel):
    """Something happened that people may need telling about (§9.1).

    Recorded first, delivered afterwards. Keeping the event separate from its
    deliveries means a failed SMS can be retried without re-deciding who should
    have been told, and an admin can see that the event happened even if every
    channel failed.
    """

    event_key = models.CharField(max_length=60, db_index=True)

    # What it was about. Generic, like the audit trail, so a new document type
    # needs no migration here.
    target_type = models.CharField(max_length=100, blank=True)
    target_id = models.CharField(max_length=64, blank=True)
    target_label = models.CharField(max_length=255, blank=True)

    #: Rendering context — the document number, quantities, who requested it.
    payload = models.JSONField(default=dict, blank=True)

    occurred_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-occurred_at",)
        indexes = [
            models.Index(fields=["organization", "event_key", "-occurred_at"]),
            models.Index(fields=["organization", "target_type", "target_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.event_key} — {self.target_label}"


class NotificationDelivery(TenantModel, TimeStampedModel):
    """One attempt to reach one person on one channel (§9.1, L3).

    A row per recipient *and* channel, so "the owner got the in-app message but
    the SMS failed" is representable — which is what an admin needs to see when
    someone says they were never told.
    """

    event = models.ForeignKey(
        NotificationEvent, on_delete=models.CASCADE, related_name="deliveries"
    )
    recipient = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="notification_deliveries"
    )
    channel = models.CharField(max_length=20, db_index=True)

    #: Where it was actually sent — the address at the time, which may since have
    #: changed. Needed to answer "what number did you use?".
    destination = models.CharField(max_length=254, blank=True)

    subject = models.CharField(max_length=255, blank=True)
    body = models.TextField(blank=True)

    status = models.CharField(
        max_length=20, choices=DeliveryStatus.choices, default=DeliveryStatus.PENDING, db_index=True
    )
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=500, blank=True)
    provider_reference = models.CharField(max_length=200, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    # In-app deliveries are read in the app rather than sent anywhere (L1).
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["organization", "recipient", "-created_at"]),
            models.Index(fields=["organization", "status"]),
            # The unread badge query (T4.24).
            models.Index(fields=["organization", "recipient", "read_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["event", "recipient", "channel"],
                name="uniq_delivery_per_event_recipient_channel",
            ),
            models.CheckConstraint(
                condition=~Q(status=DeliveryStatus.SENT) | Q(sent_at__isnull=False),
                name="a_sent_delivery_records_when",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.channel} to {self.recipient}: {self.status}"

    @property
    def is_unread(self) -> bool:
        return self.read_at is None


class SmsCreditEntry(TenantModel, TimeStampedModel):
    """Money, as a ledger (L4; §9.1a).

    SMS is the only channel with a per-message cost, so it is the only one
    metered. One SMS is one credit; one credit is KES 1.

    Append-only, for the same reason the stock ledger is: **a balance somebody
    can type is a balance nobody can defend.** Every purchase and every message
    is an entry, the balance is their sum, and the figure cached on
    `OrganizationSettings.sms_credit_balance` is a convenience that can always be
    checked against this.
    """

    class Kind(models.TextChoices):
        PURCHASE = "PURCHASE", "Credits bought"
        CONSUMPTION = "CONSUMPTION", "Message sent"
        #: A correction — a refund for a message the provider charged for and
        #: never delivered, or a goodwill credit. Always with a note.
        ADJUSTMENT = "ADJUSTMENT", "Adjustment"

    kind = models.CharField(max_length=20, choices=Kind.choices, db_index=True)

    #: Signed: positive buys, negative spends. One field rather than two, so the
    #: balance is a sum and cannot disagree with itself.
    quantity = models.IntegerField()

    #: What this paid for, when it was a send. Null for purchases.
    delivery = models.ForeignKey(
        "notifications.NotificationDelivery",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="credit_entries",
    )

    #: Who bought or adjusted. Null for consumption, which nobody performs.
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, null=True, blank=True
    )

    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["organization", "-created_at"])]
        verbose_name_plural = "SMS credit entries"

    def __str__(self) -> str:
        sign = "+" if self.quantity >= 0 else ""
        return f"{sign}{self.quantity} ({self.get_kind_display()})"
