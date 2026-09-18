"""What a project cost, derived from the ledger (design §4.14; O11, D23).

D23: **cost is computed, never stored as an editable figure.** There is no
screen in this system where somebody types what a project cost, and this module
is why. Every figure below is a query over records that exist for their own
reasons — the ledger, the closeouts, the custody expectations — so the yard's
account of the material and the commercial account of the money cannot drift
apart.

The four cost lines (D24):

==================  ==========================================================
Material            ``INSTALL`` and ``CONSUME`` movements on the project's jobs
Material loss       expectations still open on **closed** jobs
Subcontractor       the agreed price on closed subcontracted jobs
Labour              days recorded at closeout, at the rate captured then
Expenses            approved expenses, net of reversals
==================  ==========================================================

And one figure that is deliberately **not** cost: **exposure**, the material
issued to the project and not yet accounted for. It becomes cost when a closeout
says what happened to it, and reporting it as cost in the meantime would make a
project's margin swing every time a van was loaded.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from django.db.models import DecimalField, ExpressionWrapper, F, Q, Sum

if TYPE_CHECKING:  # pragma: no cover - typing only
    from network.models import Project

ZERO = Decimal("0.00")


def _money(value) -> Decimal:
    return (value or ZERO).quantize(Decimal("0.01"))


@dataclass(frozen=True)
class ProjectCost:
    """One project's figures. Every field is derived; none is stored."""

    material: Decimal = ZERO
    material_loss: Decimal = ZERO
    subcontractor: Decimal = ZERO
    labour: Decimal = ZERO
    expenses: Decimal = ZERO

    #: Issued and not yet accounted for. **Not** part of ``total``.
    exposure: Decimal = ZERO

    #: O11: movements and labour entries carrying no valuation at all. A project
    #: with any of these is reported as *partly unvalued* rather than as cheap.
    unvalued_movements: int = 0
    uncosted_labour_entries: int = 0
    jobs_closed_without_labour: int = 0

    jobs_total: int = 0
    jobs_closed: int = 0

    @property
    def total(self) -> Decimal:
        return _money(
            self.material
            + self.material_loss
            + self.subcontractor
            + self.labour
            + self.expenses
        )

    @property
    def is_fully_valued(self) -> bool:
        """Whether every input to this figure carried a valuation."""
        return (
            self.unvalued_movements == 0
            and self.uncosted_labour_entries == 0
            and self.jobs_closed_without_labour == 0
        )

    def as_dict(self) -> dict:
        return {
            "material": str(self.material),
            "material_loss": str(self.material_loss),
            "subcontractor": str(self.subcontractor),
            "labour": str(self.labour),
            "expenses": str(self.expenses),
            "total": str(self.total),
            "exposure": str(self.exposure),
            "is_fully_valued": self.is_fully_valued,
            "unvalued_movements": self.unvalued_movements,
            "uncosted_labour_entries": self.uncosted_labour_entries,
            "jobs_closed_without_labour": self.jobs_closed_without_labour,
            "jobs_total": self.jobs_total,
            "jobs_closed": self.jobs_closed,
        }


def _valued_sum(queryset, quantity_field: str, rate_field: str) -> Decimal:
    """Sum ``quantity × rate`` over rows that carry a rate."""
    expression = ExpressionWrapper(
        F(quantity_field) * F(rate_field),
        output_field=DecimalField(max_digits=20, decimal_places=4),
    )
    total = queryset.aggregate(total=Sum(expression)).get("total")
    return _money(total)


def material_cost(project) -> tuple[Decimal, int]:
    """Material the closeouts confirmed as installed or consumed (O11).

    Valued at the cost captured **onto each movement** when it posted (D27), so
    repricing an item type cannot move a closed project's figure.

    Client-owned material contributes nothing here. It costs us nothing while it
    behaves; a shortfall reaches the P&L through ``material_loss`` instead.
    """
    from stock.models import MovementType, OwnerType, StockMovement, UnitCostSource

    movements = StockMovement.objects.filter(
        movement_type__in=(MovementType.INSTALL, MovementType.CONSUME),
        owner_type=OwnerType.OWN,
        document_type="jobs.JobCloseout",
        document_id__in=_closeout_ids(project),
    )
    unvalued = movements.filter(unit_cost_source=UnitCostSource.NONE).count()
    return _valued_sum(movements, "quantity", "unit_cost"), unvalued


def _closeout_ids(project) -> list[str]:
    from jobs.models import JobCloseout

    return [
        str(pk)
        for pk in JobCloseout.objects.filter(job__project=project).values_list(
            "pk", flat=True
        )
    ]


def material_loss(project) -> tuple[Decimal, int]:
    """Material issued to the project and never accounted for (O11).

    Derived from expectations **still open on closed jobs** rather than posted
    as a write-off, and that is worth stating plainly: if the kit turns up two
    months later and is booked in, the expectation closes and this figure drops
    on its own. A typed write-off would have to be found and reversed by hand,
    and in practice would not be.

    ``WRITTEN_OFF`` counts too — that is the same loss, formally admitted.
    """
    from custody.models import CustodyExpectation, ExpectationStatus
    from jobs.models import JobStatus

    outstanding = CustodyExpectation.objects.filter(
        Q(status__in=(ExpectationStatus.OPEN, ExpectationStatus.OVERDUE))
        & Q(gate_out_line__gate_out__job__status=JobStatus.CLOSED)
        | Q(status=ExpectationStatus.WRITTEN_OFF),
        gate_out_line__gate_out__job__project=project,
    ).annotate(
        outstanding_qty=ExpressionWrapper(
            F("quantity") - F("returned_quantity"),
            output_field=DecimalField(max_digits=20, decimal_places=3),
        )
    )

    total = ZERO
    unvalued = 0
    for expectation in outstanding.select_related("item_type"):
        quantity = expectation.outstanding_qty or ZERO
        if quantity <= 0:
            continue
        unit_cost = _valuation_for(expectation)
        if unit_cost is None:
            unvalued += 1
            continue
        total += quantity * unit_cost
    return _money(total), unvalued


def _valuation_for(expectation) -> Decimal | None:
    """The captured cost of the material behind an expectation.

    Read from the ISSUE movement that put it in somebody's hands, so the figure
    is the one that applied on the day — the same rule everything else follows
    (D27). Falls back to nothing rather than to today's price.
    """
    from stock.models import StockMovement, UnitCostSource

    movement = (
        StockMovement.objects.filter(
            document_type="dispatch.GateOut",
            document_line_id=str(expectation.gate_out_line_id),
        )
        .exclude(unit_cost_source=UnitCostSource.NONE)
        .order_by("id")
        .first()
    )
    return movement.unit_cost if movement is not None else None


def subcontractor_cost(project) -> Decimal:
    """The agreed price on subcontracted jobs that have closed (O11, O3).

    On closing, not on award: a PO a third delivered should show a third of its
    subcontract cost, or mid-project margin means nothing.
    """
    from jobs.models import DeliveryMode, Job, JobStatus

    total = (
        Job.objects.filter(
            project=project,
            delivery_mode=DeliveryMode.SUBCONTRACTED,
            status=JobStatus.CLOSED,
        )
        .aggregate(total=Sum("agreed_price"))
        .get("total")
    )
    return _money(total)


def labour_cost(project) -> tuple[Decimal, int, int]:
    """Days recorded at closeout, at the rate captured then (O15, D27).

    Returns the cost, the number of entries that could not be costed, and the
    number of **closed jobs with no labour at all** — a job delivered in-house
    for nothing is the flattery O12 exists to catch.
    """
    from jobs.models import Job, JobLabour, JobStatus, RateSource

    entries = JobLabour.objects.filter(job__project=project)
    costed = entries.exclude(rate_source=RateSource.NONE)
    uncosted = entries.filter(rate_source=RateSource.NONE).count()

    silent = (
        Job.objects.filter(
            project=project,
            status=JobStatus.CLOSED,
            delivery_mode__in=["IN_HOUSE"],
        )
        .exclude(labour__isnull=False)
        .count()
    )
    return _valued_sum(costed, "days", "day_rate"), uncosted, silent


def expense_cost(project) -> Decimal:
    """Approved expenses, net of reversals (O16)."""
    from commercials.models import ExpenseStatus, ProjectExpense

    approved = ProjectExpense.objects.filter(
        project=project, status=ExpenseStatus.APPROVED
    )
    total = sum((expense.signed_amount for expense in approved), ZERO)
    return _money(total)


def exposure(project) -> Decimal:
    """Material issued to the project and not yet accounted for (O11).

    Reported, never counted as cost. It becomes cost when a closeout says what
    happened to it — counting it sooner would swing a project's margin every
    time a van was loaded and unloaded.
    """
    from custody.models import CustodyExpectation, ExpectationStatus
    from jobs.models import JobStatus

    open_expectations = CustodyExpectation.objects.filter(
        status__in=(ExpectationStatus.OPEN, ExpectationStatus.OVERDUE),
        gate_out_line__gate_out__job__project=project,
    ).exclude(gate_out_line__gate_out__job__status=JobStatus.CLOSED)

    total = ZERO
    for expectation in open_expectations.select_related("item_type"):
        quantity = (expectation.quantity or ZERO) - (
            expectation.returned_quantity or ZERO
        )
        if quantity <= 0:
            continue
        unit_cost = _valuation_for(expectation)
        if unit_cost is not None:
            total += quantity * unit_cost
    return _money(total)


def cost_for(project) -> ProjectCost:
    """Everything a project has cost, and what is still out (O11).

    Reads the snapshot for a closed project instead of recomputing, so a
    reversal posted next month cannot move figures somebody has already signed
    off (O13).
    """
    from commercials.models import ProjectSnapshot
    from jobs.models import Job, JobStatus
    from network.models import ProjectStatus

    if project.status != ProjectStatus.OPEN:
        snapshot = (
            ProjectSnapshot.objects.filter(project=project).order_by("-taken_at").first()
        )
        if snapshot is not None:
            return from_snapshot(snapshot)

    material, unvalued_movements = material_cost(project)
    loss, unvalued_expectations = material_loss(project)
    labour, uncosted_labour, silent_jobs = labour_cost(project)

    jobs = Job.objects.filter(project=project)
    return ProjectCost(
        material=material,
        material_loss=loss,
        subcontractor=subcontractor_cost(project),
        labour=labour,
        expenses=expense_cost(project),
        exposure=exposure(project),
        unvalued_movements=unvalued_movements + unvalued_expectations,
        uncosted_labour_entries=uncosted_labour,
        jobs_closed_without_labour=silent_jobs,
        jobs_total=jobs.count(),
        jobs_closed=jobs.filter(status=JobStatus.CLOSED).count(),
    )


def from_snapshot(snapshot) -> ProjectCost:
    """Rebuild the figures a project reported when it closed (O13)."""
    figures = snapshot.figures or {}
    return ProjectCost(
        material=Decimal(figures.get("material", "0.00")),
        material_loss=Decimal(figures.get("material_loss", "0.00")),
        subcontractor=Decimal(figures.get("subcontractor", "0.00")),
        labour=Decimal(figures.get("labour", "0.00")),
        expenses=Decimal(figures.get("expenses", "0.00")),
        exposure=Decimal(figures.get("exposure", "0.00")),
        unvalued_movements=figures.get("unvalued_movements", 0),
        uncosted_labour_entries=figures.get("uncosted_labour_entries", 0),
        jobs_closed_without_labour=figures.get("jobs_closed_without_labour", 0),
        jobs_total=figures.get("jobs_total", 0),
        jobs_closed=figures.get("jobs_closed", 0),
    )


@dataclass(frozen=True)
class ProjectPerformance:
    """Cost against what the project is worth (O12)."""

    project: Project
    cost: ProjectCost
    contract_value: Decimal | None = None
    cost_budget: Decimal | None = None

    @property
    def margin(self) -> Decimal | None:
        if self.contract_value is None:
            return None
        return _money(self.contract_value - self.cost.total)

    @property
    def margin_percent(self) -> Decimal | None:
        if not self.contract_value:
            return None
        margin = self.margin
        return None if margin is None else _money(
            margin / self.contract_value * Decimal("100")
        )

    @property
    def budget_variance(self) -> Decimal | None:
        """Positive means under budget."""
        if self.cost_budget is None:
            return None
        return _money(self.cost_budget - self.cost.total)

    @property
    def is_over_budget(self) -> bool:
        variance = self.budget_variance
        return variance is not None and variance < ZERO

    @property
    def progress_percent(self) -> Decimal | None:
        if not self.cost.jobs_total:
            return None
        return _money(
            Decimal(self.cost.jobs_closed)
            / Decimal(self.cost.jobs_total)
            * Decimal("100")
        )


def performance_for(project) -> ProjectPerformance:
    """A project's cost against its current value and budget (O12, O2)."""
    return ProjectPerformance(
        project=project,
        cost=cost_for(project),
        contract_value=project.current_contract_value,
        cost_budget=project.current_cost_budget,
    )
