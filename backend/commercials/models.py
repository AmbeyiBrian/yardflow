"""Expenses and project snapshots (design §4.14; O16, O13, D29).

An expense is the only cost line in Epic O with **no ledger movement and no
contract behind it** — just a receipt and somebody's word. That is why it is the
only one a second person approves before it counts (D29), and why it is
append-only once approved: a figure nobody can independently check should at
least be a figure nobody can quietly change.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q

from core.models import TimeStampedModel
from core.tenancy import TenantModel


class ExpenseKind(models.TextChoices):
    """What a category demands of the entry, so renaming one keeps behaviour (R1)."""

    GENERAL = "GENERAL", "General"
    FUEL = "FUEL", "Fuel — needs the vehicle registration"
    CASUAL_LABOUR = "CASUAL_LABOUR", "Casual labour — needs the casuals and days"


class ExpenseCategory(TenantModel, TimeStampedModel):
    """What kind of cost this was (O16). Tenant-configurable, seeded."""

    name = models.CharField(max_length=100)
    code = models.CharField(max_length=30, blank=True)
    kind = models.CharField(
        max_length=20, choices=ExpenseKind.choices, default=ExpenseKind.GENERAL
    )
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


#: Seeded with a new tenant (O16, R1). Not a fixed list — a tenant may add,
#: rename or deactivate any of them. The third column is what the category
#: demands of an entry; "Transport and fuel" stays as it was (§4.17.11).
DEFAULT_EXPENSE_CATEGORIES: tuple[tuple[str, str, str], ...] = (
    ("Transport and fuel", "TRANSPORT", ExpenseKind.GENERAL),
    ("Equipment hire", "HIRE", ExpenseKind.GENERAL),
    ("Wayleaves and permits", "PERMITS", ExpenseKind.GENERAL),
    ("Accommodation", "ACCOMMODATION", ExpenseKind.GENERAL),
    ("Other", "OTHER", ExpenseKind.GENERAL),
    ("Fuel", "FUEL", ExpenseKind.FUEL),
    ("Team allowance", "TEAM_ALLOWANCE", ExpenseKind.GENERAL),
    ("Transport", "TRANSPORT_FARE", ExpenseKind.GENERAL),
    ("Casual labour", "CASUAL_LABOUR", ExpenseKind.CASUAL_LABOUR),
)


class ExpenseStatus(models.TextChoices):
    """Shared by expenses and allowance requests (§4.17.2, R4).

    Cost counts from ``APPROVED`` and stays through ``PAID``.
    """

    PENDING_PM = "PENDING_PM", "Waiting on the project manager"
    PENDING_FINANCE = "PENDING_FINANCE", "Waiting on Finance"
    APPROVED = "APPROVED", "Approved — counts against the project"
    PAID = "PAID", "Paid"
    REJECTED = "REJECTED", "Rejected"


#: Statuses that count against a project (§4.17.11) — the one place to say so.
COSTED_STATUSES = (ExpenseStatus.APPROVED, ExpenseStatus.PAID)

#: The only moves a status may make (§4.17.3). A creation is not a move: an
#: entry may be born at ``PENDING_FINANCE`` when the PM level is skipped, and a
#: reversal is born ``APPROVED``. A rejected entry resubmits to its first open
#: level, which is ``PENDING_FINANCE`` when the PM level is skipped.
STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    ExpenseStatus.PENDING_PM: frozenset(
        {ExpenseStatus.PENDING_FINANCE, ExpenseStatus.REJECTED}
    ),
    ExpenseStatus.PENDING_FINANCE: frozenset(
        {ExpenseStatus.APPROVED, ExpenseStatus.REJECTED}
    ),
    ExpenseStatus.APPROVED: frozenset({ExpenseStatus.PAID}),
    ExpenseStatus.PAID: frozenset(),
    ExpenseStatus.REJECTED: frozenset(
        {ExpenseStatus.PENDING_PM, ExpenseStatus.PENDING_FINANCE}
    ),
}


class StatusGuardMixin(models.Model):
    """Append-only once approved, and only the §4.17.3 moves (§4.17.2 guards).

    The row remembers what it was loaded with, so the guard needs no second
    query — and no ``all_objects`` (§2.1). Once ``APPROVED`` only the columns in
    ``PAID_FIELDS`` and the status itself may change, and only to ``PAID``;
    once ``PAID``, only ``SETTLEMENT_FIELDS``.
    """

    #: What the payment step writes. Everything else is frozen from APPROVED on.
    PAID_FIELDS: frozenset[str] = frozenset(
        {"paid_at", "paid_by_id", "payment_reference", "updated_at"}
    )
    #: What may still change on a PAID row (a float's closing).
    SETTLEMENT_FIELDS: frozenset[str] = frozenset({"updated_at"})
    #: Wording for the error, so each entry kind reads naturally.
    GUARD_NOUN = "entry"

    _loaded_state: dict[str, Any] | None = None

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self._guard_changes()
        result = super().save(*args, **kwargs)
        self._loaded_state = {
            field.attname: getattr(self, field.attname)
            for field in self._meta.concrete_fields
        }
        return result

    @property
    def _loaded_status(self) -> str | None:
        state = self._loaded_state
        return None if state is None else state.get("status")

    @classmethod
    def from_db(cls, db, field_names, values):  # type: ignore[no-untyped-def]
        instance = super().from_db(db, field_names, values)
        # Guarded: a deferred ``status`` would make a read here fire a refresh,
        # which re-enters ``from_db`` without end. ``values`` is positional over
        # the loaded concrete fields, so zip rather than getattr.
        if "status" in field_names:
            instance._loaded_state = {
                field.attname: value
                for field, value in zip(cls._meta.concrete_fields, values, strict=False)
                if field.attname in field_names
            }
        return instance

    def _guard_changes(self) -> None:
        state = self._loaded_state
        if state is None or self._state.adding:
            return
        before: str = state["status"]
        after = self.status  # type: ignore[attr-defined]
        noun = self.GUARD_NOUN

        if before != after and after not in STATUS_TRANSITIONS.get(before, frozenset()):
            raise ValidationError(
                f"An {noun} cannot move from {before} to {after} (§4.17.3)."
            )

        if before in (ExpenseStatus.APPROVED, ExpenseStatus.PAID):
            allowed = (
                self.PAID_FIELDS
                if before == ExpenseStatus.APPROVED
                else self.SETTLEMENT_FIELDS
            ) | {"status"}
            changed = [
                field.attname
                for field in self._meta.concrete_fields
                if field.attname in state
                and field.attname not in allowed
                and getattr(self, field.attname) != state[field.attname]
            ]
            if changed:
                raise ValidationError(
                    f"An approved {noun} cannot be changed ({', '.join(changed)}) — "
                    "it is already counted. Record a reversing entry instead (O16)."
                )


class AllowanceType(models.TextChoices):
    FLOAT = "FLOAT", "Float"
    TRANSPORT = "TRANSPORT", "Transport"
    NIGHT_OUT = "NIGHT_OUT", "Night out"
    TEAM_ALLOWANCE = "TEAM_ALLOWANCE", "Team allowance"
    OTHER = "OTHER", "Other"


class TransportScope(models.TextChoices):
    WITHIN_NAIROBI = "WITHIN_NAIROBI", "Within Nairobi"
    OUTSIDE_NAIROBI = "OUTSIDE_NAIROBI", "Outside Nairobi"


class Casual(TenantModel, TimeStampedModel):
    """A casual worker, remembered so nobody types them twice (R3).

    Not a ``User``: a casual cannot sign in. The ID photo is an ``Attachment``.
    """

    name = models.CharField(max_length=200)
    id_number = models.CharField(max_length=40)
    #: What uniqueness is judged on, so "12 345-678" and "12345678" are one
    #: person. Derived on save; never typed.
    id_number_key = models.CharField(max_length=40, editable=False)
    phone = models.CharField(max_length=30, blank=True)
    registered_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="+"
    )
    # R6: a casual registered offline replays to the same row.
    client_uuid = models.UUIDField(null=True, blank=True)

    class Meta:
        ordering = ("name",)
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "id_number_key"],
                name="uniq_casual_id_number_per_org",
            ),
            models.UniqueConstraint(
                fields=["organization", "client_uuid"],
                condition=Q(client_uuid__isnull=False),
                name="uniq_casual_client_uuid_per_org",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    @staticmethod
    def normalise_id_number(value: str) -> str:
        """Upper-case, with spaces and dashes removed (§4.17.2)."""
        return "".join(value.upper().replace("-", "").split())

    def save(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.id_number_key = self.normalise_id_number(self.id_number)
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "id_number" in update_fields:
            kwargs["update_fields"] = {*update_fields, "id_number_key"}
        return super().save(*args, **kwargs)


class AllowanceRequest(StatusGuardMixin, TenantModel, TimeStampedModel):
    """Money asked for before it is spent (R2, §4.17.2).

    A float is one of these, of type ``FLOAT``; expenses point at it through
    ``ProjectExpense.float_request``. It goes through the same two levels as an
    expense, then Finance marks it paid.
    """

    GUARD_NOUN = "allowance request"
    #: A paid float is closed later, recording what came back (R2).
    SETTLEMENT_FIELDS = frozenset(
        {"closed_at", "closed_by_id", "returned_amount", "updated_at"}
    )

    number = models.CharField(max_length=50, blank=True)
    type = models.CharField(max_length=20, choices=AllowanceType.choices)
    transport_scope = models.CharField(
        max_length=20, choices=TransportScope.choices, blank=True
    )
    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="The whole request, not per day. Excluding VAT (D24).",
    )
    from_date = models.DateField()
    to_date = models.DateField()
    site = models.ForeignKey(
        "network.Site",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="allowance_requests",
    )
    project = models.ForeignKey(
        "network.Project", on_delete=models.PROTECT, related_name="allowance_requests"
    )
    reason = models.CharField(max_length=500, blank=True)

    recorded_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="allowances_recorded"
    )
    status = models.CharField(
        max_length=20,
        choices=ExpenseStatus.choices,
        default=ExpenseStatus.PENDING_PM,
        db_index=True,
    )
    decided_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_reason = models.CharField(max_length=500, blank=True)

    paid_at = models.DateTimeField(null=True, blank=True)
    paid_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    payment_reference = models.CharField(max_length=100, blank=True)

    # A float only: closed when what is left has come back.
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    returned_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    # R6: offline idempotency, as GateIn.client_uuid.
    client_uuid = models.UUIDField(null=True, blank=True)

    class Meta:
        ordering = ("-from_date", "-id")
        indexes = [
            models.Index(fields=["organization", "status"]),
            models.Index(fields=["organization", "recorded_by", "type"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "number"],
                condition=~Q(number=""),
                name="uniq_allowance_number_per_org",
            ),
            models.UniqueConstraint(
                fields=["organization", "client_uuid"],
                condition=Q(client_uuid__isnull=False),
                name="uniq_allowance_client_uuid_per_org",
            ),
            models.CheckConstraint(
                condition=Q(to_date__gte=models.F("from_date")),
                name="an_allowance_ends_on_or_after_it_starts",
            ),
            models.CheckConstraint(
                condition=Q(type="TRANSPORT") | Q(transport_scope=""),
                name="only_transport_has_a_scope",
            ),
            models.CheckConstraint(
                condition=Q(
                    status__in=(
                        ExpenseStatus.PENDING_PM,
                        ExpenseStatus.PENDING_FINANCE,
                    ),
                    decided_at__isnull=True,
                )
                | Q(
                    status__in=(
                        ExpenseStatus.APPROVED,
                        ExpenseStatus.PAID,
                        ExpenseStatus.REJECTED,
                    ),
                    decided_at__isnull=False,
                ),
                name="a_decided_allowance_records_when",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.number or 'Allowance'} {self.get_type_display()} {self.amount}"

    @property
    def days(self) -> int:
        """Inclusive: a one-day request has ``from_date == to_date`` (§4.17.2)."""
        return (self.to_date - self.from_date).days + 1

    @property
    def requested_by_id(self) -> int:
        """The approval engine's name for the recorder (§4.17.3)."""
        return self.recorded_by_id

    def delete(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise ValidationError(
            "Allowance requests are never deleted. Reject one that should not stand."
        )


class ProjectExpense(StatusGuardMixin, TenantModel, TimeStampedModel):
    """A cost somebody paid out of pocket on a project (O16, D29, R1).

    Recorded by whoever incurred it — a technician at a fuel station is closer
    to the fact than anybody back at the yard — and approved by the manager
    whose budget it lands on, then by Finance (R4).
    """

    GUARD_NOUN = "expense"

    project = models.ForeignKey(
        "network.Project", on_delete=models.PROTECT, related_name="expenses"
    )
    # R1: where the money was spent. Optional only when the project was given
    # directly (a permit for the PO); the rule lives in the service.
    site = models.ForeignKey(
        "network.Site",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="expenses",
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
    scope_of_work = models.TextField(blank=True)

    # Fuel (R1): the registration is required when the category is FUEL; litres
    # are optional. Enforced in the service, where the category is known.
    vehicle_reg = models.CharField(max_length=20, blank=True)
    litres = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
    )

    # R2: spent from a paid float, so it is not paid again (§4.17.2).
    float_request = models.ForeignKey(
        AllowanceRequest,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="expenses",
    )

    # R6: how many photos the phone will send, so "arriving" can be told from
    # "no evidence" (§4.17.8).
    photos_expected = models.PositiveSmallIntegerField(default=0)
    # R6: offline idempotency, as GateIn.client_uuid.
    client_uuid = models.UUIDField(null=True, blank=True)

    recorded_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="expenses_recorded"
    )

    status = models.CharField(
        max_length=20,
        choices=ExpenseStatus.choices,
        default=ExpenseStatus.PENDING_PM,
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

    # R4: YardFlow records a payment; it does not send money. Not set on a
    # float-backed expense — it was paid out of the float already.
    paid_at = models.DateTimeField(null=True, blank=True)
    paid_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    payment_reference = models.CharField(max_length=100, blank=True)

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
            models.UniqueConstraint(
                fields=["organization", "client_uuid"],
                condition=Q(client_uuid__isnull=False),
                name="uniq_expense_client_uuid_per_org",
            ),
            models.CheckConstraint(
                condition=Q(
                    status__in=(
                        ExpenseStatus.PENDING_PM,
                        ExpenseStatus.PENDING_FINANCE,
                    ),
                    decided_at__isnull=True,
                )
                | Q(
                    status__in=(
                        ExpenseStatus.APPROVED,
                        ExpenseStatus.PAID,
                        ExpenseStatus.REJECTED,
                    ),
                    decided_at__isnull=False,
                ),
                name="a_decided_expense_records_when",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.category} {self.amount} on {self.project}"

    @property
    def requested_by_id(self) -> int:
        """The approval engine's name for the recorder (§4.17.3)."""
        return self.recorded_by_id

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

    def delete(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise ValidationError(
            "Expenses are never deleted. Reject one that should not stand, or "
            "reverse one already approved (O16)."
        )


class ExpenseCasualLine(TenantModel, TimeStampedModel):
    """One casual's days on a casual-labour expense (R1, R3).

    The expense total is the authority; a per-line amount is optional. Frozen
    with the expense once it is approved.
    """

    expense = models.ForeignKey(
        ProjectExpense, on_delete=models.PROTECT, related_name="casual_lines"
    )
    casual = models.ForeignKey(Casual, on_delete=models.PROTECT, related_name="lines")
    days = models.PositiveSmallIntegerField(validators=[MinValueValidator(1)])
    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
    )

    class Meta:
        ordering = ("id",)
        constraints = [
            models.CheckConstraint(condition=Q(days__gt=0), name="a_casual_works_days"),
            models.UniqueConstraint(
                fields=["expense", "casual"], name="uniq_casual_once_per_expense"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.casual} × {self.days}"

    def _guard(self) -> None:
        status = (
            ProjectExpense.objects.filter(pk=self.expense_id)
            .values_list("status", flat=True)
            .first()
        )
        if status in (ExpenseStatus.APPROVED, ExpenseStatus.PAID):
            raise ValidationError(
                "An approved expense cannot be changed — its casual lines are "
                "frozen with it (O16)."
            )

    def save(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self._guard()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self._guard()
        return super().delete(*args, **kwargs)


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
