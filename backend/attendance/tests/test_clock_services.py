"""T16.5 — clock-in and clock-out services (§4.18.3-§4.18.5, R13)."""

import threading
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import transaction
from django.utils import timezone

from accounts.factories import UserFactory
from approvals.models import ApprovalRequest, ApprovalRequestStatus
from attendance import services
from attendance.models import ClosedBy, WorkSession
from attendance.services import (
    ClockLocationRequired,
    ClockLocationTooVague,
    ClockNotClockedIn,
    ClockOutsideArea,
    ClockOverlap,
    ClockPlaceRequired,
    ClockSessionLocked,
    ClockTimeInvalid,
    PlaceHasNoCoordinates,
    PlaceNotAvailable,
    clock_in,
    clock_out,
    record_area_change,
)
from commercials.finance import ProjectAmbiguous
from core.models import AuditLog
from core.tenancy import tenant_context
from locations.factories import OfficeFactory, YardFactory
from locations.models import Location, LocationType
from network.factories import ProjectFactory, SiteFactory
from network.models import Site, SiteStatus

HERE = {"lat": -1.2921, "lng": 36.8219, "accuracy_m": 10}
# About 1.1 km north of the site: outside any default radius.
FAR = {"lat": -1.2821, "lng": 36.8219, "accuracy_m": 10}


def ago(**kw):
    return timezone.now() - timedelta(**kw)


@pytest.fixture
def person(tenant):
    return UserFactory(organization=tenant)


@pytest.fixture
def site(tenant):
    return SiteFactory()


def add_project(site, **kw):
    project = ProjectFactory(**kw)
    project.sites.add(site, through_defaults={"organization_id": project.organization_id})
    return project


@pytest.mark.django_db
class TestClockInArea:
    def test_inside_the_area_clocks_in(self, person, site):
        s = clock_in(person=person, site=site, at=ago(hours=1), fix=HERE)
        assert s.pk and s.clock_out_at is None
        assert s.in_distance_m < 5
        assert s.in_checked_area is None and s.area_changed is False
        assert s.work_day.person == person and s.work_day.status == "OPEN"
        assert s.in_lat == Decimal("-1.292100")

    def test_outside_is_refused_with_the_distance(self, person, site):
        with pytest.raises(ClockOutsideArea) as exc:
            clock_in(person=person, site=site, at=ago(hours=1), fix=FAR)
        assert exc.value.code == "CLOCK_OUTSIDE_AREA"
        assert 1000 < exc.value.details["distance_m"] < 1300
        assert "m from" in exc.value.message
        assert not WorkSession.objects.exists()

    def test_the_accuracy_allowance_lets_a_vague_but_capped_fix_in(self, person, site):
        # ~280 m away, accuracy 90 (under the 100 cap): 280 <= 200 + 90.
        fix = {"lat": -1.2921 + 0.0025, "lng": 36.8219, "accuracy_m": 90}
        assert clock_in(person=person, site=site, at=ago(hours=1), fix=fix).pk

    @pytest.mark.parametrize("fix", [None, {}, {"lat": 1}])
    def test_no_fix_is_refused(self, person, site, fix):
        with pytest.raises(ClockLocationRequired) as exc:
            clock_in(person=person, site=site, at=ago(hours=1), fix=fix)
        assert exc.value.status_code == 400

    def test_too_vague_is_refused_even_when_the_distance_would_pass(self, person, site):
        with pytest.raises(ClockLocationTooVague) as exc:
            clock_in(
                person=person,
                site=site,
                at=ago(hours=1),
                fix={**HERE, "accuracy_m": 2000},
            )
        assert exc.value.code == "CLOCK_LOCATION_TOO_VAGUE"

    def test_the_cap_is_the_organizations_setting(self, person, site, organization):
        organization.settings.clock_accuracy_cap_m = 5
        organization.settings.save()
        with pytest.raises(ClockLocationTooVague):
            clock_in(person=person, site=site, at=ago(hours=1), fix=HERE)


@pytest.mark.django_db
class TestPlaces:
    def test_neither_or_both_places_is_refused(self, person, site):
        with pytest.raises(ClockPlaceRequired):
            clock_in(person=person, at=ago(hours=1), fix=HERE)
        with pytest.raises(ClockPlaceRequired):
            clock_in(person=person, site=site, location=YardFactory(), at=ago(hours=1), fix=HERE)

    def test_a_yard_and_an_office_are_clockable_with_no_project(self, person):
        yard = YardFactory(latitude=Decimal("-1.2921"), longitude=Decimal("36.8219"))
        office = OfficeFactory(latitude=Decimal("-1.2921"), longitude=Decimal("36.8219"))
        first = clock_in(person=person, location=yard, at=ago(hours=3), fix=HERE)
        second = clock_in(person=person, location=office, at=ago(hours=2), fix=HERE)
        assert first.project is None and second.project is None
        assert second.location == office

    def test_a_store_is_not_clockable(self, person):
        store = Location.objects.create(
            name="Store",
            type=LocationType.STORE,
            parent=YardFactory(),
            latitude=Decimal("-1.2921"),
            longitude=Decimal("36.8219"),
        )
        with pytest.raises(PlaceNotAvailable):
            clock_in(person=person, location=store, at=ago(hours=1), fix=HERE)

    def test_an_inactive_yard_and_a_decommissioned_site_are_not_available(self, person, site):
        yard = YardFactory(latitude=Decimal("-1.2921"), longitude=Decimal("36.8219"))
        Location.objects.filter(pk=yard.pk).update(is_active=False)
        yard.refresh_from_db()
        with pytest.raises(PlaceNotAvailable):
            clock_in(person=person, location=yard, at=ago(hours=1), fix=HERE)
        Site.objects.filter(pk=site.pk).update(status=SiteStatus.DECOMMISSIONED)
        site.refresh_from_db()
        with pytest.raises(PlaceNotAvailable):
            clock_in(person=person, site=site, at=ago(hours=1), fix=HERE)

    def test_a_site_without_coordinates_is_named(self, person):
        bare = SiteFactory(latitude=None, longitude=None, name="Bare site")
        with pytest.raises(PlaceHasNoCoordinates) as exc:
            clock_in(person=person, site=bare, at=ago(hours=1), fix=HERE)
        assert "Bare site" in exc.value.message


@pytest.mark.django_db
class TestProjectResolution:
    def test_one_open_project_is_taken(self, person, site):
        project = add_project(site)
        s = clock_in(person=person, site=site, at=ago(hours=1), fix=HERE)
        assert s.project == project

    def test_none_gives_null(self, person, site):
        assert clock_in(person=person, site=site, at=ago(hours=1), fix=HERE).project is None

    def test_two_open_projects_need_a_choice(self, person, site):
        a, b = add_project(site), add_project(site)
        with pytest.raises(ProjectAmbiguous) as exc:
            clock_in(person=person, site=site, at=ago(hours=1), fix=HERE)
        assert {c["id"] for c in exc.value.details["candidates"]} == {a.pk, b.pk}
        chosen = clock_in(person=person, site=site, project=b, at=ago(hours=1), fix=HERE)
        assert chosen.project == b

    def test_a_closed_project_is_not_picked(self, person, site):
        done = add_project(site)
        type(done).objects.filter(pk=done.pk).update(status="CLOSED", closed_at=timezone.now())
        assert clock_in(person=person, site=site, at=ago(hours=1), fix=HERE).project is None


@pytest.mark.django_db
class TestTimeBounds:
    def test_five_minutes_ahead_is_fine_ten_is_not(self, person, site):
        assert clock_in(
            person=person, site=site, at=timezone.now() + timedelta(minutes=3), fix=HERE
        ).pk
        other = UserFactory(organization=person.organization)
        with pytest.raises(ClockTimeInvalid):
            clock_in(person=other, site=site, at=timezone.now() + timedelta(minutes=10), fix=HERE)

    def test_over_72_hours_old_is_refused(self, person, site):
        with pytest.raises(ClockTimeInvalid):
            clock_in(person=person, site=site, at=ago(hours=73), fix=HERE)
        assert clock_in(person=person, site=site, at=ago(hours=71), fix=HERE).pk

    def test_a_naive_or_missing_time_is_refused(self, person, site):
        with pytest.raises(ClockTimeInvalid):
            clock_in(person=person, site=site, at="2026-01-01T10:00:00", fix=HERE)
        with pytest.raises(ClockTimeInvalid):
            clock_in(person=person, site=site, at=None, fix=HERE)

    def test_an_iso_string_with_zone_is_accepted(self, person, site):
        at = ago(hours=1).isoformat()
        assert clock_in(person=person, site=site, at=at, fix=HERE).pk

    def test_local_date_is_in_the_organization_timezone(self, person, site, organization):
        organization.settings.timezone = "Africa/Nairobi"
        organization.settings.save()
        at = (timezone.now() - timedelta(days=1)).replace(hour=22, minute=30)  # 01:30 EAT next day
        s = clock_in(person=person, site=site, at=at, fix=HERE)
        assert (s.local_date - at.date()).days == 1
        assert s.work_day.date == s.local_date


@pytest.mark.django_db
class TestOverlapAndNextClockIn:
    def test_a_session_inside_a_closed_one_overlaps(self, person, site):
        clock_in(person=person, site=site, at=ago(hours=5), fix=HERE)
        clock_out(person=person, at=ago(hours=3), fix=HERE)
        with pytest.raises(ClockOverlap):
            clock_in(person=person, site=site, at=ago(hours=4), fix=HERE)
        assert clock_in(person=person, site=site, at=ago(hours=3), fix=HERE).pk

    def test_before_an_open_sessions_start_overlaps(self, person, site):
        clock_in(person=person, site=site, at=ago(hours=2), fix=HERE)
        with pytest.raises(ClockOverlap):
            clock_in(person=person, site=site, at=ago(hours=3), fix=HERE)

    def test_a_second_clock_in_closes_the_first(self, person, site):
        first = clock_in(person=person, site=site, at=ago(hours=3), fix=HERE)
        at = ago(hours=1)
        second = clock_in(person=person, site=site, at=at, fix=HERE)
        first.refresh_from_db()
        assert first.clock_out_at == at and first.closed_by == ClosedBy.NEXT_CLOCK_IN
        assert first.out_lat is None and first.out_distance_m is None
        assert second.clock_out_at is None
        assert WorkSession.objects.filter(clock_out_at__isnull=True).count() == 1
        assert first.work_day == second.work_day

    def test_two_people_do_not_interfere(self, person, site):
        other = UserFactory(organization=person.organization)
        clock_in(person=person, site=site, at=ago(hours=1), fix=HERE)
        clock_in(person=other, site=site, at=ago(hours=1), fix=HERE)
        assert WorkSession.objects.filter(clock_out_at__isnull=True).count() == 2


@pytest.mark.django_db
class TestClockOut:
    def test_clocks_out_and_stores_the_position(self, person, site):
        s = clock_in(person=person, site=site, at=ago(hours=4), fix=HERE)
        out = clock_out(person=person, at=ago(hours=1), fix=HERE)
        assert out.pk == s.pk and out.closed_by == ClosedBy.PERSON
        assert out.out_distance_m < 5 and out.clock_out_received_at is not None

    def test_not_clocked_in(self, person):
        with pytest.raises(ClockNotClockedIn):
            clock_out(person=person, at=ago(hours=1), fix=HERE)

    def test_outside_is_recorded_not_refused(self, person, site):
        clock_in(person=person, site=site, at=ago(hours=4), fix=HERE)
        out = clock_out(person=person, at=ago(hours=1), fix=FAR)
        assert out.clock_out_at is not None
        assert out.out_distance_m > site.radius_m

    def test_no_fix_and_a_vague_fix_are_accepted(self, person, site):
        clock_in(person=person, site=site, at=ago(hours=6), fix=HERE)
        out = clock_out(person=person, at=ago(hours=1), fix=None)
        assert out.out_lat is None and out.out_distance_m is None and out.clock_out_at
        other = UserFactory(organization=person.organization)
        clock_in(person=other, site=site, at=ago(hours=6), fix=HERE)
        vague = clock_out(person=other, at=ago(hours=1), fix={**FAR, "accuracy_m": 5000})
        assert vague.clock_out_at and vague.out_distance_m is not None

    def test_not_before_the_clock_in_and_not_in_the_future(self, person, site):
        clock_in(person=person, site=site, at=ago(hours=2), fix=HERE)
        with pytest.raises(ClockTimeInvalid):
            clock_out(person=person, at=ago(hours=3), fix=HERE)
        with pytest.raises(ClockTimeInvalid):
            clock_out(person=person, at=timezone.now() + timedelta(minutes=20), fix=HERE)

    def test_finds_the_session_by_in_uuid_or_by_id(self, person, site):
        key = uuid.uuid4()
        s = clock_in(person=person, site=site, at=ago(hours=4), fix=HERE, client_uuid=key)
        assert (
            clock_out(person=person, at=ago(hours=3), fix=None, session_client_uuid=str(key)).pk
            == s.pk
        )
        t = clock_in(person=person, site=site, at=ago(hours=2), fix=HERE)
        assert (
            clock_out(person=person, at=ago(hours=1), fix=None, session_client_uuid=str(t.pk)).pk
            == t.pk
        )

    def test_an_unknown_session_is_not_clocked_in(self, person, site):
        clock_in(person=person, site=site, at=ago(hours=2), fix=HERE)
        with pytest.raises(ClockNotClockedIn):
            clock_out(
                person=person, at=ago(hours=1), fix=None, session_client_uuid=str(uuid.uuid4())
            )

    def test_cannot_name_another_persons_session(self, person, site):
        other = UserFactory(organization=person.organization)
        key = uuid.uuid4()
        clock_in(person=other, site=site, at=ago(hours=2), fix=HERE, client_uuid=key)
        with pytest.raises(ClockNotClockedIn):
            clock_out(person=person, at=ago(hours=1), fix=None, session_client_uuid=str(key))

    def test_a_session_already_ended_by_the_person_is_refused(self, person, site):
        key = uuid.uuid4()
        clock_in(person=person, site=site, at=ago(hours=4), fix=HERE, client_uuid=key)
        clock_out(person=person, at=ago(hours=3), fix=None)
        with pytest.raises(ClockNotClockedIn):
            clock_out(
                person=person,
                at=ago(hours=2),
                fix=None,
                client_uuid=uuid.uuid4(),
                session_client_uuid=str(key),
            )


@pytest.mark.django_db
class TestIdempotency:
    def test_a_replayed_clock_in_returns_the_same_session(self, person, site):
        key = uuid.uuid4()
        a = clock_in(person=person, site=site, at=ago(hours=1), fix=HERE, client_uuid=key)
        b = clock_in(person=person, site=site, at=ago(hours=1), fix=HERE, client_uuid=key)
        assert a.pk == b.pk and WorkSession.objects.count() == 1

    def test_a_replay_after_the_session_closed_still_returns_it(self, person, site):
        key = uuid.uuid4()
        a = clock_in(person=person, site=site, at=ago(hours=3), fix=HERE, client_uuid=key)
        clock_out(person=person, at=ago(hours=2), fix=None)
        b = clock_in(person=person, site=site, at=ago(hours=3), fix=HERE, client_uuid=key)
        assert a.pk == b.pk and WorkSession.objects.count() == 1

    def test_a_replayed_clock_out_returns_the_same_session(self, person, site):
        clock_in(person=person, site=site, at=ago(hours=3), fix=HERE)
        key = uuid.uuid4()
        a = clock_out(person=person, at=ago(hours=2), fix=None, client_uuid=key)
        b = clock_out(person=person, at=ago(hours=2), fix=None, client_uuid=key)
        assert a.pk == b.pk and b.out_client_uuid == key
        assert AuditLog.objects.filter(note__startswith="Clocked out").count() == 1

    def test_audit_rows_are_written(self, person, site):
        clock_in(person=person, site=site, at=ago(hours=3), fix=HERE)
        clock_out(person=person, at=ago(hours=2), fix=None)
        notes = list(AuditLog.objects.values_list("note", flat=True))
        assert any(n.startswith("Clocked in at") for n in notes)
        assert any("with no position" in n for n in notes)


@pytest.mark.django_db
class TestAreaChangedReplay:
    """§4.18.4: the phone's check stands only for an area the place really had."""

    OLD = {"lat": -1.2921, "lng": 36.8219, "radius": 200}

    def moved(self, site, valid_until):
        """The site moves ~1.1 km north; the old area goes into history."""
        old = {"lat": -1.2921, "lng": 36.8219, "radius_m": 200}
        Site.objects.filter(pk=site.pk).update(latitude=Decimal("-1.2821"))
        site.refresh_from_db()
        record_area_change(site, previous=old, at=valid_until)
        site.refresh_from_db()
        return site

    def test_inside_the_old_area_is_accepted_and_flagged(self, person, site):
        at = ago(hours=2)
        self.moved(site, ago(hours=1))
        s = clock_in(person=person, site=site, at=at, fix=HERE, place_area=self.OLD)
        assert s.area_changed is True
        assert s.in_checked_area == {"lat": -1.2921, "lng": 36.8219, "radius_m": 200.0}
        assert s.in_distance_m < 5

    def test_without_place_area_the_current_area_decides(self, person, site):
        self.moved(site, ago(hours=1))
        with pytest.raises(ClockOutsideArea):
            clock_in(person=person, site=site, at=ago(hours=2), fix=HERE)

    def test_inside_the_current_area_is_not_flagged_even_with_place_area(self, person, site):
        self.moved(site, ago(hours=1))
        s = clock_in(person=person, site=site, at=ago(hours=2), fix=FAR, place_area=self.OLD)
        assert s.area_changed is False and s.in_checked_area is None

    def test_an_area_that_ended_before_the_capture_is_not_valid(self, person, site):
        self.moved(site, ago(hours=3))  # the old area stopped being current before `at`
        with pytest.raises(ClockOutsideArea):
            clock_in(person=person, site=site, at=ago(hours=2), fix=HERE, place_area=self.OLD)

    def test_outside_both_areas_is_refused(self, person, site):
        self.moved(site, ago(hours=1))
        nowhere = {"lat": -1.4, "lng": 36.9, "accuracy_m": 10}
        with pytest.raises(ClockOutsideArea):
            clock_in(person=person, site=site, at=ago(hours=2), fix=nowhere, place_area=self.OLD)

    def test_a_forged_place_area_is_refused(self, person, site):
        self.moved(site, ago(hours=1))
        forged = {"lat": -1.2921, "lng": 36.8219, "radius": 5000}
        with pytest.raises(ClockOutsideArea):
            clock_in(person=person, site=site, at=ago(hours=2), fix=HERE, place_area=forged)
        with pytest.raises(ClockOutsideArea):
            clock_in(person=person, site=site, at=ago(hours=2), fix=HERE, place_area={"junk": 1})

    def test_radius_m_is_accepted_as_well_as_radius(self, person, site):
        self.moved(site, ago(hours=1))
        area = {"lat": -1.2921, "lng": 36.8219, "radius_m": 200}
        s = clock_in(person=person, site=site, at=ago(hours=2), fix=HERE, place_area=area)
        assert s.area_changed

    def test_a_vague_fix_is_still_vague_on_replay(self, person, site):
        self.moved(site, ago(hours=1))
        with pytest.raises(ClockLocationTooVague):
            clock_in(
                person=person,
                site=site,
                at=ago(hours=2),
                fix={**HERE, "accuracy_m": 900},
                place_area=self.OLD,
            )


@pytest.mark.django_db
class TestRecordAreaChange:
    def test_pushes_newest_first_and_keeps_ten(self, site):
        for n in range(12):
            record_area_change(
                site,
                previous={"lat": -1.0 - n / 1000, "lng": 36.8, "radius_m": 200},
                at=ago(hours=12 - n),
            )
        site.refresh_from_db()
        assert len(site.area_history) == 10
        assert site.area_history[0]["lat"] == pytest.approx(-1.011)
        assert set(site.area_history[0]) == {"lat", "lng", "radius_m", "valid_until"}

    def test_no_previous_coordinates_pushes_nothing(self, site):
        record_area_change(site, previous={"lat": None, "lng": None, "radius_m": 200})
        site.refresh_from_db()
        assert site.area_history == []


@pytest.mark.django_db
class TestOverrideAnAutomaticClose:
    def make_closed(self, person, site, closed_by, hours_in=8, hours_out=2):
        s = clock_in(person=person, site=site, at=ago(hours=hours_in), fix=HERE)
        WorkSession.objects.filter(pk=s.pk).update(
            clock_out_at=ago(hours=hours_out),
            clock_out_received_at=timezone.now(),
            closed_by=closed_by,
        )
        return WorkSession.objects.get(pk=s.pk)

    def test_an_earlier_clock_out_replaces_an_auto_close(self, person, site):
        s = self.make_closed(person, site, ClosedBy.AUTO)
        key = uuid.uuid4()
        out = clock_out(
            person=person,
            at=ago(hours=5),
            fix=HERE,
            client_uuid=key,
            session_client_uuid=str(s.pk),
        )
        assert out.pk == s.pk and out.closed_by == ClosedBy.PERSON
        assert out.clock_out_at < ago(hours=4)
        assert out.out_distance_m < 5 and out.out_client_uuid == key
        assert "replacing an automatic close" in AuditLog.objects.latest("pk").note

    def test_it_also_replaces_a_next_clock_in_close(self, person, site):
        s = self.make_closed(person, site, ClosedBy.NEXT_CLOCK_IN)
        out = clock_out(person=person, at=ago(hours=6), fix=None, session_client_uuid=str(s.pk))
        assert out.closed_by == ClosedBy.PERSON

    def test_a_later_clock_out_leaves_the_auto_close_alone(self, person, site):
        s = self.make_closed(person, site, ClosedBy.AUTO)
        out = clock_out(
            person=person,
            at=ago(hours=1),
            fix=None,
            client_uuid=uuid.uuid4(),
            session_client_uuid=str(s.pk),
        )
        assert out.closed_by == ClosedBy.AUTO
        assert out.out_client_uuid is None

    @pytest.mark.parametrize(
        "status", [ApprovalRequestStatus.APPROVED, ApprovalRequestStatus.REJECTED]
    )
    def test_a_decided_slice_is_locked(self, person, site, tenant, status):
        s = self.make_closed(person, site, ClosedBy.AUTO)
        req = ApprovalRequest.objects.create(
            organization=tenant,
            document_type="WORK_DAY",
            document_id=str(s.work_day_id),
            status=status,
        )
        WorkSession.objects.filter(pk=s.pk).update(approval_request=req)
        with pytest.raises(ClockSessionLocked):
            clock_out(person=person, at=ago(hours=5), fix=None, session_client_uuid=str(s.pk))

    def test_a_pending_slice_may_still_be_replaced(self, person, site, tenant):
        s = self.make_closed(person, site, ClosedBy.AUTO)
        req = ApprovalRequest.objects.create(
            organization=tenant,
            document_type="WORK_DAY",
            document_id=str(s.work_day_id),
            status=ApprovalRequestStatus.PENDING,
        )
        WorkSession.objects.filter(pk=s.pk).update(approval_request=req)
        out = clock_out(person=person, at=ago(hours=5), fix=None, session_client_uuid=str(s.pk))
        assert out.closed_by == ClosedBy.PERSON

    def test_before_the_clock_in_is_still_invalid(self, person, site):
        s = self.make_closed(person, site, ClosedBy.AUTO)
        with pytest.raises(ClockTimeInvalid):
            clock_out(person=person, at=ago(hours=9), fix=None, session_client_uuid=str(s.pk))


@pytest.mark.django_db(transaction=True)
class TestConcurrentClockIns:
    """Real threads: the person lock and the partial unique index hold (§4.18.2)."""

    def test_two_simultaneous_clock_ins_leave_one_open_session(self, organization):
        with transaction.atomic(), tenant_context(organization):
            person = UserFactory(organization=organization)
            site = SiteFactory(name="Race")
            person_id, site_id = person.pk, site.pk
        at = ago(hours=1)

        start = threading.Barrier(2)
        errors: list[BaseException] = []
        results: list[int] = []
        guard = threading.Lock()

        def go():
            try:
                start.wait(timeout=10)
                with transaction.atomic(), tenant_context(organization):
                    from accounts.models import User

                    s = clock_in(
                        person=User.objects.get(pk=person_id),
                        site=Site.objects.get(pk=site_id),
                        at=at,
                        fix=HERE,
                        client_uuid=uuid.uuid4(),
                    )
                with guard:
                    results.append(s.pk)
            except BaseException as exc:
                with guard:
                    errors.append(exc)
            finally:
                from django.db import connection

                connection.close()

        threads = [threading.Thread(target=go) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, errors
        with transaction.atomic(), tenant_context(organization):
            sessions = list(WorkSession.objects.filter(person_id=person_id))
            assert len(sessions) == 2
            assert sum(1 for s in sessions if s.clock_out_at is None) == 1
            closed = next(s for s in sessions if s.clock_out_at is not None)
            assert closed.closed_by == ClosedBy.NEXT_CLOCK_IN

    def test_the_same_uuid_sent_twice_at_once_makes_one_session(self, organization):
        with transaction.atomic(), tenant_context(organization):
            person = UserFactory(organization=organization)
            site = SiteFactory(name="Dup")
            person_id, site_id = person.pk, site.pk
        key = uuid.uuid4()
        at = ago(hours=1)
        start = threading.Barrier(2)
        errors: list[BaseException] = []
        results: list[int] = []
        guard = threading.Lock()

        def go():
            try:
                start.wait(timeout=10)
                with transaction.atomic(), tenant_context(organization):
                    from accounts.models import User

                    s = clock_in(
                        person=User.objects.get(pk=person_id),
                        site=Site.objects.get(pk=site_id),
                        at=at,
                        fix=HERE,
                        client_uuid=key,
                    )
                with guard:
                    results.append(s.pk)
            except BaseException as exc:
                with guard:
                    errors.append(exc)
            finally:
                from django.db import connection

                connection.close()

        threads = [threading.Thread(target=go) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, errors
        assert len(set(results)) == 1 and len(results) == 2
        with transaction.atomic(), tenant_context(organization):
            assert WorkSession.objects.filter(person_id=person_id).count() == 1


def test_the_module_exposes_the_documented_codes():
    codes = {
        getattr(services, name).code
        for name in dir(services)
        if isinstance(getattr(services, name), type)
        and issubclass(getattr(services, name), services.DomainError)
        and getattr(services, name).__module__ == services.__name__
    }
    assert codes == {
        "CLOCK_LOCATION_REQUIRED",
        "CLOCK_LOCATION_TOO_VAGUE",
        "CLOCK_OUTSIDE_AREA",
        "CLOCK_PLACE_REQUIRED",
        "PLACE_HAS_NO_COORDINATES",
        "PLACE_NOT_AVAILABLE",
        "CLOCK_TIME_INVALID",
        "CLOCK_OVERLAP",
        "CLOCK_NOT_CLOCKED_IN",
        "CLOCK_SESSION_LOCKED",
    }
