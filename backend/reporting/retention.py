"""Retention review (design §4.1; M5, T7.6).

M5: "a configurable retention period, so that we keep records as long as our
clients require. **Retention never silently deletes; expiry flags records for
review.**"

T7.6 restates it as a criterion — "expiry produces a review list **and no data
loss**" — so this module has no delete in it. That is the whole design:

**Nothing here removes anything.** Not even behind a flag, not even with a
confirmation. A retention job that can delete is a retention job that will, one
day, delete something an auditor then asks for — and the ledger is append-only
precisely so that cannot happen (§3.2, M6). What expiry produces is a *list*: a
person decides, and if the decision is to remove something they do it knowing
what they are removing.

**Documents, not movements.** The review list is over the documents a tenant
would archive — gate-ins, gate passes, closeouts, disposals — because those are
what a retention policy is written about. The ledger underneath them is never
listed for review at all: deleting a movement would silently change a balance
that reconciled yesterday.

**A closed document only.** Something still open cannot be past its retention,
whatever its date says, so an unreleased gate pass from two years ago appears as
an exception rather than as a candidate for archiving.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from django.utils import timezone


@dataclass(frozen=True)
class ReviewCandidate:
    """One document old enough for somebody to decide about."""

    document_type: str
    document_id: str
    number: str
    label: str
    closed_at: date | None
    age_days: int

    def as_dict(self) -> dict:
        return {
            "document_type": self.document_type,
            "document_id": self.document_id,
            "number": self.number,
            "label": self.label,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "age_days": self.age_days,
        }


def retention_cutoff(organization, *, today=None) -> date:
    """The date before which documents are past their retention (§4.1).

    Read from the tenant's own setting, because M5 exists so a tenant can keep
    records "as long as our clients require" — and an operator contract requiring
    ten years is not unusual.
    """
    today = today or timezone.now().date()
    months = organization.settings.retention_months or 84
    # Calendar months as 30-day units. A retention boundary is a policy date, not
    # an anniversary, and the extra precision would imply a promise about the
    # exact day that nothing else in the system keeps.
    return today - timedelta(days=months * 30)


def review_list(organization, *, today=None, limit: int | None = None) -> list[dict]:
    """Documents past retention, for a person to decide about (M5, T7.6).

    Returns candidates only. Nothing is deleted, flagged in the database, or
    made unreadable — the caller gets a list and the records stay exactly as they
    were.
    """
    cutoff = retention_cutoff(organization, today=today)
    today = today or timezone.now().date()

    candidates: list[ReviewCandidate] = []
    candidates.extend(_gate_ins(cutoff, today))
    candidates.extend(_gate_outs(cutoff, today))
    candidates.extend(_closeouts(cutoff, today))
    candidates.extend(_disposals(cutoff, today))

    # Oldest first: if somebody works through part of the list, the part they do
    # is the part that matters most.
    candidates.sort(key=lambda entry: entry.age_days, reverse=True)
    if limit is not None:
        candidates = candidates[:limit]
    return [candidate.as_dict() for candidate in candidates]


def _age(moment, today: date) -> int:
    if moment is None:
        return 0
    value = moment.date() if hasattr(moment, "date") else moment
    return (today - value).days


def _gate_ins(cutoff: date, today: date):
    from receiving.models import DocumentStatus, GateIn

    for gate_in in GateIn.objects.filter(
        status__in=(DocumentStatus.POSTED, DocumentStatus.VOID),
        received_at__date__lt=cutoff,
    ).select_related("to_location")[:500]:
        yield ReviewCandidate(
            document_type="receiving.GateIn",
            document_id=str(gate_in.pk),
            number=gate_in.number,
            label=f"{gate_in.get_source_type_display()} into {gate_in.to_location.name}",
            closed_at=gate_in.received_at.date() if gate_in.received_at else None,
            age_days=_age(gate_in.received_at, today),
        )


def _gate_outs(cutoff: date, today: date):
    from dispatch.models import GateOut, GateOutStatus

    # Only finished ones. A pass still open past its retention is a problem to
    # investigate, not a record to archive — see `open_documents_past_retention`.
    for gate_out in GateOut.objects.filter(
        status__in=(GateOutStatus.RELEASED, GateOutStatus.CLOSED, GateOutStatus.CANCELLED),
        created_at__date__lt=cutoff,
    ).select_related("site", "client")[:500]:
        yield ReviewCandidate(
            document_type="dispatch.GateOut",
            document_id=str(gate_out.pk),
            number=gate_out.number,
            label=f"{gate_out.get_purpose_type_display()} to {gate_out.destination_label}",
            closed_at=(gate_out.released_at or gate_out.created_at).date(),
            age_days=_age(gate_out.released_at or gate_out.created_at, today),
        )


def _closeouts(cutoff: date, today: date):
    from jobs.models import JobCloseout

    for closeout in JobCloseout.objects.filter(
        submitted_at__date__lt=cutoff
    ).select_related("job", "job__site")[:500]:
        yield ReviewCandidate(
            document_type="jobs.JobCloseout",
            document_id=str(closeout.pk),
            number=str(closeout.job.reference),
            label=f"Closeout for {closeout.job}",
            closed_at=closeout.submitted_at.date() if closeout.submitted_at else None,
            age_days=_age(closeout.submitted_at, today),
        )


def _disposals(cutoff: date, today: date):
    from disposition.models import Disposal, DisposalStatus

    for disposal in Disposal.objects.filter(
        status=DisposalStatus.DISPOSED, disposed_at__date__lt=cutoff
    )[:500]:
        yield ReviewCandidate(
            document_type="disposition.Disposal",
            document_id=str(disposal.pk),
            number=disposal.number,
            label=f"Disposal by {disposal.get_method_display()}",
            closed_at=disposal.disposed_at.date() if disposal.disposed_at else None,
            age_days=_age(disposal.disposed_at, today),
        )


def open_documents_past_retention(organization, *, today=None) -> list[dict]:
    """Documents older than the retention period and **still open**.

    Not candidates for archiving — the opposite. A gate pass approved two years
    ago and never released, or a closeout waiting on a return that never came, is
    something somebody forgot. Retention review is the moment that gets noticed,
    so the query lives beside it rather than being a separate report nobody runs.
    """
    cutoff = retention_cutoff(organization, today=today)
    today = today or timezone.now().date()

    from dispatch.models import GateOut, GateOutStatus
    from jobs.models import Job, JobStatus

    stale: list[dict] = []

    for gate_out in GateOut.objects.filter(
        status__in=(
            GateOutStatus.DRAFT,
            GateOutStatus.PENDING_APPROVAL,
            GateOutStatus.APPROVED,
            GateOutStatus.PARTIALLY_RELEASED,
        ),
        created_at__date__lt=cutoff,
    )[:200]:
        stale.append(
            {
                "document_type": "dispatch.GateOut",
                "document_id": str(gate_out.pk),
                "number": gate_out.number,
                "status": gate_out.status,
                "label": f"Gate pass still {gate_out.get_status_display().lower()}",
                "age_days": _age(gate_out.created_at, today),
            }
        )

    for job in Job.objects.filter(
        status__in=(JobStatus.OPEN, JobStatus.IN_PROGRESS, JobStatus.AWAITING_CLOSEOUT),
        created_at__date__lt=cutoff,
    )[:200]:
        stale.append(
            {
                "document_type": "jobs.Job",
                "document_id": str(job.pk),
                "number": job.reference,
                "status": job.status,
                "label": f"Job still {job.get_status_display().lower()}",
                "age_days": _age(job.created_at, today),
            }
        )

    stale.sort(key=lambda entry: entry["age_days"], reverse=True)
    return stale
