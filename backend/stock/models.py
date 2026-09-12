"""The stock ledger (design §3; requirements E1–E5, M3, M4).

§3: "Everything that happens to material has one shape: **a double-entry
movement between two nodes.**"

That single decision is what makes the rest of the system tractable. Receiving,
issuing, installing, consuming, quarantining, scrapping, returning to a client
and handing to a technician are not nine features with nine tables — they are
nine pairs of nodes. H4's reconciliation becomes one aggregation instead of five
reports that can disagree.

Two rules make the ledger trustworthy, and both are enforced by the database
rather than by convention:

* **Append-only.** No UPDATE, no DELETE, ever. Corrections are ``REVERSAL``
  movements pointing at the original (M4).
* **Quantities are always positive.** Direction is expressed by ``from_node`` and
  ``to_node``, never by a sign. A signed quantity invites the bug where a
  mistaken sign silently doubles a balance instead of failing.
"""

from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q

from catalogue.models import TrackingMode
from core.models import TimeStampedModel
from core.tenancy import TenantManager, TenantModel, TenantQuerySet


class MovementType(models.TextChoices):
    """Why material moved (§3.2).

    The type is documentation, not logic: what actually changes a balance is the
    pair of nodes. Two movements with the same nodes and different types have
    identical effects on stock, and that is deliberate — reporting needs to
    distinguish an issue from a transfer, arithmetic does not.
    """

    RECEIPT = "RECEIPT", "Received"
    ISSUE = "ISSUE", "Issued"
    TRANSFER = "TRANSFER", "Transferred"
    INSTALL = "INSTALL", "Installed at a site"
    CONSUME = "CONSUME", "Consumed on site"
    RETURN = "RETURN", "Returned"
    QUARANTINE = "QUARANTINE", "Moved to quarantine"
    RESTORE = "RESTORE", "Restored to serviceable"
    DISPOSE = "DISPOSE", "Disposed of"
    ADJUST = "ADJUST", "Adjusted after a count"
    REVERSAL = "REVERSAL", "Reversal of an earlier movement"


class OwnerType(models.TextChoices):
    """D3: client-owned and own stock, separately and unambiguously."""

    OWN = "OWN", "Own stock"
    CLIENT = "CLIENT", "Client-owned (consignment)"


class Condition(models.TextChoices):
    """D2: condition per line.

    FAULTY, DAMAGED and SCRAP land in quarantine rather than free stock (J1), so
    condition is part of a balance's identity — the same item in two conditions
    is two balances.
    """

    NEW = "NEW", "New"
    USED_SERVICEABLE = "USED_SERVICEABLE", "Used — serviceable"
    FAULTY = "FAULTY", "Faulty"
    DAMAGED = "DAMAGED", "Damaged"
    SCRAP = "SCRAP", "Scrap"


#: Conditions that must not sit in free stock (D2, J1).
UNSERVICEABLE_CONDITIONS = (Condition.FAULTY, Condition.DAMAGED, Condition.SCRAP)


class StockMovement(TenantModel):
    """One double-entry movement of material between two nodes (§3.2).

    **Append-only.** ``save()`` refuses a second write and a Postgres trigger
    refuses UPDATE and DELETE, so the ledger holds even against a raw query or a
    psql session. That is what lets a stock figure be *derived* rather than
    *maintained*, and it is what an ISO auditor is actually asking about (M3).
    """

    # Not TimeStampedModel: an append-only row is never updated, so `updated_at`
    # would be a column that must always equal `created_at`.
    #
    # `occurred_at` is when the movement happened in the real world and drives
    # the as-of-date report (M1); `posted_at` is when it was recorded. They
    # differ whenever a delivery is entered the next morning, and conflating
    # them would make yesterday's stock figure wrong.
    occurred_at = models.DateTimeField(db_index=True)
    posted_at = models.DateTimeField(auto_now_add=True, db_index=True)
    posted_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    movement_type = models.CharField(max_length=20, choices=MovementType.choices)

    item_type = models.ForeignKey(
        "catalogue.ItemType", on_delete=models.PROTECT, related_name="movements"
    )
    tracking_mode = models.CharField(max_length=20, choices=TrackingMode.choices)
    uom = models.CharField(max_length=20)

    # ALWAYS POSITIVE (§3.2). Direction lives in the node pair.
    quantity = models.DecimalField(
        max_digits=14, decimal_places=3, validators=[MinValueValidator(Decimal("0.001"))]
    )

    from_node = models.ForeignKey(
        "locations.StockNode", on_delete=models.PROTECT, related_name="movements_out"
    )
    to_node = models.ForeignKey(
        "locations.StockNode", on_delete=models.PROTECT, related_name="movements_in"
    )

    serial_unit = models.ForeignKey(
        "stock.SerialUnit",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="movements",
    )
    reel = models.ForeignKey(
        "stock.Reel", on_delete=models.PROTECT, null=True, blank=True, related_name="movements"
    )

    owner_type = models.CharField(max_length=10, choices=OwnerType.choices)
    owner_client = models.ForeignKey(
        "network.Client", on_delete=models.PROTECT, null=True, blank=True, related_name="movements"
    )

    condition = models.CharField(max_length=20, choices=Condition.choices)
    # A return is assessed at the gate, so material issued as new can come back
    # used. Both sides of the movement then need their own condition: debiting
    # the holder at "used" when their record says "new" would drive one balance
    # negative and strand the other. Blank means unchanged, which is the ordinary
    # case — see ``held_condition``.
    from_condition = models.CharField(
        max_length=20, choices=Condition.choices, blank=True,
        help_text="Condition it was held in, when the movement changed it.",
    )

    # Which document caused this. Generic text rather than a foreign key so a new
    # document type needs no migration here, and so the ledger never depends on
    # a document still existing.
    document_type = models.CharField(max_length=50, blank=True, db_index=True)
    document_id = models.CharField(max_length=64, blank=True, db_index=True)
    document_line_id = models.CharField(max_length=64, blank=True)
    document_number = models.CharField(
        max_length=50, blank=True, help_text="The document number as issued, for reporting."
    )

    # M4: corrections are reversals pointing at the original, never edits.
    reversal_of = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="reversals"
    )

    note = models.CharField(max_length=500, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity__gt=0), name="movement_quantity_is_positive"
            ),
            # A movement from a node to itself changes nothing and would break the
            # invariant test by inflating both sides of a balance.
            models.CheckConstraint(
                condition=~Q(from_node=models.F("to_node")), name="movement_moves_somewhere"
            ),
            # D3: client-owned stock names its client; own stock does not.
            models.CheckConstraint(
                condition=(
                    Q(owner_type=OwnerType.CLIENT, owner_client__isnull=False)
                    | Q(owner_type=OwnerType.OWN, owner_client__isnull=True)
                ),
                name="client_owned_movement_names_its_client",
            ),
            # §3.5: a serialized movement is one unit; a reel movement is a length.
            models.CheckConstraint(
                condition=(
                    ~Q(tracking_mode=TrackingMode.SERIALIZED)
                    | Q(serial_unit__isnull=False, quantity=1)
                ),
                name="serialized_movement_is_one_identified_unit",
            ),
            models.CheckConstraint(
                condition=~Q(tracking_mode=TrackingMode.REEL) | Q(reel__isnull=False),
                name="reel_movement_names_its_drum",
            ),
            models.CheckConstraint(
                condition=~Q(tracking_mode=TrackingMode.BULK)
                | Q(serial_unit__isnull=True, reel__isnull=True),
                name="bulk_movement_has_no_serial_or_drum",
            ),
        ]
        indexes = [
            # §3.4: the as-of-date ledger aggregation.
            models.Index(fields=["organization", "occurred_at"]),
            models.Index(fields=["organization", "item_type", "occurred_at"]),
            # E2: "look up a serial number and see its entire history" — the
            # single most likely question from an operator audit.
            models.Index(fields=["organization", "serial_unit", "occurred_at"]),
            models.Index(fields=["organization", "reel", "occurred_at"]),
            models.Index(fields=["organization", "document_type", "document_id"]),
            models.Index(fields=["organization", "to_node", "item_type"]),
            models.Index(fields=["organization", "from_node", "item_type"]),
        ]
        ordering = ("-occurred_at", "-id")

    def __str__(self) -> str:
        return (
            f"{self.movement_type} {self.quantity} {self.uom} "
            f"{self.item_type_id}: {self.from_node_id} -> {self.to_node_id}"
        )

    @property
    def held_condition(self) -> str:
        """The condition the outbound side was held in.

        Blank ``from_condition`` means the movement did not change it, which is
        the ordinary case — so this reads as one field for every caller that does
        not care about reclassification.
        """
        return self.from_condition or self.condition

    def save(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self.pk is not None and not self._state.adding:
            raise ValidationError(
                "The stock ledger is append-only. To correct a movement, post a "
                "REVERSAL pointing at it (M4, §3.2)."
            )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise ValidationError(
            "The stock ledger is append-only and nothing is ever deleted from it. "
            "Post a REVERSAL instead (M4, §3.2)."
        )


class StockBalanceQuerySet(TenantQuerySet):
    def available(self):
        """Balances that count as available to issue (§3.3, J1)."""
        from locations.models import UNAVAILABLE_NODE_TYPES, LocationType

        return (
            self.exclude(node__type__in=UNAVAILABLE_NODE_TYPES)
            .exclude(node__location__type=LocationType.QUARANTINE)
            .exclude(quantity=0)
        )

    def nonzero(self):
        return self.exclude(quantity=0)


class StockBalance(TenantModel, TimeStampedModel):  # type: ignore[django-manager-missing]
    """The cached quantity at one node, for one item, owner and condition (§3.3).

    A cache, not a source of truth: the ledger is. It exists because "what is on
    hand?" is asked constantly and summing every movement each time would not
    meet N-2's two-second target at 100,000 records.

    Kept honest three ways: it is only ever written by
    :func:`stock.services.post_movement`, it is updated in the *same transaction*
    as the movement that changes it, and ``verify_ledger`` recomputes it nightly
    and alerts on any drift without auto-correcting (T3.6).
    """

    node = models.ForeignKey(
        "locations.StockNode", on_delete=models.PROTECT, related_name="balances"
    )
    item_type = models.ForeignKey(
        "catalogue.ItemType", on_delete=models.PROTECT, related_name="balances"
    )
    owner_client = models.ForeignKey(
        "network.Client", on_delete=models.PROTECT, null=True, blank=True, related_name="balances"
    )
    condition = models.CharField(max_length=20, choices=Condition.choices)

    quantity = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    uom = models.CharField(max_length=20, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "node", "item_type", "owner_client", "condition"],
                name="uniq_balance_per_node_item_owner_condition",
                # Postgres treats NULLs as distinct by default, so own stock
                # (owner_client IS NULL) would get a new row on every post and
                # the "cache" would become a movement log. Postgres 15+ and
                # Django 5 both support turning that off, which is the whole
                # reason this constraint works.
                nulls_distinct=False,
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "item_type"]),
            models.Index(fields=["organization", "node"]),
            models.Index(fields=["organization", "owner_client"]),
        ]

    objects = TenantManager.from_queryset(StockBalanceQuerySet)()  # type: ignore[misc,django-manager-missing]

    def __str__(self) -> str:
        return f"{self.quantity} {self.uom} of {self.item_type_id} at {self.node_id}"


class SerialUnitStatus(models.TextChoices):
    IN_STOCK = "IN_STOCK", "In stock"
    IN_CUSTODY = "IN_CUSTODY", "Held by a person"
    INSTALLED = "INSTALLED", "Installed at a site"
    QUARANTINED = "QUARANTINED", "Quarantined"
    RETURNED_TO_CLIENT = "RETURNED_TO_CLIENT", "Returned to the client"
    SCRAPPED = "SCRAPPED", "Scrapped"


class SerialUnit(TenantModel, TimeStampedModel):
    """One physically identified unit (§3.5, D3, E2).

    The denormalised ``current_node``, ``condition`` and ``owner_client`` are for
    fast lookup only. They are written in the same transaction as the movement
    that changes them, and ``verify_ledger`` checks them against the ledger — so
    if they ever disagree, the ledger wins and the drift is reported (§3.5).
    """

    class Source(models.TextChoices):
        MANUFACTURER = "MANUFACTURER", "Manufacturer serial"
        # D3: "if a unit has no readable manufacturer serial and asset tags are
        # enabled, the system generates an internal asset tag."
        INTERNAL = "INTERNAL", "Internally generated asset tag"

    item_type = models.ForeignKey(
        "catalogue.ItemType", on_delete=models.PROTECT, related_name="serial_units"
    )
    serial_number = models.CharField(max_length=150, db_index=True)
    asset_tag = models.CharField(max_length=100, blank=True, db_index=True)
    source = models.CharField(
        max_length=20, choices=Source.choices, default=Source.MANUFACTURER
    )

    current_node = models.ForeignKey(
        "locations.StockNode", on_delete=models.PROTECT, related_name="serial_units"
    )
    condition = models.CharField(max_length=20, choices=Condition.choices)
    owner_type = models.CharField(max_length=10, choices=OwnerType.choices)
    owner_client = models.ForeignKey(
        "network.Client",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="serial_units",
    )
    status = models.CharField(
        max_length=20, choices=SerialUnitStatus.choices, default=SerialUnitStatus.IN_STOCK
    )

    # D5: recovered client-owned equipment keeps its origin, so the operator can
    # be shown what came off which site.
    origin_site = models.ForeignKey(
        "network.Site",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="recovered_units",
    )

    class Meta:
        constraints = [
            # D3: "duplicate serial within the tenant must be rejected with a
            # clear message identifying where the existing one sits."
            models.UniqueConstraint(
                fields=["organization", "serial_number"], name="uniq_serial_per_organization"
            ),
            models.UniqueConstraint(
                fields=["organization", "asset_tag"],
                condition=Q(asset_tag__gt=""),
                name="uniq_asset_tag_per_organization",
            ),
        ]
        indexes = [models.Index(fields=["organization", "current_node"])]
        ordering = ("serial_number",)

    def __str__(self) -> str:
        return self.serial_number


class ReelStatus(models.TextChoices):
    OPEN = "OPEN", "Open"
    # E3: "a drum reaching zero is closed automatically".
    CLOSED = "CLOSED", "Closed — nothing left"


class Reel(TenantModel, TimeStampedModel):
    """A numbered drum with a measured length (§3.5, D4, D12, E3)."""

    item_type = models.ForeignKey(
        "catalogue.ItemType", on_delete=models.PROTECT, related_name="reels"
    )
    drum_number = models.CharField(max_length=100, db_index=True)

    initial_length = models.DecimalField(max_digits=14, decimal_places=3)
    remaining_length = models.DecimalField(max_digits=14, decimal_places=3)
    uom = models.CharField(max_length=20, default="m")

    current_node = models.ForeignKey(
        "locations.StockNode", on_delete=models.PROTECT, related_name="reels"
    )
    condition = models.CharField(max_length=20, choices=Condition.choices)
    owner_type = models.CharField(max_length=10, choices=OwnerType.choices)
    owner_client = models.ForeignKey(
        "network.Client", on_delete=models.PROTECT, null=True, blank=True, related_name="reels"
    )
    status = models.CharField(max_length=20, choices=ReelStatus.choices, default=ReelStatus.OPEN)

    class Meta:
        constraints = [
            # D4: "drum numbers are unique within the tenant."
            models.UniqueConstraint(
                fields=["organization", "drum_number"], name="uniq_drum_number_per_organization"
            ),
            models.CheckConstraint(
                condition=Q(initial_length__gt=0), name="reel_starts_with_a_length"
            ),
            # E3: over-issue is rejected, so a negative remainder is unreachable.
            # The constraint is here because a negative length on a drum would be
            # silently wrong for months.
            models.CheckConstraint(
                condition=Q(remaining_length__gte=0), name="reel_remainder_is_not_negative"
            ),
            models.CheckConstraint(
                condition=Q(remaining_length__lte=models.F("initial_length")),
                name="reel_remainder_does_not_exceed_its_start",
            ),
        ]
        indexes = [models.Index(fields=["organization", "status"])]
        ordering = ("drum_number",)

    def __str__(self) -> str:
        return f"{self.drum_number} ({self.remaining_length} {self.uom} left)"

    @property
    def is_closed(self) -> bool:
        return self.status == ReelStatus.CLOSED


# --------------------------------------------------------------------------
# Stock counts (§4.13, E5)
# --------------------------------------------------------------------------


class StockCountStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    # E5: adjustments to client-owned stock always require approval, regardless
    # of category criticality. Such a count waits here rather than posting.
    PENDING_APPROVAL = "PENDING_APPROVAL", "Awaiting approval"
    POSTED = "POSTED", "Posted"
    CANCELLED = "CANCELLED", "Cancelled"


class StockCount(TenantModel, TimeStampedModel):
    """A physical count and the variances it found (E5).

    E5: "a count is a document listing expected versus counted quantity per
    item. Posting a count creates adjustment movements with a mandatory reason."

    The expected quantity is captured when the line is added, not recomputed at
    posting. A count is a statement about a moment; recomputing at posting would
    silently absorb anything that moved while the counting was happening — which
    is exactly the discrepancy the count exists to find.
    """

    number = models.CharField(max_length=50, blank=True, db_index=True)
    status = models.CharField(
        max_length=20,
        choices=StockCountStatus.choices,
        default=StockCountStatus.DRAFT,
        db_index=True,
    )

    location = models.ForeignKey(
        "locations.Location", on_delete=models.PROTECT, related_name="stock_counts"
    )
    counted_at = models.DateTimeField()
    counted_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    posted_at = models.DateTimeField(null=True, blank=True)
    posted_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    notes = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "number"],
                condition=Q(number__gt=""),
                name="uniq_stock_count_number_per_org",
            ),
        ]
        ordering = ("-counted_at", "-id")

    def __str__(self) -> str:
        return self.number or f"Draft count {self.pk}"

    @property
    def touches_client_owned_stock(self) -> bool:
        """E5: such a count always needs approval, whatever the category says."""
        return self.lines.filter(owner_client__isnull=False).exists()

    @property
    def has_variances(self) -> bool:
        return self.lines.exclude(counted_quantity=models.F("expected_quantity")).exists()


class StockCountLine(TenantModel, TimeStampedModel):
    """One counted item at one location (E5)."""

    stock_count = models.ForeignKey(
        StockCount, on_delete=models.CASCADE, related_name="lines"
    )
    item_type = models.ForeignKey(
        "catalogue.ItemType", on_delete=models.PROTECT, related_name="count_lines"
    )
    owner_client = models.ForeignKey(
        "network.Client",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="count_lines",
    )
    condition = models.CharField(
        max_length=20, choices=Condition.choices, default=Condition.NEW
    )

    expected_quantity = models.DecimalField(max_digits=14, decimal_places=3)
    counted_quantity = models.DecimalField(max_digits=14, decimal_places=3)
    uom = models.CharField(max_length=20)

    # E5: "posting a count creates adjustment movements with a mandatory reason."
    # Required per line rather than once per document: a variance with no
    # explanation is precisely what an auditor asks about.
    reason = models.CharField(max_length=500, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["stock_count", "item_type", "owner_client", "condition"],
                name="uniq_count_line_per_item_owner_condition",
                nulls_distinct=False,
            ),
            models.CheckConstraint(
                condition=Q(counted_quantity__gte=0), name="counted_quantity_is_not_negative"
            ),
        ]
        ordering = ("id",)

    def __str__(self) -> str:
        return f"{self.item_type}: counted {self.counted_quantity} of {self.expected_quantity}"

    @property
    def variance(self) -> Decimal:
        """Counted minus expected. Positive means more was found than expected."""
        return self.counted_quantity - self.expected_quantity

    @property
    def has_variance(self) -> bool:
        return self.variance != 0
