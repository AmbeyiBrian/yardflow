"""T16.8 (part 2) — decide, correct and the Director's add over HTTP (§4.18.5-6a; R13)."""

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.factories import RoleFactory, UserFactory, UserRoleFactory
from accounts.permissions_registry import PERM
from approvals.models import ApprovalRequest
from attendance.models import WorkDay, WorkDayStatus, WorkSession
from attendance.routing import route_day
from attendance.tests.support import (
    MONDAY,
    at,
    make_director_role,
    make_project,
    make_session,
    make_user,
)
from core.tenancy import tenant_context
from locations.factories import OfficeFactory
from network.factories import SiteFactory

pytestmark = pytest.mark.django_db


def client_for(user, settings):
    settings.TENANT_BASE_DOMAIN = "localhost"
    client = APIClient(HTTP_HOST="silvertech.localhost")
    client.force_authenticate(user)
    return client


@pytest.fixture
def person(tenant):
    return make_user(tenant, "Wanjiru Worker")


@pytest.fixture
def site(tenant):
    return SiteFactory()


@pytest.fixture
def pm_a(tenant):
    return make_user(tenant, "Pam A")


@pytest.fixture
def pm_b(tenant):
    return make_user(tenant, "Pete B")


@pytest.fixture
def director_role(tenant):
    return make_director_role(tenant)


@pytest.fixture
def director(tenant, director_role):
    return make_user(tenant, "Dora Director", director_role)


def day_of(session):
    return WorkDay.objects.get(pk=session.work_day_id)


def iso(day, hour, minute=0):
    return at(day, hour, minute).isoformat()


@pytest.fixture
def pm_day(tenant, person, site, pm_a):
    session = make_session(tenant, person, site, start=8, end=12, project=make_project(site, pm_a))
    route_day(day_of(session))
    return session


@pytest.fixture
def two_pm_day(tenant, person, site, pm_a, pm_b):
    other = SiteFactory()
    a = make_session(tenant, person, site, start=8, end=10, project=make_project(site, pm_a))
    b = make_session(tenant, person, other, start=11, end=13, project=make_project(other, pm_b))
    route_day(day_of(a))
    return a, b


class TestDecide:
    def test_the_pm_approves_their_day(self, pm_day, pm_a, settings):
        http = client_for(pm_a, settings)
        response = http.post(
            f"/api/v1/work-days/{pm_day.work_day_id}/decide", {"approved": True}, format="json"
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "APPROVED"
        assert body["slices"][0]["status"] == "APPROVED"
        assert body["slices"][0]["decided_at"] is not None

    def test_a_rejection_needs_a_reason_and_keeps_it(self, pm_day, pm_a, settings):
        http = client_for(pm_a, settings)
        url = f"/api/v1/work-days/{pm_day.work_day_id}/decide"
        refused = http.post(url, {"approved": False}, format="json")
        assert refused.status_code == 400
        assert day_of(pm_day).status == WorkDayStatus.PENDING

        body = http.post(url, {"approved": False, "reason": "Too long"}, format="json").json()
        assert body["status"] == "REJECTED"
        assert body["rejection_reason"] == "Too long"
        assert body["slices"][0]["reason"] == "Too long"

    def test_the_director_decides_a_director_routed_day(
        self, tenant, person, site, director, settings
    ):
        session = make_session(tenant, person, site)
        route_day(day_of(session))
        http = client_for(director, settings)
        day = http.get(f"/api/v1/work-days/{session.work_day_id}").json()
        assert day["slices"][0]["is_mine"] is True
        body = http.post(
            f"/api/v1/work-days/{session.work_day_id}/decide", {"approved": True}, format="json"
        ).json()
        assert body["status"] == "APPROVED"

    def test_nobody_decides_their_own_day(self, tenant, site, director, settings):
        mine = make_session(tenant, director, site)
        route_day(day_of(mine))
        response = client_for(director, settings).post(
            f"/api/v1/work-days/{mine.work_day_id}/decide", {"approved": True}, format="json"
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "WORK_DAY_SELF_APPROVAL"

    def test_someone_it_is_not_addressed_to_is_refused(self, two_pm_day, tenant, settings):
        a, _ = two_pm_day
        url = f"/api/v1/work-days/{a.work_day_id}/decide"
        # Cannot even see it.
        bystander = make_user(tenant, "Bea Bystander")
        denied = client_for(bystander, settings).post(url, {"approved": True}, format="json")
        assert denied.status_code == 404

        # Sees everyone's days, but none is addressed to them: refused, not hidden.
        viewer = UserFactory(organization=tenant)
        UserRoleFactory(
            user=viewer,
            role=RoleFactory(name="Viewer", codenames=[PERM.ATTENDANCE_VIEW_ALL]),
        )
        refused = client_for(viewer, settings).post(url, {"approved": True}, format="json")
        assert refused.status_code == 403
        assert day_of(a).status == WorkDayStatus.PENDING

    def test_one_pm_answering_leaves_the_day_pending(self, two_pm_day, pm_a, pm_b, settings):
        a, _ = two_pm_day
        url = f"/api/v1/work-days/{a.work_day_id}/decide"
        body = client_for(pm_a, settings).post(url, {"approved": True}, format="json").json()
        assert body["status"] == "PENDING"
        assert sorted(s["status"] for s in body["slices"]) == ["APPROVED", "PENDING"]
        assert [s["is_mine"] for s in body["slices"] if s["approver"] == pm_a.pk] == [False]
        final = client_for(pm_b, settings).post(url, {"approved": True}, format="json").json()
        assert final["status"] == "APPROVED"

    def test_deciding_twice_is_not_decidable(self, pm_day, pm_a, settings):
        http = client_for(pm_a, settings)
        url = f"/api/v1/work-days/{pm_day.work_day_id}/decide"
        http.post(url, {"approved": True}, format="json")
        again = http.post(url, {"approved": True}, format="json")
        assert again.status_code == 409
        assert again.json()["error"]["code"] == "WORK_DAY_NOT_DECIDABLE"

    def test_slices_flag_only_the_callers_as_mine(self, two_pm_day, pm_a, pm_b, settings):
        a, _ = two_pm_day
        slices = (
            client_for(pm_b, settings).get(f"/api/v1/work-days/{a.work_day_id}").json()["slices"]
        )
        assert {s["approver"]: s["is_mine"] for s in slices} == {pm_a.pk: False, pm_b.pk: True}
        assert all(s["project"] is not None for s in slices)


@pytest.fixture
def rejected(pm_day, pm_a, settings):
    client_for(pm_a, settings).post(
        f"/api/v1/work-days/{pm_day.work_day_id}/decide",
        {"approved": False, "reason": "Check hours"},
        format="json",
    )
    pm_day.refresh_from_db()
    return pm_day


class TestCorrect:
    def test_edit_reopens_the_slice_for_the_same_pm(self, rejected, person, pm_a, settings):
        http = client_for(person, settings)
        assert http.get(f"/api/v1/work-sessions/{rejected.pk}").json()["can_correct"] is True
        response = http.post(
            f"/api/v1/work-sessions/{rejected.pk}/correct",
            {"kind": "EDIT", "corrected_out_at": iso(MONDAY, 11), "reason": "I left at eleven"},
            format="json",
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "PENDING"
        (open_slice,) = [s for s in body["slices"] if s["status"] == "PENDING"]
        assert open_slice["approver"] == pm_a.pk
        assert body["hours"] == "3.00"
        assert "CORRECTED" in body["sessions"][0]["flags"]
        assert body["sessions"][0]["corrections"][0]["reason"] == "I left at eleven"

    def test_add_a_missing_session_to_the_slice(self, rejected, person, settings):
        response = client_for(person, settings).post(
            f"/api/v1/work-sessions/{rejected.pk}/correct",
            {
                "kind": "ADD",
                "corrected_in_at": iso(MONDAY, 13),
                "corrected_out_at": iso(MONDAY, 15),
                "reason": "Forgot to clock in",
            },
            format="json",
        )
        assert response.status_code == 200
        body = response.json()
        assert body["session_count"] == 2
        assert body["hours"] == "6.00"
        assert body["status"] == "PENDING"

    def test_only_the_owner_corrects(self, rejected, pm_a, settings):
        response = client_for(pm_a, settings).post(
            f"/api/v1/work-sessions/{rejected.pk}/correct",
            {"kind": "EDIT", "corrected_out_at": iso(MONDAY, 11), "reason": "x"},
            format="json",
        )
        assert response.status_code == 403

    def test_a_reason_is_required(self, rejected, person, settings):
        response = client_for(person, settings).post(
            f"/api/v1/work-sessions/{rejected.pk}/correct",
            {"kind": "EDIT", "corrected_out_at": iso(MONDAY, 11)},
            format="json",
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "CORRECTION_REASON_REQUIRED"

    def test_outside_the_window_is_refused(self, rejected, person, settings):
        ApprovalRequest.objects.filter(pk=rejected.approval_request_id).update(
            resolved_at=timezone.now() - timedelta(days=31)
        )
        http = client_for(person, settings)
        late = http.post(
            f"/api/v1/work-sessions/{rejected.pk}/correct",
            {"kind": "EDIT", "corrected_out_at": iso(MONDAY, 11), "reason": "late"},
            format="json",
        )
        assert late.status_code == 409
        assert late.json()["error"]["code"] == "CORRECTION_NOT_ALLOWED"
        assert http.get(f"/api/v1/work-sessions/{rejected.pk}").json()["can_correct"] is False

    def test_a_pending_slice_cannot_be_corrected(self, pm_day, person, settings):
        response = client_for(person, settings).post(
            f"/api/v1/work-sessions/{pm_day.pk}/correct",
            {"kind": "EDIT", "corrected_out_at": iso(MONDAY, 11), "reason": "x"},
            format="json",
        )
        assert response.status_code == 409


class TestAddDay:
    def body(self, person, place, **extra):
        return {
            "person": person.pk,
            "date": MONDAY.isoformat(),
            "place": place,
            "start": iso(MONDAY, 8),
            "end": iso(MONDAY, 12),
            "reason": "Phone died",
            **extra,
        }

    def test_the_director_adds_a_day_routed_to_the_pm(self, person, site, pm_a, director, settings):
        project = make_project(site, pm_a)
        response = client_for(director, settings).post(
            "/api/v1/work-days/add",
            self.body(person, {"site": site.pk}, project=project.pk),
            format="json",
        )
        assert response.status_code == 201
        body = response.json()
        assert body["person"] == person.pk
        assert body["hours"] == "4.00"
        assert body["status"] == "PENDING"
        assert body["slices"][0]["approver"] == pm_a.pk
        assert "ADDED_BY_DIRECTOR" in body["sessions"][0]["flags"]

    def test_at_a_location(self, person, director, settings):
        office = OfficeFactory()
        response = client_for(director, settings).post(
            "/api/v1/work-days/add", self.body(person, {"location": office.pk}), format="json"
        )
        assert response.status_code == 201
        assert response.json()["sessions"][0]["location"] == office.pk

    def test_a_director_day_never_goes_to_its_adder(self, person, site, director, settings):
        response = client_for(director, settings).post(
            "/api/v1/work-days/add", self.body(person, {"site": site.pk}), format="json"
        )
        assert response.status_code == 201
        assert all(s["is_mine"] is False for s in response.json()["slices"])

    def test_only_the_director_may_add(self, person, site, pm_a, settings):
        response = client_for(pm_a, settings).post(
            "/api/v1/work-days/add", self.body(person, {"site": site.pk}), format="json"
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "WORK_DAY_ADD_NOT_ALLOWED"
        assert not WorkSession.objects.filter(person=person).exists()

    def test_not_for_their_own_day(self, director, site, settings):
        response = client_for(director, settings).post(
            "/api/v1/work-days/add", self.body(director, {"site": site.pk}), format="json"
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "WORK_DAY_ADD_NOT_ALLOWED"

    def test_reason_and_place_are_required(self, person, site, director, settings):
        http = client_for(director, settings)
        no_reason = http.post(
            "/api/v1/work-days/add", self.body(person, {"site": site.pk}, reason=""), format="json"
        )
        assert no_reason.json()["error"]["code"] == "CORRECTION_REASON_REQUIRED"
        no_place = http.post("/api/v1/work-days/add", self.body(person, {}), format="json")
        assert no_place.status_code == 400

    def test_plain_post_to_the_collection_is_refused(self, director, settings):
        assert client_for(director, settings).post("/api/v1/work-days", {}).status_code == 403


class TestIsolation:
    def test_another_tenants_day_and_person_are_not_reachable(
        self, tenant, other_organization, director, pm_a, settings
    ):
        with tenant_context(other_organization):
            rival = UserFactory(organization=other_organization)
            rival_session = make_session(other_organization, rival, SiteFactory())
            rival_day = rival_session.work_day

        http = client_for(pm_a, settings)
        decide = http.post(f"/api/v1/work-days/{rival_day.pk}/decide", {"approved": True})
        assert decide.status_code == 404
        correct = http.post(
            f"/api/v1/work-sessions/{rival_session.pk}/correct", {"kind": "EDIT", "reason": "x"}
        )
        assert correct.status_code == 404

        add = client_for(director, settings).post(
            "/api/v1/work-days/add",
            {
                "person": rival.pk,
                "date": MONDAY.isoformat(),
                "place": {"site": SiteFactory().pk},
                "start": iso(MONDAY, 8),
                "end": iso(MONDAY, 12),
                "reason": "x",
            },
            format="json",
        )
        assert add.status_code in (400, 404)
