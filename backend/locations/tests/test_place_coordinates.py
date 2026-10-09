"""T16.1 — places get coordinates, a radius and OFFICE (§4.18.2; R13)."""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from rest_framework.test import APIClient

from accounts.factories import RoleFactory, UserFactory, UserRoleFactory
from accounts.permissions_registry import PERM
from core.models import OrganizationSettings
from locations.factories import OfficeFactory, YardFactory
from locations.models import Location, LocationType, StockNode
from locations.nodes import node_for_location, seed_locations_and_nodes
from network.factories import SiteFactory
from network.models import Site
from network.seeding import seed_demo_network

pytestmark = pytest.mark.django_db


def _db_rejects(model, pk, **fields):
    """Bypass full_clean/serializers: the database itself must refuse (§4.18.2)."""
    with pytest.raises(IntegrityError), transaction.atomic():
        model.objects.filter(pk=pk).update(**fields)


class TestSiteConstraints:
    def test_a_site_without_coordinates_is_still_valid(self, tenant):
        site = SiteFactory(latitude=None, longitude=None)
        assert site.radius_m == 200
        assert site.area_history == []

    @pytest.mark.parametrize(
        "fields",
        [
            {"latitude": Decimal("90.000001"), "longitude": Decimal("1")},
            {"latitude": Decimal("-91"), "longitude": Decimal("1")},
            {"latitude": Decimal("1"), "longitude": Decimal("180.5")},
            {"latitude": Decimal("1"), "longitude": Decimal("-181")},
        ],
    )
    def test_coordinates_must_be_on_the_globe(self, tenant, fields):
        _db_rejects(Site, SiteFactory().pk, **fields)

    def test_latitude_without_longitude_is_refused(self, tenant):
        _db_rejects(Site, SiteFactory().pk, longitude=None)

    def test_longitude_without_latitude_is_refused(self, tenant):
        _db_rejects(Site, SiteFactory().pk, latitude=None)

    @pytest.mark.parametrize("radius", [19, 2001])
    def test_radius_is_between_20_and_2000(self, tenant, radius):
        _db_rejects(Site, SiteFactory().pk, radius_m=radius)

    @pytest.mark.parametrize("radius", [20, 2000])
    def test_the_radius_bounds_themselves_are_allowed(self, tenant, radius):
        assert SiteFactory(radius_m=radius).radius_m == radius


class TestLocationConstraints:
    def test_a_legacy_yard_without_coordinates_can_still_be_saved_and_deactivated(self, tenant):
        yard = YardFactory(latitude=None, longitude=None)
        yard.is_active = False
        yard.save()  # full_clean runs; both-null must be valid
        yard.refresh_from_db()
        assert yard.is_active is False

    def test_save_refuses_half_a_pair(self, tenant):
        with pytest.raises(ValidationError, match="location_coordinates_both_or_neither"):
            YardFactory(longitude=None)

    def test_save_refuses_an_out_of_range_radius(self, tenant):
        with pytest.raises(ValidationError):
            YardFactory(radius_m=10)

    @pytest.mark.parametrize(
        "fields",
        [
            {"latitude": Decimal("91"), "longitude": Decimal("1")},
            {"latitude": Decimal("1"), "longitude": Decimal("-181")},
            {"longitude": None},
            {"latitude": None},
            {"radius_m": 19},
            {"radius_m": 2001},
        ],
    )
    def test_the_database_refuses_bad_places(self, tenant, fields):
        _db_rejects(Location, YardFactory().pk, **fields)

    def test_defaults(self, tenant):
        yard = YardFactory()
        assert yard.radius_m == 200
        assert yard.area_history == []


class TestOffice:
    def test_an_office_never_gets_a_stock_node(self, tenant):
        office = OfficeFactory()
        assert not StockNode.objects.filter(location=office).exists()

    def test_asking_for_an_offices_node_is_refused(self, tenant):
        with pytest.raises(ValueError, match="no stock node"):
            node_for_location(OfficeFactory())
        assert not StockNode.objects.filter(location__type=LocationType.OFFICE).exists()

    def test_yards_still_get_nodes(self, tenant):
        assert StockNode.objects.filter(location=YardFactory()).exists()


@pytest.fixture
def http(tenant, settings):
    settings.TENANT_BASE_DOMAIN = "localhost"
    role = RoleFactory(name="Catalogue", codenames=[PERM.CATALOGUE_MANAGE])
    user = UserFactory(organization=tenant)
    UserRoleFactory(user=user, role=role)
    client = APIClient(HTTP_HOST="silvertech.localhost")
    client.force_authenticate(user)
    return client


class TestPickers:
    def test_the_location_list_leaves_offices_out(self, http):
        yard = YardFactory()
        office = OfficeFactory()

        ids = {row["id"] for row in http.get("/api/v1/locations").json()["results"]}

        assert yard.pk in ids
        assert office.pk not in ids

    def test_settings_can_still_ask_for_offices(self, http):
        office = OfficeFactory()
        rows = http.get("/api/v1/locations?type=OFFICE").json()["results"]
        assert [row["id"] for row in rows] == [office.pk]

    def test_stock_nodes_and_available_list_no_office(self, http):
        OfficeFactory()
        for path in ("/api/v1/stock-nodes", "/api/v1/stock-nodes/available"):
            body = http.get(path).json()
            rows = body["results"] if isinstance(body, dict) else body
            assert all(row["location"] is None or Location.objects.get(pk=row["location"]).type
                       != LocationType.OFFICE for row in rows)

    def test_the_offline_bundle_leaves_offices_out(self, http):
        office = OfficeFactory()
        yard = YardFactory()
        ids = {row["id"] for row in http.get("/api/v1/sync/bundle").json()["locations"]}
        assert yard.pk in ids
        assert office.pk not in ids

    def test_the_serializer_exposes_the_new_fields_and_ignores_area_history(self, http):
        yard = YardFactory()
        body = http.get(f"/api/v1/locations/{yard.pk}").json()
        assert body["latitude"] == "-1.264000"
        assert body["radius_m"] == 200
        assert body["area_history"] == []

        reply = http.patch(
            f"/api/v1/locations/{yard.pk}",
            {"radius_m": 350, "area_history": [{"x": 1}]},
            format="json",
        )
        assert reply.status_code == 200
        yard.refresh_from_db()
        assert yard.radius_m == 350
        # The client's own value is ignored; the only entry is the one the
        # radius change itself pushed (T16.8).
        assert [entry["radius_m"] for entry in yard.area_history] == [200]
        assert all("x" not in entry for entry in yard.area_history)


class TestSiteApi:
    def test_a_site_without_coordinates_is_listed_and_editable(self, http):
        site = SiteFactory(latitude=None, longitude=None)
        body = http.get(f"/api/v1/sites/{site.pk}").json()
        assert body["latitude"] is None
        assert body["radius_m"] == 200
        reply = http.patch(f"/api/v1/sites/{site.pk}", {"notes": "hi"}, format="json")
        assert reply.status_code == 200


class TestSettings:
    def test_clock_defaults(self, tenant):
        settings_row = OrganizationSettings.objects.get(organization=tenant)
        assert settings_row.clock_auto_close_hour == 18
        assert settings_row.clock_accuracy_cap_m == 100

    @pytest.mark.parametrize("fields", [{"clock_auto_close_hour": 24}, {"clock_accuracy_cap_m": 0}])
    def test_the_database_refuses_bad_values(self, tenant, fields):
        with pytest.raises(IntegrityError), transaction.atomic():
            OrganizationSettings.objects.filter(organization=tenant).update(**fields)


class TestSeeding:
    def test_a_new_tenants_main_yard_starts_blank(self, tenant):
        seed_locations_and_nodes(tenant)
        yard = Location.objects.get(organization=tenant, name="Main yard")
        assert yard.latitude is None and yard.longitude is None

    def test_the_demo_network_has_coordinates_including_the_yard(self, tenant):
        seed_locations_and_nodes(tenant)
        seed_demo_network(tenant)
        assert not Site.objects.filter(organization=tenant, latitude__isnull=True).exists()
        yard = Location.objects.get(organization=tenant, name="Main yard")
        assert yard.latitude is not None and yard.longitude is not None
