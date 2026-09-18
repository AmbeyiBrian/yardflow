"""Expenses and project snapshots (design §4.14; O16, O13, D29).

An expense is the only cost line in Epic O with **no ledger movement and no
contract behind it** — just a receipt and somebody's word. That is why it is the
only one a second person approves before it counts (D29), and why it is
append-only once approved: a figure nobody can independently check should at
least be a figure nobody can quietly change.
"""

from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q

from core.models import TimeStampedModel
from core.tenancy import TenantModel


class ExpenseCategory(TenantModel, TimeStampedModel):
    """What kind of cost this was (O16). Tenant-configurable, seeded."""

    name = models.CharField(max_length=100)
    code = models.CharField(max_length=30, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "name"], name="uniq_expense_category_per_org"
            )
        ]
        ordering = ("name",)
        verbose_name_plural = "expense categories"

    def __str__(self) -> str:
        return self.name


#: Seeded with a new tenant (O16). Not a fixed list — a tenant may add, rename
#: or deactivate any of them.
DEFAULT_EXPENSE_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("Transport and fuel", "TRANSPORT"),
    ("Equipment hire", "HIRE"),
    ("Wayleaves and permits", "PERMITS"),
    ("Accommodation", "ACCOMMODATION"),
    ("Other", "OTHER"),
)


class ExpenseStatus(models.TextChoices):
    SUBMITTED = "SUBMITTED", "Waiting on the project manager"
    APPROVED = "APPROVED", "Approved — counts against the project"
    REJECTED = "REJECTED", "Rejected"


class ProjectExpense(TenantModel, TimeStampedModel):
    """A cost somebody paid out of pocket on a project (O16, D29).

    Recorded by whoever incurred it — a technician at a fuel station is closer
    to the fact than anybody back at the yard — and approved by the manager
    whose budget it lands on.
    """

    project = models.ForeignKey(
        "network.Project", on_delete=models.PROTECT, related_name="expenses"
    )
    # Optional: some costs belong to the PO rather than to any one site.
    job = models.ForeignKey(
        "jobs.Job",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="expenses",
    )
    category = models.ForeignKey(
        ExpenseCategory, on_delete=models.PROTECT, related_name="expenses"
    )

    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Excluding VAT (D24).",
    )
    incurred_on = models.DateField()
    description = models.CharField(max_length=500, blank=True)

    recorded_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="expenses_recorded"
    )

    status = models.CharField(
        max_length=20,
        choices=ExpenseStatus.choices,
        default=ExpenseStatus.SUBMITTED,
        db_index=True,
    )
    decided_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_reason = models.CharField(max_length=500, blank=True)

    # M4's discipline, applied to money: an approved expense is corrected by a
    # reversing entry, never by an edit.
    reverses = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversals",
    )

    class Meta:
        ordering = ("-incurred_on", "-id")
        indexes = [
            models.Index(fields=["organization", "project", "status"]),
            models.Index(fields=["organization", "status"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(status=ExpenseStatus.SUBMITTED, decided_at__isnull=True)
                | Q(
                    status__in=(ExpenseStatus.APPROVED, ExpenseStatus.REJECTED),
                    decided_at__isnull=False,
                ),
                name="a_decided_expense_records_when",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.category} {self.amount} on {self.project}"

    @property
    def is_evidenced(self) -> bool:
        """Whether a receipt is attached (O16).

        An unevidenced expense is accepted and flagged to the manager, not
        refused. Refusing it would lose the **cost** when all that is missing is
        the evidence — a real receipt that would not photograph is still a real
        cost.
        """
        from core.models import Attachment

        return Attachment.objects.filter(
            target_type=self._meta.label, target_id=str(self.pk)
        ).exists()

    @property
    def signed_amount(self) -> Decimal:
        """What this contributes to project cost: negative for a reversal."""
        return -self.amount if self.reverses_id else self.amount

    #: The status this row was read with, so the append-only guard needs no
    #: second query — and no ``all_objects`` (§2.1).
    _loaded_status: str | None = None

    @classmethod
    def from_db(cls, db, field_names, values):  # type: ignore[no-untyped-def]
        instance = super().from_db(db, field_names, values)
        # Guarded: a deferred ``status`` would make this read fire a refresh,
        # which re-enters ``from_db`` without end.
        if "status" in field_names:
            instance._loaded_status = instance.status
        return instance

    def save(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self._loaded_status == ExpenseStatus.APPROVED:
            raise ValidationError(
                "An approved expense cannot be changed — it is already counted "
                "against the project. Record a reversing entry instead (O16)."
            )
        result = super().save(*args, **kwargs)
        self._loaded_status = self.status
        return result

    def delete(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise ValidationError(
            "Expenses are never deleted. Reject one that should not stand, or "
            "reverse one already approved (O16)."
        )


class ProjectSnapshot(TenantModel, TimeStampedModel):
    """A project's figures as they stood when it closed (O13).

    The ledger stays live after a project closes — a reversal posted next month
    is a true correction to the record. But a closed PO's reported margin should
    not move under the people who signed it off, so the figures are frozen here
    and the report reads these rather than recomputing.
    """

    project = models.ForeignKey(
        "network.Project", on_delete=models.CASCADE, related_name="snapshots"
    )
    taken_at = models.DateTimeField(auto_now_add=True)
    taken_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    figures = models.JSONField(default=dict)

    class Meta:
        ordering = ("-taken_at",)

    def __str__(self) -> str:
        return f"{self.project} as at {self.taken_at:%Y-%m-%d}"
