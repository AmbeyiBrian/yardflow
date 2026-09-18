"""Job closeout, return matching and the close guard (design §4.9; H2–H5).

The sequence this implements is the loop the whole system closes:

    issue -> install / consume -> declare what is coming back -> receive it ->
    reconcile

Each step writes to the same ledger, which is why H4's four figures always agree.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from core.audit import record
from core.exceptions import DomainError
from core.models import AuditAction
from custody.models import CustodyExpectation, ExpectationStatus
from jobs.models import (
    CloseoutAction,
    CloseoutStatus,
    Job,
    JobCloseout,
    JobStatus,
    Variance,
    VarianceStatus,
    VarianceType,
)
from locations.nodes import consumed_node, node_for_site, node_for_user
from stock.models import Condition, MovementType, OwnerType
from stock.services import MovementRequest, post_movement


class CloseoutNotReady(DomainError):
    code = "CLOSEOUT_NOT_READY"
    status_code = 400
    default_message = "This closeout cannot be submitted yet."


class JobHasUnaccountedMaterial(DomainError):
    """H5: closing is blocked while material is unaccounted for.

    Overridable by someone holding ``job.close_with_variance``, with a reason —
    because a job genuinely can end with material written off, and the honest
    record of that is better than a job left open forever.
    """

    code = "JOB_HAS_UNACCOUNTED_MATERIAL"
    status_code = 400
    default_message = (
        "This job still has material unaccounted for and cannot be closed."
    )


# --------------------------------------------------------------------------
# T5.3 — submitting a closeout
# --------------------------------------------------------------------------


@transaction.atomic
def submit_closeout(closeout: JobCloseout, *, submitted_by=None, request=None) -> JobCloseout:
    """Post what has gone, and record what is expected back (H2, §4.9).

    INSTALLED and CONSUMED post movements immediately: that material is gone, and
    the ledger should say so before anyone asks. RETURNING and RECOVERED create
    *expectations* — recording them as received would be a lie the reconciliation
    would then quietly absorb.
    """
    lines = list(closeout.lines.select_related("item_type", "serial_unit", "reel").all())
    if not lines:
        raise CloseoutNotReady("Report at least one line before submitting.")

    # Submitting twice would post every movement twice — installing the same
    # antenna at the site again and duplicating every expected return. On a phone
    # at a site, a double tap or a retry after a timeout is not an edge case, so
    # the second call has to be refused rather than trusted. ``submitted_at``
    # rather than ``status``, which a closeout carries from birth (§4.9).
    if closeout.submitted_at is not None:
        raise CloseoutNotReady(
            f"This closeout was already submitted at "
            f"{closeout.submitted_at:%Y-%m-%d %H:%M}. Its material has moved "
            f"once and must not move again."
        )

    job = closeout.job
    holder_node = node_for_user(job.assignee)
    site_node = node_for_site(job.site)
    consumed = consumed_node(job.organization_id)

    for line in lines:
        if line.action == CloseoutAction.INSTALLED:
            _post_closeout_movement(
                closeout, line, holder_node, site_node, MovementType.INSTALL, submitted_by
            )
        elif line.action == CloseoutAction.CONSUMED:
            _post_closeout_movement(
                closeout, line, holder_node, consumed, MovementType.CONSUME, submitted_by
            )
        else:
            # RETURNING and RECOVERED: nothing moves yet. The expectation is what
            # the storekeeper will match the delivery against (H3).
            _create_return_expectation(closeout, line)

    closeout.status = CloseoutStatus.SUBMITTED
    closeout.submitted_at = timezone.now()
    closeout.save(update_fields=["status", "submitted_at", "updated_at"])

    # O15: the days reported on this closeout become the job's labour cost.
    # Written at submission rather than at confirmation: confirmation is the
    # storekeeper checking material back in (H3), and the days are the
    # technician's report about their own week, not a fact about the returns.
    cost_labour(closeout)

    if job.status != JobStatus.AWAITING_CLOSEOUT:
        job.status = JobStatus.AWAITING_CLOSEOUT
        job.save(update_fields=["status", "updated_at"])

    record(
        AuditAction.STATUS_CHANGED,
        actor=submitted_by,
        organization=closeout.organization_id,
        target=closeout,
        target_label=str(closeout),
        request=request,
        note=(
            f"Closeout submitted for {job}: "
            f"{sum(1 for line in lines if line.posts_immediately)} posted, "
            f"{sum(1 for line in lines if not line.posts_immediately)} expected back."
        ),
    )

    return closeout


def cost_labour(closeout: JobCloseout) -> None:
    """Capture a rate onto every labour entry on this closeout (O15, D27).

    The rate is resolved and **stored** now. Changing somebody's rate next year
    must not rewrite what a closed project cost, which is the same rule the
    ledger's ``unit_cost`` follows.

    Also sets ``overlaps_day`` where this person's total for the date passes one
    day across every job. It warns rather than refuses: a technician
    apportioning fractions in a yard at dusk will guess, and a refusal would
    block a late closeout because of an earlier one (O15).
    """
    from accounts.services import day_rate_for
    from jobs.models import RateSource

    entries = list(closeout.labour.select_related("person").all())
    if not entries:
        return

    for entry in entries:
        rate, source = day_rate_for(entry.person)
        entry.day_rate = rate
        entry.rate_source = source or RateSource.NONE
        entry.overlaps_day = _exceeds_one_day(entry)
        entry.save(
            update_fields=["day_rate", "rate_source", "overlaps_day", "updated_at"]
        )


def _exceeds_one_day(entry) -> bool:
    """Whether this person is now recorded for more than a day on that date."""
    from django.db.models import Sum

    from jobs.models import JobLabour

    total = (
        JobLabour.objects.filter(person=entry.person, work_date=entry.work_date)
        .aggregate(total=Sum("days"))
        .get("total")
        or Decimal("0")
    )
    return total > Decimal("1")


def _post_closeout_movement(closeout, line, source, destination, movement_type, actor):
    """Move material out of the technician's custody to where it ended up."""
    post_movement(
        MovementRequest(
            item_type=line.item_type,
            quantity=line.quantity,
            from_node=source,
            to_node=destination,
            movement_type=movement_type,
            owner_type=(
                OwnerType.CLIENT
                if getattr(line.serial_unit, "owner_client_id", None)
                else OwnerType.OWN
            ),
            owner_client=getattr(line.serial_unit, "owner_client", None),
            condition=line.condition or Condition.NEW,
            tracking_mode=line.item_type.default_tracking_mode
            if not (line.serial_unit_id or line.reel_id)
            else ("SERIALIZED" if line.serial_unit_id else "REEL"),
            uom=line.uom,
            serial_unit=line.serial_unit,
            reel=line.reel,
            posted_by=actor,
            document_type="jobs.JobCloseout",
            document_id=str(closeout.pk),
            document_line_id=str(line.pk),
            note=line.notes,
        )
    )


def _create_return_expectation(closeout, line) -> CustodyExpectation:
    """Record what the yard should expect back (H2, H3)."""
    return CustodyExpectation.objects.create(
        organization_id=closeout.organization_id,
        holder=closeout.job.assignee,
        item_type=line.item_type,
        serial_unit=line.serial_unit,
        reel=line.reel,
        quantity=line.quantity,
        expected_return_date=timezone.now().date(),
    )


# --------------------------------------------------------------------------
# T5.4 — matching a return against what was declared
# --------------------------------------------------------------------------


@transaction.atomic
def match_return(gate_in, *, matched_by=None, request=None) -> list[Variance]:
    """Match a return against the technician's declaration (H3).

    H3: "a difference between declared and actual creates a **variance**
    requiring investigation and an approver's sign-off."

    Both directions matter. Receiving less than was declared is the obvious case.
    Receiving *more* is also a variance — it means something arrived that nobody
    said was coming, which is exactly as interesting to an auditor.
    """
    from receiving.models import GateInSource

    if gate_in.source_type not in (
        GateInSource.RETURN_FROM_SITE,
        GateInSource.RECOVERY,
    ):
        return []

    variances: list[Variance] = []

    for line in gate_in.lines.select_related("item_type").all():
        expectations = list(
            CustodyExpectation.objects.filter(
                item_type=line.item_type,
                status__in=(ExpectationStatus.OPEN, ExpectationStatus.OVERDUE),
            ).order_by("expected_return_date", "id")
        )

        expected_total = sum(
            (expectation.outstanding_quantity for expectation in expectations),
            Decimal("0"),
        )
        actual = line.quantity

        remaining = actual
        for expectation in expectations:
            if remaining <= 0:
                break
            take = min(remaining, expectation.outstanding_quantity)
            expectation.returned_quantity += take
            if expectation.outstanding_quantity <= 0:
                expectation.status = ExpectationStatus.RETURNED
                expectation.returned_at = timezone.now()
            expectation.save(
                update_fields=["returned_quantity", "status", "returned_at", "updated_at"]
            )
            remaining -= take

        if expected_total != actual:
            variances.append(
                _raise_return_variance(
                    gate_in,
                    line,
                    expected=expected_total,
                    actual=actual,
                    raised_by=matched_by,
                    request=request,
                )
            )

    return variances


def _raise_return_variance(gate_in, line, *, expected, actual, raised_by, request) -> Variance:
    """Open a variance, and tell the people who need to chase it (H3, L2)."""
    variance = Variance.objects.create(
        organization_id=gate_in.organization_id,
        type=VarianceType.RETURN,
        item_type=line.item_type,
        expected=expected,
        actual=actual,
        uom=line.uom,
        reason=(
            f"{line.item_type}: {expected} {line.uom} declared as returning, "
            f"{actual} {line.uom} received on {gate_in.number or 'this gate-in'}."
        ),
        raised_by=raised_by,
    )

    record(
        AuditAction.VARIANCE_RAISED,
        actor=raised_by,
        organization=gate_in.organization_id,
        target=variance,
        target_label=str(variance),
        request=request,
        note=variance.reason,
    )

    try:
        from notifications.events import emit
        from notifications.matrix import Event

        emit(Event.RETURN_VARIANCE_RAISED, variance)
    except ImportError:  # pragma: no cover
        pass

    return variance


@transaction.atomic
def resolve_variance(
    variance: Variance, *, resolution: str, resolved_by=None, write_off: bool = False, request=None
) -> Variance:
    """Close a variance (H3, M1).

    A resolution is required. "Resolved" with no explanation would leave the
    exceptions register clean and the question unanswered, which is worse than
    leaving it open.
    """
    if not resolution:
        raise CloseoutNotReady("Resolving a variance requires an explanation (H3).")

    variance.status = (
        VarianceStatus.WRITTEN_OFF if write_off else VarianceStatus.RESOLVED
    )
    variance.resolution = resolution
    variance.resolved_by = resolved_by
    variance.resolved_at = timezone.now()
    variance.save(
        update_fields=["status", "resolution", "resolved_by", "resolved_at", "updated_at"]
    )

    record(
        AuditAction.VARIANCE_RESOLVED,
        actor=resolved_by,
        organization=variance.organization_id,
        target=variance,
        target_label=str(variance),
        request=request,
        note=f"Variance {variance.get_status_display().lower()}: {resolution}",
    )

    return variance


# --------------------------------------------------------------------------
# T5.6 — the close guard
# --------------------------------------------------------------------------


def outstanding_for_job(job: Job) -> list[CustodyExpectation]:
    """What this job is still waiting on (H5)."""
    assignee_expectations = CustodyExpectation.objects.filter(
        holder=job.assignee,
        status__in=(ExpectationStatus.OPEN, ExpectationStatus.OVERDUE),
    ).select_related("item_type")
    return list(assignee_expectations)


@transaction.atomic
def close_job(
    job: Job, *, closed_by, reason: str = "", override: bool = False, request=None
) -> Job:
    """Close a job, blocked while material is unaccounted for (H5).

    H5: "blocked from closing while material remains unaccounted for, **unless
    someone with authority overrides it with a reason**." The override is
    deliberate and audited — a job left open forever is not a control, it is
    just a stale list.
    """
    outstanding = outstanding_for_job(job)
    open_variances = job.variances.filter(
        status__in=(VarianceStatus.OPEN, VarianceStatus.INVESTIGATING)
    ).exists()

    if outstanding or open_variances:
        if not override:
            raise JobHasUnaccountedMaterial(
                "This job still has material unaccounted for. It can only be "
                "closed by someone permitted to close with a variance, and only "
                "with a reason (H5).",
                details={
                    "outstanding": [
                        {
                            "item": str(expectation.item_type),
                            "quantity": str(expectation.outstanding_quantity),
                        }
                        for expectation in outstanding
                    ],
                    "open_variances": open_variances,
                },
            )
        if not reason:
            raise JobHasUnaccountedMaterial(
                "Closing a job with material unaccounted for requires a reason (H5)."
            )

    job.status = JobStatus.CLOSED
    job.closed_at = timezone.now()
    job.closed_by = closed_by
    job.closed_with_variance = bool(outstanding or open_variances)
    job.close_reason = reason
    job.save(
        update_fields=[
            "status",
            "closed_at",
            "closed_by",
            "closed_with_variance",
            "close_reason",
            "updated_at",
        ]
    )

    record(
        AuditAction.STATUS_CHANGED,
        actor=closed_by,
        organization=job.organization_id,
        target=job,
        target_label=str(job),
        request=request,
        after={"status": job.status, "closed_with_variance": job.closed_with_variance},
        note=(
            f"Job closed with material unaccounted for: {reason}"
            if job.closed_with_variance
            else "Job closed, fully reconciled."
        ),
    )

    if job.closed_with_variance:
        # L2: the owner is told when a job closes with material unaccounted for.
        try:
            from notifications.events import emit
            from notifications.matrix import Event

            emit(Event.JOB_CLOSED_WITH_UNACCOUNTED, job)
        except ImportError:  # pragma: no cover
            pass

    return job
