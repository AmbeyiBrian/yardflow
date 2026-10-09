"""T16.6 and T16.9 — routing a day, deciding it, and its notifications (§4.18.5, §4.18.10; R13)."""

import pytest

from accounts.factories import UserFactory
from approvals.engine import NotAnApprover
from approvals.models import ApprovalAction, ApprovalRequest, ApprovalRequestStatus
from attendance.models import WorkDay, WorkDayStatus
from attendance.routing import (
    WorkDayNotDecidable,
    WorkDaySelfApproval,
    decide,
    refresh_status,
    route_day,
)
from attendance.tests.support import (
    make_director_role,
    make_owner,
    make_project,
    make_session,
    make_user,
)
from commercials.finance import RejectionReasonRequired
from network.factories import SiteFactory
from notifications.matrix import Event
from notifications.models import NotificationDelivery, NotificationEvent

pytestmark = pytest.mark.django_db


@pytest.fixture
def person(tenant):
    return make_user(tenant, "Wanjiru Worker")


@pytest.fixture
def site(tenant):
    return SiteFactory()


@pytest.fixture
def director_role(tenant):
    return make_director_role(tenant)


@pytest.fixture
def director(tenant, director_role):
    return make_user(tenant, "Dora Director", director_role)


@pytest.fixture
def pm_a(tenant):
    return make_user(tenant, "Pam A")


@pytest.fixture
def pm_b(tenant):
    return make_user(tenant, "Pete B")


def day_of(session):
    return WorkDay.objects.get(pk=session.work_day_id)


def requests_for(day):
    return list(ApprovalRequest.objects.filter(document_id=str(day.pk)).order_by("id"))


def refreshed(*sessions):
    for item in sessions:
        item.refresh_from_db()


class TestRouteDay:
    def test_one_pm_gets_one_slice_and_the_day_is_pending(self, tenant, person, site, pm_a):
        project = make_project(site, manager=pm_a)
        a = make_session(tenant, person, site, start=8, end=10, project=project)
        b = make_session(tenant, person, site, start=11, end=13, project=project)

        created = route_day(day_of(a))

        assert len(created) == 1
        request = created[0]
        assert (request.level, request.required_user, request.status) == (
            1,
            pm_a,
            ApprovalRequestStatus.PENDING,
        )
        assert request.requested_by == person and request.due_at is None
        refreshed(a, b)
        assert a.approval_request == b.approval_request == request
        day = day_of(a)
        assert day.status == WorkDayStatus.PENDING and day.formed_at is not None

    def test_two_pms_give_two_parallel_slices(self, tenant, person, site, pm_a, pm_b):
        other_site = SiteFactory()
        a = make_session(tenant, person, site, start=8, end=10, project=make_project(site, pm_a))
        b = make_session(
            tenant, person, other_site, start=11, end=13, project=make_project(other_site, pm_b)
        )

        created = route_day(day_of(a))

        assert sorted(item.required_user_id for item in created) == sorted([pm_a.pk, pm_b.pk])
        assert {item.level for item in created} == {1}
        refreshed(a, b)
        assert a.approval_request != b.approval_request
        assert a.approval_request.required_user == pm_a
        assert b.approval_request.required_user == pm_b

    def test_no_project_goes_to_the_director_role(self, tenant, person, site, director):
        s = make_session(tenant, person, site)
        (request,) = route_day(day_of(s))
        assert request.required_role == tenant.settings.finance_director_role
        assert request.required_user is None

    def test_a_project_without_an_active_manager_goes_to_the_director(
        self, tenant, person, site, director
    ):
        s = make_session(tenant, person, site, project=make_project(site, manager=None))
        (request,) = route_day(day_of(s))
        assert request.required_role is not None

        gone = make_user(tenant, "Gone PM")
        gone.is_active = False
        gone.save()
        other = make_session(
            tenant, person, site, start=14, end=15, project=make_project(site, manager=gone)
        )
        route_day(day_of(other))
        other.refresh_from_db()
        assert other.approval_request == request  # same open Director slice reused

    def test_a_pms_own_sessions_go_to_the_director(self, tenant, site, director, pm_a):
        s = make_session(tenant, pm_a, site, project=make_project(site, manager=pm_a))
        (request,) = route_day(day_of(s))
        assert request.required_user is None
        assert request.required_role == tenant.settings.finance_director_role

    def test_a_directors_own_goes_to_the_role_and_another_holder_decides(
        self, tenant, site, director, director_role
    ):
        second = make_user(tenant, "Dan Director", director_role)
        s = make_session(tenant, director, site)
        (request,) = route_day(day_of(s))
        assert request.required_role == director_role

        with pytest.raises(WorkDaySelfApproval):
            decide(day_of(s), director, True)
        decide(day_of(s), second, True)
        assert day_of(s).status == WorkDayStatus.APPROVED

    def test_a_director_with_nobody_else_is_unrouted(self, tenant, site, director):
        make_owner(tenant)
        s = make_session(tenant, director, site)
        assert route_day(day_of(s)) == []
        s.refresh_from_db()
        assert s.approval_request is None
        assert NotificationEvent.objects.filter(event_key=Event.ATTENDANCE_UNROUTED).count() == 1

    def test_nobody_to_approve_leaves_it_unrouted_and_tells_the_owner(
        self, tenant, person, site
    ):
        make_owner(tenant)
        s = make_session(tenant, person, site)  # no Director role configured

        assert route_day(day_of(s)) == []

        s.refresh_from_db()
        assert s.approval_request is None
        assert not ApprovalRequest.objects.exists()
        [event] = NotificationEvent.objects.filter(event_key=Event.ATTENDANCE_UNROUTED)
        assert event.target_id == str(s.work_day_id)

    def test_an_open_session_is_not_routed(self, tenant, person, site, director):
        closed = make_session(tenant, person, site, start=8, end=10)
        open_ = make_session(tenant, person, site, start=11, end=None)
        route_day(day_of(closed))
        open_.refresh_from_db()
        assert open_.approval_request is None

    def test_it_is_idempotent(self, tenant, person, site, pm_a):
        project = make_project(site, manager=pm_a)
        s = make_session(tenant, person, site, project=project)
        first = route_day(day_of(s))
        again = route_day(day_of(s))
        assert len(first) == 1 and again == []
        assert ApprovalRequest.objects.count() == 1
        assert NotificationEvent.objects.filter(
            event_key=Event.ATTENDANCE_AWAITING_APPROVAL
        ).count() == 1

    def test_a_late_session_joins_an_open_slice(self, tenant, person, site, pm_a):
        project = make_project(site, manager=pm_a)
        first = make_session(tenant, person, site, start=8, end=10, project=project)
        route_day(day_of(first))
        late = make_session(tenant, person, site, start=13, end=15, project=project)
        assert route_day(day_of(first)) == []
        late.refresh_from_db()
        first.refresh_from_db()
        assert first.approval_request_id is not None
        assert late.approval_request_id == first.approval_request_id

    def test_a_late_session_reopens_only_its_own_slice(
        self, tenant, person, site, pm_a, pm_b
    ):
        other_site = SiteFactory()
        proj_a, proj_b = make_project(site, pm_a), make_project(other_site, pm_b)
        a = make_session(tenant, person, site, start=8, end=10, project=proj_a)
        b = make_session(tenant, person, other_site, start=11, end=13, project=proj_b)
        route_day(day_of(a))
        decide(day_of(a), pm_a, True)
        decide(day_of(a), pm_b, True)
        assert day_of(a).status == WorkDayStatus.APPROVED
        refreshed(a, b)
        approved_a, approved_b = a.approval_request, b.approval_request

        late = make_session(tenant, person, site, start=14, end=16, project=proj_a)
        (fresh,) = route_day(day_of(a))

        assert fresh.required_user == pm_a and fresh.status == ApprovalRequestStatus.PENDING
        late.refresh_from_db()
        refreshed(a, b)
        assert late.approval_request == fresh
        assert a.approval_request == approved_a and b.approval_request == approved_b
        approved_a.refresh_from_db()
        approved_b.refresh_from_db()
        assert approved_a.status == approved_b.status == ApprovalRequestStatus.APPROVED
        assert day_of(a).status == WorkDayStatus.PENDING


class TestDecide:
    @pytest.fixture
    def two_pm_day(self, tenant, person, site, pm_a, pm_b):
        other_site = SiteFactory()
        a = make_session(tenant, person, site, start=8, end=10, project=make_project(site, pm_a))
        b = make_session(
            tenant, person, other_site, start=11, end=13, project=make_project(other_site, pm_b)
        )
        route_day(day_of(a))
        return day_of(a), a, b

    def test_one_approval_leaves_the_day_pending(self, two_pm_day, pm_a):
        day, a, _b = two_pm_day
        decide(day, pm_a, True)
        assert day_of(a).status == WorkDayStatus.PENDING

    def test_every_slice_approved_approves_the_day(self, two_pm_day, pm_a, pm_b):
        day, *_ = two_pm_day
        decide(day, pm_a, True)
        decide(day, pm_b, True)
        assert day_of(day.sessions.first()).status == WorkDayStatus.APPROVED
        assert ApprovalAction.objects.count() == 2

    def test_mixed_decisions_reject_the_day_only_once_all_are_decided(
        self, two_pm_day, pm_a, pm_b
    ):
        day, a, b = two_pm_day
        decide(day, pm_a, False, "Wrong site")
        assert day_of(a).status == WorkDayStatus.PENDING  # B has not answered
        decide(day, pm_b, True)
        assert day_of(a).status == WorkDayStatus.REJECTED
        a.refresh_from_db()
        b.refresh_from_db()
        assert a.approval_request.status == ApprovalRequestStatus.REJECTED
        assert b.approval_request.status == ApprovalRequestStatus.APPROVED

    def test_a_rejection_does_not_supersede_the_other_slice(self, two_pm_day, pm_a):
        day, _a, b = two_pm_day
        decide(day, pm_a, False, "No")
        b.refresh_from_db()
        assert b.approval_request.status == ApprovalRequestStatus.PENDING

    def test_the_person_cannot_decide_their_own_day(self, two_pm_day, person):
        day, *_ = two_pm_day
        with pytest.raises(WorkDaySelfApproval) as exc:
            decide(day, person, True)
        assert exc.value.code == "WORK_DAY_SELF_APPROVAL" and exc.value.status_code == 403

    def test_a_pm_who_is_the_person_is_refused_too(self, tenant, site, director, pm_a):
        s = make_session(tenant, pm_a, site, project=make_project(site, pm_a))
        route_day(day_of(s))
        with pytest.raises(WorkDaySelfApproval):
            decide(day_of(s), pm_a, True)

    def test_rejecting_needs_a_reason(self, two_pm_day, pm_a):
        day, *_ = two_pm_day
        with pytest.raises(RejectionReasonRequired):
            decide(day, pm_a, False, "  ")
        assert day_of(day.sessions.first()).status == WorkDayStatus.PENDING

    def test_someone_not_addressed_is_refused(self, two_pm_day, tenant):
        day, *_ = two_pm_day
        with pytest.raises(NotAnApprover):
            decide(day, make_user(tenant, "Random"), True)

    def test_nothing_open_is_not_decidable(self, two_pm_day, pm_a, pm_b):
        day, *_ = two_pm_day
        decide(day, pm_a, True)
        decide(day, pm_b, True)
        with pytest.raises(WorkDayNotDecidable) as exc:
            decide(day, pm_a, True)
        assert exc.value.code == "WORK_DAY_NOT_DECIDABLE" and exc.value.status_code == 409

    def test_one_decision_answers_every_slice_addressed_to_the_actor(
        self, tenant, person, site
    ):
        pm = make_user(tenant, "Pam Both")
        other_site = SiteFactory()
        a = make_session(tenant, person, site, start=8, end=10, project=make_project(site, pm))
        make_session(
            tenant, person, other_site, start=11, end=13, project=make_project(other_site, pm)
        )
        # One manager across two projects is one slice, so it is one decision.
        assert len(route_day(day_of(a))) == 1
        decide(day_of(a), pm, True)
        assert day_of(a).status == WorkDayStatus.APPROVED

    def test_the_director_slice_is_decided_by_any_holder(
        self, tenant, person, site, director, director_role
    ):
        second = make_user(tenant, "Dan Director", director_role)
        s = make_session(tenant, person, site)
        route_day(day_of(s))
        decide(day_of(s), second, True)
        assert day_of(s).status == WorkDayStatus.APPROVED

    def test_the_action_row_records_the_reason_and_actor(self, two_pm_day, pm_a):
        day, *_ = two_pm_day
        decide(day, pm_a, False, "Wrong site")
        action = ApprovalAction.objects.get()
        assert action.actor == pm_a and action.reason == "Wrong site"

    def test_a_slice_already_decided_is_not_asked_again(self, two_pm_day, pm_a):
        day, *_ = two_pm_day
        decide(day, pm_a, True)
        with pytest.raises(NotAnApprover):
            decide(day, pm_a, True)

    def test_an_unrouted_session_keeps_the_day_pending(self, tenant, person, site, pm_a):
        s = make_session(tenant, person, site, project=make_project(site, pm_a))
        route_day(day_of(s))
        decide(day_of(s), pm_a, True)
        make_session(tenant, person, site, start=14, end=15)  # nobody routes this one yet
        assert refresh_status(day_of(s)) == WorkDayStatus.PENDING


class TestNotifications:
    def deliveries(self, key, user=None):
        found = NotificationDelivery.objects.filter(event__event_key=key)
        return found.filter(recipient=user) if user is not None else found

    def test_awaiting_approval_reaches_both_pms_once_each_and_never_the_person(
        self, tenant, person, site, pm_a, pm_b, django_capture_on_commit_callbacks
    ):
        other_site = SiteFactory()
        a = make_session(tenant, person, site, start=8, end=10, project=make_project(site, pm_a))
        make_session(
            tenant, person, other_site, start=11, end=13, project=make_project(other_site, pm_b)
        )
        with django_capture_on_commit_callbacks(execute=True):
            route_day(day_of(a))

        key = Event.ATTENDANCE_AWAITING_APPROVAL
        assert NotificationEvent.objects.filter(event_key=key).count() == 2  # one per slice
        for pm in (pm_a, pm_b):
            assert self.deliveries(key, pm).filter(channel="in_app").count() == 1
            assert self.deliveries(key, pm).filter(channel="email").count() == 1
        assert not self.deliveries(key, person).exists()

    def test_each_slice_notifies_only_its_own_addressee(
        self, tenant, person, site, pm_a, pm_b, django_capture_on_commit_callbacks
    ):
        other_site = SiteFactory()
        a = make_session(tenant, person, site, start=8, end=10, project=make_project(site, pm_a))
        make_session(
            tenant, person, other_site, start=11, end=13, project=make_project(other_site, pm_b)
        )
        with django_capture_on_commit_callbacks(execute=True):
            route_day(day_of(a))
        for event in NotificationEvent.objects.filter(
            event_key=Event.ATTENDANCE_AWAITING_APPROVAL
        ):
            who = set(
                NotificationDelivery.objects.filter(event=event).values_list(
                    "recipient_id", flat=True
                )
            )
            expected = ApprovalRequest.objects.get(pk=event.payload["approval_request_id"])
            assert who == {expected.required_user_id}

    def test_the_director_slice_reaches_every_holder(
        self, tenant, person, site, director, director_role, django_capture_on_commit_callbacks
    ):
        second = make_user(tenant, "Dan Director", director_role)
        s = make_session(tenant, person, site)
        with django_capture_on_commit_callbacks(execute=True):
            route_day(day_of(s))
        key = Event.ATTENDANCE_AWAITING_APPROVAL
        assert self.deliveries(key, director).exists() and self.deliveries(key, second).exists()

    def test_the_wording_names_person_date_hours_and_flags(
        self, tenant, person, site, pm_a, django_capture_on_commit_callbacks
    ):
        s = make_session(
            tenant, person, site, start=8, end=16, project=make_project(site, pm_a),
            area_changed=True,
        )  # fmt: skip
        with django_capture_on_commit_callbacks(execute=True):
            route_day(day_of(s))
        delivery = self.deliveries(Event.ATTENDANCE_AWAITING_APPROVAL, pm_a).get(
            channel="email"
        )
        assert delivery.subject == (
            "Work day waiting for your approval: Wanjiru Worker, Mon 2 Mar 2026, 8 h"
        )
        assert "Flags: area changed." in delivery.body
        assert "Open Approvals to decide." in delivery.body

    def test_a_rejection_tells_the_person_with_reason_and_approver(
        self, tenant, person, site, pm_a, django_capture_on_commit_callbacks
    ):
        s = make_session(tenant, person, site, project=make_project(site, pm_a))
        route_day(day_of(s))
        with django_capture_on_commit_callbacks(execute=True):
            decide(day_of(s), pm_a, False, "Not on site that day")

        key = Event.ATTENDANCE_REJECTED
        assert {d.channel for d in self.deliveries(key, person)} == {"in_app", "email"}
        assert not self.deliveries(key, pm_a).exists()
        body = self.deliveries(key, person).get(channel="email").body
        assert "Rejected by Pam A." in body
        assert "Reason: Not on site that day" in body
        assert "My time" in body

    def test_an_approval_sends_no_rejection(
        self, tenant, person, site, pm_a, django_capture_on_commit_callbacks
    ):
        s = make_session(tenant, person, site, project=make_project(site, pm_a))
        route_day(day_of(s))
        with django_capture_on_commit_callbacks(execute=True):
            decide(day_of(s), pm_a, True)
        assert not NotificationEvent.objects.filter(event_key=Event.ATTENDANCE_REJECTED).exists()

    def test_unrouted_goes_to_the_owner_in_app_only(
        self, tenant, person, site, django_capture_on_commit_callbacks
    ):
        owner = make_owner(tenant)
        s = make_session(tenant, person, site)
        with django_capture_on_commit_callbacks(execute=True):
            route_day(day_of(s))
        found = self.deliveries(Event.ATTENDANCE_UNROUTED)
        assert {(d.recipient_id, d.channel) for d in found} == {(owner.pk, "in_app")}
        assert "nobody to approve" in found.get().subject

    def test_the_notification_links_to_my_time(self, tenant, person, site):
        from notifications.views import resource_for

        assert resource_for("attendance.WorkDay", "7", Event.ATTENDANCE_REJECTED) == "/time"
        key = Event.ATTENDANCE_AWAITING_APPROVAL
        assert resource_for("attendance.WorkDay", "7", key) == "/time"

    def test_other_users_are_untouched(self, tenant, person, site, pm_a):
        stranger = UserFactory(organization=tenant)
        s = make_session(tenant, person, site, project=make_project(site, pm_a))
        route_day(day_of(s))
        assert not NotificationDelivery.objects.filter(recipient=stranger).exists()
