"""The area check and CoordinatesMixin (design §4.18.4; R13).

The cases live in ``shared/area-cases.json`` and are also read by the Vitest
twin, so the server and the phone cannot drift.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from rest_framework import serializers

from core.geo import CoordinatesMixin, check_area, haversine_m

CASES_FILE = Path(__file__).resolve().parents[3] / "shared" / "area-cases.json"
CASES = json.loads(CASES_FILE.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_shared_area_cases(case):
    result = check_area(case["fix"], case["place"], case["cap_m"])
    expected = case["expected"]

    assert result.inside is expected["inside"]
    assert result.problem == expected["problem"]
    if expected["distance_m"] is None:
        assert result.distance_m is None
    else:
        assert result.distance_m == pytest.approx(expected["distance_m"], abs=0.01)


def test_haversine_one_degree_of_latitude():
    assert haversine_m(0, 0, 1, 0) == pytest.approx(111_195, abs=5)


def test_haversine_is_symmetric_and_zero_at_a_point():
    assert haversine_m(10, 20, 10, 20) == 0
    assert haversine_m(-1.29, 36.82, 0.5, -170) == pytest.approx(
        haversine_m(0.5, -170, -1.29, 36.82)
    )


class PlaceSerializer(CoordinatesMixin, serializers.Serializer):
    name = serializers.CharField(required=False)
    latitude = serializers.DecimalField(
        max_digits=9, decimal_places=6, required=False, allow_null=True
    )
    longitude = serializers.DecimalField(
        max_digits=9, decimal_places=6, required=False, allow_null=True
    )


class RequiredPlaceSerializer(PlaceSerializer):
    coordinates_required = True


class _Obj:
    latitude = 1
    longitude = 2


def errors_of(serializer_class, data):
    serializer = serializer_class(data=data)
    assert not serializer.is_valid()
    return serializer.errors


class TestCoordinatesMixin:
    def test_valid_pair_passes(self):
        serializer = PlaceSerializer(data={"latitude": "-1.29", "longitude": "36.82"})
        assert serializer.is_valid(), serializer.errors

    def test_neither_passes_when_not_required(self):
        assert PlaceSerializer(data={"name": "x"}).is_valid()

    def test_both_null_passes_when_not_required(self):
        assert PlaceSerializer(data={"latitude": None, "longitude": None}).is_valid()

    @pytest.mark.parametrize(
        "data, field",
        [
            ({"latitude": "90.5", "longitude": "0"}, "latitude"),
            ({"latitude": "-90.5", "longitude": "0"}, "latitude"),
            ({"latitude": "0", "longitude": "180.5"}, "longitude"),
            ({"latitude": "0", "longitude": "-180.5"}, "longitude"),
        ],
    )
    def test_out_of_range_is_refused(self, data, field):
        assert field in errors_of(PlaceSerializer, data)

    @pytest.mark.parametrize(
        "data",
        [
            {"latitude": "90", "longitude": "180"},
            {"latitude": "-90", "longitude": "-180"},
        ],
    )
    def test_the_limits_themselves_pass(self, data):
        assert PlaceSerializer(data=data).is_valid()

    def test_one_without_the_other_is_refused(self):
        assert "longitude" in errors_of(PlaceSerializer, {"latitude": "1"})
        assert "latitude" in errors_of(PlaceSerializer, {"longitude": "1"})

    def test_required_refuses_neither(self):
        errors = errors_of(RequiredPlaceSerializer, {"name": "x"})
        assert "COORDINATES_REQUIRED" in str(errors["latitude"][0])
        assert "longitude" in errors

    def test_required_accepts_a_pair(self):
        assert RequiredPlaceSerializer(data={"latitude": "1", "longitude": "2"}).is_valid()

    def test_required_can_be_a_callable_on_the_attrs(self):
        class System(PlaceSerializer):
            coordinates_required = staticmethod(lambda attrs: attrs.get("name") != "system")

        assert System(data={"name": "system"}).is_valid()
        assert not System(data={"name": "yard"}).is_valid()

    def test_partial_update_uses_the_instance_for_the_other_half(self):
        serializer = RequiredPlaceSerializer(_Obj(), data={"name": "renamed"}, partial=True)
        assert serializer.is_valid(), serializer.errors
        serializer = RequiredPlaceSerializer(_Obj(), data={"latitude": "95"}, partial=True)
        assert not serializer.is_valid()
