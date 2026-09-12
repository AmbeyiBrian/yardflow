"""Custody and returns (design §4.10; I1–I5).

§4.10's key decision: "**custody balance is simply StockBalance at the holder's
PERSON node — no separate ledger.**"

That is why "what is Tom holding?" and "what is in the yard?" can never disagree.
The models here are not a second record of custody; they are the *expectations*
that hang off it — what is owed back, by when, and to whom it was handed on.
"""

from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q

from core.models import TimeStampedModel
from core.tenancy import TenantModel


class ExpectationStatus(models.TextChoices):
    OPEN = "OPEN", "Outstanding"
    RETURNED = "RETURNED", "Returned"
    OVERDUE = "OVERDUE", "Overdue"
    # I4: a persistent offender's items are eventually written off, with a
    # reason. The record stays, because the write-off is the finding.
    WRITTEN_OFF = "WRITTEN_OFF", "Written off"


class CustodyExpectation(TenantModel, TimeStampedModel):
    """Something a person owes back (I2, I3).

    Created when a returnable line is *released*, not when it is requested:
    nothing is owed until it physically leaves the yard.

    I3's escalation chain (holder, then supervisor, then owner) is driven from
    here by the nightly sweep, and each stage is recorded so a reminder fires
    once rather than once per run.
    """

    holder = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="custody_expectations"
    )
    gate_out_line = models.ForeignKey(
        "dispatch.GateOutLine",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="custody_expectations",
    )

    item_type = models.ForeignKey(
        "catalogue.ItemType", on_delete=models.PROTECT, related_name="custody_expectations"
    )
    serial_unit = models.ForeignKey(
        "stock.SerialUnit",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="custody_expectations",
    )
    reel = models.ForeignKey(
        "stock.Reel",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="custody_expectations",
    )

    quantity = models.DecimalField(
        max_digits=14, decimal_places=3, validators=[MinValueValidator(Decimal("0.001"))]
    )
    returned_quantity = models.DecimalField(
        max_digits=14, decimal_places=3, default=Decimal("0")
    )

    expected_return_date = models.DateField(null=True, blank=True, db_index=True)
    status = models.CharField(
        max_length=20,
        choices=ExpectationStatus.choices,
        default=ExpectationStatus.OPEN,
        db_index=True,
    )

    # I3: "escalating reminders: to the holder, then to their supervisor, then to
    # the owner." Recorded so each stage fires once, not once per nightly run.
    reminded_holder_at = models.DateTimeField(null=True, blank=True)
    reminded_supervisor_at = models.DateTimeField(null=True, blank=True)
    reminded_owner_at = models.DateTimeField(null=True, blank=True)

    written_off_reason = models.CharField(max_length=500, blank=True)
    returned_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("expected_return_date", "id")
        indexes = [
            models.Index(fields=["organization", "holder", "status"]),
            models.Index(fields=["organization", "status", "expected_return_date"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(returned_quantity__gte=0)
                & Q(returned_quantity__lte=models.F("quantity")),
                name="returned_quantity_is_within_what_was_issued",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.holder} owes {self.outstanding_quantity} {self.item_type}"

    @property
    def outstanding_quantity(self) -> Decimal:
        return self.quantity - self.returned_quantity

    @property
    def is_open(self) -> bool:
        return self.status in (ExpectationStatus.OPEN, ExpectationStatus.OVERDUE)

    def is_overdue_on(self, today) -> bool:
        if not self.is_open or self.expected_return_date is None:
            return False
        return self.expected_return_date < today


class TransferStatus(models.TextChoices):
    PENDING = "PENDING", "Awaiting acknowledgement"
    ACKNOWLEDGED = "ACKNOWLEDGED", "Acknowledged"
    DECLINED = "DECLINED", "Declined"
    CANCELLED = "CANCELLED", "Cancelled"


class CustodyTransfer(TenantModel, TimeStampedModel):
    """A handover of material from one person to another (I5).

    I5: "requires acknowledgement by the receiving person." Movements post **only
    on acknowledgement** — until then the material is still with the original
    holder, because a handover nobody confirmed is exactly how tools go missing
    while each person believes the other has them.
    """

    from_holder = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="custody_transfers_out"
    )
    to_holder = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="custody_transfers_in"
    )

    status = models.CharField(
        max_length=20, choices=TransferStatus.choices, default=TransferStatus.PENDING, db_index=True
    )
    number = models.CharField(max_length=50, blank=True, db_index=True)

    requested_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    declined_reason = models.CharField(max_length=500, blank=True)
    notes = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.CheckConstraint(
                condition=~Q(from_holder=models.F("to_holder")),
                name="custody_transfer_changes_holder",
            ),
            models.CheckConstraint(
                condition=~Q(status=TransferStatus.ACKNOWLEDGED)
                | Q(acknowledged_at__isnull=False),
                name="acknowledged_transfer_records_when",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.from_holder} -> {self.to_holder} ({self.get_status_display()})"

    def clean(self) -> None:
        super().clean()
        if self.from_holder_id == self.to_holder_id:
            raise ValidationError("A handover needs two different people.")

    def save(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.full_clean(exclude=["organization"])
        return super().save(*args, **kwargs)


class CustodyTransferLine(TenantModel):
    """One item on a handover (I5)."""

    transfer = models.ForeignKey(
        CustodyTransfer, on_delete=models.CASCADE, related_name="lines"
    )
    item_type = models.ForeignKey(
        "catalogue.ItemType", on_delete=models.PROTECT, related_name="+"
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
    condition = models.CharField(max_length=20, blank=True)

    class Meta:
        ordering = ("id",)

    def __str__(self) -> str:
        return f"{self.quantity} {self.uom} {self.item_type}"
