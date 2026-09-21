"""Where the money went, to whom, and when (design §10; O4, O12, O16).

The four reports in ``commercials.reports`` say *what* a purchase order cost and
whether to believe the figure. These three answer the questions that follow the
moment a margin looks wrong:

* **Expenses ledger** — the expense figure opened up, one line per claim, with
  what happened to it. The only place a *rejected* expense is visible at all.
* **Subcontractor spend** — cost rolled up by party, which is the whole reason
  the subcontractor register exists (O4).
* **Budget variance by month** — cost against budget as it accumulated, so
  "went over in month two of nine" and "went over in month nine" stop looking
  identical.

Each cost line is placed in the month it became a cost *under the costing
rules* — an expense when incurred, subcontract and labour when the job closed,
material when the movement posted — so the months add up to the performance
figure and not to something else. Every one is gated on ``project.view_cost``
for the reason given in ``commercials.reports``.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from accounts.permissions_registry import PERM
from commercials.reports import (
    MANAGER_FILTER,
    PROJECT_FILTER,
    STATUS_FILTER,
    _projects,
)
from reporting.framework import (
    Filter,
    Report,
    flag,
    integer,
    money,
    on_date,
    register,
    text,
)

ZERO = Decimal("0")

EXPENSE_STATUS_FILTER = Filter(
    "status",
    "Status",
    kind="choice",
    choices=(
        ("SUBMITTED", "Awaiting decision"),
        ("APPROVED", "Approved"),
        ("REJECTED", "Rejected"),
    ),
)
FROM_FILTER = Filter("date_from", "From", kind="date")
TO_FILTER = Filter("date_to", "To", kind="date")


def _bounded(queryset, field: str, params: dict):
    """Apply the optional from/to window on ``field``."""
    if params.get("date_from"):
        queryset = queryset.filter(**{f"{field}__gte": params["date_from"]})
    if params.get("date_to"):
        queryset = queryset.filter(**{f"{field}__lte": params["date_to"]})
    return queryset


@register
class ExpensesLedgerReport(Report):
    """O16: every expense, one row each, with what happened to it."""

    slug = "expenses-ledger"
    title = "Expenses ledger"
    category = "Finance"
    description = (
        "Every recorded expense, line by line: project, job, category, amount, "
        "who recorded it and whether it was approved. Approved lines are what "
        "the performance report counts; the rest are here so a refusal is "
        "visible rather than silent."
    )
    requirement = "O16"
    required_permission = PERM.PROJECT_VIEW_COST
    can_be_large = True

    filters = (
        PROJECT_FILTER,
        Filter("category", "Category", kind="reference", resource="expense-categories"),
        EXPENSE_STATUS_FILTER,
        FROM_FILTER,
        TO_FILTER,
    )

    columns = (
        on_date("incurred_on", "Date"),
        text("project_reference", "PO", width=16),
        text("job_reference", "Job", width=14, wide_only=True),
        text("category_name", "Category", width=18),
        text("description", "What for", width=32, wide_only=True),
        money("amount", "Amount"),
        text("status", "Status", width=30),
        text("recorded_by", "Recorded by", width=18, wide_only=True),
        text("decided_by", "Decided by", width=18, wide_only=True),
    )

    def query(self, params: dict):
        from commercials.models import ProjectExpense

        queryset = ProjectExpense.objects.select_related(
            "project", "job", "category", "recorded_by", "decided_by"
        )
        if params.get("project"):
            queryset = queryset.filter(project_id=params["project"])
        if params.get("category"):
            queryset = queryset.filter(category_id=params["category"])
        if params.get("status"):
            queryset = queryset.filter(status=params["status"])
        return _bounded(queryset, "incurred_on", params).order_by("-incurred_on", "-id")

    def rows(self, params: dict):
        for expense in self.query(params):
            job = expense.job
            yield {
                "incurred_on": expense.incurred_on,
                "project_reference": str(expense.project),
                "job_reference": job.reference if job is not None else "",
                "category_name": expense.category.name,
                "description": expense.description,
                "amount": expense.amount,
                "status": expense.get_status_display(),
                # The raw code travels with the row so the total can be decided
                # on it. The display label is the model's wording and may change;
                # a total that matched on wording would silently go to zero.
                "status_code": expense.status,
                "recorded_by": expense.recorded_by.full_name if expense.recorded_by else "",
                "decided_by": expense.decided_by.full_name if expense.decided_by else "",
            }

    def totals(self, rows):
        # Only what counts. Summing a rejected line into the total would put a
        # figure on the page that the performance report contradicts.
        return {
            "amount": sum(
                (row["amount"] for row in rows if row["status_code"] == "APPROVED"), ZERO
            )
        }


@register
class SubcontractorSpendReport(Report):
    """O4: what each contractor has delivered, and what is still committed."""

    slug = "subcontractor-spend"
    title = "Subcontractor spend"
    category = "Finance"
    description = (
        "Per subcontractor: jobs awarded, jobs delivered, the agreed price on "
        "delivered work (which is what the project cost counts) and on work "
        "still open (which is money already committed)."
    )
    requirement = "O4"
    required_permission = PERM.PROJECT_VIEW_COST
    can_be_large = False

    filters = (
        Filter("subcontractor", "Subcontractor", kind="reference", resource="subcontractors"),
        PROJECT_FILTER,
        Filter(
            "register",
            "Register",
            kind="choice",
            choices=(("active", "Active only"), ("all", "Including deactivated")),
        ),
    )

    columns = (
        text("name", "Subcontractor", width=24),
        text("code", "Code", width=10, wide_only=True),
        integer("projects", "POs"),
        integer("jobs_total", "Jobs"),
        integer("jobs_closed", "Delivered"),
        money("delivered", "Delivered (counted)"),
        money("committed", "Open (committed)"),
        flag("is_active", "Active", wide_only=True),
    )

    def rows(self, params: dict):
        from django.db.models import Count, Q, Sum

        from jobs.models import DeliveryMode, JobStatus
        from network.models import Subcontractor

        contractors = Subcontractor.objects.all()
        if params.get("subcontractor"):
            contractors = contractors.filter(pk=params["subcontractor"])
        if params.get("register", "active") == "active":
            contractors = contractors.filter(is_active=True)

        awarded = Q(jobs__delivery_mode=DeliveryMode.SUBCONTRACTED)
        if params.get("project"):
            awarded &= Q(jobs__project_id=params["project"])
        # Delivered follows the costing rule: closed jobs only (O11, O3).
        delivered = awarded & Q(jobs__status=JobStatus.CLOSED)
        still_open = awarded & ~Q(jobs__status__in=(JobStatus.CLOSED, JobStatus.CANCELLED))

        contractors = contractors.annotate(
            projects=Count("jobs__project", filter=awarded, distinct=True),
            jobs_total=Count("jobs", filter=awarded, distinct=True),
            jobs_closed=Count("jobs", filter=delivered, distinct=True),
            delivered=Sum("jobs__agreed_price", filter=delivered),
            committed=Sum("jobs__agreed_price", filter=still_open),
        ).order_by("-delivered", "name")

        for contractor in contractors:
            yield {
                "name": contractor.name,
                "code": contractor.code,
                "projects": contractor.projects,
                "jobs_total": contractor.jobs_total,
                "jobs_closed": contractor.jobs_closed,
                "delivered": contractor.delivered or ZERO,
                "committed": contractor.committed or ZERO,
                "is_active": contractor.is_active,
            }

    def totals(self, rows):
        return {
            "jobs_total": sum(row["jobs_total"] for row in rows),
            "jobs_closed": sum(row["jobs_closed"] for row in rows),
            "delivered": sum((row["delivered"] for row in rows), ZERO),
            "committed": sum((row["committed"] for row in rows), ZERO),
        }


@register
class BudgetVarianceByMonthReport(Report):
    """O12: cost against budget as it accumulated, month by month."""

    slug = "budget-by-month"
    title = "Budget variance by month"
    category = "Finance"
    description = (
        "For each purchase order, cost by month and the running total against "
        "the cost budget — when a project went over, not just that it did. The "
        "months add up to the performance report's cost to date."
    )
    requirement = "O12"
    required_permission = PERM.PROJECT_VIEW_COST
    can_be_large = False

    filters = (PROJECT_FILTER, MANAGER_FILTER, STATUS_FILTER, FROM_FILTER, TO_FILTER)

    columns = (
        text("reference", "PO", width=16),
        text("month", "Month", width=10),
        money("material", "Material", wide_only=True),
        money("subcontractor", "Subcontract", wide_only=True),
        money("labour", "Labour", wide_only=True),
        money("expenses", "Expenses", wide_only=True),
        money("month_cost", "Cost this month"),
        money("cumulative", "Cost to date"),
        money("budget", "Budget", wide_only=True),
        money("remaining", "Remaining"),
        flag("is_over_budget", "Over budget"),
    )

    def rows(self, params: dict):
        from django.db.models import Sum
        from django.db.models.functions import TruncMonth

        from commercials.costing import _closeout_ids, _valued_sum
        from commercials.models import ExpenseStatus, ProjectExpense
        from jobs.models import DeliveryMode, Job, JobLabour, JobStatus, RateSource
        from stock.models import MovementType, OwnerType, StockMovement

        window_from = params.get("date_from")
        window_to = params.get("date_to")

        for project in _projects(params):
            months: dict = defaultdict(
                lambda: {
                    "material": ZERO,
                    "subcontractor": ZERO,
                    "labour": ZERO,
                    "expenses": ZERO,
                }
            )

            # Expenses land in the month they were incurred (O16).
            for row in (
                ProjectExpense.objects.filter(project=project, status=ExpenseStatus.APPROVED)
                .annotate(month=TruncMonth("incurred_on"))
                .values("month")
                .annotate(total=Sum("amount"))
            ):
                months[_month_of(row["month"])]["expenses"] += row["total"] or ZERO

            # Subcontract lands when the job closes (O3, O11).
            for row in (
                Job.objects.filter(
                    project=project,
                    delivery_mode=DeliveryMode.SUBCONTRACTED,
                    status=JobStatus.CLOSED,
                    closed_at__isnull=False,
                )
                .annotate(month=TruncMonth("closed_at"))
                .values("month")
                .annotate(total=Sum("agreed_price"))
            ):
                months[_month_of(row["month"])]["subcontractor"] += row["total"] or ZERO

            # Labour lands when its job closes, at the rate captured then (O15).
            for job in Job.objects.filter(
                project=project, status=JobStatus.CLOSED, closed_at__isnull=False
            ).only("id", "closed_at"):
                entries = JobLabour.objects.filter(job=job).exclude(rate_source=RateSource.NONE)
                cost = _valued_sum(entries, "days", "day_rate")
                if cost:
                    months[_month_of(job.closed_at)]["labour"] += cost

            # Material lands when the movement posts, valued as it posted (D27).
            movements = StockMovement.objects.filter(
                movement_type__in=(MovementType.INSTALL, MovementType.CONSUME),
                owner_type=OwnerType.OWN,
                document_type="jobs.JobCloseout",
                document_id__in=_closeout_ids(project),
            )
            posted_months = movements.annotate(month=TruncMonth("occurred_at")).values("month")
            for row in posted_months.distinct():
                start = _month_of(row["month"])
                in_month = movements.filter(
                    occurred_at__year=start.year, occurred_at__month=start.month
                )
                cost = _valued_sum(in_month, "quantity", "unit_cost")
                if cost:
                    months[start]["material"] += cost

            budget = project.cost_budget
            cumulative = ZERO
            for start in sorted(months):
                line = months[start]
                month_cost = sum(line.values(), ZERO)
                cumulative += month_cost
                # A window hides rows; it does not restart the running total.
                # The cost to date before the window is part of the answer.
                if window_from and start < window_from.replace(day=1):
                    continue
                if window_to and start > window_to:
                    break
                remaining = (budget - cumulative) if budget is not None else None
                yield {
                    "reference": str(project),
                    "month": start.strftime("%Y-%m"),
                    "material": line["material"],
                    "subcontractor": line["subcontractor"],
                    "labour": line["labour"],
                    "expenses": line["expenses"],
                    "month_cost": month_cost,
                    "cumulative": cumulative,
                    "budget": budget,
                    "remaining": remaining,
                    "is_over_budget": remaining is not None and remaining < 0,
                }


def _month_of(value):
    """The first day of ``value``'s month, whatever ``TruncMonth`` handed back."""
    if hasattr(value, "date"):
        value = value.date()
    return value.replace(day=1)
