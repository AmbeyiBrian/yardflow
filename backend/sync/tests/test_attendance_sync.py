"""T16.10 — clock-in and clock-out replayed from a phone (R13, R6, design §4.18.9).

The queue is another door to ``attendance.services``, so what is pinned here is
that a replay lands the same session the online call would, once, that a refusal
is a recorded exception carrying the domain code, and that the bundle gives the
phone what it needs to check the area itself.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.factories import UserFactory
from attendance.models import WorkSession
from attendance.services import record_area_change
from locations.factories import OfficeFactory, YardFactory
from network.factories import SiteFactory
from network.models import Site
from sync.models import SubmissionStatus, SyncException, SyncSubmission
from sync.services import apply_submission

pytestmark = pytest.mark.django_db

HERE = {"lat": -1.2921, "lng": 36.8219, "accuracy_m": 10}
FAR = {"lat": -1.2821, "lng": 36.8219, "accuracy_m": 10}
OLD_AREA = {"lat": -1.2921, "lng": 36.8219, "radius": 200}


def ago(**kw):
    return timezone.now() - timedelta(**kw)


@pytest.fixture
def tech(tenant):
    return UserFactory(organization=tenant, full_name="Tom Technician")


@pytest.fixture
def site(tenant):
    return SiteFactory(name="Ruiru")


def send(tenant, tech, operation, payload, *, captured_at=None, uuid=None):
    uuid = uuid or uuid4()
    return apply_submission(
        organization=tenant,
        client_uuid=uuid,
        operation=operation,
        payload={**payload, "client_uuid": str(uuid)},
        submitted_by=tech,
        captured_at=captured_at,
    )


class TestReplay:
    def test_a_clock_in_lands_at_the_captured_time(self, tenant, tech, site):
        at = ago(hours=3)
        submission, replay = send(
            tenant, tech, "CLOCK_IN", {"site": site.pk, "fix": HERE}, captured_at=at
        )

        assert not replay
        assert submission.status == SubmissionStatus.APPLIED
        session = WorkSession.objects.get()
        assert submission.document_id == str(session.pk)
        assert session.person == tech and session.site == site
        assert session.clock_in_at == at
        assert session.in_client_uuid == submission.client_uuid

    def test_a_location_clock_in_lands(self, tenant, tech):
        yard = YardFactory()
        submission, _ = send(
            tenant,
            tech,
            "CLOCK_IN",
            {
                "location": yard.pk,
                "fix": {**HERE, "lat": float(yard.latitude), "lng": float(yard.longitude)},
            },
            captured_at=ago(hours=1),
        )
        assert submission.status == SubmissionStatus.APPLIED
        assert WorkSession.objects.get().location == yard

    def test_a_payload_at_wins_over_the_envelope(self, tenant, tech, site):
        at = ago(hours=5)
        send(
            tenant,
            tech,
            "CLOCK_IN",
            {"site": site.pk, "fix": HERE, "at": at.isoformat()},
            captured_at=ago(hours=1),
        )
        assert WorkSession.objects.get().clock_in_at == at

    def test_a_clock_out_closes_the_open_session(self, tenant, tech, site):
        send(tenant, tech, "CLOCK_IN", {"site": site.pk, "fix": HERE}, captured_at=ago(hours=4))
        out_at = ago(hours=1)
        submission, _ = send(
            tenant, tech, "CLOCK_OUT", {"fix": None}, captured_at=out_at
        )

        assert submission.status == SubmissionStatus.APPLIED
        session = WorkSession.objects.get()
        assert session.clock_out_at == out_at
        assert session.out_client_uuid == submission.client_uuid

    def test_resending_a_submission_makes_one_session(self, tenant, tech, site):
        uuid = uuid4()
        at = ago(hours=2)
        first, replay1 = send(
            tenant, tech, "CLOCK_IN", {"site": site.pk, "fix": HERE}, captured_at=at, uuid=uuid
        )
        second, replay2 = send(
            tenant, tech, "CLOCK_IN", {"site": site.pk, "fix": HERE}, captured_at=at, uuid=uuid
        )
        assert (replay1, replay2) == (False, True)
        assert first.pk == second.pk
        assert WorkSession.objects.count() == 1

    def test_a_new_submission_with_the_same_session_uuid_is_still_one_session(
        self, tenant, tech, site
    ):
        """The service's own idempotency, behind the envelope's."""
        uuid = uuid4()
        payload = {"site": site.pk, "fix": HERE, "client_uuid": str(uuid)}
        for _ in range(2):
            apply_submission(
                organization=tenant,
                client_uuid=uuid4(),
                operation="CLOCK_IN",
                payload=payload,
                submitted_by=tech,
                captured_at=ago(hours=2),
            )
        assert WorkSession.objects.count() == 1


class TestRefusals:
    def test_outside_the_area_is_recorded_with_its_code(self, tenant, tech, site):
        submission, _ = send(
            tenant, tech, "CLOCK_IN", {"site": site.pk, "fix": FAR}, captured_at=ago(hours=1)
        )

        assert submission.status == SubmissionStatus.REJECTED
        exception = SyncException.objects.get(submission=submission)
        assert exception.code == "CLOCK_OUTSIDE_AREA"
        assert exception.details["distance_m"] > 1000
        assert not WorkSession.objects.exists()

    def test_a_clock_out_with_nothing_open_is_recorded(self, tenant, tech):
        submission, _ = send(tenant, tech, "CLOCK_OUT", {"fix": None}, captured_at=ago(hours=1))
        assert submission.status == SubmissionStatus.REJECTED
        assert SyncException.objects.get().code == "CLOCK_NOT_CLOCKED_IN"

    def test_no_time_at_all_is_refused_not_crashed(self, tenant, tech, site):
        submission, _ = send(tenant, tech, "CLOCK_IN", {"site": site.pk, "fix": HERE})
        assert submission.status == SubmissionStatus.REJECTED
        assert SyncException.objects.get().code == "SYNC_REFUSED"

    def test_another_tenants_site_is_refused(self, tenant, tech):
        submission, _ = send(
            tenant, tech, "CLOCK_IN", {"site": 99999999, "fix": HERE}, captured_at=ago(hours=1)
        )
        assert submission.status == SubmissionStatus.REJECTED
        assert SyncException.objects.get().code == "SYNC_REFUSED"


class TestSessionClientUuid:
    def test_a_clock_out_names_a_session_queued_in_the_same_batch(self, tenant, tech, site):
        in_uuid = uuid4()
        send(
            tenant,
            tech,
            "CLOCK_IN",
            {"site": site.pk, "fix": HERE},
            captured_at=ago(hours=4),
            uuid=in_uuid,
        )
        submission, _ = send(
            tenant,
            tech,
            "CLOCK_OUT",
            {"fix": HERE, "session_client_uuid": str(in_uuid)},
            captured_at=ago(hours=1),
        )

        assert submission.status == SubmissionStatus.APPLIED
        session = WorkSession.objects.get(in_client_uuid=in_uuid)
        assert session.clock_out_at is not None


class TestAreaChanged:
    def moved(self, site, valid_until):
        old = {"lat": -1.2921, "lng": 36.8219, "radius_m": 200}
        Site.objects.filter(pk=site.pk).update(latitude=Decimal("-1.2821"))
        site.refresh_from_db()
        record_area_change(site, previous=old, at=valid_until)

    def test_the_old_area_in_place_area_is_accepted_and_flagged(self, tenant, tech, site):
        self.moved(site, ago(hours=1))
        submission, _ = send(
            tenant,
            tech,
            "CLOCK_IN",
            {"site": site.pk, "fix": HERE, "place_area": OLD_AREA},
            captured_at=ago(hours=2),
        )

        assert submission.status == SubmissionStatus.APPLIED
        session = WorkSession.objects.get()
        assert session.area_changed is True
        assert session.in_checked_area["radius_m"] == 200.0

    def test_without_place_area_the_moved_site_refuses(self, tenant, tech, site):
        self.moved(site, ago(hours=1))
        submission, _ = send(
            tenant, tech, "CLOCK_IN", {"site": site.pk, "fix": HERE}, captured_at=ago(hours=2)
        )
        assert submission.status == SubmissionStatus.REJECTED
        assert SyncException.objects.get().code == "CLOCK_OUTSIDE_AREA"


class TestBundle:
    @pytest.fixture
    def http(self, tenant, tech, settings):
        settings.TENANT_BASE_DOMAIN = "localhost"
        client = APIClient(HTTP_HOST="silvertech.localhost")
        client.force_authenticate(tech)
        return client

    def test_it_carries_areas_offices_and_settings(self, http, tenant, site):
        yard = YardFactory(name="Main yard")
        office = OfficeFactory(name="Head office")

        body = http.get("/api/v1/sync/bundle").json()

        [row] = [s for s in body["sites"] if s["id"] == site.pk]
        assert row["latitude"] == pytest.approx(float(site.latitude))
        assert row["longitude"] == pytest.approx(float(site.longitude))
        assert row["radius_m"] == site.radius_m
        assert row["has_coordinates"] is True

        offices = {o["id"]: o for o in body["offices"]}
        assert set(offices) >= {yard.pk, office.pk}
        assert set(offices[office.pk]) == {
            "id",
            "name",
            "type",
            "latitude",
            "longitude",
            "radius_m",
            "has_coordinates",
        }
        assert offices[office.pk]["type"] == "OFFICE"
        # The plain locations list still leaves OFFICE out.
        assert office.pk not in {loc["id"] for loc in body["locations"]}

        settings = tenant.settings
        assert body["attendance"] == {
            "accuracy_cap_m": settings.clock_accuracy_cap_m,
            "auto_close_hour": settings.clock_auto_close_hour,
        }

    def test_a_place_without_coordinates_says_so(self, http, tenant):
        bare = SiteFactory(name="Bare", latitude=None, longitude=None)
        body = http.get("/api/v1/sync/bundle").json()
        [row] = [s for s in body["sites"] if s["id"] == bare.pk]
        assert row["has_coordinates"] is False
        assert row["latitude"] is None


def test_submissions_are_counted(tenant, tech, site):
    send(tenant, tech, "CLOCK_IN", {"site": site.pk, "fix": HERE}, captured_at=ago(hours=1))
    assert SyncSubmission.objects.count() == 1
