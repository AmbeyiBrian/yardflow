"""Routing a day to its approvers, and deciding it (design §4.18.5, R13).

A day is not one request but one request **per addressee**: each project's
manager answers for their own sessions, in parallel, and the day is approved
only when every slice is. ``route_day`` makes the slices; ``decide`` answers
them; ``refresh_status`` projects them back onto ``WorkDay.status``.

Nothing here refuses a clock-in. A session nobody can approve stays unrouted and
the owner is told (§4.18.5), because a person who was on site must be able to
say so.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from django.db import transaction
from django.utils import timezone

from accounts.models import User
from approvals import engine
from approvals.models import ApprovalDecision, ApprovalRequest, ApprovalRequestStatus
from attendance.models import WorkDay, WorkDayStatus, WorkSession
from commercials.finance import RejectionReasonRequired
from core.audit import client_ip, record
from core.exceptions import DomainError
from core.models import AuditAction
from notifications.events import emit
from notifications.matrix import Event
from notifications.models import NotificationEvent

OPEN_STATUSES = (ApprovalRequestStatus.PENDING, ApprovalRequestStatus.ESCALATED)


class WorkDaySelfApproval(DomainError):
    code = "WORK_DAY_SELF_APPROVAL"
    status_code = 403
    default_message = "Nobody approves their own day, or a day they added."


class WorkDayNotDecidable(DomainError):
    code = "WORK_DAY_NOT_DECIDABLE"
    status_code = 409
    default_message = "There is nothing on this day waiting for your decision."


# --------------------------------------------------------------------------
# Addressing
# --------------------------------------------------------------------------


def _director_role(day: WorkDay):  # type: ignore[no-untyped-def]
    return day.organization.settings.finance_director_role


def _holds(user, role) -> bool:  # type: ignore[no-untyped-def]
    return role is not None and user.user_roles.filter(role_id=role.pk).exists()


def _role_has_approver(day: WorkDay, role, excluded: set[int]) -> bool:  # type: ignore[no-untyped-def]
    """Is there an active holder of ``role`` who is allowed to decide this?"""
    if role is None:
        return False
    return (
        User.objects.filter(
            organization_id=day.organization_id,
            is_active=True,
            user_roles__role_id=role.pk,
        )
        .exclude(pk__in=excluded)
        .exists()
    )


def addressee_of(session: WorkSession, day: WorkDay) -> engine.WorkDaySlice | None:
    """Who answers ``session`` (§4.18.5), or ``None`` when nobody can.

    The active manager of its project; the Director role when there is no
    project or no active manager. A manager's own sessions, a Director's own,
    and anything the Director added go to the Director role, and never to the
    person or to whoever added the session (an add cannot approve itself).
    """
    person = day.person
    director = _director_role(day)
    excluded = {person.pk}
    if session.added_by_id is not None:
        excluded.add(session.added_by_id)

    project = session.project
    manager = project.manager if project is not None else None
    director_own = _holds(person, director)
    use_manager = (
        manager is not None
        and manager.is_active
        and manager.pk not in excluded
        and not director_own
    )
    if use_manager:
        return engine.WorkDaySlice(user=manager)
    if _role_has_approver(day, director, excluded):
        return engine.WorkDaySlice(role=director)
    return None


def _slice_key(target: engine.WorkDaySlice) -> tuple[str, int]:
    if target.user is not None:
        return ("user", int(target.user.pk))  # type: ignore[attr-defined]
    assert target.role is not None
    return ("role", target.role.pk)


def _matching_open_request(day: WorkDay, target: engine.WorkDaySlice) -> ApprovalRequest | None:
    open_requests = ApprovalRequest.objects.filter(
        document_type=engine.document_type_of(day),
        document_id=str(day.pk),
        status__in=OPEN_STATUSES,
    )
    if target.user is not None:
        return open_requests.filter(required_user_id=_slice_key(target)[1]).order_by("id").first()
    return (
        open_requests.filter(required_user__isnull=True, required_role_id=_slice_key(target)[1])
        .order_by("id")
        .first()
    )


# --------------------------------------------------------------------------
# route_day
# --------------------------------------------------------------------------


def route_day(day: WorkDay, *, now: datetime | None = None) -> list[ApprovalRequest]:
    """Send a day's unrouted sessions to their approvers; returns the new requests.

    Idempotent: a session already on a request is left alone, and calling it
    twice changes nothing. Unrouted sessions are grouped by addressee; an open
    request for the same addressee is reused, a decided one never is (a late
    session then opens a fresh slice for that addressee and touches no other).
    Open sessions are not routed until they close. The day becomes ``PENDING``.
    """
    now = now or timezone.now()
    with transaction.atomic():
        day = WorkDay.objects.select_for_update().select_related("person").get(pk=day.pk)
        sessions = list(
            day.sessions.select_related("project__manager", "site", "location")
            .filter(approval_request__isnull=True, clock_out_at__isnull=False)
            .order_by("clock_in_at", "pk")
        )

        groups: dict[tuple[str, int], list[WorkSession]] = {}
        targets: dict[tuple[str, int], engine.WorkDaySlice] = {}
        unrouted: list[WorkSession] = []
        for session in sessions:
            target = addressee_of(session, day)
            if target is None:
                unrouted.append(session)
                continue
            key = _slice_key(target)
            groups.setdefault(key, []).append(session)
            targets[key] = target

        created: list[ApprovalRequest] = []
        for key, members in groups.items():
            request = _matching_open_request(day, targets[key])
            if request is None:
                (request,) = engine.create_requests(
                    day, requested_by=day.person, work_day_slice=targets[key]
                )
                created.append(request)
            for session in members:
                session.approval_request = request
                session.save()

        if unrouted:
            _notify_unrouted(day, unrouted)

        if day.formed_at is None:
            day.formed_at = now
            day.save(update_fields=["formed_at", "updated_at"])
        refresh_status(day)

        for request in created:
            notify_awaiting(day, request)
    return created


def refresh_status(day: WorkDay) -> str:
    """Project the day's slices onto ``WorkDay.status`` (§4.18.2).

    Any slice still open (or any session no request covers yet) keeps the day
    ``PENDING``; once all are decided it is ``REJECTED`` if any slice was, else
    ``APPROVED``. The slices are the requests the day's sessions point at now,
    so a reopened slice replaces the rejected one it came from.
    """
    sessions = day.sessions.all()
    statuses = set(
        ApprovalRequest.objects.filter(
            pk__in=sessions.exclude(approval_request__isnull=True).values("approval_request_id")
        ).values_list("status", flat=True)
    )
    if statuses & set(OPEN_STATUSES) or sessions.filter(approval_request__isnull=True).exists():
        status = WorkDayStatus.PENDING
    elif ApprovalRequestStatus.REJECTED in statuses:
        status = WorkDayStatus.REJECTED
    elif ApprovalRequestStatus.APPROVED in statuses:
        status = WorkDayStatus.APPROVED
    else:
        return day.status
    if day.status != status:
        day.status = status
        day.save(update_fields=["status", "updated_at"])
    return str(status)


# --------------------------------------------------------------------------
# decide
# --------------------------------------------------------------------------


def decide(
    day: WorkDay,
    actor,  # type: ignore[no-untyped-def]
    approved: bool,
    reason: str = "",
    *,
    request=None,  # type: ignore[no-untyped-def]
) -> WorkDay:
    """Decide every open slice of ``day`` addressed to ``actor`` (§4.18.5, R13).

    One decision answers all of the actor's slices. Slices addressed to
    somebody else stay open, so the day is ``PENDING`` until every manager has
    answered. A rejection needs a ``reason``. Refused for the day's person and
    for whoever added a session on it (``WORK_DAY_SELF_APPROVAL``).
    """
    reason = (reason or "").strip()
    with transaction.atomic():
        day = WorkDay.objects.select_for_update().select_related("person").get(pk=day.pk)
        if day.person_id == actor.pk or day.sessions.filter(
            added_by_id=actor.pk, approval_request__status__in=OPEN_STATUSES
        ).exists():
            raise WorkDaySelfApproval()
        if not approved and not reason:
            raise RejectionReasonRequired()

        open_requests = list(
            ApprovalRequest.objects.select_for_update()
            .filter(
                document_type=engine.document_type_of(day),
                document_id=str(day.pk),
                status__in=OPEN_STATUSES,
            )
            .order_by("level", "id")
        )
        if not open_requests:
            raise WorkDayNotDecidable()
        mine = [item for item in open_requests if engine.can_approve(actor, item, document=day)[0]]
        if not mine:
            raise engine.NotAnApprover("This day is waiting on someone else.")

        decision = ApprovalDecision.APPROVED if approved else ApprovalDecision.REJECTED
        for slice_request in mine:
            decided, _ = engine.record_decision(
                day,
                actor=actor,
                decision=decision,
                reason=reason,
                approval_request=slice_request,
                ip=client_ip(request) if request is not None else None,
                user_agent=(request.META.get("HTTP_USER_AGENT") or "")[:400]
                if request is not None
                else "",
            )
            record(
                AuditAction.APPROVED if approved else AuditAction.REJECTED,
                actor=actor,
                organization=day.organization_id,
                target=day,
                target_label=str(day),
                request=request,
                note=("Approved" if approved else f"Rejected: {reason}")
                + f" (slice {decided.pk}).",
            )
            if not approved:
                _notify_rejected(day, decided, actor, reason)
        refresh_status(day)
    return day


# --------------------------------------------------------------------------
# Notifications (§4.18.10)
# --------------------------------------------------------------------------


def slice_payload(day: WorkDay, request: ApprovalRequest | None) -> dict[str, Any]:
    """What an approver or the person needs to read about one slice."""
    from attendance import corrections

    sessions = list(
        day.sessions.filter(approval_request=request).select_related("site", "location")
        if request is not None
        else day.sessions.all()
    )
    hours = corrections.hours_of(sessions)
    person = day.person.full_name or day.person.email
    flags: list[str] = []
    for session in sessions:
        for flag in corrections.session_flags(session):
            if flag not in flags:
                flags.append(flag)
    return {
        "label": f"{person} · {day.date.isoformat()}",
        "person": person,
        "person_id": day.person_id,
        "date": day.date.isoformat(),
        "date_display": _display(day),
        "hours": str(hours),
        "hours_display": f"{hours:.1f}".rstrip("0").rstrip(".") + " h",
        "sessions": len(sessions),
        "flags": flags,
        "corrected": "corrected" in flags,
        "approval_request_id": request.pk if request is not None else None,
    }


def _display(day: WorkDay) -> str:
    return f"{day.date:%a} {day.date.day} {day.date:%b %Y}"


def notify_awaiting(day: WorkDay, request: ApprovalRequest) -> None:
    """Tell the slice's approver(s); the payload names the request so only they hear."""
    emit(Event.ATTENDANCE_AWAITING_APPROVAL, day, payload=slice_payload(day, request))


def _notify_rejected(day: WorkDay, request: ApprovalRequest, actor, reason: str) -> None:  # type: ignore[no-untyped-def]
    payload = slice_payload(day, request)
    payload.update(reason=reason, approver=actor.full_name or actor.email)
    emit(Event.ATTENDANCE_REJECTED, day, payload=payload)


def _notify_unrouted(day: WorkDay, sessions: list[WorkSession]) -> None:
    """Tell the owner once per day that nobody can approve (§4.18.5)."""
    if NotificationEvent.objects.filter(
        event_key=Event.ATTENDANCE_UNROUTED,
        target_type=day._meta.label,
        target_id=str(day.pk),
    ).exists():
        return
    payload = slice_payload(day, None)
    payload["sessions"] = len(sessions)
    emit(Event.ATTENDANCE_UNROUTED, day, payload=payload)
