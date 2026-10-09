"""T16.8 (part 1) — coordinates are required through the API (§4.18.8; R13)."""

from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from accounts.factories import RoleFactory, UserFactory, UserRoleFactory
from accounts.permissions_registry import PERM
from locations.factories import OfficeFactory, YardFactory
from locations.models import Location, LocationType
from network.factories import ClientFactory, SiteFactory
from network.models import Site

pytestmark = pytest.mark.django_db

COORDS = {"latitude": "-1.300000", "longitude": "36.800000"}


@pytest.fixture
def http(tenant, settings):
    settings.TENANT_BASE_DOMAIN = "localhost"
    role = RoleFactory(name="Catalogue", codenames=[PERM.CATALOGUE_MANAGE])
    user = UserFactory(organization=tenant)
    UserRoleFactory(user=user, role=role)
    client = APIClient(HTTP_HOST="silvertech.localhost")
    client.force_authenticate(user)
    return client


def new_site(http, **extra):
    client = ClientFactory()
    body = {"client": client.pk, "internal_ref": "NEW-1", "name": "New site", **extra}
    return http.post("/api/v1/sites", body, format="json")


def refused(reply):
    assert reply.status_code == 400, reply.content
    error = reply.json()["error"]
    assert error["code"] == "COORDINATES_REQUIRED"
    assert set(error["field_errors"]) >= {"latitude", "longitude"}


class TestSites:
    def test_create_without_coordinates_is_refused(self, http):
        refused(new_site(http))
        assert not Site.objects.filter(internal_ref="NEW-1").exists()

    def test_create_with_coordinates_works_and_reports_them(self, http):
        reply = new_site(http, **COORDS)
        assert reply.status_code == 201, reply.content
        assert reply.json()["has_coordinates"] is True
        assert reply.json()["radius_m"] == 200

    def test_half_a_pair_is_refused(self, http):
        reply = new_site(http, latitude="-1.3")
        assert reply.status_code == 400
        assert "longitude" in reply.json()["error"]["field_errors"]

    def test_out_of_range_is_refused(self, http):
        assert new_site(http, latitude="91", longitude="0").status_code == 400

    def test_update_cannot_remove_coordinates(self, http):
        site = SiteFactory()
        refused(
            http.patch(
                f"/api/v1/sites/{site.pk}", {"latitude": None, "longitude": None}, format="json"
            )
        )
        site.refresh_from_db()
        assert site.latitude is not None

    def test_an_old_site_without_coordinates_is_still_editable(self, http):
        site = SiteFactory(latitude=None, longitude=None)
        reply = http.patch(f"/api/v1/sites/{site.pk}", {"notes": "hi"}, format="json")
        assert reply.status_code == 200
        assert reply.json()["has_coordinates"] is False

    def test_giving_an_old_site_its_coordinates_works(self, http):
        site = SiteFactory(latitude=None, longitude=None)
        reply = http.patch(f"/api/v1/sites/{site.pk}", COORDS, format="json")
        assert reply.status_code == 200
        assert reply.json()["has_coordinates"] is True
        site.refresh_from_db()
        # Nothing to remember: it had no area before.
        assert site.area_history == []

    def test_missing_coordinates_filter(self, http):
        bare = SiteFactory(latitude=None, longitude=None)
        placed = SiteFactory()
        ids = {
            r["id"]
            for r in http.get("/api/v1/sites", {"missing_coordinates": "true"}).json()["results"]
        }
        assert ids == {bare.pk}
        ids = {
            r["id"]
            for r in http.get("/api/v1/sites", {"missing_coordinates": "false"}).json()["results"]
        }
        assert ids == {placed.pk}
        everything = http.get("/api/v1/sites").json()["results"]
        assert {r["id"] for r in everything} == {bare.pk, placed.pk}


class TestLocations:
    @pytest.mark.parametrize("kind", [LocationType.YARD, LocationType.OFFICE])
    def test_a_yard_or_office_needs_coordinates(self, http, kind):
        refused(http.post("/api/v1/locations", {"name": "Somewhere", "type": kind}, format="json"))
        reply = http.post(
            "/api/v1/locations", {"name": "Somewhere", "type": kind, **COORDS}, format="json"
        )
        assert reply.status_code == 201, reply.content
        assert reply.json()["has_coordinates"] is True

    @pytest.mark.parametrize("kind", [LocationType.VEHICLE, LocationType.STORE])
    def test_other_types_do_not(self, http, kind):
        parent = YardFactory().pk if kind == LocationType.STORE else None
        reply = http.post(
            "/api/v1/locations",
            {"name": "Thing", "type": kind, "parent": parent, "vehicle_reg": "KAA 001A"},
            format="json",
        )
        assert reply.status_code == 201, reply.content
        assert reply.json()["has_coordinates"] is False

    def test_a_system_row_is_exempt(self, http):
        yard = YardFactory()
        quarantine = Location.objects.get(parent=yard, type=LocationType.QUARANTINE)
        assert quarantine.is_system
        reply = http.patch(f"/api/v1/locations/{quarantine.pk}", {"name": "Q"}, format="json")
        assert reply.status_code == 200

    def test_a_yard_cannot_lose_its_coordinates(self, http):
        yard = YardFactory()
        refused(
            http.patch(
                f"/api/v1/locations/{yard.pk}", {"latitude": None, "longitude": None}, format="json"
            )
        )

    def test_a_store_turned_into_a_yard_needs_them(self, http):
        store = Location.objects.create(name="S", type=LocationType.STORE, parent=YardFactory())
        refused(http.patch(f"/api/v1/locations/{store.pk}", {"type": "YARD"}, format="json"))

    def test_a_blank_yard_can_be_renamed_and_then_placed(self, http):
        yard = YardFactory(latitude=None, longitude=None)
        assert (
            http.patch(f"/api/v1/locations/{yard.pk}", {"name": "Main"}, format="json").status_code
            == 200
        )
        reply = http.patch(f"/api/v1/locations/{yard.pk}", COORDS, format="json")
        assert reply.status_code == 200 and reply.json()["has_coordinates"] is True

    def test_missing_coordinates_filter_includes_offices(self, http):
        blank_yard = YardFactory(latitude=None, longitude=None)
        blank_office = OfficeFactory(latitude=None, longitude=None)
        YardFactory()
        OfficeFactory()
        rows = http.get("/api/v1/locations", {"missing_coordinates": "true"}).json()["results"]
        assert {r["id"] for r in rows} == {blank_yard.pk, blank_office.pk}
        assert all(r["has_coordinates"] is False for r in rows)

    def test_offices_stay_out_of_the_plain_list(self, http):
        office = OfficeFactory()
        assert office.pk not in {r["id"] for r in http.get("/api/v1/locations").json()["results"]}
        assert [
            r["id"] for r in http.get("/api/v1/locations", {"type": "OFFICE"}).json()["results"]
        ] == [office.pk]


class TestAreaHistory:
    def test_moving_a_site_pushes_the_old_area(self, http):
        site = SiteFactory(
            latitude=Decimal("-1.292100"), longitude=Decimal("36.821900"), radius_m=200
        )
        reply = http.patch(
            f"/api/v1/sites/{site.pk}",
            {"latitude": "-1.300000", "radius_m": 300, "longitude": "36.8"},
            format="json",
        )
        assert reply.status_code == 200, reply.content
        site.refresh_from_db()
        assert len(site.area_history) == 1
        entry = site.area_history[0]
        assert (entry["lat"], entry["lng"], entry["radius_m"]) == (-1.2921, 36.8219, 200)
        assert "valid_until" in entry
        assert reply.json()["area_history"] == site.area_history

    def test_a_radius_change_alone_is_an_area_change(self, http):
        yard = YardFactory()
        http.patch(f"/api/v1/locations/{yard.pk}", {"radius_m": 500}, format="json")
        yard.refresh_from_db()
        assert yard.radius_m == 500
        assert [e["radius_m"] for e in yard.area_history] == [200]

    def test_an_edit_that_leaves_the_area_adds_nothing(self, http):
        site = SiteFactory()
        http.patch(f"/api/v1/sites/{site.pk}", {"notes": "x"}, format="json")
        same = {
            "latitude": str(site.latitude),
            "longitude": str(site.longitude),
            "radius_m": site.radius_m,
        }
        http.patch(f"/api/v1/sites/{site.pk}", same, format="json")
        site.refresh_from_db()
        assert site.area_history == []

    def test_history_is_capped_at_ten_newest_first(self, http):
        site = SiteFactory()
        for radius in range(21, 36):
            http.patch(f"/api/v1/sites/{site.pk}", {"radius_m": radius}, format="json")
        site.refresh_from_db()
        assert len(site.area_history) == 10
        assert site.area_history[0]["radius_m"] == 34
