"""Jobs, closeout and variances (design §4.9; H1–H5, M1).

H2 is the requirement everything here serves: the technician reports what was
installed, what is coming back, and what was consumed — so the yard knows what to
expect.

§4.9's split matters: **INSTALLED and CONSUMED post movements immediately**,
because that material is gone and the ledger should say so. **RETURNING and
RECOVERED create expectations**, because that material has not arrived yet and
recording it as received would be a lie the reconciliation would then hide.
"""

from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q

from core.models import TimeStampedModel
from core.tenancy import TenantModel


class DeliveryMode(models.TextChoices):
    """Who actually does the work (O3).

    Recorded per job rather than per project because a PO awarded in bulk is
    usually allocated afterwards, by region or by capacity — so one PO routinely
    has both kinds of job under it.
    """

    IN_HOUSE = "IN_HOUSE", "Delivered by our own crews"
    SUBCONTRACTED = "SUBCONTRACTED", "Delivered by a subcontractor"


class JobStatus(models.TextChoices):
    OPEN = "OPEN", "Open"
    IN_PROGRESS = "IN_PROGRESS", "In progress"
    AWAITING_CLOSEOUT = "AWAITING_CLOSEOUT", "Awaiting closeout"
    CLOSED = "CLOSED", "Closed"
    CANCELLED = "CANCELLED", "Cancelled"


class Job(TenantModel, TimeStampedModel):
    """A piece of work at a site, with someone accountable for closing it (H1).

    H1: "assign a site or job to a named person who is responsible for closing it
    out, **so that accountability is explicit**." The assignee is required for
    that reason — an unassigned job is one nobody has to finish.
    """

    reference = models.CharField(max_length=100, blank=True, db_index=True)
    client = models.ForeignKey(
        "network.Client", on_delete=models.PROTECT, related_name="jobs"
    )
    site = models.ForeignKey("network.Site", on_delete=models.PROTECT, related_name="jobs")
    # C7, D14: the project layer is optional throughout.
    project = models.ForeignKey(
        "network.Project",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="jobs",
    )

    assignee = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="jobs_assigned"
    )
    description = models.CharField(max_length=500, blank=True)

    # O3: how this job is delivered, and what it costs the project. A PO may be
    # mixed — some sites in-house, some passed to one contractor, some to
    # another — so this lives on the job, never on the project.
    delivery_mode = models.CharField(
        max_length=20, choices=DeliveryMode.choices, default=DeliveryMode.IN_HOUSE
    )
    subcontractor = models.ForeignKey(
        "network.Subcontractor",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="jobs",
    )
    agreed_price = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
        help_text="What was agreed with the contractor for this job, excluding VAT.",
    )

    status = models.CharField(
        max_length=20, choices=JobStatus.choices, default=JobStatus.OPEN, db_index=True
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    # H5: closing with material unaccounted for requires an override and a
    # reason. Recorded on the job, because it is a finding about this job.
    closed_with_variance = models.BooleanField(default=False)
    close_reason = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["organization", "status"]),
            models.Index(fields=["organization", "assignee", "status"]),
            models.Index(fields=["organization", "site"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "reference"],
                condition=Q(reference__gt=""),
                name="uniq_job_reference_per_org",
            ),
            models.CheckConstraint(
                condition=~Q(status=JobStatus.CLOSED) | Q(closed_at__isnull=False),
                name="a_closed_job_records_when",
            ),
            # O3: the two halves of a subcontracted job travel together. A
            # contractor with no agreed price contributes nothing to the
            # project's cost and would quietly flatter it; a price with no
            # contractor cannot be rolled up by party.
            models.CheckConstraint(
                condition=Q(
                    delivery_mode=DeliveryMode.IN_HOUSE,
                    subcontractor__isnull=True,
                    agreed_price__isnull=True,
                )
                | Q(
                    delivery_mode=DeliveryMode.SUBCONTRACTED,
                    subcontractor__isnull=False,
                    agreed_price__isnull=False,
                ),
                name="delivery_mode_and_its_cost_agree",
            ),
        ]

    def __str__(self) -> str:
        return self.reference or f"Job at {self.site}"

    @property
    def is_closed(self) -> bool:
        return self.status == JobStatus.CLOSED

    #: What delivery looked like when this row was read, so the guard below can
    #: tell a change from a re-save without a second query.
    _loaded_delivery: tuple | None = None

    #: The fields the guard compares. Named here because a deferred load must
    #: not be triggered while capturing them — see ``from_db``.
    _DELIVERY_FIELDS = ("status", "delivery_mode", "subcontractor_id", "agreed_price")

    @classmethod
    def from_db(cls, db, field_names, values):  # type: ignore[no-untyped-def]
        instance = super().from_db(db, field_names, values)
        # Only when the row really carries these columns. A query that defers
        # them — Django's own delete collector fetches pk alone — would
        # otherwise have this attribute access fire a refresh, which calls
        # ``from_db`` again, forever.
        if set(cls._DELIVERY_FIELDS) <= set(field_names):
            instance._loaded_delivery = tuple(
                getattr(instance, name) for name in cls._DELIVERY_FIELDS
            )
        return instance

    def _check_delivery_is_still_changeable(self) -> None:
        """A closed job's delivery cost is settled (O3).

        Changing the mode or the price after closing would rewrite a cost the
        project has already counted — the same objection that makes the ledger
        append-only. Correcting a genuinely wrong price means reopening the job,
        which is recorded.

        Called from both ``clean`` and ``save``: ``Job`` does not run
        ``full_clean`` on save, and DRF does not call it either, so a guard that
        lived only in ``clean`` would not fire on the path that matters.
        """
        if self._loaded_delivery is None:
            return
        was_status, was_mode, was_subcontractor, was_price = self._loaded_delivery
        if was_status != JobStatus.CLOSED:
            return
        if (
            self.delivery_mode,
            self.subcontractor_id,
            self.agreed_price,
        ) != (was_mode, was_subcontractor, was_price):
            raise ValidationError(
                "This job is closed, and its delivery cost has already been "
                "counted against the project. Reopen it first (O3)."
            )

    def clean(self) -> None:
        super().clean()
        self._check_delivery_is_still_changeable()

    def save(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self._check_delivery_is_still_changeable()
        result = super().save(*args, **kwargs)
        self._loaded_delivery = tuple(
            getattr(self, name) for name in self._DELIVERY_FIELDS
        )
        return result


class CloseoutStatus(models.TextChoices):
    SUBMITTED = "SUBMITTED", "Submitted by the technician"
    # H3: the storekeeper confirms the returns against what was declared.
    CONFIRMED = "CONFIRMED", "Confirmed at the yard"


class JobCloseout(TenantModel, TimeStampedModel):
    """What a technician reports at the end of a job (H2).

    Q2 flags a real risk here: "does a technician close out a job, or does the
    storekeeper do it for them? If field staff will not use the app reliably,
    reconciliation quality collapses." The design's answer, adopted: the same
    endpoint, a different actor. ``submitted_by`` records who actually did it, so
    the answer is visible in the data rather than assumed.
    """

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="closeouts")
    submitted_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="closeouts_submitted"
    )
    # Set when a storekeeper closes out on a technician's behalf, so Q2 can be
    # answered from the data after a month of real use.
    on_behalf_of = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    status = models.CharField(
        max_length=20, choices=CloseoutStatus.choices, default=CloseoutStatus.SUBMITTED
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    confirmed_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    notes = models.TextField(blank=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"Closeout of {self.job} by {self.submitted_by}"


class CloseoutAction(models.TextChoices):
    """What happened to the material (H2).

    The split between the first two and the last two is the design decision in
    §4.9: installed and consumed material is gone and posts immediately;
    returning and recovered material has not arrived and only creates an
    expectation.
    """

    INSTALLED = "INSTALLED", "Installed at the site"
    CONSUMED = "CONSUMED", "Consumed on site"
    RETURNING = "RETURNING", "Coming back to the yard"
    RECOVERED = "RECOVERED", "Recovered from the site"


class JobCloseoutLine(TenantModel, TimeStampedModel):
    """One reported outcome (H2)."""

    closeout = models.ForeignKey(
        JobCloseout, on_delete=models.CASCADE, related_name="lines"
    )
    action = models.CharField(max_length=20, choices=CloseoutAction.choices)

    item_type = models.ForeignKey(
        "catalogue.ItemType", on_delete=models.PROTECT, related_name="closeout_lines"
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
    notes = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ("id",)
        indexes = [models.Index(fields=["organization", "action"])]

    def __str__(self) -> str:
        return f"{self.get_action_display()}: {self.quantity} {self.uom} {self.item_type}"

    @property
    def posts_immediately(self) -> bool:
        """§4.9: installed and consumed material has already gone."""
        return self.action in (CloseoutAction.INSTALLED, CloseoutAction.CONSUMED)

    def clean(self) -> None:
        super().clean()
        # Q5: "partial installation of a serialized unit is impossible, but
        # partial consumption of a drum on site is normal."
        if self.serial_unit_id and self.quantity != 1:
            raise ValidationError(
                {
                    "quantity": (
                        "A serialized unit is installed or returned whole — one "
                        "unit per line (Q5)."
                    )
                }
            )

    def save(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.full_clean(exclude=["organization"])
        return super().save(*args, **kwargs)


class VarianceType(models.TextChoices):
    """Where a discrepancy came from (§4.9, M1)."""

    RETURN = "RETURN", "Return differed from what was declared"
    RELEASE = "RELEASE", "Release differed from what was approved"
    COUNT = "COUNT", "Stock count found a difference"
    SYNC = "SYNC", "Offline document could not be posted"


class VarianceStatus(models.TextChoices):
    OPEN = "OPEN", "Open"
    INVESTIGATING = "INVESTIGATING", "Under investigation"
    RESOLVED = "RESOLVED", "Resolved"
    WRITTEN_OFF = "WRITTEN_OFF", "Written off"


class Variance(TenantModel, TimeStampedModel):
    """A difference between what was expected and what happened (H3, M1).

    H3: "a difference between declared and actual creates a variance requiring
    investigation and an approver's sign-off. **Variances appear on an exceptions
    report until resolved.**"

    So a variance is not a log line — it is a piece of open work. That is why it
    has a status and a sign-off rather than just a timestamp.
    """

    type = models.CharField(max_length=20, choices=VarianceType.choices, db_index=True)
    status = models.CharField(
        max_length=20, choices=VarianceStatus.choices, default=VarianceStatus.OPEN, db_index=True
    )

    job = models.ForeignKey(
        Job, on_delete=models.PROTECT, null=True, blank=True, related_name="variances"
    )
    closeout_line = models.ForeignKey(
        JobCloseoutLine,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="variances",
    )
    item_type = models.ForeignKey(
        "catalogue.ItemType", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    serial_unit = models.ForeignKey(
        "stock.SerialUnit", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    expected = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    actual = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    uom = models.CharField(max_length=20, blank=True)

    reason = models.CharField(max_length=500, blank=True)
    resolution = models.CharField(max_length=500, blank=True)

    raised_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    resolved_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    resolved_at = models.DateTimeField(null=True, blank=True)

    # H3: an approver signs it off. The request is created by the approval engine
    # so a variance follows the same routing as anything else needing authority.
    approval_request = models.ForeignKey(
        "approvals.ApprovalRequest",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="variances",
    )

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["organization", "status", "-created_at"]),
            models.Index(fields=["organization", "type", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_type_display()}: expected {self.expected}, got {self.actual}"

    @property
    def difference(self) -> Decimal:
        return self.actual - self.expected

    @property
    def is_open(self) -> bool:
        """What the exceptions register lists (M1)."""
        return self.status in (VarianceStatus.OPEN, VarianceStatus.INVESTIGATING)


class RateSource(models.TextChoices):
    """Where a labour entry's rate came from (O15).

    ``NONE`` is the one that matters. A person with no rate of their own and no
    rate on any role they hold cannot be costed, and the entry says so rather
    than carrying a zero — a job whose labour costs nothing reads as a job
    delivered for free, and would flatter its project exactly where §10 is
    trying to tell the truth.
    """

    USER = "USER", "The person's own rate"
    ROLE = "ROLE", "The rate on a role they hold"
    NONE = "NONE", "No rate applies — uncosted"


class JobLabour(TenantModel, TimeStampedModel):
    """A person's time on a job, as reported at closeout (O15, D26).

    Written from the closeout that already exists, because that is a form the
    technician already has to complete before the job can close. A separate
    timesheet would be a new habit, and Q2's doubt about closeout discipline
    applies with more force to a screen nobody has to open.

    The rate is **captured here** rather than looked up later (D27): changing
    somebody's rate next year must not rewrite what a closed project cost.
    """

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="labour")
    closeout = models.ForeignKey(
        "JobCloseout", on_delete=models.CASCADE, related_name="labour", null=True, blank=True
    )
    person = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="labour_entries"
    )

    work_date = models.DateField()
    days = models.DecimalField(
        max_digits=4,
        decimal_places=1,
        validators=[MinValueValidator(Decimal("0.1"))],
        help_text="Days worked, to one decimal place, so half days work.",
    )

    day_rate = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    rate_source = models.CharField(
        max_length=10, choices=RateSource.choices, default=RateSource.NONE
    )

    # O15: set when this person's total for the date passes one day across every
    # job. Stored rather than recomputed so the owner's report finds them with an
    # index instead of summing all of history.
    overlaps_day = models.BooleanField(default=False)

    class Meta:
        ordering = ("work_date", "id")
        indexes = [
            models.Index(fields=["organization", "person", "work_date"]),
            models.Index(fields=["organization", "overlaps_day"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "job", "person", "work_date"],
                name="one_labour_entry_per_person_per_day_per_job",
            ),
            models.CheckConstraint(
                condition=Q(day_rate__isnull=True, rate_source=RateSource.NONE)
                | Q(day_rate__isnull=False) & ~Q(rate_source=RateSource.NONE),
                name="a_costed_labour_entry_says_where_its_rate_came_from",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.person} — {self.days}d on {self.job} ({self.work_date})"

    @property
    def cost(self) -> Decimal | None:
        """What this entry costs the project, or None if it cannot be costed."""
        if self.day_rate is None:
            return None
        return (self.days * self.day_rate).quantize(Decimal("0.01"))
