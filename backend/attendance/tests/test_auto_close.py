"""T16.6 — auto-close and day formation (§4.18.5; R13)."""

from datetime import date, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.conf import settings as django_settings

from accounts.factories import UserFactory
from attendance import sweeps
from attendance.models import ClosedBy, WorkDay, WorkDayStatus, WorkSession
from attendance.tests.support import (
    MONDAY,
    NAIROBI,
    at,
    make_director_role,
    make_owner,
    make_project,
    make_session,
    make_user,
)
from core.models import AuditLog
from network.factories import SiteFactory
from notifications.matrix import Event
from notifications.models import NotificationEvent

pytestmark = pytest.mark.django_db

LONDON = ZoneInfo("Europe/London")
PACIFIC = ZoneInfo("Pacific/Auckland")


@pytest.fixture
def person(tenant):
    return UserFactory(organization=tenant, full_name="Wanjiru Worker")


@pytest.fixture
def site(tenant):
    return SiteFactory()


def refresh(session):
    session.refresh_from_db()
    return session


class TestCloseStaleSessions:
    def test_before_the_hour_nothing_closes(self, tenant, person, site):
        session = make_session(tenant, person, site, start=8, end=None)
        assert sweeps.close_stale_sessions(tenant, now=at(MONDAY, 17, 59)) == 0
        assert refresh(session).clock_out_at is None

    def test_after_the_hour_closes_at_the_cutoff_not_the_sweep_time(
        self, tenant, person, site
    ):
        session = make_session(tenant, person, site, start=8, end=None)
        assert sweeps.close_stale_sessions(tenant, now=at(MONDAY, 22, 17)) == 1
        session = refresh(session)
        assert session.clock_out_at == at(MONDAY, 18)
        assert session.closed_by == ClosedBy.AUTO
        assert session.clock_out_received_at == at(MONDAY, 22, 17)
        assert session.out_lat is None and session.out_distance_m is None
        assert AuditLog.objects.filter(note__startswith="Closed automatically").exists()

    def test_at_the_hour_exactly_it_closes(self, tenant, person, site):
        session = make_session(tenant, person, site, start=8, end=None)
        sweeps.close_stale_sessions(tenant, now=at(MONDAY, 18))
        assert refresh(session).clock_out_at == at(MONDAY, 18)

    def test_a_session_opened_after_the_hour_closes_at_midnight(self, tenant, person, site):
        session = make_session(tenant, person, site, start=19, end=None)
        assert sweeps.close_stale_sessions(tenant, now=at(MONDAY, 23, 59)) == 0
        assert sweeps.close_stale_sessions(tenant, now=at(MONDAY + timedelta(days=1), 3)) == 1
        assert refresh(session).clock_out_at == at(MONDAY + timedelta(days=1), 0)

    def test_the_hour_is_the_tenants_setting(self, tenant, person, site):
        tenant.settings.clock_auto_close_hour = 20
        tenant.settings.save()
        session = make_session(tenant, person, site, start=8, end=None)
        assert sweeps.close_stale_sessions(tenant, now=at(MONDAY, 19, 30)) == 0
        sweeps.close_stale_sessions(tenant, now=at(MONDAY, 20, 5))
        assert refresh(session).clock_out_at == at(MONDAY, 20)

    def test_a_closed_session_is_left_alone(self, tenant, person, site):
        session = make_session(tenant, person, site, start=8, end=12)
        assert sweeps.close_stale_sessions(tenant, now=at(MONDAY, 23)) == 0
        assert refresh(session).closed_by == ClosedBy.PERSON

    @pytest.mark.parametrize("zone_name", ["Europe/London", "Pacific/Auckland"])
    def test_another_timezone_uses_its_own_local_day(self, tenant, person, site, zone_name):
        zone = ZoneInfo(zone_name)
        tenant.settings.timezone = zone_name
        tenant.settings.save()
        session = make_session(tenant, person, site, start=8, end=None, zone=zone)
        # Just before 18:00 local: still open. Just after: closed at 18:00 local.
        assert sweeps.close_stale_sessions(tenant, now=at(MONDAY, 17, 59, zone)) == 0
        assert sweeps.close_stale_sessions(tenant, now=at(MONDAY, 21, 0, zone)) == 1
        assert refresh(session).clock_out_at == at(MONDAY, 18, zone=zone)

    def test_the_same_instant_is_a_different_cutoff_in_another_zone(
        self, tenant, person, site
    ):
        # 17:30 Nairobi (14:30 UTC) is 14:30 in London in winter: before its 18:00.
        tenant.settings.timezone = "Europe/London"
        tenant.settings.save()
        session = make_session(tenant, person, site, start=8, end=None, zone=LONDON)
        assert sweeps.close_stale_sessions(tenant, now=at(MONDAY, 17, 30)) == 0
        assert refresh(session).clock_out_at is None

    def test_a_session_after_the_hour_in_london_closes_at_london_midnight(
        self, tenant, person, site
    ):
        tenant.settings.timezone = "Europe/London"
        tenant.settings.save()
        session = make_session(tenant, person, site, start=19, end=None, zone=LONDON)
        sweeps.close_stale_sessions(tenant, now=at(MONDAY + timedelta(days=1), 6, 0, LONDON))
        assert refresh(session).clock_out_at == at(MONDAY + timedelta(days=1), 0, zone=LONDON)


class TestFormDays:
    def test_a_finished_past_day_is_routed_and_pending(self, tenant, person, site):
        director_role = make_director_role(tenant)
        make_user(tenant, "Dora Director", director_role)
        session = make_session(tenant, person, site)

        assert sweeps.form_days(tenant, now=at(MONDAY + timedelta(days=1), 6)) == 1

        day = WorkDay.objects.get(pk=session.work_day_id)
        assert day.status == WorkDayStatus.PENDING
        assert day.formed_at is not None
        assert refresh(session).approval_request is not None

    def test_today_is_not_formed(self, tenant, person, site):
        session = make_session(tenant, person, site)
        assert sweeps.form_days(tenant, now=at(MONDAY, 23)) == 0
        assert WorkDay.objects.get(pk=session.work_day_id).status == WorkDayStatus.OPEN

    def test_a_day_with_an_open_session_waits(self, tenant, person, site):
        make_session(tenant, person, site, start=8, end=12)
        make_session(tenant, person, site, start=13, end=None)
        assert sweeps.form_days(tenant, now=at(MONDAY + timedelta(days=1), 6)) == 0

    def test_the_day_boundary_is_the_organizations_timezone(self, tenant, person, site):
        tenant.settings.timezone = "Pacific/Auckland"
        tenant.settings.save()
        make_session(tenant, person, site, zone=PACIFIC)
        # 13:00 UTC on Monday is already Tuesday 02:00 in Auckland (NZDT, UTC+13);
        # Nairobi would still call it Monday afternoon.
        instant = at(MONDAY, 13, 0).replace(tzinfo=ZoneInfo("UTC"))
        assert sweeps.form_days(tenant, now=instant) == 1

    def test_the_same_instant_is_still_today_in_nairobi(self, tenant, person, site):
        make_session(tenant, person, site)
        instant = at(MONDAY, 22, 0, NAIROBI)
        assert sweeps.form_days(tenant, now=instant) == 0

    def test_unrouted_sessions_notify_the_owner_once_across_runs(self, tenant, person, site):
        make_owner(tenant)
        make_session(tenant, person, site)  # no project, no Director role set
        later = at(MONDAY + timedelta(days=1), 6)
        sweeps.form_days(tenant, now=later)
        sweeps.form_days(tenant, now=later + timedelta(hours=1))
        assert NotificationEvent.objects.filter(event_key=Event.ATTENDANCE_UNROUTED).count() == 1

    def test_a_late_session_on_a_formed_day_is_routed_by_the_next_run(
        self, tenant, person, site
    ):
        director_role = make_director_role(tenant)
        make_user(tenant, "Dora Director", director_role)
        first = make_session(tenant, person, site, start=8, end=10)
        sweeps.form_days(tenant, now=at(MONDAY + timedelta(days=1), 6))
        refresh(first)
        late = make_session(tenant, person, site, start=13, end=15)
        assert sweeps.form_days(tenant, now=at(MONDAY + timedelta(days=1), 7)) == 1
        assert refresh(late).approval_request_id == first.approval_request_id

    def test_manager_routing_is_used_by_the_sweep(self, tenant, person, site):
        pm = make_user(tenant, "Pam PM")
        project = make_project(site, manager=pm)
        session = make_session(tenant, person, site, project=project)
        sweeps.form_days(tenant, now=at(MONDAY + timedelta(days=1), 6))
        assert refresh(session).approval_request.required_user == pm


class TestTasks:
    def test_the_tenant_task_closes_then_forms(self, tenant, person, site):
        make_owner(tenant)
        make_session(tenant, person, site, day=date(2026, 2, 2), start=8, end=None)
        result = sweeps.attendance_sweep_tenant(organization_id=str(tenant.pk))
        assert result["closed"] == 1
        assert result["formed"] == 1
        assert WorkSession.objects.get().closed_by == ClosedBy.AUTO

    def test_one_failing_step_does_not_stop_the_other(self, tenant, monkeypatch):
        def boom(_org):
            raise RuntimeError("nope")

        monkeypatch.setattr(sweeps, "close_stale_sessions", boom)
        result = sweeps.attendance_sweep_tenant(organization_id=str(tenant.pk))
        assert result["closed"] == "failed"
        assert result["formed"] == 0

    def test_the_dispatcher_fans_out_to_active_organizations(self, tenant, monkeypatch):
        seen = []
        monkeypatch.setattr(
            sweeps.attendance_sweep_tenant, "delay", lambda **kw: seen.append(kw)
        )
        assert sweeps.dispatch_attendance_sweep() == {"organizations": 1}
        assert seen == [{"organization_id": str(tenant.pk)}]

    def test_the_beat_entry_is_hourly_and_registered(self):
        from config.celery import app

        entry = django_settings.CELERY_BEAT_SCHEDULE["attendance-sweep"]
        assert entry["task"] == "attendance.sweeps.dispatch_attendance_sweep"
        assert str(entry["schedule"].minute) == "{5}"
        app.loader.import_default_modules()
        assert entry["task"] in app.tasks
