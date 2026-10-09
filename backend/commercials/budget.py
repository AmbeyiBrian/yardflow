"""What a project may still spend (design §4.19.5; R9, D23).

Nothing here is stored. Every shilling is counted in **exactly one cell** of
the §4.19.5 table, which is the deliberate departure from R9's wording ("spent
= project cost plus paid entries"): a paid expense is already project cost, and
counting it again as "paid" would spend it twice.

==============  ===============================  ============================
Component       Spent                            Committed
==============  ===============================  ============================
COST            ``cost_for(project).total`` (material, loss, subcontract jobs,
                labour, expenses, USED_AT_SITE purchases)
ALLOWANCE       PAID                             APPROVED, unpaid
FLOAT           PAID, less what its costed       APPROVED, unpaid
                expenses and the returned money
                already cover
PURCHASE        (INTO_YARD only)                 APPROVED, gate-in not POSTED
SUBCONTRACT     advance paid over work done      contract value still to do
==============  ===============================  ============================

Entries awaiting approval are a third figure, ``pending``, outside both.
``remaining = budget - spent - committed``. A project with no budget (no PO,
R12) has no rules: ``budget`` and ``remaining`` are ``None`` and nothing is
ever "over".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import Q

from commercials.costing import ZERO, _money, cost_for


@dataclass(frozen=True)
class BudgetComponent:
    kind: str
    spent: Decimal = ZERO
    committed: Decimal = ZERO

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "spent": str(self.spent),
            "committed": str(self.committed),
        }


@dataclass(frozen=True)
class BudgetPosition:
    budget: Decimal | None
    components: tuple[BudgetComponent, ...] = ()
    pending: Decimal = ZERO
    over_budget_entries: list[dict] = field(default_factory=list)

    @property
    def spent(self) -> Decimal:
        return _money(sum((c.spent for c in self.components), ZERO))

    @property
    def committed(self) -> Decimal:
        return _money(sum((c.committed for c in self.components), ZERO))

    @property
    def remaining(self) -> Decimal | None:
        if self.budget is None:
            return None
        return _money(self.budget - self.spent - self.committed)

    @property
    def is_over_budget(self) -> bool:
        remaining = self.remaining
        return remaining is not None and remaining < ZERO

    def as_dict(self, *, with_entries: bool = True) -> dict:
        data: dict = {
            "budget": None if self.budget is None else str(self.budget),
            "spent": str(self.spent),
            "committed": str(self.committed),
            "pending": str(self.pending),
            "remaining": None if self.remaining is None else str(self.remaining),
            "components": [c.as_dict() for c in self.components],
        }
        if with_entries:
            data["over_budget_entries"] = self.over_budget_entries
        return data


def _signed(queryset) -> Decimal:  # type: ignore[no-untyped-def]
    return sum((row.signed_amount for row in queryset), ZERO)


def _allowance_components(project) -> tuple[BudgetComponent, BudgetComponent]:  # type: ignore[no-untyped-def]
    from commercials.models import (
        COSTED_STATUSES,
        AllowanceRequest,
        AllowanceType,
        ExpenseStatus,
        ProjectExpense,
    )

    allowance_spent = allowance_committed = ZERO
    float_spent = float_committed = ZERO
    live = AllowanceRequest.objects.filter(
        project=project, status__in=(ExpenseStatus.APPROVED, ExpenseStatus.PAID)
    )
    for request in live:
        is_float = request.type == AllowanceType.FLOAT
        if request.status == ExpenseStatus.APPROVED:
            if is_float:
                float_committed += request.amount
            else:
                allowance_committed += request.amount
        elif is_float:
            # Expenses on the float are already cost; only the part nobody has
            # spent or handed back is still money out of the company's hands.
            costed = _signed(
                ProjectExpense.objects.filter(
                    float_request=request, status__in=COSTED_STATUSES
                )
            )
            float_spent += max(
                request.amount - costed - (request.returned_amount or ZERO), ZERO
            )
        else:
            allowance_spent += request.amount
    return (
        BudgetComponent("ALLOWANCE", _money(allowance_spent), _money(allowance_committed)),
        BudgetComponent("FLOAT", _money(float_spent), _money(float_committed)),
    )


def _purchase_committed(project) -> Decimal:  # type: ignore[no-untyped-def]
    """INTO_YARD purchases approved and not yet received (§4.19.3)."""
    from commercials.models import COSTED_STATUSES, PurchaseDestination, SitePurchase
    from receiving.models import DocumentStatus

    unreceived = SitePurchase.objects.filter(
        project=project,
        destination=PurchaseDestination.INTO_YARD,
        status__in=COSTED_STATUSES,
    ).exclude(gate_in__status=DocumentStatus.POSTED)
    return _money(_signed(unreceived))


def subcontract_work_done(contract) -> Decimal:  # type: ignore[no-untyped-def]
    """Σ of the agreed prices of the contract's closed jobs (R8)."""
    from jobs.models import Job, JobStatus

    return sum(
        (
            job.agreed_price or ZERO
            for job in Job.objects.filter(subcontract=contract, status=JobStatus.CLOSED)
        ),
        ZERO,
    )


def _subcontract_component(project) -> BudgetComponent:  # type: ignore[no-untyped-def]
    from commercials.models import (
        ExpenseStatus,
        Subcontract,
        SubcontractPayment,
        SubcontractStatus,
    )
    spent = committed = ZERO
    for contract in Subcontract.objects.filter(project=project):
        work_done = subcontract_work_done(contract)
        paid = _signed(
            SubcontractPayment.objects.filter(
                subcontract=contract, status=ExpenseStatus.APPROVED
            )
        )
        spent += max(paid - work_done, ZERO)
        if contract.status == SubcontractStatus.ACTIVE:
            committed += max(contract.contract_value - max(work_done, paid), ZERO)
    return BudgetComponent("SUBCONTRACT", _money(spent), _money(committed))


def _pending_total(project) -> Decimal:  # type: ignore[no-untyped-def]
    """Awaiting approval: expenses (not float-backed), allowances, purchases."""
    from commercials.models import (
        AllowanceRequest,
        ExpenseStatus,
        ProjectExpense,
        SitePurchase,
    )

    waiting = (ExpenseStatus.PENDING_PM, ExpenseStatus.PENDING_FINANCE)
    total = ZERO
    for model, extra in (
        (ProjectExpense, Q(float_request__isnull=True)),
        (AllowanceRequest, Q()),
        (SitePurchase, Q()),
    ):
        for row in model.objects.filter(extra, project=project, status__in=waiting):
            total += row.amount
    return _money(total)


def over_budget_entries(project) -> list[dict]:  # type: ignore[no-untyped-def]
    """Entries recorded past the budget, with the reason their recorder gave."""
    from commercials.models import AllowanceRequest, ProjectExpense, SitePurchase

    rows: list[dict] = []
    for kind, model in (
        ("expense", ProjectExpense),
        ("allowance", AllowanceRequest),
        ("purchase", SitePurchase),
    ):
        for entry in model.objects.filter(
            project=project, over_budget_by__isnull=False
        ).order_by("-created_at"):
            rows.append(
                {
                    "document_type": kind,
                    "id": entry.pk,
                    "reference": getattr(entry, "number", "") or "",
                    "over_budget_by": str(entry.over_budget_by),
                    "over_budget_reason": entry.over_budget_reason,
                    "recorded_on": entry.created_at.isoformat(),
                }
            )
    return rows


def budget_position(project) -> BudgetPosition:  # type: ignore[no-untyped-def]
    """Budget, spent, committed and pending, each shilling counted once."""
    from network.models import ProjectStatus

    budget = project.current_cost_budget
    cost = BudgetComponent("COST", spent=cost_for(project).total)
    if project.status != ProjectStatus.OPEN:
        # A closed project reads its snapshot and commits nothing (O13).
        return BudgetPosition(
            budget=budget,
            components=(cost,),
            over_budget_entries=over_budget_entries(project),
        )

    allowance, float_ = _allowance_components(project)
    return BudgetPosition(
        budget=budget,
        components=(
            cost,
            allowance,
            float_,
            BudgetComponent("PURCHASE", committed=_purchase_committed(project)),
            _subcontract_component(project),
        ),
        pending=_pending_total(project),
        over_budget_entries=over_budget_entries(project),
    )


def would_exceed(project, amount: Decimal) -> Decimal | None:  # type: ignore[no-untyped-def]
    """By how much ``amount`` would take ``project`` past its budget, or ``None``.

    ``committed + spent + pending + amount > budget``; pending is included so two
    entries recorded together cannot each slip under. A project with no budget
    has no rules (R12). Exactly reaching the budget is not over it.
    """
    position = budget_position(project)
    if position.budget is None:
        return None
    over_by = _money(
        position.spent + position.committed + position.pending + amount - position.budget
    )
    return over_by if over_by > ZERO else None


#: The design's names (§4.19.5).
position = budget_position
check = would_exceed
