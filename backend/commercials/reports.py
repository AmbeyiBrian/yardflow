"""Project performance reports (design §10; O12, O15, O6).

Four reports, and two of them exist to keep the other two honest. Project
performance and the ranking answer *did this PO make money*. The other two —
self-approved releases, and labour that was never costed — answer *should you
believe that figure*, which for a number this new is the better question.

Every one of them is gated on ``project.view_cost`` rather than
``report.view_all``: a report is just another way to read the same figures, and
it would be a poor place to lose the restriction §10 puts everywhere else.
"""

from __future__ import annotations

from decimal import Decimal

from accounts.permissions_registry import PERM
from reporting.framework import (
    Column,
    Filter,
    Report,
    flag,
    integer,
    money,
    register,
    text,
    when,
)

PROJECT_FILTER = Filter("project", "Project", kind="reference", resource="projects")
MANAGER_FILTER = Filter("manager", "Manager", kind="reference", resource="users")
STATUS_FILTER = Filter(
    "status",
    "Status",
    kind="choice",
    choices=(("OPEN", "Open"), ("CLOSED", "Closed"), ("CANCELLED", "Cancelled")),
)


def _projects(params: dict):
    """The projects a report covers: those carrying a PO (O1)."""
    from network.models import Project

    queryset = Project.objects.exclude(po_number="").select_related(
        "client", "manager"
    )
    if params.get("project"):
        queryset = queryset.filter(pk=params["project"])
    if params.get("manager"):
        queryset = queryset.filter(manager_id=params["manager"])
    if params.get("status"):
        queryset = queryset.filter(status=params["status"])
    if params.get("client"):
        queryset = queryset.filter(client_id=params["client"])
    return queryset


@register
class ProjectPerformanceReport(Report):
    """O12: one row per PO — what it is worth, what it has cost, what is left."""

    slug = "project-performance"
    category = "Finance"
    title = "Project performance"
    description = (
        "Cost against contract value for every purchase order, split by cost "
        "line. Margin is stated before overheads — the four lines here are the "
        "whole of it (O11)."
    )
    requirement = "O12"
    required_permission = PERM.PROJECT_VIEW_COST
    can_be_large = False

    filters = (
        PROJECT_FILTER,
        MANAGER_FILTER,
        STATUS_FILTER,
        Filter("client", "Client", kind="reference", resource="clients"),
    )

    columns = (
        text("reference", "PO", width=16),
        text("client_name", "Client", width=22),
        text("manager_name", "Manager", width=20, wide_only=True),
        text("status", "Status", width=12),
        money("contract_value", "Value"),
        money("material", "Material", wide_only=True),
        money("material_loss", "Loss", wide_only=True),
        money("subcontractor", "Subcontract", wide_only=True),
        money("labour", "Labour", wide_only=True),
        money("expenses", "Expenses", wide_only=True),
        money("cost_to_date", "Cost"),
        money("margin", "Margin"),
        money("exposure", "Exposure", wide_only=True),
        money("budget_variance", "Vs budget", wide_only=True),
        flag("is_over_budget", "Over budget"),
        flag("is_fully_valued", "Fully valued"),
        integer("jobs_closed", "Jobs done", wide_only=True),
        integer("jobs_total", "Jobs", wide_only=True),
    )

    def rows(self, params: dict):
        from commercials.costing import performance_for

        for project in _projects(params):
            result = performance_for(project)
            manager = project.manager
            yield {
                "reference": str(project),
                "client_name": project.client.name,
                "manager_name": manager.full_name if manager is not None else "",
                "status": project.get_status_display(),
                "contract_value": result.contract_value,
                "material": result.cost.material,
                "material_loss": result.cost.material_loss,
                "subcontractor": result.cost.subcontractor,
                "labour": result.cost.labour,
                "expenses": result.cost.expenses,
                "cost_to_date": result.cost.total,
                "margin": result.margin,
                "exposure": result.cost.exposure,
                "budget_variance": result.budget_variance,
                "is_over_budget": result.is_over_budget,
                "is_fully_valued": result.cost.is_fully_valued,
                "jobs_closed": result.cost.jobs_closed,
                "jobs_total": result.cost.jobs_total,
            }


@register
class ProjectsRankedReport(Report):
    """O12: the same figures, worst first, so attention goes somewhere useful."""

    slug = "projects-ranked"
    category = "Finance"
    title = "Projects by margin"
    description = (
        "Purchase orders ordered by margin, then by overrun and exposure. A "
        "project whose figures are not fully valued is marked, because ranking "
        "an incomplete cost against a complete one would mislead."
    )
    requirement = "O12"
    required_permission = PERM.PROJECT_VIEW_COST
    can_be_large = False

    filters = (MANAGER_FILTER, STATUS_FILTER)

    columns = (
        text("reference", "PO", width=16),
        text("manager_name", "Manager", width=20),
        money("contract_value", "Value"),
        money("cost_to_date", "Cost"),
        money("margin", "Margin"),
        text("margin_percent", "Margin %", width=10),
        money("exposure", "Exposure", wide_only=True),
        flag("is_over_budget", "Over budget"),
        flag("is_fully_valued", "Fully valued"),
    )

    def rows(self, params: dict):
        from commercials.costing import performance_for

        results = [performance_for(project) for project in _projects(params)]
        # Nulls last: an unvalued project has no margin to rank, and sorting it
        # to the top as if it were the worst would be a fiction.
        results.sort(
            key=lambda result: (
                result.margin is None,
                result.margin if result.margin is not None else Decimal("0"),
            )
        )
        for result in results:
            manager = result.project.manager
            yield {
                "reference": str(result.project),
                "manager_name": manager.full_name if manager is not None else "",
                "contract_value": result.contract_value,
                "cost_to_date": result.cost.total,
                "margin": result.margin,
                "margin_percent": (
                    f"{result.margin_percent}%"
                    if result.margin_percent is not None
                    else ""
                ),
                "exposure": result.cost.exposure,
                "is_over_budget": result.is_over_budget,
                "is_fully_valued": result.cost.is_fully_valued,
            }


@register
class SelfApprovedReleasesReport(Report):
    """O6, R2: every release a manager approved for themselves.

    With criticality routing off for project material (D22) and self-approval
    permitted (O6), this list is the only thing standing between one signature
    and nobody noticing. It exists to be read.
    """

    slug = "self-approved-releases"
    category = "Finance"
    title = "Self-approved project releases"
    description = (
        "Gate passes raised and approved by the same person on a project they "
        "manage. Permitted (O6), and recorded rather than hidden."
    )
    requirement = "O6"
    required_permission = PERM.PROJECT_VIEW_COST
    can_be_large = False

    filters = (MANAGER_FILTER, PROJECT_FILTER)

    columns = (
        text("number", "Gate pass", width=16),
        when("decided_at", "Approved"),
        text("actor_name", "Approved by", width=22),
        text("project_reference", "Project", width=16),
        text("destination", "Destination", width=24, wide_only=True),
    )

    def rows(self, params: dict):
        from approvals.models import ApprovalAction
        from dispatch.models import GateOut

        actions = (
            ApprovalAction.objects.filter(
                self_approved=True,
                approval_request__document_type="dispatch.GateOut",
            )
            .select_related("actor", "approval_request")
            .order_by("-decided_at")
        )
        if params.get("manager"):
            actions = actions.filter(actor_id=params["manager"])

        document_ids = [action.approval_request.document_id for action in actions]
        passes = {
            str(gate_out.pk): gate_out
            for gate_out in GateOut.objects.filter(pk__in=document_ids).select_related(
                "project", "job", "job__project", "site"
            )
        }

        for action in actions:
            gate_out = passes.get(action.approval_request.document_id)
            if gate_out is None:
                continue
            project = gate_out.project_attribution
            if params.get("project") and (
                project is None or str(project.pk) != str(params["project"])
            ):
                continue
            yield {
                "number": gate_out.number or "—",
                "decided_at": action.decided_at,
                "actor_name": action.actor.full_name if action.actor_id else "",
                "project_reference": str(project) if project else "",
                "destination": gate_out.destination_label,
            }


@register
class LabourGapsReport(Report):
    """O15, R1: the labour figures nobody should take at face value.

    R1 says plainly that labour cost rests on a field somebody fills in, and
    that a storekeeper may fill it in on a technician's behalf (D31). None of
    that can be fixed in code. What this does is name every place the figure is
    missing or improbable, so a margin is not read as solid when it is not.
    """

    slug = "labour-gaps"
    category = "Finance"
    title = "Uncosted and overlapping labour"
    description = (
        "Closed jobs with no days recorded, entries with no rate to cost them "
        "at, and people recorded for more than a day on one date."
    )
    requirement = "O15"
    required_permission = PERM.PROJECT_VIEW_COST
    can_be_large = False

    filters = (PROJECT_FILTER,)

    columns = (
        text("finding", "Finding", width=26),
        text("project_reference", "Project", width=16),
        text("job_reference", "Job", width=16),
        text("person_name", "Person", width=22),
        Column("work_date", "Date", kind="date", width=12),
        text("days", "Days", width=8),
    )

    def rows(self, params: dict):
        from jobs.models import DeliveryMode, Job, JobLabour, JobStatus, RateSource

        project_id = params.get("project")

        silent = Job.objects.filter(
            status=JobStatus.CLOSED,
            delivery_mode=DeliveryMode.IN_HOUSE,
            project__isnull=False,
        ).exclude(labour__isnull=False)
        if project_id:
            silent = silent.filter(project_id=project_id)
        for job in silent.select_related("project"):
            yield {
                "finding": "Closed with no days recorded",
                "project_reference": str(job.project),
                "job_reference": job.reference,
                "person_name": "",
                "work_date": job.closed_at,
                "days": "",
            }

        entries = JobLabour.objects.filter(job__project__isnull=False).select_related(
            "job", "job__project", "person"
        )
        if project_id:
            entries = entries.filter(job__project_id=project_id)

        for entry in entries.filter(rate_source=RateSource.NONE):
            yield {
                "finding": "No rate — uncosted",
                "project_reference": str(entry.job.project),
                "job_reference": entry.job.reference,
                "person_name": entry.person.full_name,
                "work_date": entry.work_date,
                "days": str(entry.days),
            }

        for entry in entries.filter(overlaps_day=True):
            yield {
                "finding": "More than a day on this date",
                "project_reference": str(entry.job.project),
                "job_reference": entry.job.reference,
                "person_name": entry.person.full_name,
                "work_date": entry.work_date,
                "days": str(entry.days),
            }
