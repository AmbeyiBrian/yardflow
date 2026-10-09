"""Shared place behaviour: the area history on edit (design §4.18.2, §4.18.4; R13).

``Site`` and ``Location`` both keep an ``area_history``. Whoever edits a place's
area through the API goes through this mixin, so the area an offline phone may
legitimately still hold is remembered no matter which screen changed it.
"""

from __future__ import annotations

from typing import Any

from attendance.services import record_area_change

_AREA_FIELDS = ("latitude", "longitude", "radius_m")


def _snapshot(place) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    return {
        "lat": place.latitude,
        "lng": place.longitude,
        "radius_m": place.radius_m,
    }


def _same(a: dict[str, Any], b: dict[str, Any]) -> bool:
    def norm(value: Any) -> float | None:
        return None if value is None else float(value)

    return all(norm(a[key]) == norm(b[key]) for key in ("lat", "lng", "radius_m"))


def coordinates_needed(serializer, rule: bool) -> bool:  # type: ignore[no-untyped-def]
    """Whether a save through ``serializer`` must carry coordinates (§4.18.8).

    ``rule`` is "this kind of place needs them". A new place always does. An
    existing one without them keeps working for any edit that leaves the area
    and type alone (existing sites simply cannot be clocked in at); touching the
    coordinates or type, or already having coordinates, brings the rule in.
    """
    if not rule:
        return False
    instance = serializer.instance
    if instance is None:
        return True
    if instance.latitude is not None:
        return True
    data = getattr(serializer, "initial_data", None) or {}
    return any(name in data for name in ("latitude", "longitude", "type"))


class AreaHistoryMixin:
    """For a ``ModelViewSet`` over a place: remember the area an update leaves."""

    def perform_update(self, serializer):  # type: ignore[no-untyped-def]
        place = serializer.instance
        previous = _snapshot(place)
        touched = any(name in serializer.validated_data for name in _AREA_FIELDS)
        super().perform_update(serializer)  # type: ignore[misc]
        if touched:
            place.refresh_from_db()
            if not _same(previous, _snapshot(place)):
                record_area_change(place, previous=previous)
