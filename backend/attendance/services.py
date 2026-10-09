"""Clock-in and clock-out (design §4.18.3-§4.18.5; R13).

Every clock-in and clock-out goes through here, online or replayed from a phone
(R6), so the area check, the time bounds and the one-open-session rule run once,
in one place. Hours are never stored: this module only records the two moments
and where the phone was.

Day routing and auto-close are separate (T16.6); corrections are T16.7.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from accounts.models import User
from approvals.models import ApprovalRequestStatus
from attendance.models import ClosedBy, WorkDay, WorkSession
from commercials.finance import ProjectAmbiguous, open_projects_of, resolve_project
from core.audit import record
from core.exceptions import DomainError
from core.geo import NO_FIX, TOO_VAGUE, check_area
from core.models import AuditAction
from locations.models import Location, LocationType
from network.models import Site, SiteStatus

#: A phone's clock may run a little fast; further ahead than this is a wrong clock.
MAX_AHEAD = timedelta(minutes=5)
#: Older than this and the person should correct the day instead (§4.18.6).
MAX_AGE = timedelta(hours=72)
#: Entries kept in ``area_history`` (§4.18.2).
AREA_HISTORY_LIMIT = 10

_CLOCKABLE_LOCATION_TYPES = (LocationType.YARD, LocationType.OFFICE)

# --------------------------------------------------------------------------
# Errors (§4.18.12). The code is the contract; the message is for a person.
# --------------------------------------------------------------------------


class ClockLocationRequired(DomainError):
    code = "CLOCK_LOCATION_REQUIRED"
    status_code = 400
    default_message = "Turn location on to clock in."


class ClockLocationTooVague(DomainError):
    code = "CLOCK_LOCATION_TOO_VAGUE"
    status_code = 400
    default_message = "Your phone's location is not accurate enough. Move outside and try again."


class ClockOutsideArea(DomainError):
    code = "CLOCK_OUTSIDE_AREA"
    status_code = 409
    default_message = "You are not at this place."


class ClockPlaceRequired(DomainError):
    code = "CLOCK_PLACE_REQUIRED"
    status_code = 400
    default_message = "Choose one place: a site or a location."


class PlaceHasNoCoordinates(DomainError):
    code = "PLACE_HAS_NO_COORDINATES"
    status_code = 409
    default_message = "This place has no coordinates yet, so nobody can clock in there."


class PlaceNotAvailable(DomainError):
    code = "PLACE_NOT_AVAILABLE"
    status_code = 409
    default_message = "You cannot clock in at this place."


class ClockTimeInvalid(DomainError):
    code = "CLOCK_TIME_INVALID"
    status_code = 400
    default_message = "That time cannot be used."


class ClockOverlap(DomainError):
    code = "CLOCK_OVERLAP"
    status_code = 409
    default_message = "That overlaps another session of yours."


class ClockNotClockedIn(DomainError):
    code = "CLOCK_NOT_CLOCKED_IN"
    status_code = 409
    default_message = "You are not clocked in."


class ClockSessionLocked(DomainError):
    code = "CLOCK_SESSION_LOCKED"
    status_code = 409
    default_message = "This session has already been decided and can no longer change."


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _lock_person(person) -> None:  # type: ignore[no-untyped-def]
    """Serialise one person's clock events (the rows they read may not exist yet)."""
    User.objects.select_for_update().filter(pk=person.pk).first()


def _coord(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def _local_date(person, at: datetime):  # type: ignore[no-untyped-def]
    zone = ZoneInfo(person.organization.settings.timezone)
    return at.astimezone(zone).date()


def _current_area(place) -> dict[str, float]:  # type: ignore[no-untyped-def]
    return {
        "lat": float(place.latitude),
        "lng": float(place.longitude),
        "radius_m": float(place.radius_m),
    }


def _check_clockable(place) -> None:  # type: ignore[no-untyped-def]
    """§4.18.3: active, a clockable kind, and with coordinates."""
    if isinstance(place, Site):
        available = place.status != SiteStatus.DECOMMISSIONED
    else:
        available = (
            place.is_active and not place.is_system and place.type in _CLOCKABLE_LOCATION_TYPES
        )
    if not available:
        raise PlaceNotAvailable(f"You cannot clock in at {place}.", details={"place": str(place)})
    if place.latitude is None or place.longitude is None:
        raise PlaceHasNoCoordinates(
            f"{place} has no coordinates yet. Ask an administrator to set them.",
            details={"place": str(place)},
        )


def _aware(at: Any) -> datetime:
    if isinstance(at, str):
        at = parse_datetime(at)
    if not isinstance(at, datetime) or timezone.is_naive(at):
        raise ClockTimeInvalid(
            "The time is missing or has no time zone.", field_errors={"at": ["Invalid time."]}
        )
    return at


def _area_of(raw: Mapping[str, Any] | None) -> dict[str, float] | None:
    """The phone's ``place_area``; it names the radius ``radius`` (queued.ts) or ``radius_m``."""
    if not isinstance(raw, Mapping):
        return None
    try:
        radius = raw["radius_m"] if "radius_m" in raw else raw["radius"]
        return {"lat": float(raw["lat"]), "lng": float(raw["lng"]), "radius_m": float(radius)}
    except (KeyError, TypeError, ValueError):
        return None


def _same_area(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    return (
        abs(float(a["lat"]) - float(b["lat"])) < 1e-6
        and abs(float(a["lng"]) - float(b["lng"])) < 1e-6
        and abs(float(a["radius_m"]) - float(b["radius_m"])) < 0.5
    )


def _held_at(place, claimed: Mapping[str, float], at: datetime) -> bool:  # type: ignore[no-untyped-def]
    """Did ``place`` really have ``claimed`` as its area at ``at``? (§4.18.4 step 2)

    The current area, or a history entry still valid at ``at``. A phone cannot
    name an area the place never had.
    """
    if _same_area(claimed, _current_area(place)):
        return True
    for entry in place.area_history or []:
        try:
            valid_until = parse_datetime(str(entry["valid_until"]))
            if valid_until is None:
                continue
            if timezone.is_naive(valid_until):
                valid_until = valid_until.replace(tzinfo=UTC)
            if _same_area(claimed, entry) and valid_until >= at:
                return True
        except (KeyError, TypeError, ValueError):
            continue
    return False


def record_area_change(place, *, previous: Mapping[str, Any], at: datetime | None = None) -> None:  # type: ignore[no-untyped-def]
    """Push the area ``place`` just left onto its ``area_history`` (§4.18.2).

    ``previous`` is ``{lat, lng, radius_m}`` as it was before the edit; ``at`` is
    when it stopped being current (default now). Newest first, at most 10.
    Saves only ``area_history``. Call it from whatever changes the area.
    """
    if previous.get("lat") is None or previous.get("lng") is None:
        return  # a place with no coordinates before has no area to remember
    when = at or timezone.now()
    entry = {
        "lat": float(previous["lat"]),
        "lng": float(previous["lng"]),
        "radius_m": int(previous["radius_m"]),
        "valid_until": when.isoformat(),
    }
    place.area_history = [entry, *(place.area_history or [])][:AREA_HISTORY_LIMIT]
    place.save(update_fields=["area_history", "updated_at"])


def _audit(action, session, *, actor, note: str, request=None) -> None:  # type: ignore[no-untyped-def]
    record(
        action,
        actor=actor,
        organization=session.organization_id,
        target=session,
        target_label=str(session),
        request=request,
        note=note,
    )


# --------------------------------------------------------------------------
# Clock-in
# --------------------------------------------------------------------------


def clock_in(
    *,
    person,  # type: ignore[no-untyped-def]
    at: Any,
    fix: Mapping[str, Any] | None,
    site=None,  # type: ignore[no-untyped-def]
    location=None,  # type: ignore[no-untyped-def]
    project=None,  # type: ignore[no-untyped-def]
    client_uuid: uuid.UUID | None = None,
    place_area: Mapping[str, Any] | None = None,
    request=None,  # type: ignore[no-untyped-def]
) -> WorkSession:
    """Start a session at one place (§4.18.5). Idempotent on ``client_uuid`` (R6).

    ``place_area`` is sent only by a replayed capture; with it the phone's own
    check can stand when the place's area changed in the meantime (§4.18.4).
    Raises the ``CLOCK_*`` / ``PLACE_*`` / ``PROJECT_AMBIGUOUS`` errors.
    """
    if (site is None) == (location is None):
        raise ClockPlaceRequired()
    place = site if site is not None else location
    at = _aware(at)

    existing = _existing_in(client_uuid)
    if existing is not None:
        return existing

    try:
        with transaction.atomic():
            _lock_person(person)
            existing = _existing_in(client_uuid)
            if existing is not None:
                return existing
            return _clock_in_locked(
                person, place, site, project, at, fix, client_uuid, place_area, request
            )
    except IntegrityError:
        replay = _existing_in(client_uuid)
        if replay is not None:
            return replay
        raise ClockOverlap("You are already clocked in. Refresh and try again.") from None


def _existing_in(client_uuid):  # type: ignore[no-untyped-def]
    if client_uuid is None:
        return None
    return WorkSession.objects.filter(in_client_uuid=client_uuid).first()


def _clock_in_locked(  # type: ignore[no-untyped-def]
    person, place, site, project, at, fix, client_uuid, place_area, request
) -> WorkSession:
    now = timezone.now()
    _check_clockable(place)

    if at > now + MAX_AHEAD:
        raise ClockTimeInvalid("That time is in the future. Check your phone's clock.")
    if at < now - MAX_AGE:
        raise ClockTimeInvalid("That is more than 72 hours ago. Correct the day instead.")

    if (
        WorkSession.objects.filter(person=person)
        .filter(Q(clock_out_at__gt=at) | Q(clock_out_at__isnull=True, clock_in_at__gt=at))
        .exists()
    ):
        raise ClockOverlap()

    area_checked, distance, changed = _check_position(place, fix, place_area, at, person)
    chosen = _resolve_project(site, project)

    open_session = (
        WorkSession.objects.select_for_update()
        .filter(person=person, clock_out_at__isnull=True)
        .first()
    )
    if open_session is not None:
        open_session.clock_out_at = at
        open_session.clock_out_received_at = now
        open_session.closed_by = ClosedBy.NEXT_CLOCK_IN
        open_session.save()
        _audit(
            AuditAction.STATUS_CHANGED,
            open_session,
            actor=person,
            request=request,
            note="Closed by the next clock-in.",
        )

    local_date = _local_date(person, at)
    work_day, _ = WorkDay.objects.get_or_create(
        organization_id=person.organization_id, person=person, date=local_date
    )
    fix_data = fix or {}
    session = WorkSession.objects.create(
        organization_id=person.organization_id,
        person=person,
        site=place if isinstance(place, Site) else None,
        location=place if isinstance(place, Location) else None,
        project=chosen,
        work_day=work_day,
        local_date=local_date,
        clock_in_at=at,
        clock_in_received_at=now,
        in_lat=_coord(fix_data.get("lat")),
        in_lng=_coord(fix_data.get("lng")),
        in_accuracy_m=fix_data.get("accuracy_m"),
        in_distance_m=distance,
        in_client_uuid=client_uuid,
        in_checked_area=area_checked,
        area_changed=changed,
    )
    _audit(
        AuditAction.DOCUMENT_POSTED,
        session,
        actor=person,
        request=request,
        note=f"Clocked in at {place}"
        + (f" on {chosen}" if chosen else "")
        + (" (area changed since the phone checked)" if changed else "")
        + ".",
    )
    return session


def _check_position(place, fix, place_area, at, person):  # type: ignore[no-untyped-def]
    """The §4.18.4 decision: returns ``(in_checked_area, distance_m, area_changed)``."""
    cap = person.organization.settings.clock_accuracy_cap_m
    current = _current_area(place)
    result = check_area(fix, current, cap)
    if result.problem == NO_FIX:
        raise ClockLocationRequired()
    if result.problem == TOO_VAGUE:
        raise ClockLocationTooVague(
            details={"accuracy_m": (fix or {}).get("accuracy_m"), "cap_m": cap}
        )
    if result.inside:
        return None, result.distance_m, False

    claimed = _area_of(place_area)
    if claimed is not None and _held_at(place, claimed, at):
        replay = check_area(fix, claimed, cap)
        if replay.inside:
            return claimed, replay.distance_m, not _same_area(claimed, current)

    metres = round(result.distance_m or 0)
    raise ClockOutsideArea(
        f"You are about {metres} m from {place}.",
        details={"distance_m": metres, "radius_m": int(current["radius_m"]), "place": str(place)},
    )


def _resolve_project(site, project):  # type: ignore[no-untyped-def]
    """§4.18.5 step 5: one open project is taken, none is null, several need a choice."""
    if site is None:
        return None
    if project is not None:
        return resolve_project(site, project)
    open_projects = open_projects_of(site)
    if len(open_projects) == 1:
        return open_projects[0]
    if len(open_projects) > 1:
        raise ProjectAmbiguous(
            f"{site} is on {len(open_projects)} open projects. Choose which one.",
            details={"candidates": [{"id": p.pk, "reference": p.reference} for p in open_projects]},
        )
    return None


# --------------------------------------------------------------------------
# Clock-out
# --------------------------------------------------------------------------


def clock_out(
    *,
    person,  # type: ignore[no-untyped-def]
    at: Any,
    fix: Mapping[str, Any] | None = None,
    client_uuid: uuid.UUID | None = None,
    session_client_uuid: str | uuid.UUID | None = None,
    place_area: Mapping[str, Any] | None = None,
    request=None,  # type: ignore[no-untyped-def]
) -> WorkSession:
    """End a session (§4.18.5). Never refused on position (R13); idempotent on ``client_uuid``.

    The session is the one ``session_client_uuid`` names (the clock-in's uuid, or
    the session id as text), else the person's open one. A replayed clock-out
    earlier than an ``AUTO`` / ``NEXT_CLOCK_IN`` close replaces it while its
    slice is undecided. ``place_area`` is accepted for payload parity and not
    used: a clock-out only records the distance.
    """
    at = _aware(at)
    existing = _existing_out(client_uuid)
    if existing is not None:
        return existing

    try:
        with transaction.atomic():
            _lock_person(person)
            existing = _existing_out(client_uuid)
            if existing is not None:
                return existing
            return _clock_out_locked(person, at, fix, client_uuid, session_client_uuid, request)
    except IntegrityError:
        replay = _existing_out(client_uuid)
        if replay is not None:
            return replay
        raise


def _existing_out(client_uuid):  # type: ignore[no-untyped-def]
    if client_uuid is None:
        return None
    return WorkSession.objects.filter(out_client_uuid=client_uuid).first()


def _find_session(person, session_client_uuid):  # type: ignore[no-untyped-def]
    sessions = WorkSession.objects.select_for_update().filter(person=person)
    if session_client_uuid in (None, ""):
        return sessions.filter(clock_out_at__isnull=True).first()
    text = str(session_client_uuid)
    try:
        return sessions.filter(in_client_uuid=uuid.UUID(text)).first()
    except ValueError:
        pass
    if text.isdigit():
        return sessions.filter(pk=int(text)).first()
    return None


def _clock_out_locked(person, at, fix, client_uuid, session_client_uuid, request):  # type: ignore[no-untyped-def]
    now = timezone.now()
    session = _find_session(person, session_client_uuid)
    if session is None:
        raise ClockNotClockedIn()
    if at > now + MAX_AHEAD:
        raise ClockTimeInvalid("That time is in the future. Check your phone's clock.")
    if at < session.clock_in_at:
        raise ClockTimeInvalid("You cannot clock out before you clocked in.")

    overriding = False
    if session.clock_out_at is not None:
        if session.closed_by not in (ClosedBy.AUTO, ClosedBy.NEXT_CLOCK_IN):
            raise ClockNotClockedIn("This session was already ended.")
        if at >= session.clock_out_at:
            return session  # nothing earlier to recover
        status = session.approval_request.status if session.approval_request_id else None
        if status in (ApprovalRequestStatus.APPROVED, ApprovalRequestStatus.REJECTED):
            raise ClockSessionLocked()
        overriding = True

    place = session.site or session.location
    cap = person.organization.settings.clock_accuracy_cap_m
    result = check_area(fix, _current_area(place), cap)
    fix_data = fix or {}
    positioned = result.distance_m is not None
    session.clock_out_at = at
    session.clock_out_received_at = now
    session.closed_by = ClosedBy.PERSON
    session.out_client_uuid = client_uuid
    session.out_lat = _coord(fix_data.get("lat")) if positioned else None
    session.out_lng = _coord(fix_data.get("lng")) if positioned else None
    session.out_accuracy_m = fix_data.get("accuracy_m") if positioned else None
    session.out_distance_m = result.distance_m
    session.save()
    _audit(
        AuditAction.STATUS_CHANGED,
        session,
        actor=person,
        request=request,
        note="Clocked out"
        + (", replacing an automatic close" if overriding else "")
        + ("" if positioned else ", with no position")
        + ".",
    )
    return session
