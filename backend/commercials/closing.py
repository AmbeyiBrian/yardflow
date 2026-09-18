"""Closing a project, and freezing what it reported (§4.14; O13).

The ledger stays live after a project closes. A reversal posted next month is a
true correction to the record and must land. But a closed PO's reported margin
should not move under the people who signed it off, so the figures are
snapshotted here and §10 reads the snapshot instead of recomputing.

Those two facts are not in tension — they are the same discipline applied to two
different questions. *What happened* is the ledger's, and it never stops being
correctable. *What we reported on the day we closed* is a historical statement,
and a statement that rewrites itself is not one.
"""

from __future__ import annotations

from django.utils import timezone

from core.audit import record
from core.exceptions import DomainError
from core.models import AuditAction


class ProjectNotClosable(DomainError):
    """This project cannot be closed in the state it is in."""


class NotTheProjectManager(DomainError):
    """O13: the manager closes their own project."""


def close_project(project, *, actor, reason: str = "", request=None):
    """Close a project and freeze its figures (O13).

    Warns rather than blocks on open jobs and unreconciled material, as C7 does
    — but a close carrying either needs a reason, the same pattern a job's
    ``closed_with_variance`` follows (H5).
    """
    from commercials.costing import performance_for
    from commercials.models import ProjectSnapshot
    from jobs.models import Job, JobStatus
    from network.models import ProjectStatus

    if project.status != ProjectStatus.OPEN:
        raise ProjectNotClosable(
            f"{project} is already {project.get_status_display().lower()}."
        )

    if project.manager_id is not None and project.manager_id != actor.pk:
        raise NotTheProjectManager(
            "O13: the manager closes their own project — its performance "
            "becomes final at that moment, and they are the one accountable "
            "for the figure."
        )

    open_jobs = Job.objects.filter(project=project).exclude(
        status__in=(JobStatus.CLOSED, JobStatus.CANCELLED)
    )
    open_job_count = open_jobs.count()
    summary = project.unreconciled_summary()
    unreconciled = bool(summary.get("unreconciled"))

    if (open_job_count or unreconciled) and not reason:
        raise ProjectNotClosable(
            f"{project} has {open_job_count} open job(s)"
            + (" and unreconciled material" if unreconciled else "")
            + ". Closing anyway needs a reason."
        )

    # Snapshot **before** the status changes: `cost_for` reads the snapshot for
    # a closed project, so computing it after would read the one we are about
    # to write and freeze zeroes.
    result = performance_for(project)
    figures = {
        **result.cost.as_dict(),
        "contract_value": _text(result.contract_value),
        "cost_budget": _text(result.cost_budget),
        "margin": _text(result.margin),
        "margin_percent": _text(result.margin_percent),
        "budget_variance": _text(result.budget_variance),
        "is_over_budget": result.is_over_budget,
        "open_jobs_at_close": open_job_count,
        "unreconciled_at_close": unreconciled,
    }

    project.status = ProjectStatus.CLOSED
    project.closed_at = timezone.now()
    project.close_reason = reason
    project.closed_with_unreconciled = unreconciled
    project.save(
        update_fields=[
            "status",
            "closed_at",
            "close_reason",
            "closed_with_unreconciled",
        ]
    )

    ProjectSnapshot.objects.create(
        organization_id=project.organization_id,
        project=project,
        taken_by=actor,
        figures=figures,
    )

    record(
        AuditAction.STATUS_CHANGED,
        actor=actor,
        organization=project.organization_id,
        target=project,
        target_label=str(project),
        request=request,
        note=(
            f"Project closed. Cost {figures.get('total')}, "
            f"{open_job_count} job(s) still open."
        ),
    )
    return project, summary


def reopen_project(project, *, actor, reason: str, request=None):
    """Reopen a closed project (O13).

    An owner's action, and recorded. The snapshots stay — a project reopened
    and closed again has two, and the history of what it reported each time is
    worth more than a tidy single row.
    """
    from network.models import ProjectStatus

    if project.status == ProjectStatus.OPEN:
        raise ProjectNotClosable(f"{project} is already open.")
    if not reason:
        raise ProjectNotClosable("Reopening a project needs a reason.")

    project.status = ProjectStatus.OPEN
    project.closed_at = None
    project.close_reason = ""
    project.save(update_fields=["status", "closed_at", "close_reason"])

    record(
        AuditAction.STATUS_CHANGED,
        actor=actor,
        organization=project.organization_id,
        target=project,
        target_label=str(project),
        request=request,
        note=f"Project reopened: {reason}",
    )
    return project


def _text(value):  # type: ignore[no-untyped-def]
    return None if value is None else str(value)
