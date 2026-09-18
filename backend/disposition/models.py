"""Disposition and disposal (design §4.11; J1, J2, J3).

Two documents, and the distinction between them is the point.

A **Disposition** decides what happens to something sitting in quarantine. J2:
"move quarantined material to repair, back to serviceable, to the client, or to
scrap, **so that it does not sit there forever**" — the requirement is about
quarantine not becoming a graveyard, so the document exists to force a decision
and record who made it.

A **Disposal** is the write-off itself: material leaving the books for good. J3
requires approval, and §5.2 makes one part of that non-configurable — disposing
of client-owned material always needs a sign-off, whatever a tenant's rules say.
That check lives in the approvals engine; what lives here is the property it
reads.

Why SCRAP is two documents rather than one: deciding a faulty radio is scrap and
actually destroying it are different acts, often days apart and often by
different people. Collapsing them would mean either approving a decision nobody
has taken yet, or destroying material on the authority of a triage note.
"""

from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q

from core.models import TimeStampedModel
from core.tenancy import TenantModel
from stock.models import Condition, OwnerType


class DispositionDecision(models.TextChoices):
    """§4.11's four outcomes (J2).

    Each says where the material goes, and the ledger movement follows from it:

    * ``REPAIR`` — off to a repair vendor, still ours, still not issuable
    * ``RESTORE_TO_SERVICEABLE`` — back into free stock, condition changed
    * ``RETURN_TO_CLIENT`` — theirs to deal with, via a gate-out (K1)
    * ``SCRAP`` — bound for disposal, which is its own approved document (J3)
    """

    REPAIR = "REPAIR", "Send for repair"
    RESTORE_TO_SERVICEABLE = "RESTORE_TO_SERVICEABLE", "Restore to serviceable"
    RETURN_TO_CLIENT = "RETURN_TO_CLIENT", "Return to the client"
    SCRAP = "SCRAP", "Scrap"


class DispositionStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    PENDING_APPROVAL = "PENDING_APPROVAL", "Awaiting approval"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    POSTED = "POSTED", "Posted"
    CANCELLED = "CANCELLED", "Cancelled"


class Disposition(TenantModel, TimeStampedModel):
    """A decision about quarantined material (J2, §4.11)."""

    number = models.CharField(max_length=50, blank=True, db_index=True)
    status = models.CharField(
        max_length=20,
        choices=DispositionStatus.choices,
        default=DispositionStatus.DRAFT,
        db_index=True,
        # Moved only through the service, like a gate-out's (§4.7).
        editable=False,
    )

    decision = models.CharField(max_length=30, choices=DispositionDecision.choices)
    # J2: "each outcome is an explicit, approved decision with a recorded
    # reason." Not blank=True — a disposition without a reason is the thing this
    # document exists to prevent.
    reason = models.CharField(max_length=500)

    #: Where the material is being taken from — a quarantine location.
    from_location = models.ForeignKey(
        "locations.Location", on_delete=models.PROTECT, related_name="dispositions"
    )
    #: RESTORE_TO_SERVICEABLE needs somewhere to put it back; the others do not.
    to_location = models.ForeignKey(
        "locations.Location",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="inbound_dispositions",
    )
    #: REPAIR names who has it, so it can be chased.
    vendor_name = models.CharField(max_length=200, blank=True)
    expected_return_date = models.DateField(null=True, blank=True)

    decided_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    posted_at = models.DateTimeField(null=True, blank=True)
    posted_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    #: Set when SCRAP material reaches an actual disposal (J3).
    disposal = models.ForeignKey(
        "disposition.Disposal",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="dispositions",
    )

    reject_reason = models.CharField(max_length=500, blank=True)
    cancel_reason = models.CharField(max_length=500, blank=True)
    notes = models.TextField(blank=True)

    # N2: offline idempotency, as everywhere else.
    client_uuid = models.UUIDField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "number"],
                condition=Q(number__gt=""),
                name="uniq_disposition_number_per_org",
            ),
            models.UniqueConstraint(
                fields=["organization", "client_uuid"],
                condition=Q(client_uuid__isnull=False),
                name="uniq_disposition_client_uuid_per_org",
            ),
        ]
        ordering = ("-created_at", "-id")
        indexes = [models.Index(fields=["organization", "status", "decision"])]

    def __str__(self) -> str:
        return f"{self.number or 'Disposition'} — {self.get_decision_display()}"

    @property
    def involves_client_owned_material(self) -> bool:
        """Read by the approvals engine's hardcoded escalation (§5.2, J3).

        A disposition of somebody else's property is never a decision to take
        unilaterally, so this is what makes the sign-off unavoidable — for
        RETURN_TO_CLIENT it is the ordinary case, and for SCRAP it is the one
        that matters most.
        """
        return self.lines.filter(owner_client__isnull=False).exists()

    @property
    def is_posted(self) -> bool:
        return self.status == DispositionStatus.POSTED

    def clean(self) -> None:
        super().clean()

        if self.decision == DispositionDecision.RESTORE_TO_SERVICEABLE and not self.to_location_id:
            raise ValidationError(
                {
                    "to_location": (
                        "Restoring to serviceable has to say where the material "
                        "goes back to, or it would leave quarantine for nowhere "
                        "(J2)."
                    )
                }
            )
        if self.decision == DispositionDecision.REPAIR and not self.vendor_name:
            raise ValidationError(
                {
                    "vendor_name": (
                        "A repair has to name who has the material, or nobody can "
                        "chase it (J2, I3)."
                    )
                }
            )

    def save(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.full_clean(exclude=["organization", "disposal"])
        return super().save(*args, **kwargs)


class DispositionLine(TenantModel, TimeStampedModel):
    """One quarantined thing and what is being done with it (J2)."""

    disposition = models.ForeignKey(
        Disposition, on_delete=models.CASCADE, related_name="lines"
    )
    item_type = models.ForeignKey(
        "catalogue.ItemType", on_delete=models.PROTECT, related_name="disposition_lines"
    )
    serial_unit = models.ForeignKey(
        "stock.SerialUnit", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    reel = models.ForeignKey(
        "stock.Reel", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    quantity = models.DecimalField(
        max_digits=14, decimal_places=3, validators=[MinValueValidator(Decimal("0.001"))]
    )
    uom = models.CharField(max_length=20)

    #: The condition it is in now — which is why it is in quarantine.
    condition = models.CharField(
        max_length=20, choices=Condition.choices, default=Condition.FAULTY
    )
    #: What it becomes. Only RESTORE_TO_SERVICEABLE changes this, and that
    #: change is the whole of "returns stock to availability" (J2, T6.1).
    to_condition = models.CharField(max_length=20, choices=Condition.choices, blank=True)

    owner_type = models.CharField(
        max_length=10, choices=OwnerType.choices, default=OwnerType.OWN
    )
    owner_client = models.ForeignKey(
        "network.Client",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="disposition_lines",
    )

    line_number = models.PositiveIntegerField(default=1)
    notes = models.CharField(max_length=500, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(owner_type=OwnerType.CLIENT, owner_client__isnull=False)
                    | Q(owner_type=OwnerType.OWN, owner_client__isnull=True)
                ),
                name="client_owned_disposition_line_names_its_client",
            ),
        ]
        ordering = ("line_number", "id")

    def __str__(self) -> str:
        return f"{self.quantity} {self.uom} {self.item_type}"

    def clean(self) -> None:
        super().clean()
        # Q5, as everywhere: a serialized unit is disposed of whole.
        if self.serial_unit_id and self.quantity != 1:
            raise ValidationError(
                {"quantity": "A serialized unit is dispositioned whole — one per line (Q5)."}
            )

    def save(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.full_clean(exclude=["organization", "disposition"])
        return super().save(*args, **kwargs)


class DisposalMethod(models.TextChoices):
    """How it was destroyed or got rid of (J3, §4.11).

    Named methods rather than free text because a disposal certificate has to
    say something specific, and "disposed of" satisfies nobody auditing it.
    """

    SCRAP_DEALER = "SCRAP_DEALER", "Sold or given to a scrap dealer"
    LICENSED_HANDLER = "LICENSED_HANDLER", "Collected by a licensed e-waste handler"
    DESTROYED_ON_SITE = "DESTROYED_ON_SITE", "Destroyed on our premises"
    RETURNED_TO_SUPPLIER = "RETURNED_TO_SUPPLIER", "Returned to the supplier"
    OTHER = "OTHER", "Other — see the note"


class DisposalStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    PENDING_APPROVAL = "PENDING_APPROVAL", "Awaiting approval"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    #: Posted: the material has left the books and the certificate can print.
    DISPOSED = "DISPOSED", "Disposed"
    CANCELLED = "CANCELLED", "Cancelled"


class Disposal(TenantModel, TimeStampedModel):
    """Material leaving the books for good (J3, §4.11).

    Always approved before anything moves — there is no path from DRAFT to
    DISPOSED, which is the same shape as a gate-out's status machine and for the
    same reason: a write-off nobody authorised is indistinguishable from theft.
    """

    number = models.CharField(max_length=50, blank=True, db_index=True)
    status = models.CharField(
        max_length=20,
        choices=DisposalStatus.choices,
        default=DisposalStatus.DRAFT,
        db_index=True,
        editable=False,
    )

    method = models.CharField(max_length=30, choices=DisposalMethod.choices)
    #: Who took it away, and their paperwork reference — an e-waste handler's
    #: certificate number is what an ISO auditor asks to see.
    handler_name = models.CharField(max_length=200, blank=True)
    handler_reference = models.CharField(max_length=100, blank=True)

    # O10: which project bears the write-off, where it belongs to one. Optional,
    # because most scrap is nobody's PO in particular.
    project = models.ForeignKey(
        "network.Project",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="disposals",
        help_text="The project this write-off is charged to, if any (O10).",
    )

    #: O10: the project manager is **added above** the disposal's own rules
    #: rather than replacing them, unlike a gate-out (D22). Disposal is
    #: permanent, so nothing already in place is given up for it.
    project_approval_replaces_rules = False

    from_location = models.ForeignKey(
        "locations.Location", on_delete=models.PROTECT, related_name="disposals"
    )

    requested_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)

    disposed_at = models.DateTimeField(null=True, blank=True)
    disposed_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    reject_reason = models.CharField(max_length=500, blank=True)
    cancel_reason = models.CharField(max_length=500, blank=True)
    notes = models.TextField(blank=True)

    client_uuid = models.UUIDField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "number"],
                condition=Q(number__gt=""),
                name="uniq_disposal_number_per_org",
            ),
            models.UniqueConstraint(
                fields=["organization", "client_uuid"],
                condition=Q(client_uuid__isnull=False),
                name="uniq_disposal_client_uuid_per_org",
            ),
            # J3: a disposal that happened must say when, and one that has not
            # must not claim to have.
            models.CheckConstraint(
                condition=(
                    Q(status="DISPOSED", disposed_at__isnull=False)
                    | ~Q(status="DISPOSED")
                ),
                name="a_completed_disposal_records_when",
            ),
        ]
        ordering = ("-created_at", "-id")
        indexes = [models.Index(fields=["organization", "status"])]

    def __str__(self) -> str:
        return f"{self.number or 'Disposal'} — {self.get_method_display()}"

    @property
    def involves_client_owned_material(self) -> bool:
        """§5.2's hardcoded escalation reads this (J3).

        "Disposal of client-owned material always requires approval regardless of
        category criticality — a hardcoded rule, deliberately not configurable."
        """
        return self.lines.filter(owner_client__isnull=False).exists()

    @property
    def is_disposed(self) -> bool:
        return self.status == DisposalStatus.DISPOSED


class DisposalLine(TenantModel, TimeStampedModel):
    """One thing being written off (J3)."""

    disposal = models.ForeignKey(Disposal, on_delete=models.CASCADE, related_name="lines")
    item_type = models.ForeignKey(
        "catalogue.ItemType", on_delete=models.PROTECT, related_name="disposal_lines"
    )
    serial_unit = models.ForeignKey(
        "stock.SerialUnit", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    reel = models.ForeignKey(
        "stock.Reel", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    quantity = models.DecimalField(
        max_digits=14, decimal_places=3, validators=[MinValueValidator(Decimal("0.001"))]
    )
    uom = models.CharField(max_length=20)
    condition = models.CharField(
        max_length=20, choices=Condition.choices, default=Condition.SCRAP
    )

    owner_type = models.CharField(
        max_length=10, choices=OwnerType.choices, default=OwnerType.OWN
    )
    owner_client = models.ForeignKey(
        "network.Client",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="disposal_lines",
    )

    #: What it was worth, when the tenant tracks money (C8, §4.14). Captured at
    #: disposal rather than read from the item type later, because a write-off is
    #: a statement about a moment.
    written_off_value = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )

    line_number = models.PositiveIntegerField(default=1)
    notes = models.CharField(max_length=500, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(owner_type=OwnerType.CLIENT, owner_client__isnull=False)
                    | Q(owner_type=OwnerType.OWN, owner_client__isnull=True)
                ),
                name="client_owned_disposal_line_names_its_client",
            ),
        ]
        ordering = ("line_number", "id")

    def __str__(self) -> str:
        return f"{self.quantity} {self.uom} {self.item_type}"

    def clean(self) -> None:
        super().clean()
        if self.serial_unit_id and self.quantity != 1:
            raise ValidationError(
                {"quantity": "A serialized unit is disposed of whole — one per line (Q5)."}
            )

    def save(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.full_clean(exclude=["organization", "disposal"])
        return super().save(*args, **kwargs)


class ClientReturnAck(TenantModel, TimeStampedModel):
    """The client's acknowledgement of a return (K3, §4.12).

    K3: "record the client's acknowledgement — signed document upload or
    reference number — **so that our liability for that material ends on the
    record**." Until this exists, the material sits at the CLIENT node as "in
    transit" and still counts as the tenant's exposure (K1).

    One per gate-out, because a return is acknowledged once. A partial
    acknowledgement would be a second return, not a second signature.
    """

    gate_out = models.OneToOneField(
        "dispatch.GateOut", on_delete=models.PROTECT, related_name="client_return_ack"
    )
    #: Their reference — a GRN number, an email subject, a delivery note stamp.
    acknowledged_ref = models.CharField(max_length=200)
    acknowledged_at = models.DateTimeField()
    #: Who at the client signed for it, when they gave a name.
    acknowledged_by_name = models.CharField(max_length=200, blank=True)

    recorded_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    notes = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ("-acknowledged_at", "-id")
        indexes = [models.Index(fields=["organization", "acknowledged_at"])]

    def __str__(self) -> str:
        return f"Acknowledged {self.acknowledged_ref} on {self.acknowledged_at:%Y-%m-%d}"
