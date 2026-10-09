"""The area check for clock-in (design §4.18.4; R13).

Pure arithmetic: no Django, no database. The TypeScript twin is
``frontend/src/features/attendance/area.ts``; both read ``shared/area-cases.json``
so the phone and the server cannot disagree about who is inside.

A *fix* is ``{"lat", "lng", "accuracy_m"}`` and a *place* is
``{"lat", "lng", "radius_m"}``. The accuracy allowance (``inside = distance <=
radius + accuracy``) lets a real phone indoors pass; the cap stops a bad fix
doing the same.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

EARTH_RADIUS_M = 6_371_008.8

NO_FIX = "NO_FIX"
TOO_VAGUE = "TOO_VAGUE"


@dataclass(frozen=True)
class AreaResult:
    """``distance_m`` is None only when there is no usable fix."""

    distance_m: float | None
    inside: bool
    problem: str | None


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres between two points in degrees."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lng2 - lng1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def check_area(fix: Mapping[str, Any] | None, place: Mapping[str, Any], cap_m: float) -> AreaResult:
    """Is ``fix`` inside ``place``? NO_FIX, TOO_VAGUE, or distance and ``inside``."""
    if not fix:
        return AreaResult(None, False, NO_FIX)
    lat = _number(fix.get("lat"))
    lng = _number(fix.get("lng"))
    accuracy = _number(fix.get("accuracy_m"))
    if lat is None or lng is None or accuracy is None or accuracy < 0:
        return AreaResult(None, False, NO_FIX)

    distance = haversine_m(lat, lng, float(place["lat"]), float(place["lng"]))
    if accuracy > cap_m:
        return AreaResult(distance, False, TOO_VAGUE)
    return AreaResult(distance, distance <= float(place["radius_m"]) + accuracy, None)


class CoordinatesMixin:
    """Validation for a serializer that carries ``latitude`` and ``longitude``.

    Mix in *before* ``serializers.ModelSerializer``. Both-or-neither and the
    ranges always apply; ``coordinates_required`` (a bool, or a callable taking
    the merged attrs) additionally demands them (``COORDINATES_REQUIRED``).
    Not yet wired into the site and location serializers.
    """

    coordinates_required: Any = False
    instance: Any = None

    def _coordinates_needed(self, attrs: Mapping[str, Any]) -> bool:
        flag = self.coordinates_required
        return bool(flag(attrs)) if callable(flag) else bool(flag)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        from rest_framework import serializers

        parent = super()
        checked: dict[str, Any] = parent.validate(attrs) if hasattr(parent, "validate") else attrs

        def merged(name: str) -> Any:
            if name in checked:
                return checked[name]
            return getattr(self.instance, name, None) if self.instance is not None else None

        lat, lng = merged("latitude"), merged("longitude")
        errors: dict[str, list[str]] = {}
        if lat is not None and not -90 <= float(lat) <= 90:
            errors["latitude"] = ["Latitude must be between -90 and 90."]
        if lng is not None and not -180 <= float(lng) <= 180:
            errors["longitude"] = ["Longitude must be between -180 and 180."]
        if not errors:
            if (lat is None) != (lng is None):
                missing = "longitude" if lng is None else "latitude"
                errors[missing] = ["Give both latitude and longitude, or neither."]
            elif lat is None and self._coordinates_needed(checked):
                message = ["COORDINATES_REQUIRED: set the latitude and longitude."]
                errors = {"latitude": message, "longitude": message}
        if errors:
            raise serializers.ValidationError(errors)
        return checked
