"""The asset register (Epic R, R14, design §4.20.2).

A thin app: what the company owns (vehicles, generators, tools) and who holds
it. Not stock: nothing here is a quantity (R14).
"""

from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import F, Q

from core.models import TimeStampedModel
from core.tenancy import TenantModel


class AssetType(models.TextChoices):
    VEHICLE = "VEHICLE", "Vehicle"
    GENERATOR = "GENERATOR", "Generator"
    TOOL = "TOOL", "Tool"
    EQUIPMENT = "EQUIPMENT", "Equipment"
    OTHER = "OTHER", "Other"


class AssetStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    CLOSED = "CLOSED", "Closed"


class AssetCloseReason(models.TextChoices):
    SOLD = "SOLD", "Sold"
    WRITTEN_OFF = "WRITTEN_OFF", "Written off"


class Asset(TenantModel, TimeStampedModel):
    """Something the company owns. Never deleted: close it instead."""

    #: Fields that only mean something for a vehicle.
    VEHICLE_ONLY_FIELDS = (
        "make",
        "model",
        "insurance_expires_on",
        "inspection_expires_on",
    )

    type = models.CharField(max_length=12, choices=AssetType.choices)
    name = models.CharField(max_length=200)
    #: The registration for a vehicle; may be blank for other types.
    tag = models.CharField(max_length=40, blank=True)
    #: What uniqueness is judged on: upper-case, spaces and dashes removed.
    tag_key = models.CharField(max_length=40, blank=True, editable=False)

    purchase_date = models.DateField(null=True, blank=True)
    #: Who it was bought from (R15). A register entry, whatever its status.
    supplier = models.ForeignKey(
        "network.Supplier",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="assets",
    )
    cost = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    purchase_terms = models.TextField(blank=True)

    # Vehicle only.
    make = models.CharField(max_length=80, blank=True)
    model = models.CharField(max_length=80, blank=True)
    insurance_expires_on = models.DateField(null=True, blank=True)
    inspection_expires_on = models.DateField(null=True, blank=True)
    #: The expiry the sweep last alerted on, so a daily run cannot nag and a
    #: renewal (a new date) re-arms by itself (§4.20.4).
    insurance_alerted_for = models.DateField(null=True, blank=True)
    inspection_alerted_for = models.DateField(null=True, blank=True)

    #: Null means held in the yard. A projection of the last handover.
    holder = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="held_assets",
    )

    status = models.CharField(
        max_length=8, choices=AssetStatus.choices, default=AssetStatus.ACTIVE
    )
    closed_on = models.DateField(null=True, blank=True)
    closed_reason = models.CharField(
        max_length=12, choices=AssetCloseReason.choices, blank=True
    )
    closed_note = models.TextField(blank=True)
    closed_by = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )

    class Meta:
        ordering = ("name",)
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "tag_key"],
                condition=~Q(tag_key=""),
                name="uniq_asset_tag_per_org",
            ),
            models.CheckConstraint(
                condition=~Q(status="CLOSED")
                | (Q(closed_on__isnull=False) & ~Q(closed_reason="")),
                name="a_closed_asset_records_when_and_why",
            ),
            models.CheckConstraint(
                condition=Q(type="VEHICLE")
                | (
                    Q(make="")
                    & Q(model="")
                    & Q(insurance_expires_on__isnull=True)
                    & Q(inspection_expires_on__isnull=True)
                ),
                name="a_vehicle_only_fields_need_a_vehicle",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.tag})" if self.tag else self.name

    @staticmethod
    def normalise_tag(value: str) -> str:
        """Upper-case, with spaces and dashes removed (§4.20.2)."""
        return "".join(value.upper().replace("-", "").split())

    def clean(self) -> None:
        super().clean()
        if self.type != AssetType.VEHICLE:
            filled = [f for f in self.VEHICLE_ONLY_FIELDS if getattr(self, f)]
            if filled:
                raise ValidationError(dict.fromkeys(filled, "Only a vehicle has this."))
        elif not self.tag.strip():
            raise ValidationError({"tag": "A vehicle needs its registration."})

    def save(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.tag_key = self.normalise_tag(self.tag)
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "tag" in update_fields:
            kwargs["update_fields"] = {*update_fields, "tag_key"}
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise ValidationError("Assets are never deleted. Close one that has gone.")


class AssetHandover(TenantModel):
    """Who handed an asset to whom, and when. Append-only (trigger).

    Null on either side means the yard. No acknowledgement and no ledger:
    that is what separates it from a custody transfer (§4.20.1).
    """

    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name="handovers")
    from_holder = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    to_holder = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    handed_over_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="+"
    )
    handed_over_on = models.DateField()
    note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-handed_over_on", "-id")
        constraints = [
            models.CheckConstraint(
                condition=~(Q(from_holder__isnull=True) & Q(to_holder__isnull=True))
                & ~Q(from_holder=F("to_holder")),
                name="a_handover_changes_hands",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.asset}: {self.from_holder or 'yard'} -> {self.to_holder or 'yard'}"
