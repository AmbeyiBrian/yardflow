"""T16.8 (part 1) — clock-in endpoints, visibility and settings (§4.18.7, §4.18.8; R13)."""

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.factories import RoleFactory, UserFactory, UserRoleFactory
from accounts.permissions_registry import PERM
from approvals.models import ApprovalRequest
from attendance import services
from attendance.models import ClosedBy, CorrectionKind, WorkDay, WorkSession, WorkSessionCorrection
from locations.factories import OfficeFactory, YardFactory
from network.factories import ProjectFactory, SiteFactory

pytestmark = pytest.mark.django_db

HERE = {"lat": -1.2921, "lng": 36.8219, "accuracy_m": 10}
FAR = {"lat": -1.2821, "lng": 36.8219, "accuracy_m": 10}


def ago(**kw):
    return timezone.now() - timedelta(**kw)


def client_for(user, settings):
    settings.TENANT_BASE_DOMAIN = "localhost"
    client = APIClient(HTTP_HOST="silvertech.localhost")
    client.force_authenticate(user)
    return client


@pytest.fixture
def person(tenant):
    return UserFactory(organization=tenant)


@pytest.fixture
def http(person, settings):
    return client_for(person, settings)


@pytest.fixture
def site(tenant):
    return SiteFactory()


def add_project(site, **kw):
    project = ProjectFactory(**kw)
    project.sites.add(site, through_defaults={"organization_id": project.organization_id})
    return project


def live_in(person, place_site, *, hours=2, **kw):
    """Clock in `hours` ago as if it reached the server at once (not SENT_LATE)."""
    session = services.clock_in(person=person, site=place_site, at=ago(hours=hours), fix=HERE, **kw)
    WorkSession.objects.filter(pk=session.pk).update(clock_in_received_at=session.clock_in_at)
    return session


def closed_session(person, place_site, *, start=5, end=1, project=None):
    session = live_in(person, place_site, hours=start, project=project)
    closed = services.clock_out(person=person, at=ago(hours=end), fix=HERE)
    WorkSession.objects.filter(pk=closed.pk).update(clock_out_received_at=closed.clock_out_at)
    return closed, session


def with_role(user, *codenames, name=None):
    role = RoleFactory(name=name or f"Role {uuid.uuid4().hex[:6]}", codenames=list(codenames))
    UserRoleFactory(user=user, role=role)
    return role


class TestOpen:
    def test_nothing_open_gives_the_limits_and_no_session(self, http):
        body = http.get("/api/v1/work-sessions/open").json()
        assert "id" not in body
        assert body["sessions_today"] == []
        assert body["accuracy_cap_m"] == 100
        assert body["auto_close_hour"] == 18

    def test_the_open_session_and_todays_sessions(self, http, person, site):
        services.clock_in(person=person, site=site, at=ago(minutes=30), fix=HERE)
        body = http.get("/api/v1/work-sessions/open").json()
        assert body["place_name"] == site.name
        assert body["site"] == site.pk and body["location"] is None
        assert body["clock_out_at"] is None
        assert body["hours"] is None
        assert [s["id"] for s in body["sessions_today"]] == [body["id"]]
        assert body["radius_m"] == 200

    def test_it_is_only_mine(self, http, tenant, site):
        other = UserFactory(organization=tenant)
        services.clock_in(person=other, site=site, at=ago(minutes=30), fix=HERE)
        assert "id" not in http.get("/api/v1/work-sessions/open").json()


class TestClockIn:
    def test_clocks_in_at_a_site(self, http, site):
        reply = http.post(
            "/api/v1/work-sessions/clock-in",
            {"site": site.pk, "fix": HERE, "client_uuid": str(uuid.uuid4())},
            format="json",
        )
        assert reply.status_code == 201, reply.content
        body = reply.json()
        assert body["site"] == site.pk
        assert body["in_distance_m"] < 5
        assert body["flags"] == []
        assert body["closed_by"] is None

    def test_clocks_in_at_a_yard_and_an_office(self, http):
        yard = YardFactory(latitude=Decimal("-1.2921"), longitude=Decimal("36.8219"))
        office = OfficeFactory(latitude=Decimal("-1.2921"), longitude=Decimal("36.8219"))
        first = http.post(
            "/api/v1/work-sessions/clock-in",
            {"location": yard.pk, "fix": HERE, "at": ago(hours=3).isoformat()},
            format="json",
        )
        second = http.post(
            "/api/v1/work-sessions/clock-in",
            {"location": office.pk, "fix": HERE, "at": ago(hours=2).isoformat()},
            format="json",
        )
        assert first.status_code == second.status_code == 201
        assert second.json()["place_name"] == office.name

    def test_a_replay_with_the_same_uuid_returns_the_same_session(self, http, site):
        body = {"site": site.pk, "fix": HERE, "client_uuid": str(uuid.uuid4())}
        first = http.post("/api/v1/work-sessions/clock-in", body, format="json").json()
        again = http.post("/api/v1/work-sessions/clock-in", body, format="json").json()
        assert first["id"] == again["id"]
        assert WorkSession.objects.count() == 1

    def test_outside_the_area_is_refused_with_the_distance(self, http, site):
        reply = http.post(
            "/api/v1/work-sessions/clock-in", {"site": site.pk, "fix": FAR}, format="json"
        )
        assert reply.status_code == 409
        error = reply.json()["error"]
        assert error["code"] == "CLOCK_OUTSIDE_AREA"
        assert 1000 < error["details"]["distance_m"] < 1300

    def test_no_fix_is_refused(self, http, site):
        reply = http.post(
            "/api/v1/work-sessions/clock-in", {"site": site.pk, "fix": None}, format="json"
        )
        assert reply.status_code == 400
        assert reply.json()["error"]["code"] == "CLOCK_LOCATION_REQUIRED"

    def test_a_place_is_required(self, http):
        reply = http.post("/api/v1/work-sessions/clock-in", {"fix": HERE}, format="json")
        assert reply.status_code == 400
        assert reply.json()["error"]["code"] == "CLOCK_PLACE_REQUIRED"

    def test_an_unknown_site_is_a_validation_error(self, http):
        reply = http.post(
            "/api/v1/work-sessions/clock-in", {"site": 999999, "fix": HERE}, format="json"
        )
        assert reply.status_code == 400
        assert "site" in reply.json()["error"]["field_errors"]

    def test_the_project_is_taken_and_ambiguity_is_refused(self, http, site):
        first = add_project(site)
        second = add_project(site)
        reply = http.post(
            "/api/v1/work-sessions/clock-in", {"site": site.pk, "fix": HERE}, format="json"
        )
        assert reply.status_code == 400
        assert reply.json()["error"]["code"] == "PROJECT_AMBIGUOUS"
        reply = http.post(
            "/api/v1/work-sessions/clock-in",
            {"site": site.pk, "fix": HERE, "project": second.pk},
            format="json",
        )
        assert reply.status_code == 201
        assert reply.json()["project"] == second.pk
        assert reply.json()["project_name"] == second.title
        assert first.pk != second.pk

    def test_it_acts_on_the_caller_only(self, http, person, tenant, site):
        reply = http.post(
            "/api/v1/work-sessions/clock-in",
            {"site": site.pk, "fix": HERE, "person": UserFactory(organization=tenant).pk},
            format="json",
        )
        assert reply.status_code == 201
        assert WorkSession.objects.get().person == person

    def test_the_collection_cannot_be_posted_to(self, http):
        assert http.post("/api/v1/work-sessions", {}, format="json").status_code == 403

    def test_unauthenticated_is_refused(self, tenant, settings):
        settings.TENANT_BASE_DOMAIN = "localhost"
        anon = APIClient(HTTP_HOST="silvertech.localhost")
        assert anon.post("/api/v1/work-sessions/clock-in", {}, format="json").status_code == 401


class TestClockOut:
    def test_clocks_out_the_open_session(self, http, person, site):
        live_in(person, site)
        reply = http.post(
            "/api/v1/work-sessions/clock-out",
            {"fix": HERE, "client_uuid": str(uuid.uuid4())},
            format="json",
        )
        assert reply.status_code == 200, reply.content
        body = reply.json()
        assert body["clock_out_at"] is not None
        assert body["closed_by"] == "PERSON"
        assert body["hours"] == "2.00"
        assert body["flags"] == []

    def test_clocking_out_far_away_is_allowed_and_flagged(self, http, person, site):
        live_in(person, site)
        reply = http.post("/api/v1/work-sessions/clock-out", {"fix": FAR}, format="json")
        assert reply.status_code == 200
        assert reply.json()["flags"] == ["OUTSIDE_AT_CLOCK_OUT"]
        assert reply.json()["out_distance_m"] > 1000

    def test_clocking_out_with_no_position_is_allowed_and_flagged(self, http, person, site):
        live_in(person, site)
        reply = http.post("/api/v1/work-sessions/clock-out", {}, format="json")
        assert reply.status_code == 200
        assert reply.json()["flags"] == ["NO_POSITION_AT_CLOCK_OUT"]

    def test_clock_out_by_session_uuid_and_replay(self, http, person, site):
        key = uuid.uuid4()
        live_in(person, site, client_uuid=key)
        body = {"fix": HERE, "session_client_uuid": str(key), "client_uuid": str(uuid.uuid4())}
        first = http.post("/api/v1/work-sessions/clock-out", body, format="json")
        again = http.post("/api/v1/work-sessions/clock-out", body, format="json")
        assert first.status_code == again.status_code == 200
        assert first.json()["id"] == again.json()["id"]
        assert first.json()["in_client_uuid"] == str(key)

    def test_not_clocked_in_is_refused(self, http):
        reply = http.post("/api/v1/work-sessions/clock-out", {"fix": HERE}, format="json")
        assert reply.status_code == 409
        assert reply.json()["error"]["code"] == "CLOCK_NOT_CLOCKED_IN"


class TestFlags:
    def flags_of(self, http, session):
        return http.get(f"/api/v1/work-sessions/{session.pk}").json()["flags"]

    def test_each_derived_flag(self, http, person, site):
        session, _ = closed_session(person, site)
        assert self.flags_of(http, session) == []

        WorkSession.objects.filter(pk=session.pk).update(
            closed_by=ClosedBy.AUTO,
            area_changed=True,
            clock_in_received_at=session.clock_in_at + timedelta(hours=3),
        )
        assert self.flags_of(http, session) == [
            "CLOSED_AUTOMATICALLY",
            "SENT_LATE",
            "AREA_CHANGED",
        ]

        WorkSession.objects.filter(pk=session.pk).update(added_by=person)
        WorkSessionCorrection.objects.create(
            session=session,
            work_day=session.work_day,
            kind=CorrectionKind.EDIT,
            original_in_at=session.clock_in_at,
            original_out_at=session.clock_out_at,
            corrected_in_at=session.clock_in_at,
            corrected_out_at=session.clock_out_at + timedelta(hours=1),
            reason="Forgot",
            made_by=person,
            made_at=timezone.now(),
        )
        flags = self.flags_of(http, session)
        assert flags[-2:] == ["CORRECTED", "ADDED_BY_DIRECTOR"]

    def test_hours_use_the_corrected_times(self, http, person, site):
        session, _ = closed_session(person, site, start=5, end=1)
        WorkSessionCorrection.objects.create(
            session=session,
            work_day=session.work_day,
            kind=CorrectionKind.EDIT,
            corrected_out_at=session.clock_out_at + timedelta(hours=1),
            reason="Stayed on",
            made_by=person,
            made_at=timezone.now(),
        )
        body = http.get(f"/api/v1/work-days/{session.work_day_id}").json()
        assert body["hours"] == "5.00"
        assert body["sessions"][0]["hours"] == "5.00"
        assert body["sessions"][0]["corrections"][0]["reason"] == "Stayed on"


class TestWorkDays:
    def test_my_days_list_and_detail(self, http, person, site):
        session, _ = closed_session(person, site)
        rows = http.get("/api/v1/work-days").json()["results"]
        assert [r["id"] for r in rows] == [session.work_day_id]
        assert rows[0]["person"] == person.pk
        assert rows[0]["person_name"] == str(person)
        assert rows[0]["hours"] == "4.00"
        assert rows[0]["status"] == "OPEN"
        assert rows[0]["session_count"] == 1

        detail = http.get(f"/api/v1/work-days/{session.work_day_id}").json()
        assert detail["sessions"][0]["id"] == session.pk
        assert detail["sessions"][0]["place_name"] == site.name
        assert detail["slices"] == []

    def test_days_are_newest_first(self, http, person, site):
        old = WorkDay.objects.create(person=person, date="2026-01-01")
        new = WorkDay.objects.create(person=person, date="2026-02-01")
        ids = [r["id"] for r in http.get("/api/v1/work-days").json()["results"]]
        assert ids == [new.pk, old.pk]

    def test_other_peoples_days_are_not_mine(self, http, tenant, site):
        other = UserFactory(organization=tenant)
        closed_session(other, site)
        assert http.get("/api/v1/work-days").json()["results"] == []

    def test_a_stranger_gets_404_for_someone_elses_day(self, http, tenant, site):
        other = UserFactory(organization=tenant)
        session, _ = closed_session(other, site)
        assert http.get(f"/api/v1/work-days/{session.work_day_id}").status_code == 404
        assert http.get(f"/api/v1/work-sessions/{session.pk}").status_code == 404

    def test_filters(self, http, person, site):
        other_site = SiteFactory()
        project = add_project(other_site)
        WorkDay.objects.create(person=person, date="2026-01-05", status="APPROVED")
        WorkDay.objects.create(person=person, date="2026-03-05")
        session, _ = closed_session(person, other_site, project=project)

        def ids(**params):
            return {r["id"] for r in http.get("/api/v1/work-days", params).json()["results"]}

        assert len(ids()) == 3
        assert len(ids(status="APPROVED")) == 1
        assert len(ids(date_from="2026-02-01", date_to="2026-03-31")) == 1
        assert len(ids(**{"from": "2026-02-01", "to": "2026-03-31"})) == 1
        assert ids(project=project.pk) == {session.work_day_id}
        assert ids(site=other_site.pk) == {session.work_day_id}
        assert len(ids(person=person.pk)) == 3
        assert http.get("/api/v1/work-days", {"date_from": "nope"}).status_code == 400
        assert http.get("/api/v1/work-days", {"status": "NOPE"}).status_code == 400
        assert http.get("/api/v1/work-days", {"scope": "NOPE"}).status_code == 400


class TestVisibility:
    def test_a_pm_sees_days_on_their_projects_in_team_scope(self, tenant, settings, site):
        pm = UserFactory(organization=tenant)
        worker = UserFactory(organization=tenant)
        project = add_project(site, manager=pm)
        elsewhere = SiteFactory()
        on_project, _ = closed_session(worker, site, project=project)
        off_project, _ = closed_session(UserFactory(organization=tenant), elsewhere)
        http = client_for(pm, settings)

        team = http.get("/api/v1/work-days", {"scope": "team"}).json()["results"]
        assert [r["id"] for r in team] == [on_project.work_day_id]
        assert http.get(f"/api/v1/work-days/{on_project.work_day_id}").status_code == 200
        assert http.get(f"/api/v1/work-days/{off_project.work_day_id}").status_code == 404
        assert http.get("/api/v1/work-days", {"scope": "mine"}).json()["results"] == []
        assert http.get("/api/v1/work-days", {"scope": "all"}).status_code == 403

    def test_view_all_sees_everyone(self, tenant, settings, site):
        viewer = UserFactory(organization=tenant)
        with_role(viewer, PERM.ATTENDANCE_VIEW_ALL)
        first, _ = closed_session(UserFactory(organization=tenant), site)
        second, _ = closed_session(UserFactory(organization=tenant), site)
        http = client_for(viewer, settings)
        rows = http.get("/api/v1/work-days", {"scope": "all"}).json()["results"]
        assert {r["id"] for r in rows} == {first.work_day_id, second.work_day_id}
        assert http.get(f"/api/v1/work-days/{first.work_day_id}").status_code == 200

    def test_the_director_role_sees_everyone(self, tenant, settings, site):
        director = UserFactory(organization=tenant)
        role = with_role(director, name="Director")
        tenant.settings.finance_director_role = role
        tenant.settings.save()
        first, _ = closed_session(UserFactory(organization=tenant), site)
        http = client_for(director, settings)
        rows = http.get("/api/v1/work-days", {"scope": "all"}).json()["results"]
        assert [r["id"] for r in rows] == [first.work_day_id]

    def test_an_ordinary_member_cannot_ask_for_all(self, http):
        assert http.get("/api/v1/work-days", {"scope": "all"}).status_code == 403

    def test_sessions_are_scoped_like_the_days(self, tenant, settings, site):
        pm = UserFactory(organization=tenant)
        project = add_project(site, manager=pm)
        mine, _ = closed_session(UserFactory(organization=tenant), site, project=project)
        stranger = UserFactory(organization=tenant)
        assert client_for(pm, settings).get(f"/api/v1/work-sessions/{mine.pk}").status_code == 200
        assert (
            client_for(stranger, settings).get(f"/api/v1/work-sessions/{mine.pk}").status_code
            == 404
        )


class TestSlicesAndAwaitingMe:
    def make_request(self, day, **kw):
        return ApprovalRequest.objects.create(
            document_type="attendance.WorkDay", document_id=str(day.pk), level=1, **kw
        )

    def test_slices_come_from_the_days_requests(self, tenant, settings, site):
        pm = UserFactory(organization=tenant, full_name="Pat Manager")
        project = add_project(site, manager=pm)
        worker = UserFactory(organization=tenant)
        session, _ = closed_session(worker, site, project=project)
        day = session.work_day
        request = self.make_request(day, required_user=pm)
        WorkSession.objects.filter(pk=session.pk).update(approval_request=request)

        http = client_for(pm, settings)
        slices = http.get(f"/api/v1/work-days/{day.pk}").json()["slices"]
        assert slices == [
            {
                "id": request.pk,
                "approver": pm.pk,
                "approver_name": "Pat Manager",
                "project": project.pk,
                "project_name": project.title,
                "status": "PENDING",
                "is_mine": True,
                "reason": "",
                "decided_at": None,
            }
        ]
        session_body = http.get(f"/api/v1/work-sessions/{session.pk}").json()
        assert session_body["slice_status"] == "PENDING"
        assert session_body["can_correct"] is False

        # The worker sees the slice, but it is not theirs.
        theirs = client_for(worker, settings).get(f"/api/v1/work-days/{day.pk}").json()
        assert theirs["slices"][0]["is_mine"] is False

    def test_awaiting_me_lists_days_with_a_slice_for_the_caller(self, tenant, settings, site):
        pm = UserFactory(organization=tenant)
        worker = UserFactory(organization=tenant)
        session, _ = closed_session(worker, site)
        other, _ = closed_session(UserFactory(organization=tenant), site)
        self.make_request(session.work_day, required_user=pm)
        self.make_request(other.work_day, required_user=UserFactory(organization=tenant))

        http = client_for(pm, settings)
        # A day routed to me is visible and listed even with no project of mine on it.
        rows = http.get("/api/v1/work-days", {"awaiting_me": "true"}).json()["results"]
        assert [r["id"] for r in rows] == [session.work_day_id]
        assert rows[0]["slices"][0]["is_mine"] is True
        assert http.get(f"/api/v1/work-days/{session.work_day_id}").status_code == 200
        assert http.get(f"/api/v1/work-days/{other.work_day_id}").status_code == 404

    def test_a_rejected_slice_can_be_corrected_by_its_owner(self, tenant, settings, site):
        pm = UserFactory(organization=tenant)
        worker = UserFactory(organization=tenant)
        session, _ = closed_session(worker, site)
        request = self.make_request(session.work_day, required_user=pm)
        ApprovalRequest.objects.filter(pk=request.pk).update(
            status="REJECTED", resolved_at=timezone.now()
        )
        WorkSession.objects.filter(pk=session.pk).update(approval_request=request)
        mine = client_for(worker, settings).get(f"/api/v1/work-sessions/{session.pk}").json()
        assert mine["slice_status"] == "REJECTED"
        assert mine["can_correct"] is True
        day = client_for(worker, settings).get(f"/api/v1/work-days/{session.work_day_id}").json()
        assert day["slices"][0]["status"] == "REJECTED"


class TestSettings:
    def test_any_member_reads(self, http):
        assert http.get("/api/v1/attendance/settings").json() == {
            "clock_auto_close_hour": 18,
            "clock_accuracy_cap_m": 100,
        }

    def test_a_member_cannot_write(self, http):
        reply = http.patch(
            "/api/v1/attendance/settings", {"clock_accuracy_cap_m": 50}, format="json"
        )
        assert reply.status_code == 403

    def test_settings_manage_writes(self, tenant, settings):
        admin = UserFactory(organization=tenant)
        with_role(admin, PERM.SETTINGS_MANAGE)
        http = client_for(admin, settings)
        reply = http.patch(
            "/api/v1/attendance/settings",
            {"clock_auto_close_hour": 20, "clock_accuracy_cap_m": 60},
            format="json",
        )
        assert reply.status_code == 200
        assert reply.json() == {"clock_auto_close_hour": 20, "clock_accuracy_cap_m": 60}
        tenant.settings.refresh_from_db()
        assert tenant.settings.clock_auto_close_hour == 20
        assert http.get("/api/v1/work-sessions/open").json()["accuracy_cap_m"] == 60

    def test_one_field_alone_leaves_the_other(self, tenant, settings):
        admin = UserFactory(organization=tenant)
        with_role(admin, PERM.SETTINGS_MANAGE)
        http = client_for(admin, settings)
        reply = http.patch(
            "/api/v1/attendance/settings", {"clock_auto_close_hour": 7}, format="json"
        )
        assert reply.json() == {"clock_auto_close_hour": 7, "clock_accuracy_cap_m": 100}

    @pytest.mark.parametrize(
        "body",
        [
            {"clock_auto_close_hour": 24},
            {"clock_auto_close_hour": -1},
            {"clock_accuracy_cap_m": 0},
            {"clock_accuracy_cap_m": "abc"},
            {"clock_auto_close_hour": 1.5},
        ],
    )
    def test_bad_values_are_refused(self, tenant, settings, body):
        admin = UserFactory(organization=tenant)
        with_role(admin, PERM.SETTINGS_MANAGE)
        reply = client_for(admin, settings).patch(
            "/api/v1/attendance/settings", body, format="json"
        )
        assert reply.status_code == 400
        assert reply.json()["error"]["field_errors"]
