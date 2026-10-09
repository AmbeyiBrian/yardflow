"""Auto-close and day formation (design §4.18.5, R13).

An hourly sweep, because the closing hour is the tenant's to set and the daily
05:30 ``core.sweeps.dispatch_sweeps`` is too coarse for it. It follows that
module's shape: a cross-tenant dispatcher that only lists organizations, and one
child task per organization with each step guarded separately.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from celery import shared_task
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from attendance.models import ClosedBy, WorkDay, WorkDayStatus, WorkSession
from core.audit import record_system
from core.models import AuditAction, Organization
from core.tasks import TenantTask

logger = logging.getLogger(__name__)


def _organization(organization) -> Organization:  # type: ignore[no-untyped-def]
    if isinstance(organization, Organization):
        return organization
    return Organization.objects.get(pk=organization)


def cutoff_for(clock_in_at: datetime, *, hour: int, zone: ZoneInfo) -> datetime:
    """When a session opened at ``clock_in_at`` is closed if nobody closed it (§4.18.5).

    ``hour`` o'clock on the session's local date; for a session opened at or
    after that hour, the midnight that ends its local date.
    """
    local = clock_in_at.astimezone(zone)
    at_hour = datetime.combine(local.date(), time(hour), tzinfo=zone)
    if local < at_hour:
        return at_hour
    return datetime.combine(local.date() + timedelta(days=1), time(0), tzinfo=zone)


def close_stale_sessions(organization, *, now: datetime | None = None) -> int:
    """Close every open session whose cutoff has passed, **at the cutoff** (§4.18.5).

    Not at the time the sweep happened to run: a sweep that ran at 22:00 still
    records 18:00, so being late costs the person nothing. ``closed_by=AUTO``
    and no out position. Returns how many were closed.
    """
    organization = _organization(organization)
    settings = organization.settings
    zone = ZoneInfo(settings.timezone)
    hour = settings.clock_auto_close_hour
    now = now or timezone.now()

    closed = 0
    open_ids = list(
        WorkSession.objects.filter(clock_out_at__isnull=True).values_list("pk", flat=True)
    )
    for pk in open_ids:
        with transaction.atomic():
            session = WorkSession.objects.select_for_update().filter(pk=pk).first()
            if session is None or session.clock_out_at is not None:
                continue  # the person clocked out while we were looking
            cutoff = cutoff_for(session.clock_in_at, hour=hour, zone=zone)
            if cutoff > now:
                continue
            session.clock_out_at = cutoff
            session.clock_out_received_at = now
            session.closed_by = ClosedBy.AUTO
            session.save()
            record_system(
                AuditAction.STATUS_CHANGED,
                organization=session.organization_id,
                target=session,
                target_label=str(session),
                note=f"Closed automatically at {cutoff.astimezone(zone):%Y-%m-%d %H:%M}.",
            )
            closed += 1
    return closed


def form_days(organization, *, now: datetime | None = None) -> int:
    """Route every past day that has no open session and something unrouted (§4.18.5).

    A day is a candidate when its local date is over, nothing on it is still
    open, and it is either ``OPEN`` or holds a closed session no request covers
    yet (a late offline replay). Returns how many days were routed.
    """
    from attendance.routing import route_day

    organization = _organization(organization)
    zone = ZoneInfo(organization.settings.timezone)
    today = (now or timezone.now()).astimezone(zone).date()

    candidates = (
        WorkDay.objects.filter(date__lt=today)
        .filter(
            Q(status=WorkDayStatus.OPEN)
            | Q(
                sessions__approval_request__isnull=True,
                sessions__clock_out_at__isnull=False,
            )
        )
        .exclude(sessions__clock_out_at__isnull=True)
        .distinct()
        .order_by("date", "pk")
    )
    formed = 0
    for day_id in list(candidates.values_list("pk", flat=True)):
        with transaction.atomic():
            day = WorkDay.objects.select_for_update().get(pk=day_id)
            route_day(day, now=now)
            formed += 1
    return formed


@shared_task(base=TenantTask, requires_organization=False)
def dispatch_attendance_sweep() -> dict:
    """Fan the hourly attendance sweep out per active organization (beat, :05).

    Cross-tenant by design and touches no tenant data itself, like
    ``core.sweeps.dispatch_sweeps``.
    """
    scheduled = 0
    for organization_id in (
        Organization.objects.filter(status=Organization.Status.ACTIVE)
        .values_list("pk", flat=True)
        .iterator()
    ):
        attendance_sweep_tenant.delay(organization_id=str(organization_id))
        scheduled += 1
    logger.info("scheduled attendance sweeps for %s organization(s)", scheduled)
    return {"organizations": scheduled}


@shared_task(base=TenantTask, bind=True)
def attendance_sweep_tenant(self, organization_id) -> dict:  # type: ignore[no-untyped-def]
    """Close stale sessions, then form days, for one tenant.

    Closing runs first so a session that has just been closed lets its day form
    in the same run. Each step is guarded separately: one failing must not stop
    the other.
    """
    results: dict[str, object] = {"organization_id": str(organization_id)}
    for name, run in (("closed", close_stale_sessions), ("formed", form_days)):
        try:
            with transaction.atomic():
                results[name] = run(organization_id)
        except Exception:
            logger.exception("attendance %s failed for organization %s", name, organization_id)
            results[name] = "failed"
    return results
