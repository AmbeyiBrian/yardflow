"""Correcting a rejected day, and a day the Director adds (design §4.18.6, §4.18.6a; R13).

A correction is the person's statement, not a measurement: no position is taken
and the session is flagged *corrected*. The session's recorded times are never
overwritten; a ``WorkSessionCorrection`` row holds original and corrected, and
the effective times are the latest correction's.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date as date_type
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from approvals import engine
from approvals.models import ApprovalRequest, ApprovalRequestStatus
from attendance import routing, services
from attendance.models import (
    ClosedBy,
    CorrectionKind,
    WorkDay,
    WorkSession,
    WorkSessionCorrection,
)
from core.audit import record
from core.exceptions import DomainError, PermissionDeniedError
from core.models import AuditAction
from locations.models import Location
from network.models import Site

#: How long after a rejection the person may still correct it (§4.18.6).
CORRECTION_WINDOW = timedelta(days=30)


class CorrectionNotAllowed(DomainError):
    code = "CORRECTION_NOT_ALLOWED"
    status_code = 409
    default_message = "This can only be corrected while it is rejected, and within 30 days."


class CorrectionReasonRequired(DomainError):
    code = "CORRECTION_REASON_REQUIRED"
    status_code = 400
    default_message = "Say why, so the approver can see what changed."


class WorkDayAddNotAllowed(DomainError):
    code = "WORK_DAY_ADD_NOT_ALLOWED"
    status_code = 403
    default_message = "Only a Director can add a day, and never their own."


# --------------------------------------------------------------------------
# Reading corrections
# --------------------------------------------------------------------------


def effective_times(session: WorkSession) -> tuple[datetime, datetime | None]:
    """The session's times with its corrections applied, oldest to newest (§4.18.6)."""
    clock_in, clock_out = session.clock_in_at, session.clock_out_at
    for correction in session.corrections.filter(kind=CorrectionKind.EDIT).order_by(
        "made_at", "pk"
    ):
        clock_in = correction.corrected_in_at or clock_in
        clock_out = correction.corrected_out_at or clock_out
    return clock_in, clock_out


def hours_of(sessions: Iterable[WorkSession]) -> Decimal:
    """Hours across ``sessions`` at their effective times; an open session counts none."""
    total = timedelta()
    for session in sessions:
        clock_in, clock_out = effective_times(session)
        if clock_out is not None:
            total += clock_out - clock_in
    return (Decimal(total.total_seconds()) / Decimal(3600)).quantize(Decimal("0.01"))


def is_corrected(session: WorkSession) -> bool:
    """Was this session edited, or created, by a correction?"""
    if session.corrections.exists():
        return True
    return WorkSessionCorrection.objects.filter(
        work_day_id=session.work_day_id,
        kind=CorrectionKind.ADD,
        corrected_in_at=session.clock_in_at,
        site_id=session.site_id,
        location_id=session.location_id,
    ).exists()


def session_flags(session: WorkSession) -> list[str]:
    """Plain-words flags that follow a session wherever it shows (§4.18.6, §4.18.6a)."""
    flags: list[str] = []
    if session.added_by_id is not None:
        flags.append("added by the Director")
    if is_corrected(session):
        flags.append("corrected")
    if session.closed_by == ClosedBy.AUTO:
        flags.append("closed automatically")
    if session.area_changed:
        flags.append("area changed")
    return flags


def rejected_at(request: ApprovalRequest) -> datetime | None:
    return request.resolved_at


# --------------------------------------------------------------------------
# Shared checks
# --------------------------------------------------------------------------


def _check_span(day: WorkDay, person, clock_in, clock_out, *, ignore: WorkSession | None) -> None:  # type: ignore[no-untyped-def]
    """§4.18.6: on the day's local date, out >= in, overlapping nothing else."""
    zone = ZoneInfo(day.organization.settings.timezone)
    for value in (clock_in, clock_out):
        if value.astimezone(zone).date() != day.date:
            raise services.ClockTimeInvalid(f"Times must stay on {day.date:%d %b %Y}.")
    if clock_out < clock_in:
        raise services.ClockTimeInvalid("The end cannot be before the start.")
    others = WorkSession.objects.filter(
        person=person,
        local_date__range=(day.date - timedelta(days=1), day.date + timedelta(days=1)),
    )
    if ignore is not None:
        others = others.exclude(pk=ignore.pk)
    for other in others:
        other_in, other_out = effective_times(other)
        if other_out is None:
            overlaps = clock_out > other_in
        else:
            overlaps = clock_in < other_out and clock_out > other_in
        if overlaps:
            raise services.ClockOverlap()


def _when(value: datetime | str | None) -> datetime | None:
    return None if value is None else services._aware(value)


# --------------------------------------------------------------------------
# correct_session
# --------------------------------------------------------------------------


def correct_session(
    *,
    actor,  # type: ignore[no-untyped-def]
    session: WorkSession,
    kind: str,
    reason: str,
    corrected_in_at: datetime | str | None = None,
    corrected_out_at: datetime | str | None = None,
    place: Site | Location | None = None,
    project=None,  # type: ignore[no-untyped-def]
    now: datetime | None = None,
    request=None,  # type: ignore[no-untyped-def]
) -> WorkSessionCorrection:
    """Correct a rejected slice and send it back to the same approver (§4.18.6).

    ``session`` is the session being edited (``EDIT``) or any session of the
    rejected slice an added session joins (``ADD``, which needs ``place`` and
    both times). Only the day's person may correct, only while the slice's
    request is ``REJECTED`` and at most 30 days after it was, with a ``reason``.

    Only that slice reopens: a new level-1 request to the **same addressee**,
    the slice's sessions repointed at it, the day back to ``PENDING``. The old
    request and its actions stay, so history reads reject, correct, decide; the
    other manager's slice is not touched. Returns the correction row, whose
    ``rejected_request`` and ``reopened_request`` link old to new.
    """
    now = now or timezone.now()
    reason = (reason or "").strip()
    if kind not in (CorrectionKind.EDIT, CorrectionKind.ADD):
        raise services.ClockTimeInvalid("Choose edit or add.")

    with transaction.atomic():
        day = (
            WorkDay.objects.select_for_update()
            .select_related("person")
            .get(pk=session.work_day_id)
        )
        if actor.pk != day.person_id:
            raise PermissionDeniedError("Only the person whose day this is can correct it.")
        # Re-read under the lock: a second submit finds the slice already reopened.
        session = WorkSession.objects.select_related("approval_request").get(pk=session.pk)
        old = session.approval_request
        rejected = rejected_at(old) if old is not None else None
        if (
            old is None
            or old.status != ApprovalRequestStatus.REJECTED
            or rejected is None
            or now - rejected > CORRECTION_WINDOW
        ):
            raise CorrectionNotAllowed()
        if not reason:
            raise CorrectionReasonRequired()

        if kind == CorrectionKind.EDIT:
            fields = _edit_fields(day, session, corrected_in_at, corrected_out_at)
        else:
            fields = _add_fields(day, place, corrected_in_at, corrected_out_at)

        target = engine.WorkDaySlice(user=old.required_user, role=old.required_role)
        (reopened,) = engine.create_requests(day, requested_by=day.person, work_day_slice=target)

        if kind == CorrectionKind.ADD:
            WorkSession.objects.create(
                organization_id=day.organization_id,
                person=day.person,
                site=fields["site"],
                location=fields["location"],
                project=services._resolve_project(fields["site"], project),
                work_day=day,
                local_date=day.date,
                clock_in_at=fields["corrected_in_at"],
                clock_in_received_at=now,
                clock_out_at=fields["corrected_out_at"],
                clock_out_received_at=now,
                closed_by=ClosedBy.PERSON,
                approval_request=reopened,
            )
        for member in WorkSession.objects.filter(approval_request=old):
            member.approval_request = reopened
            member.save()

        correction = WorkSessionCorrection.objects.create(
            organization_id=day.organization_id,
            session=session if kind == CorrectionKind.EDIT else None,
            work_day=day,
            kind=kind,
            reason=reason,
            made_by=actor,
            made_at=now,
            rejected_request=old,
            reopened_request=reopened,
            **fields,
        )
        record(
            AuditAction.DOCUMENT_AMENDED,
            actor=actor,
            organization=day.organization_id,
            target=day,
            target_label=str(day),
            request=request,
            note=f"Corrected ({kind.lower()}) and sent back for approval: {reason}",
        )
        routing.refresh_status(day)
        routing.notify_awaiting(day, reopened)
    return correction


def _edit_fields(day, session, new_in, new_out) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    new_in, new_out = _when(new_in), _when(new_out)
    if new_in is None and new_out is None:
        raise services.ClockTimeInvalid("Give the corrected start or end.")
    current_in, current_out = effective_times(session)
    final_in = new_in or current_in
    final_out = new_out or current_out
    if final_out is None:
        raise services.ClockTimeInvalid("Give the time you clocked out.")
    _check_span(day, day.person, final_in, final_out, ignore=session)
    return {
        "original_in_at": current_in,
        "original_out_at": current_out,
        "corrected_in_at": new_in,
        "corrected_out_at": new_out,
    }


def _add_fields(day, place, new_in, new_out) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    new_in, new_out = _when(new_in), _when(new_out)
    if place is None:
        raise services.ClockPlaceRequired()
    if new_in is None or new_out is None:
        raise services.ClockTimeInvalid("An added session needs a start and an end.")
    services._check_clockable(place)
    _check_span(day, day.person, new_in, new_out, ignore=None)
    return {
        "site": place if isinstance(place, Site) else None,
        "location": place if isinstance(place, Location) else None,
        "corrected_in_at": new_in,
        "corrected_out_at": new_out,
    }


# --------------------------------------------------------------------------
# add_day (§4.18.6a)
# --------------------------------------------------------------------------


def add_day(
    *,
    actor,  # type: ignore[no-untyped-def]
    person,  # type: ignore[no-untyped-def]
    date: date_type,
    place: Site | Location,
    start: datetime | str,
    end: datetime | str,
    reason: str,
    project=None,  # type: ignore[no-untyped-def]
    now: datetime | None = None,
    request=None,  # type: ignore[no-untyped-def]
) -> WorkSession:
    """A Director adds a session for someone who could not clock in (§4.18.6a).

    Only holders of ``finance_director_role``, never for their own day. The
    session has no position, ``closed_by=PERSON``, is flagged "added by the
    Director", is audited, and is routed as usual, but never to its adder.
    Raises ``WORK_DAY_ADD_NOT_ALLOWED`` and ``CORRECTION_REASON_REQUIRED``.
    """
    now = now or timezone.now()
    reason = (reason or "").strip()
    director = actor.organization.settings.finance_director_role
    if director is None or not actor.user_roles.filter(role_id=director.pk).exists():
        raise WorkDayAddNotAllowed()
    if person.pk == actor.pk:
        raise WorkDayAddNotAllowed("A Director cannot add their own day.")
    if not reason:
        raise CorrectionReasonRequired()

    clock_in, clock_out = services._aware(start), services._aware(end)
    if clock_out > now + services.MAX_AHEAD:
        raise services.ClockTimeInvalid("That time is in the future.")

    with transaction.atomic():
        services._lock_person(person)
        services._check_clockable(place)
        day, _ = WorkDay.objects.get_or_create(
            organization_id=person.organization_id, person=person, date=date
        )
        _check_span(day, person, clock_in, clock_out, ignore=None)
        site = place if isinstance(place, Site) else None
        session = WorkSession.objects.create(
            organization_id=person.organization_id,
            person=person,
            site=site,
            location=place if isinstance(place, Location) else None,
            project=services._resolve_project(site, project),
            work_day=day,
            local_date=date,
            clock_in_at=clock_in,
            clock_in_received_at=now,
            clock_out_at=clock_out,
            clock_out_received_at=now,
            closed_by=ClosedBy.PERSON,
            added_by=actor,
            added_reason=reason,
        )
        record(
            AuditAction.DOCUMENT_POSTED,
            actor=actor,
            organization=person.organization_id,
            target=session,
            target_label=str(session),
            request=request,
            note=f"Added by the Director for {person}, {date:%d %b %Y}, at {place}: {reason}",
        )
        routing.route_day(day, now=now)
    session.refresh_from_db()
    return session
