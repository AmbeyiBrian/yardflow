"""Gate-out — request, approval and release (design §4.7; F1–F8, G1–G3).

This is the document the system exists for. Everything before it establishes what
is in the yard; this controls what leaves.

The status machine is the spine. §4.7 puts every transition in one place with an
explicit allowed-transition map, and **no view sets ``status`` directly** — because
a status set from two places is a control that can be walked around, and the
whole point of a gate pass is that it cannot be.
"""

from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from catalogue.models import TrackingMode
from core.exceptions import InvalidTransition
from core.models import TimeStampedModel
from core.tenancy import TenantModel
from stock.models import Condition, OwnerType


class GateOutPurpose(models.TextChoices):
    """Why the material is leaving (F1)."""

    INSTALLATION = "INSTALLATION", "Installation"
    MAINTENANCE = "MAINTENANCE", "Maintenance"
    RETURN_TO_CLIENT = "RETURN_TO_CLIENT", "Return to client"
    TRANSFER = "TRANSFER", "Transfer to another location"
    DISPOSAL = "DISPOSAL", "Disposal"
    TOOL_ISSUE = "TOOL_ISSUE", "Tool issue"


class GateOutStatus(models.TextChoices):
    """§4.7's status machine."""

    DRAFT = "DRAFT", "Draft"
    PENDING_APPROVAL = "PENDING_APPROVAL", "Awaiting approval"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    PARTIALLY_RELEASED = "PARTIALLY_RELEASED", "Partially released"
    RELEASED = "RELEASED", "Released"
    CLOSED = "CLOSED", "Closed"
    CANCELLED = "CANCELLED", "Cancelled"
    # Q3: an approved pass not released within the window expires, so a stale
    # approval cannot be used days later against stock that has since changed.
    EXPIRED = "EXPIRED", "Expired"


#: The only transitions that exist (§4.7). Anything absent is refused.
#:
#: Read this as the control itself, not as bookkeeping: there is no path from
#: DRAFT to RELEASED, and no path from REJECTED to APPROVED. Material cannot
#: leave the yard without passing through APPROVED.
ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    GateOutStatus.DRAFT: (GateOutStatus.PENDING_APPROVAL, GateOutStatus.CANCELLED),
    GateOutStatus.PENDING_APPROVAL: (
        GateOutStatus.APPROVED,
        GateOutStatus.REJECTED,
        GateOutStatus.CANCELLED,
    ),
    # F6: amending an approved gate-out voids the approval and re-triggers
    # routing, which is this edge back to PENDING_APPROVAL.
    GateOutStatus.APPROVED: (
        GateOutStatus.PARTIALLY_RELEASED,
        GateOutStatus.RELEASED,
        GateOutStatus.EXPIRED,
        GateOutStatus.CANCELLED,
        GateOutStatus.PENDING_APPROVAL,
    ),
    # F6: a rejected request may be amended and resubmitted.
    GateOutStatus.REJECTED: (GateOutStatus.PENDING_APPROVAL, GateOutStatus.CANCELLED),
    # F7: a partially released pass stays open until fully released, or closed
    # with a reason. F8: cancellation is impossible after any release.
    GateOutStatus.PARTIALLY_RELEASED: (
        GateOutStatus.PARTIALLY_RELEASED,
        GateOutStatus.RELEASED,
        GateOutStatus.CLOSED,
    ),
    GateOutStatus.RELEASED: (GateOutStatus.CLOSED,),
    GateOutStatus.EXPIRED: (GateOutStatus.PENDING_APPROVAL, GateOutStatus.CANCELLED),
    # Terminal.
    GateOutStatus.CLOSED: (),
    GateOutStatus.CANCELLED: (),
}

#: Statuses in which material may actually leave the gate (G1).
RELEASABLE_STATUSES = (GateOutStatus.APPROVED, GateOutStatus.PARTIALLY_RELEASED)


class GateOut(TenantModel, TimeStampedModel):
    """A gate pass: a request to take material out of the yard (F1).

    Destination is **exactly one** of site, work order, client or location — a
    pass with two destinations could not be reconciled against either, and one
    with none could not be reconciled at all.
    """

    number = models.CharField(max_length=50, blank=True, db_index=True)
    status = models.CharField(
        max_length=20,
        choices=GateOutStatus.choices,
        default=GateOutStatus.DRAFT,
        db_index=True,
        # Set only through `transition()`. Nothing else may assign it.
        editable=False,
    )

    purpose_type = models.CharField(max_length=30, choices=GateOutPurpose.choices)

    # Exactly one destination — see the check constraint.
    site = models.ForeignKey(
        "network.Site", on_delete=models.PROTECT, null=True, blank=True, related_name="gate_outs"
    )
    work_order = models.ForeignKey(
        "network.WorkOrder",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="gate_outs",
    )
    client = models.ForeignKey(
        "network.Client",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="gate_outs",
    )
    to_location = models.ForeignKey(
        "locations.Location",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="inbound_gate_outs",
    )

    from_location = models.ForeignKey(
        "locations.Location", on_delete=models.PROTECT, related_name="gate_outs"
    )

    # F1: "a request must name the person taking custody (usually a technician)."
    # Without it, released material has no holder and I1's custody view is empty
    # exactly when it matters.
    custody_holder = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="gate_outs_held"
    )
    requested_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="gate_outs_requested"
    )

    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    # Q3: set from `gate_pass_expiry_hours` when the final approval lands.
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)

    # G2: recorded at release so the load is attributable.
    vehicle_reg = models.CharField(max_length=30, blank=True)
    driver_name = models.CharField(max_length=200, blank=True)
    released_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    released_at = models.DateTimeField(null=True, blank=True)

    # F6: the version history is retained and visible.
    version = models.PositiveIntegerField(default=1)
    supersedes = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="superseded_by"
    )

    # N2: offline idempotency.
    client_uuid = models.UUIDField(null=True, blank=True)

    reject_reason = models.CharField(max_length=500, blank=True)
    cancel_reason = models.CharField(max_length=500, blank=True)
    close_reason = models.CharField(max_length=500, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "number"],
                condition=Q(number__gt=""),
                name="uniq_gate_out_number_per_org",
            ),
            models.UniqueConstraint(
                fields=["organization", "client_uuid"],
                condition=Q(client_uuid__isnull=False),
                name="uniq_gate_out_client_uuid_per_org",
            ),
            # F1: exactly one destination. Enforced by the database because a
            # pass with two destinations cannot be reconciled against either
            # (H4), and the arithmetic has no way to recover from it.
            models.CheckConstraint(
                condition=(
                    Q(
                        site__isnull=False,
                        work_order__isnull=True,
                        client__isnull=True,
                        to_location__isnull=True,
                    )
                    | Q(
                        site__isnull=True,
                        work_order__isnull=False,
                        client__isnull=True,
                        to_location__isnull=True,
                    )
                    | Q(
                        site__isnull=True,
                        work_order__isnull=True,
                        client__isnull=False,
                        to_location__isnull=True,
                    )
                    | Q(
                        site__isnull=True,
                        work_order__isnull=True,
                        client__isnull=True,
                        to_location__isnull=False,
                    )
                ),
                name="gate_out_has_exactly_one_destination",
            ),
        ]
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=["organization", "status", "-created_at"]),
            models.Index(fields=["organization", "custody_holder"]),
            models.Index(fields=["organization", "expires_at"]),
        ]

    def __str__(self) -> str:
        return self.number or f"Draft gate-out {self.pk}"

    # --- destination -----------------------------------------------------

    @property
    def destination_label(self) -> str:
        if self.site_id:
            return str(self.site)
        if self.work_order_id:
            return str(self.work_order)
        if self.client_id:
            return str(self.client)
        if self.to_location_id:
            return str(self.to_location)
        return "—"

    # --- status ----------------------------------------------------------

    @property
    def is_releasable(self) -> bool:
        """G1: "the system will not permit release of an unapproved or expired
        gate pass"."""
        if self.status not in RELEASABLE_STATUSES:
            return False
        return not self.is_expired

    @property
    def is_expired(self) -> bool:
        if self.status == GateOutStatus.EXPIRED:
            return True
        if self.expires_at is None:
            return False
        return self.expires_at <= timezone.now() and self.status == GateOutStatus.APPROVED

    def transition(self, to_status: str, *, reason: str = "", save: bool = True) -> None:
        """The only way ``status`` ever changes (§4.7).

        One method with an explicit map, so every path material can take out of
        the yard is visible in one place and reviewable. A status assigned from a
        view would be a second, invisible path.
        """
        allowed = ALLOWED_TRANSITIONS.get(self.status, ())
        if to_status not in allowed:
            raise InvalidTransition(
                f"A gate pass cannot go from {self.get_status_display()} to "
                f"{GateOutStatus(to_status).label}.",
                details={
                    "from": self.status,
                    "to": to_status,
                    "allowed": list(allowed),
                },
            )

        # Reasons that the requirements make mandatory. Checked here rather than
        # at each call site, so no path can skip them.
        if to_status == GateOutStatus.REJECTED and not reason:
            raise InvalidTransition("Rejecting a request requires a reason (F4).")
        if to_status == GateOutStatus.CANCELLED and not reason:
            raise InvalidTransition("Cancelling a request requires a reason (F8).")
        if (
            to_status == GateOutStatus.CLOSED
            and self.status == GateOutStatus.PARTIALLY_RELEASED
            and not reason
        ):
            raise InvalidTransition(
                "Closing a partially released gate pass requires a reason, because "
                "the outstanding balance is being written off (F7)."
            )

        self.status = to_status

        if to_status == GateOutStatus.REJECTED:
            self.reject_reason = reason
        elif to_status == GateOutStatus.CANCELLED:
            self.cancel_reason = reason
        elif to_status == GateOutStatus.CLOSED:
            self.close_reason = reason

        if save:
            self.save(
                update_fields=[
                    "status",
                    "reject_reason",
                    "cancel_reason",
                    "close_reason",
                    "updated_at",
                ]
            )

    # --- lines -----------------------------------------------------------

    @property
    def is_fully_released(self) -> bool:
        return all(line.is_fully_released for line in self.lines.all())

    @property
    def has_outstanding_lines(self) -> bool:
        return any(not line.is_fully_released for line in self.lines.all())

    def clean(self) -> None:
        super().clean()

        destinations = [self.site_id, self.work_order_id, self.client_id, self.to_location_id]
        named = [value for value in destinations if value is not None]
        if len(named) != 1:
            raise ValidationError(
                "A gate pass needs exactly one destination — a site, a work order, "
                "a client or another location (F1)."
            )

        if self.purpose_type == GateOutPurpose.RETURN_TO_CLIENT and not self.client_id:
            raise ValidationError(
                {"client": "A return to client must name the client (K1)."}
            )

    def save(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.full_clean(exclude=["organization", "status"])
        return super().save(*args, **kwargs)


class GateOutLine(TenantModel, TimeStampedModel):
    """One line of a gate pass (F1, F7)."""

    gate_out = models.ForeignKey(GateOut, on_delete=models.CASCADE, related_name="lines")
    item_type = models.ForeignKey(
        "catalogue.ItemType", on_delete=models.PROTECT, related_name="gate_out_lines"
    )
    tracking_mode = models.CharField(max_length=20, choices=TrackingMode.choices)

    requested_qty = models.DecimalField(
        max_digits=14, decimal_places=3, validators=[MinValueValidator(Decimal("0.001"))]
    )
    # F7: "released quantities are recorded per line; the balance stays
    # outstanding."
    released_qty = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    uom = models.CharField(max_length=20)

    condition = models.CharField(
        max_length=20, choices=Condition.choices, default=Condition.NEW
    )
    owner_type = models.CharField(
        max_length=10, choices=OwnerType.choices, default=OwnerType.OWN
    )
    owner_client = models.ForeignKey(
        "network.Client",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="gate_out_lines",
    )

    # I2: tools and returnable material carry an expected return date, so
    # overdue items can be chased.
    is_returnable = models.BooleanField(default=False)
    expected_return_date = models.DateField(null=True, blank=True)

    # D3, mirroring the gate-in rule: a serialized item can only leave as an
    # anonymous quantity if the yard genuinely holds untagged units of it, and
    # then the reason is part of the record. Without this field the mode is a
    # free choice, and the gate has nothing to check the load against (G1).
    no_serial_reason = models.CharField(max_length=300, blank=True)

    line_number = models.PositiveIntegerField(default=1)
    notes = models.CharField(max_length=500, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(released_qty__gte=0), name="released_qty_is_not_negative"
            ),
            # F7: a line can be released short, never over. Releasing more than
            # was approved is the exact thing the gate control prevents.
            models.CheckConstraint(
                condition=Q(released_qty__lte=models.F("requested_qty")),
                name="released_qty_does_not_exceed_what_was_approved",
            ),
            models.CheckConstraint(
                condition=(
                    Q(owner_type=OwnerType.CLIENT, owner_client__isnull=False)
                    | Q(owner_type=OwnerType.OWN, owner_client__isnull=True)
                ),
                name="client_owned_gate_out_line_names_its_client",
            ),
        ]
        ordering = ("line_number", "id")

    def __str__(self) -> str:
        return f"{self.requested_qty} {self.uom} {self.item_type}"

    def clean(self) -> None:
        """A serialized item does not leave the yard anonymously (D3, G2).

        Found in the ledger, not by a test: a gate pass raised for a serialized
        antenna with ``tracking_mode: BULK`` was accepted, approved and released,
        and put a *quantity* of antennas onto a technician's custody with no
        identity attached. Nothing downstream could say which unit it was — not
        the serial history, not the installed base, not an operator audit — and
        the line looked no different from one that was always bulk.

        The mode still is not simply derived from the item, because a yard really
        can hold untagged units: D3 allows a serialized item to be *received* as
        bulk when no serial is available, as long as the system records why. This
        is the same rule at the other gate.
        """
        super().clean()

        from catalogue.models import TrackingMode as CatalogueTrackingMode

        if (
            self.item_type_id
            and self.item_type.default_tracking_mode == CatalogueTrackingMode.SERIALIZED
            and self.tracking_mode != TrackingMode.SERIALIZED
            and not self.no_serial_reason
        ):
            raise ValidationError(
                {
                    "tracking_mode": (
                        f"{self.item_type} is tracked by serial number, so the "
                        f"line has to name the units going out. If the yard holds "
                        f"untagged ones, say so in no_serial_reason and they can "
                        f"go as a quantity (D3)."
                    )
                }
            )

    def save(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.full_clean(exclude=["organization", "gate_out"])
        return super().save(*args, **kwargs)

    @property
    def outstanding_qty(self) -> Decimal:
        return self.requested_qty - self.released_qty

    @property
    def is_fully_released(self) -> bool:
        return self.released_qty >= self.requested_qty


class GateOutLineSerial(TenantModel):
    """A specific unit named on a line (F1, §4.7).

    Serialized material is requested by identity, not by count: "these two RRUs",
    not "two RRUs". That is what lets the gate check the physical load against
    the pass (G1) rather than merely counting boxes.
    """

    line = models.ForeignKey(GateOutLine, on_delete=models.CASCADE, related_name="serials")
    serial_unit = models.ForeignKey(
        "stock.SerialUnit", on_delete=models.PROTECT, related_name="gate_out_lines"
    )
    released = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["line", "serial_unit"], name="uniq_serial_per_gate_out_line"
            )
        ]

    def __str__(self) -> str:
        return str(self.serial_unit)


class GateOutLineReel(TenantModel):
    """A length taken from a specific drum (F1, E3)."""

    line = models.ForeignKey(GateOutLine, on_delete=models.CASCADE, related_name="reels")
    reel = models.ForeignKey(
        "stock.Reel", on_delete=models.PROTECT, related_name="gate_out_lines"
    )
    length_requested = models.DecimalField(
        max_digits=14, decimal_places=3, validators=[MinValueValidator(Decimal("0.001"))]
    )
    length_released = models.DecimalField(
        max_digits=14, decimal_places=3, default=Decimal("0")
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["line", "reel"], name="uniq_reel_per_gate_out_line"
            ),
            models.CheckConstraint(
                condition=Q(length_released__lte=models.F("length_requested")),
                name="reel_length_released_does_not_exceed_request",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.length_requested} from {self.reel}"


class ReleaseVariance(TenantModel, TimeStampedModel):
    """The physical load differed from the approved list (G1).

    G1's edge case: "if the physical load differs from the approved list, the
    storekeeper records the actual quantity, and the difference is flagged as a
    release variance requiring an approver's acknowledgement."

    It **blocks nothing at the gate** — the driver leaves with what was actually
    loaded — but it stays on the exceptions register until acknowledged. Blocking
    would strand a job over a paperwork mismatch; hiding it would lose the
    discrepancy.
    """

    gate_out_line = models.ForeignKey(
        GateOutLine, on_delete=models.CASCADE, related_name="variances"
    )
    approved_qty = models.DecimalField(max_digits=14, decimal_places=3)
    released_qty = models.DecimalField(max_digits=14, decimal_places=3)
    reason = models.CharField(max_length=500)

    recorded_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    acknowledged_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    acknowledged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["organization", "acknowledged_at"])]

    def __str__(self) -> str:
        return (
            f"{self.gate_out_line.item_type}: approved {self.approved_qty}, "
            f"released {self.released_qty}"
        )

    @property
    def is_open(self) -> bool:
        """Open variances are what the exceptions register lists (M1)."""
        return self.acknowledged_at is None

    @property
    def difference(self) -> Decimal:
        return self.released_qty - self.approved_qty
