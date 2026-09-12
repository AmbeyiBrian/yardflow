"""The item catalogue (design §4.3; requirements C1, C2, C3).

D10: the catalogue is **user-definable**, seeded with a starter telecom list. The
seed is a convenience, not a schema — a tenant may rename or delete anything in
it, and the second customer will want different items.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models

from core.models import TimeStampedModel
from core.tenancy import TenantModel


class Criticality(models.TextChoices):
    """How tightly material of this kind must be controlled (C1).

    Drives approval routing (F3, §5.1): the higher the criticality on a gate-out,
    the more senior the approver required. This is the *only* routing dimension
    active in v1.
    """

    NONE = "NONE", "None"
    LOW = "LOW", "Low"
    MEDIUM = "MEDIUM", "Medium"
    HIGH = "HIGH", "High"


#: Ordered lowest to highest, so a document with several categories can resolve
#: to the highest applicable level (F3).
CRITICALITY_ORDER: dict[str, int] = {
    Criticality.NONE: 0,
    Criticality.LOW: 1,
    Criticality.MEDIUM: 2,
    Criticality.HIGH: 3,
}


class TrackingMode(models.TextChoices):
    """How a given item type is counted (C3, D11, D12, §3.5)."""

    SERIALIZED = "SERIALIZED", "Serialized — each unit individually identified"
    BULK = "BULK", "Bulk — a counted quantity"
    REEL = "REEL", "Reel — a measured length on a numbered drum"


class ItemCategory(TenantModel, TimeStampedModel):
    """A category of material, with a criticality flag (C1).

    Hierarchical to at least two levels, as C1 requires. The depth limit is
    enforced rather than left to convention: a routing rule that has to walk an
    arbitrarily deep tree to find a criticality becomes both slow and hard to
    reason about (F3).
    """

    MAX_DEPTH = 2

    parent = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="children"
    )
    name = models.CharField(max_length=150)
    code = models.CharField(max_length=50, blank=True)
    criticality = models.CharField(
        max_length=10,
        choices=Criticality.choices,
        default=Criticality.NONE,
        help_text="Drives which role must approve a gate-out containing this material.",
    )
    is_archived = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "parent", "name"],
                name="uniq_category_name_per_parent",
            ),
        ]
        ordering = ("name",)
        verbose_name_plural = "item categories"

    def __str__(self) -> str:
        return self.name

    @property
    def depth(self) -> int:
        """1 for a top-level category, 2 for a child."""
        parent = self.parent
        return 1 if parent is None else 1 + parent.depth

    def clean(self) -> None:
        super().clean()

        parent = self.parent
        if parent is not None:
            if self.parent_id == self.pk:
                raise ValidationError({"parent": "A category cannot be its own parent."})
            if parent.depth >= self.MAX_DEPTH:
                raise ValidationError(
                    {
                        "parent": (
                            f"Categories go {self.MAX_DEPTH} levels deep. "
                            f"'{parent}' is already at the deepest level."
                        )
                    }
                )
            # A cycle would make `depth` recurse forever.
            ancestor: ItemCategory | None = parent
            seen: set[int] = set()
            while ancestor is not None:
                if ancestor.pk in seen or ancestor.pk == self.pk:
                    raise ValidationError({"parent": "That would create a loop."})
                seen.add(ancestor.pk)
                ancestor = ancestor.parent

    def save(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.full_clean(exclude=["organization"])
        return super().save(*args, **kwargs)

    def effective_criticality(self) -> str:
        """This category's criticality, or its parent's if it has none set.

        Inheritance matters for routing: an admin who sets HIGH on "Active
        equipment" expects its children to be treated as HIGH without having to
        repeat it on each one (C1, F3).
        """
        if self.criticality != Criticality.NONE:
            return self.criticality
        parent = self.parent
        if parent is not None:
            return parent.effective_criticality()
        return Criticality.NONE


class CategoryCustomField(TenantModel, TimeStampedModel):
    """An extra attribute captured for items in a category (C2).

    C2: "define custom fields per category, so that I can capture the attributes
    that matter for that kind of equipment."
    """

    class FieldType(models.TextChoices):
        TEXT = "TEXT", "Text"
        NUMBER = "NUMBER", "Number"
        DATE = "DATE", "Date"
        DROPDOWN = "DROPDOWN", "Dropdown"
        BOOLEAN = "BOOLEAN", "Yes / no"

    category = models.ForeignKey(
        ItemCategory, on_delete=models.CASCADE, related_name="custom_fields"
    )
    label = models.CharField(max_length=100)
    key = models.SlugField(
        max_length=100,
        help_text="Stable identifier used in stored values. Immutable once set.",
    )
    field_type = models.CharField(max_length=20, choices=FieldType.choices)
    required_at_gate_in = models.BooleanField(default=False)
    options = models.JSONField(
        default=list, blank=True, help_text="Choices, for a dropdown field."
    )
    order = models.PositiveIntegerField(default=0)
    is_archived = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["category", "key"], name="uniq_custom_field_key_per_category"
            ),
        ]
        ordering = ("order", "label")

    def __str__(self) -> str:
        return f"{self.category.name}: {self.label}"

    def clean(self) -> None:
        super().clean()
        if self.field_type == self.FieldType.DROPDOWN and not self.options:
            raise ValidationError(
                {"options": "A dropdown field needs at least one option."}
            )
        if self.field_type != self.FieldType.DROPDOWN and self.options:
            raise ValidationError(
                {"options": f"A {self.get_field_type_display()} field takes no options."}
            )

        # The key is written into every stored value, so changing it would
        # orphan history that has already been captured against it.
        if self.pk:
            previous = (
                CategoryCustomField.objects.filter(pk=self.pk)
                .values_list("key", flat=True)
                .first()
            )
            if previous is not None and previous != self.key:
                raise ValidationError(
                    {
                        "key": (
                            f"A custom field key is immutable once set: cannot "
                            f"change '{previous}' to '{self.key}'. Archive this "
                            f"field and add a new one instead."
                        )
                    }
                )

    def save(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.full_clean(exclude=["organization"])
        return super().save(*args, **kwargs)

    def validate_value(self, value) -> None:
        """Check one captured value against this field's type (C2).

        Raises :class:`ValidationError` keyed by this field's ``key``, so the
        message lands on the right input in the gate-in form (§6.1).
        """
        if value in (None, ""):
            if self.required_at_gate_in:
                raise ValidationError({self.key: f"{self.label} is required."})
            return

        if self.field_type == self.FieldType.NUMBER:
            try:
                float(value)
            except (TypeError, ValueError) as exc:
                raise ValidationError({self.key: f"{self.label} must be a number."}) from exc

        elif self.field_type == self.FieldType.BOOLEAN:
            if not isinstance(value, bool):
                raise ValidationError({self.key: f"{self.label} must be yes or no."})

        elif self.field_type == self.FieldType.DROPDOWN:
            if value not in (self.options or []):
                raise ValidationError(
                    {
                        self.key: (
                            f"{self.label} must be one of: "
                            f"{', '.join(str(o) for o in self.options)}."
                        )
                    }
                )

        elif self.field_type == self.FieldType.DATE:
            from django.utils.dateparse import parse_date

            if not isinstance(value, str) or parse_date(value) is None:
                raise ValidationError(
                    {self.key: f"{self.label} must be a date, as YYYY-MM-DD."}
                )


class ItemType(TenantModel, TimeStampedModel):  # type: ignore[django-manager-missing]
    """A catalogue entry — "RRU 2x40W", "LDF4 feeder cable" (C3)."""

    category = models.ForeignKey(
        ItemCategory, on_delete=models.PROTECT, related_name="item_types"
    )
    name = models.CharField(max_length=200)
    code = models.CharField(max_length=50, blank=True)
    description = models.CharField(max_length=500, blank=True)

    # D11: defaults per item type, overridable per receipt.
    default_tracking_mode = models.CharField(
        max_length=20, choices=TrackingMode.choices, default=TrackingMode.BULK
    )
    uom = models.CharField(
        max_length=20, default="ea", help_text="Unit of measure: ea, m, kg, roll."
    )

    # I2: tools and returnable material have an expected return date.
    is_returnable = models.BooleanField(default=False)
    default_return_days = models.PositiveIntegerField(null=True, blank=True)

    # E6: only consulted when the tenant has minimum stock enabled.
    min_stock_qty = models.DecimalField(
        max_digits=14, decimal_places=3, null=True, blank=True
    )

    # D16: only meaningful when money tracking is on. Kept nullable rather than
    # defaulting to zero, so "not priced" is distinguishable from "free".
    unit_cost = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)

    # C3: item types cannot be deleted once movements exist — only archived.
    is_archived = models.BooleanField(default=False, db_index=True)

    attributes = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "name"], name="uniq_item_type_name_per_org"
            ),
            models.UniqueConstraint(
                fields=["organization", "code"],
                condition=models.Q(code__gt=""),
                name="uniq_item_type_code_per_org",
            ),
            # A returnable item without a return period would silently never
            # become overdue, which is the whole point of tracking it (I2, I3).
            models.CheckConstraint(
                condition=models.Q(is_returnable=False)
                | models.Q(default_return_days__isnull=False),
                name="returnable_item_has_a_return_period",
            ),
        ]
        ordering = ("name",)
        indexes = [models.Index(fields=["organization", "is_archived"])]

    def __str__(self) -> str:
        return self.name

    @property
    def criticality(self) -> str:
        """Routing criticality, inherited from the category (C1, F3)."""
        return self.category.effective_criticality()

    def clean(self) -> None:
        super().clean()
        if self.is_returnable and not self.default_return_days:
            raise ValidationError(
                {
                    "default_return_days": (
                        "A returnable item needs a default return period, or it "
                        "could never be reported overdue (I2, I3)."
                    )
                }
            )
        if self.default_tracking_mode == TrackingMode.REEL and self.uom == "ea":
            raise ValidationError(
                {
                    "uom": (
                        "A reel is measured, not counted. Set the unit of measure "
                        "to a length such as 'm' (D12)."
                    )
                }
            )

    def save(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.full_clean(exclude=["organization"])
        return super().save(*args, **kwargs)

    def custom_fields(self):
        """Custom fields that apply here — the category's and its parent's."""
        category_ids = [self.category_id]
        if self.category.parent_id:
            category_ids.append(self.category.parent_id)
        return CategoryCustomField.objects.filter(
            category_id__in=category_ids, is_archived=False
        ).order_by("order", "label")

    def validate_custom_field_values(self, values: dict | None) -> None:
        """Validate captured values against the applicable fields (C2)."""
        values = values or {}
        errors: dict[str, list[str]] = {}

        for field in self.custom_fields():
            try:
                field.validate_value(values.get(field.key))
            except ValidationError as exc:
                for key, messages in exc.message_dict.items():
                    errors.setdefault(key, []).extend(messages)

        if errors:
            raise ValidationError(errors)
