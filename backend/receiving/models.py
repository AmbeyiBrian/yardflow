"""Gate-in — receiving (design §4.6; requirements D1–D8, J1).

D1: "record material arriving at the yard against a source type, so that its
origin and ownership are unambiguous." This is the document that replaces the
GRN book.

Two rules shape the model:

* **Only posting affects stock** (D8). A draft is freely editable and consumes no
  document number, so a storekeeper can enter a large delivery over an hour
  without burning a number or half-changing the ledger.
* **Ownership is set here and carried forever** (D1). Whether a unit belongs to
  Silvertech or to Safaricom is decided at the gate, and every later movement
  carries it. Getting it wrong here is not correctable by editing — it needs a
  reversal (M4).
"""

from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q

from catalogue.models import TrackingMode
from core.models import TimeStampedModel
from core.tenancy import TenantModel
from stock.models import Condition, OwnerType


class GateInSource(models.TextChoices):
    """Where the material came from (D1).

    Not cosmetic: each source implies different required information, and
    together they are what makes origin and ownership unambiguous.
    """

    PURCHASE = "PURCHASE", "Purchase — own stock"
    CLIENT_ISSUE = "CLIENT_ISSUE", "Client issue — consignment stock"
    RECOVERY = "RECOVERY", "Recovery from a decommissioned or demolished site"
    RETURN_FROM_SITE = "RETURN_FROM_SITE", "Unused material returning from site"
    WARRANTY_RETURN = "WARRANTY_RETURN", "Warranty or faulty return"
    TRANSFER = "TRANSFER", "Transfer from another location"


class DocumentStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    POSTED = "POSTED", "Posted"
    # M6: a voided document keeps its number and is marked void. Numbers are
    # never reused, so the sequence stays gap-free and the void is visible.
    VOID = "VOID", "Void"


class GateIn(TenantModel, TimeStampedModel):
    """A numbered goods received note (D1).

    ``number`` is blank until posting: allocating at draft creation would leave a
    gap for every abandoned delivery, and a gap is exactly what an auditor asks
    about (M6).
    """

    number = models.CharField(max_length=50, blank=True, db_index=True)
    status = models.CharField(
        max_length=20, choices=DocumentStatus.choices, default=DocumentStatus.DRAFT, db_index=True
    )

    source_type = models.CharField(max_length=30, choices=GateInSource.choices)

    # PURCHASE names a supplier; CLIENT_ISSUE names the client whose stock it is.
    supplier_name = models.CharField(max_length=200, blank=True)
    client = models.ForeignKey(
        "network.Client", on_delete=models.PROTECT, null=True, blank=True, related_name="gate_ins"
    )

    # H3: a return from site comes out of somebody's custody, so the document has
    # to name whose. Without this the material would be received from nowhere and
    # the technician would keep holding it on their record forever.
    returned_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="gate_ins_returned",
        help_text="Who is handing the material back (required for a return from site).",
    )

    # D5: a recovery records the site it came from, so the operator can be shown
    # what was retrieved from where.
    origin_site = models.ForeignKey(
        "network.Site",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="recoveries",
        help_text=(
            "Where the material came off. Required for a recovery (D5); worth "
            "recording on a return so the site's reconciliation can see it (H4)."
        ),
    )

    to_location = models.ForeignKey(
        "locations.Location", on_delete=models.PROTECT, related_name="gate_ins"
    )

    received_at = models.DateTimeField()
    posted_at = models.DateTimeField(null=True, blank=True)
    posted_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    # D6: the supplier's or client's own delivery note reference, so the paper
    # trail is preserved.
    client_delivery_note_ref = models.CharField(max_length=100, blank=True)

    # H3: a return is matched against what the technician declared on the
    # gate-out it is coming back from. The foreign key to `dispatch.GateOut` is
    # added in Phase 4, once that model exists (T4.1); the matching logic that
    # uses it is T5.4. Adding it here would be a forward reference to a model
    # that has no table yet.

    # N2: idempotency for offline capture. The client generates this before
    # submitting, so a retry after a flaky connection cannot double-post.
    client_uuid = models.UUIDField(null=True, blank=True)

    void_reason = models.CharField(max_length=500, blank=True)
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    notes = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "number"],
                condition=Q(number__gt=""),
                name="uniq_gate_in_number_per_org",
            ),
            models.UniqueConstraint(
                fields=["organization", "client_uuid"],
                condition=Q(client_uuid__isnull=False),
                name="uniq_gate_in_client_uuid_per_org",
            ),
            # A posted document must have a number; a draft must not.
            models.CheckConstraint(
                condition=(
                    Q(status=DocumentStatus.DRAFT, number="")
                    | (~Q(status=DocumentStatus.DRAFT) & ~Q(number=""))
                ),
                name="posted_gate_in_has_a_number",
            ),
        ]
        ordering = ("-received_at", "-id")
        indexes = [
            models.Index(fields=["organization", "status", "-received_at"]),
            models.Index(fields=["organization", "source_type"]),
            models.Index(fields=["organization", "origin_site"]),
        ]

    def __str__(self) -> str:
        return self.number or f"Draft gate-in {self.pk}"

    @property
    def is_posted(self) -> bool:
        return self.status == DocumentStatus.POSTED

    @property
    def is_editable(self) -> bool:
        """D8: "drafts are freely editable; posted documents are not"."""
        return self.status == DocumentStatus.DRAFT

    def clean(self) -> None:
        super().clean()

        # D1: each source type carries the information that makes origin
        # unambiguous. Enforced here rather than trusted to the UI, because the
        # offline flow (N1) posts without one.
        if self.source_type == GateInSource.CLIENT_ISSUE and self.client_id is None:
            raise ValidationError(
                {
                    "client": (
                        "Consignment stock must name the client it belongs to, or it "
                        "cannot be audited back to them (D1)."
                    )
                }
            )
        if (
            self.source_type == GateInSource.RETURN_FROM_SITE
            and self.returned_by_id is None
        ):
            raise ValidationError(
                {
                    "returned_by": (
                        "A return from site must name who is handing the material "
                        "back, so it can be taken off their custody record (H3, I1)."
                    )
                }
            )

        if self.source_type == GateInSource.RECOVERY and self.origin_site_id is None:
            raise ValidationError(
                {
                    "origin_site": (
                        "A recovery must record the site it came from, so the "
                        "operator can be shown what was retrieved (D5)."
                    )
                }
            )

    def save(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.full_clean(exclude=["organization"])
        return super().save(*args, **kwargs)


class GateInLine(TenantModel, TimeStampedModel):
    """One line of a delivery (D2).

    D2: "add lines to a gate-in choosing item type, quantity and condition, so
    that mixed deliveries are recorded in one document."
    """

    gate_in = models.ForeignKey(GateIn, on_delete=models.CASCADE, related_name="lines")
    item_type = models.ForeignKey(
        "catalogue.ItemType", on_delete=models.PROTECT, related_name="gate_in_lines"
    )

    # D3, D11: defaults from the item type, overridable on the line — so a batch
    # of recovered units with unreadable serials can still be received.
    tracking_mode = models.CharField(max_length=20, choices=TrackingMode.choices)

    quantity = models.DecimalField(
        max_digits=14, decimal_places=3, validators=[MinValueValidator(Decimal("0.001"))]
    )
    uom = models.CharField(max_length=20)

    condition = models.CharField(
        max_length=20, choices=Condition.choices, default=Condition.NEW
    )

    # D1: ownership is set at gate-in and carried on the stock permanently.
    owner_type = models.CharField(
        max_length=10, choices=OwnerType.choices, default=OwnerType.OWN
    )
    owner_client = models.ForeignKey(
        "network.Client",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="gate_in_lines",
    )

    # C2: values for the category's custom fields.
    custom_field_values = models.JSONField(default=dict, blank=True)

    # D3: "if asset tags are disabled and no serial is available, the line must
    # be received as bulk, and the system records why." This is that record —
    # without it, a bulk line that should have been serialized is indistinguishable
    # from one that was always bulk.
    no_serial_reason = models.CharField(max_length=300, blank=True)

    line_number = models.PositiveIntegerField(default=1)
    notes = models.CharField(max_length=500, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(owner_type=OwnerType.CLIENT, owner_client__isnull=False)
                    | Q(owner_type=OwnerType.OWN, owner_client__isnull=True)
                ),
                name="client_owned_line_names_its_client",
            ),
        ]
        ordering = ("line_number", "id")

    def __str__(self) -> str:
        return f"{self.quantity} {self.uom} {self.item_type}"

    @property
    def is_unserviceable(self) -> bool:
        """D2, J1: faulty, damaged and scrap lines land in quarantine."""
        from stock.models import UNSERVICEABLE_CONDITIONS

        return self.condition in UNSERVICEABLE_CONDITIONS


class GateInSerial(TenantModel):
    """One identified unit on a serialized line (D3)."""

    class Source(models.TextChoices):
        MANUFACTURER = "MANUFACTURER", "Manufacturer serial"
        INTERNAL = "INTERNAL", "Internally generated asset tag"

    line = models.ForeignKey(GateInLine, on_delete=models.CASCADE, related_name="serials")
    serial_number = models.CharField(max_length=150)
    asset_tag = models.CharField(max_length=100, blank=True)
    source = models.CharField(
        max_length=20, choices=Source.choices, default=Source.MANUFACTURER
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["line", "serial_number"], name="uniq_serial_per_gate_in_line"
            )
        ]
        ordering = ("serial_number",)

    def __str__(self) -> str:
        return self.serial_number


class GateInReel(TenantModel):
    """One drum on a reel line (D4)."""

    line = models.ForeignKey(GateInLine, on_delete=models.CASCADE, related_name="reels")
    drum_number = models.CharField(max_length=100)
    length = models.DecimalField(
        max_digits=14, decimal_places=3, validators=[MinValueValidator(Decimal("0.001"))]
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["line", "drum_number"], name="uniq_drum_per_gate_in_line"
            ),
            models.CheckConstraint(
                condition=Q(length__gt=0), name="gate_in_reel_has_a_length"
            ),
        ]
        ordering = ("drum_number",)

    def __str__(self) -> str:
        return f"{self.drum_number} ({self.length})"
