"""The scheduled sweeps (design §2.2, §9.1, §12; I3, K3, L2, Q3).

Several requirements are only met by something running on a clock. An overdue
tool nobody is reminded about is a lost tool (I3); an approval that sits
unanswered blocks a job (F5); a return the client never acknowledged is exposure
nobody is watching (K3). Each sweep already exists as a plain function in its own
app — this is where they get run.

Two decisions:

**Fan out per tenant.** The dispatcher is deliberately cross-tenant and does
nothing but list organizations; every sweep that touches data runs as a child
task carrying its ``organization_id``, so `TenantTask` establishes both the
Python context and the Postgres setting (§2.2). A single sweep looping over
tenants inside one transaction would hold the context of whichever it happened
to be on, which is exactly the bug row-level security exists to catch.

**One tenant's failure does not stop the rest.** A sweep raising for one
organization must not leave every later organization unswept, so each child task
is independent and failures are logged rather than propagated.
"""

from __future__ import annotations

import logging

from celery import shared_task

from core.tasks import TenantTask

logger = logging.getLogger(__name__)

#: How long a client may sit on a return before somebody is told (K3, L2).
#:
#: A week: long enough that a store with a backlog is not chased for nothing,
#: short enough that the material is still findable if it went astray.
RETURN_ACKNOWLEDGEMENT_GRACE_DAYS = 7


@shared_task(base=TenantTask, requires_organization=False)
def dispatch_sweeps() -> dict:
    """Fan out every per-tenant sweep. Run by beat.

    Cross-tenant by design, and the one place that is true: it reads
    organizations and schedules work, and touches no tenant data itself.
    """
    from core.models import Organization

    scheduled = 0
    # `Organization` is the tenant rather than tenant-*scoped*, so its ordinary
    # manager already sees them all — no bypass is needed, and none is used.
    #
    # Suspended tenants are skipped: A2 makes a suspension read-only, and
    # reminding somebody about an overdue tool in a system they cannot write to
    # is a message with nothing to do about it.
    for organization_id in (
        Organization.objects.filter(status=Organization.Status.ACTIVE)
        .values_list("pk", flat=True)
        .iterator()
    ):
        sweep_tenant.delay(organization_id=str(organization_id))
        scheduled += 1

    logger.info("scheduled sweeps for %s organization(s)", scheduled)
    return {"organizations": scheduled}


@shared_task(base=TenantTask, requires_organization=False)
def retry_notifications() -> dict:
    """Retry transiently failed deliveries, per tenant (L3).

    Fanned out the same way and for the same reason as the sweeps: the retry
    reads notification rows, which are tenant data.
    """
    from core.models import Organization

    scheduled = 0
    for organization_id in (
        Organization.objects.filter(status=Organization.Status.ACTIVE)
        .values_list("pk", flat=True)
        .iterator()
    ):
        retry_notifications_for.delay(organization_id=str(organization_id))
        scheduled += 1
    return {"organizations": scheduled}


@shared_task(base=TenantTask, bind=True)
def retry_notifications_for(self, organization_id) -> int:  # type: ignore[no-untyped-def]
    from notifications.events import retry_pending

    return retry_pending(organization_id)


@shared_task(base=TenantTask, requires_organization=False)
def verify_ledgers() -> dict:
    """Check every tenant's cached balances against the ledger (§3.4).

    **Reports; never corrects.** A cache that silently repaired itself would hide
    the bug that caused the drift, and the drift is the only evidence that bug
    exists.
    """
    from core.models import Organization

    reports: dict[str, object] = {}
    for organization_id in (
        Organization.objects.filter(status=Organization.Status.ACTIVE)
        .values_list("pk", flat=True)
        .iterator()
    ):
        verify_ledger_for.delay(organization_id=str(organization_id))
        reports[str(organization_id)] = "scheduled"
    return reports


@shared_task(base=TenantTask, bind=True)
def verify_ledger_for(self, organization_id) -> dict:  # type: ignore[no-untyped-def]
    from stock.verification import verify_ledger

    result = verify_ledger(organization_id)
    if not result.ok:
        # N-11: this is the line somebody has to see. Silent drift is worse than
        # loud drift, because the ledger is what the whole product rests on.
        logger.error(
            "ledger drift for organization %s: %s",
            organization_id,
            result.summary(),
        )
    return {"ok": result.ok, "summary": result.summary()}


@shared_task(base=TenantTask, bind=True)
def sweep_tenant(self, organization_id) -> dict:  # type: ignore[no-untyped-def]
    """Every clock-driven check for one tenant (I3, F5, K3, Q3).

    One task rather than five, because they are cheap, they run at the same
    cadence, and a single result line per tenant is what somebody reading the
    logs at 6am actually wants.

    Each step is guarded separately: a failure in the overdue sweep must not stop
    expiring stale gate passes, which is a control rather than a reminder.
    """
    results: dict[str, object] = {"organization_id": str(organization_id)}

    for name, run in (
        ("custody_overdue", _sweep_custody_overdue),
        ("custody_escalations", _sweep_custody_escalations),
        ("approval_escalations", _sweep_approval_escalations),
        ("expired_gate_passes", _sweep_expired_gate_passes),
        ("unacknowledged_returns", _sweep_unacknowledged_returns),
    ):
        try:
            results[name] = run(organization_id)
        except Exception:
            logger.exception("sweep %s failed for organization %s", name, organization_id)
            results[name] = "failed"

    return results


def _sweep_custody_overdue(organization_id) -> int:
    """I3: material past its return date is flagged."""
    from custody.services import mark_overdue

    return mark_overdue(organization_id)


def _sweep_custody_escalations(organization_id) -> dict:
    """I3's escalating chain: holder, then supervisor, then owner."""
    from custody.services import escalate_overdue

    return escalate_overdue(organization_id)


def _sweep_approval_escalations(organization_id) -> int:
    """F5: an approval unanswered past its due time escalates."""
    from dispatch.services import escalate_overdue_approvals

    return escalate_overdue_approvals(organization_id)


def _sweep_expired_gate_passes(organization_id) -> int:
    """Q3: an approved pass not released within the window expires.

    A control, not a reminder — a stale approval must not be usable days later
    against stock that has since changed.
    """
    from dispatch.services import expire_stale_passes

    return expire_stale_passes(organization_id)


def _sweep_unacknowledged_returns(organization_id) -> int:
    """K3, L2: "a beat task notifies on returns still unacknowledged after N days".

    Emitted once per return per sweep. The notification layer is what decides
    whether that reaches somebody as an in-app line or an SMS (L1), and its own
    deduplication is what stops a daily sweep becoming a daily nag.
    """
    from django.utils import timezone

    from disposition.services import unacknowledged_returns
    from notifications.events import emit

    count = 0
    for gate_out in unacknowledged_returns(
        organization_id, older_than_days=RETURN_ACKNOWLEDGEMENT_GRACE_DAYS
    ):
        days = (
            (timezone.now() - gate_out.released_at).days if gate_out.released_at else None
        )
        emit(
            "client_return.unacknowledged",
            gate_out,
            payload={
                "number": gate_out.number,
                "client": str(gate_out.client) if gate_out.client_id else "",
                "days_outstanding": days,
            },
        )
        count += 1
    return count
