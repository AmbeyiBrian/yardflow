"""T16.7 — corrections and the Director-added day (§4.18.6, §4.18.6a; R13)."""

from datetime import timedelta

import pytest
from django.utils import timezone

from approvals.models import ApprovalRequest, ApprovalRequestStatus
from attendance import corrections
from attendance.corrections import (
    CorrectionNotAllowed,
    CorrectionReasonRequired,
    WorkDayAddNotAllowed,
    add_day,
    correct_session,
    effective_times,
)
from attendance.models import ClosedBy, CorrectionKind, WorkDay, WorkDayStatus, WorkSession
from attendance.routing import WorkDaySelfApproval, decide, route_day
from attendance.services import ClockOverlap, ClockTimeInvalid
from attendance.tests.support import (
    MONDAY,
    at,
    make_director_role,
    make_owner,
    make_project,
    make_session,
    make_user,
)
from core.exceptions import PermissionDeniedError
from core.models import AuditLog
from locations.factories import OfficeFactory
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


@pytest.fixture
def rejected(tenant, person, site, pm_a):
    """One session on pm_a's project, routed and rejected."""
    project = make_project(site, pm_a)
    session = make_session(tenant, person, site, start=8, end=12, project=project)
    route_day(day_of(session))
    decide(day_of(session), pm_a, False, "Hours look long")
    session.refresh_from_db()
    return session


@pytest.fixture
def rejected_two_pm(tenant, person, site, pm_a, pm_b):
    """pm_a rejected their slice; pm_b has not answered."""
    other_site = SiteFactory()
    a = make_session(tenant, person, site, start=8, end=10, project=make_project(site, pm_a))
    b = make_session(
        tenant, person, other_site, start=11, end=13, project=make_project(other_site, pm_b)
    )
    route_day(day_of(a))
    decide(day_of(a), pm_a, False, "No")
    a.refresh_from_db()
    b.refresh_from_db()
    return a, b


class TestEdit:
    def test_an_edit_reopens_the_slice_to_the_same_approver(self, rejected, person, pm_a):
        old = rejected.approval_request
        correction = correct_session(
            actor=person,
            session=rejected,
            kind=CorrectionKind.EDIT,
            corrected_out_at=at(MONDAY, 11),
            reason="I left at eleven",
        )

        rejected.refresh_from_db()
        new = rejected.approval_request
        assert new != old and new.status == ApprovalRequestStatus.PENDING
        assert new.required_user == pm_a and new.level == 1 and new.due_at is None
        assert (correction.rejected_request, correction.reopened_request) == (old, new)
        old.refresh_from_db()
        assert old.status == ApprovalRequestStatus.REJECTED  # history stays
        assert old.actions.count() == 1
        assert day_of(rejected).status == WorkDayStatus.PENDING

    def test_the_original_times_are_never_overwritten(self, rejected, person):
        correct_session(
            actor=person,
            session=rejected,
            kind=CorrectionKind.EDIT,
            corrected_in_at=at(MONDAY, 9),
            corrected_out_at=at(MONDAY, 11),
            reason="Late start",
        )
        rejected.refresh_from_db()
        assert rejected.clock_in_at == at(MONDAY, 8)
        assert rejected.clock_out_at == at(MONDAY, 12)
        assert effective_times(rejected) == (at(MONDAY, 9), at(MONDAY, 11))
        [row] = rejected.corrections.all()
        assert (row.original_in_at, row.original_out_at) == (at(MONDAY, 8), at(MONDAY, 12))
        assert corrections.hours_of([rejected]) == 2

    def test_it_is_flagged_corrected(self, rejected, person):
        assert "corrected" not in corrections.session_flags(rejected)
        correct_session(
            actor=person,
            session=rejected,
            kind=CorrectionKind.EDIT,
            corrected_out_at=at(MONDAY, 11),
            reason="x",
        )
        assert "corrected" in corrections.session_flags(rejected)

    def test_an_auto_closed_session_gets_its_clock_out(self, tenant, person, site, pm_a):
        project = make_project(site, pm_a)
        s = make_session(
            tenant, person, site, start=8, end=18, project=project, closed_by=ClosedBy.AUTO
        )
        route_day(day_of(s))
        decide(day_of(s), pm_a, False, "Auto closed")
        correct_session(
            actor=person,
            session=s,
            kind=CorrectionKind.EDIT,
            corrected_out_at=at(MONDAY, 16),
            reason="I left at four",
        )
        s.refresh_from_db()
        assert effective_times(s)[1] == at(MONDAY, 16)
        assert s.clock_out_at == at(MONDAY, 18)

    def test_the_approver_decides_the_reopened_slice_and_the_day_follows(
        self, rejected, person, pm_a
    ):
        correct_session(
            actor=person,
            session=rejected,
            kind=CorrectionKind.EDIT,
            corrected_out_at=at(MONDAY, 11),
            reason="x",
        )
        decide(day_of(rejected), pm_a, True)
        assert day_of(rejected).status == WorkDayStatus.APPROVED

    def test_a_second_rejection_allows_a_second_correction(self, rejected, person, pm_a):
        for hour in (11, 10):
            correct_session(
                actor=person,
                session=rejected,
                kind=CorrectionKind.EDIT,
                corrected_out_at=at(MONDAY, hour),
                reason=f"out at {hour}",
            )
            decide(day_of(rejected), pm_a, False, "Still wrong")
            rejected.refresh_from_db()
        assert rejected.corrections.count() == 2
        assert effective_times(rejected)[1] == at(MONDAY, 10)
        assert ApprovalRequest.objects.filter(document_id=str(rejected.work_day_id)).count() == 3

    def test_only_the_rejected_slice_reopens(
        self, rejected_two_pm, person, pm_a, pm_b
    ):
        a, b = rejected_two_pm
        pending_b = b.approval_request
        correct_session(
            actor=person,
            session=a,
            kind=CorrectionKind.EDIT,
            corrected_out_at=at(MONDAY, 9, 30),
            reason="x",
        )
        b.refresh_from_db()
        assert b.approval_request == pending_b
        pending_b.refresh_from_db()
        assert pending_b.status == ApprovalRequestStatus.PENDING
        decide(day_of(a), pm_b, True)
        assert day_of(a).status == WorkDayStatus.PENDING  # A's reopened slice is open

    def test_an_approved_other_slice_is_not_touched(self, rejected_two_pm, person, pm_a, pm_b):
        a, b = rejected_two_pm
        decide(day_of(a), pm_b, True)
        approved = ApprovalRequest.objects.get(pk=b.approval_request_id)
        correct_session(
            actor=person,
            session=a,
            kind=CorrectionKind.EDIT,
            corrected_out_at=at(MONDAY, 9, 30),
            reason="x",
        )
        approved.refresh_from_db()
        assert approved.status == ApprovalRequestStatus.APPROVED
        assert day_of(a).status == WorkDayStatus.PENDING
        decide(day_of(a), pm_a, True)
        assert day_of(a).status == WorkDayStatus.APPROVED

    def test_it_is_audited(self, rejected, person):
        correct_session(
            actor=person,
            session=rejected,
            kind=CorrectionKind.EDIT,
            corrected_out_at=at(MONDAY, 11),
            reason="x",
        )
        assert AuditLog.objects.filter(note__startswith="Corrected (edit)").exists()


class TestRules:
    def edit(self, person, session, **kw):
        values = {"corrected_out_at": at(MONDAY, 11), "reason": "x", **kw}
        return correct_session(
            actor=person, session=session, kind=CorrectionKind.EDIT, **values
        )

    def test_only_the_person_may_correct(self, rejected, pm_a):
        with pytest.raises(PermissionDeniedError):
            self.edit(pm_a, rejected)

    def test_a_pending_slice_cannot_be_corrected(self, tenant, person, site, pm_a):
        s = make_session(tenant, person, site, project=make_project(site, pm_a))
        route_day(day_of(s))
        with pytest.raises(CorrectionNotAllowed) as exc:
            self.edit(person, s)
        assert exc.value.code == "CORRECTION_NOT_ALLOWED" and exc.value.status_code == 409

    def test_an_approved_slice_cannot_be_corrected(self, tenant, person, site, pm_a):
        s = make_session(tenant, person, site, project=make_project(site, pm_a))
        route_day(day_of(s))
        decide(day_of(s), pm_a, True)
        with pytest.raises(CorrectionNotAllowed):
            self.edit(person, s)

    def test_an_unrouted_session_cannot_be_corrected(self, tenant, person, site):
        s = make_session(tenant, person, site)
        with pytest.raises(CorrectionNotAllowed):
            self.edit(person, s)

    def test_the_window_is_thirty_days_from_the_rejection(self, rejected, person):
        rejected_at = rejected.approval_request.resolved_at
        with pytest.raises(CorrectionNotAllowed):
            self.edit(person, rejected, now=rejected_at + timedelta(days=30, minutes=1))
        # Still inside the window on day 30.
        self.edit(person, rejected, now=rejected_at + timedelta(days=29, hours=23))

    def test_the_window_restarts_with_a_second_rejection(self, rejected, person, pm_a):
        self.edit(person, rejected)
        decide(day_of(rejected), pm_a, False, "Again")
        rejected.refresh_from_db()
        later = timezone.now() + timedelta(days=29)
        self.edit(person, rejected, now=later, corrected_out_at=at(MONDAY, 10))

    def test_a_reason_is_required(self, rejected, person):
        with pytest.raises(CorrectionReasonRequired) as exc:
            self.edit(person, rejected, reason="  ")
        assert exc.value.code == "CORRECTION_REASON_REQUIRED" and exc.value.status_code == 400
        rejected.refresh_from_db()
        assert rejected.approval_request.status == ApprovalRequestStatus.REJECTED

    def test_times_must_stay_on_the_day(self, rejected, person):
        with pytest.raises(ClockTimeInvalid):
            self.edit(person, rejected, corrected_out_at=at(MONDAY + timedelta(days=1), 1))

    def test_out_cannot_precede_in(self, rejected, person):
        with pytest.raises(ClockTimeInvalid):
            self.edit(person, rejected, corrected_out_at=at(MONDAY, 7))

    def test_it_cannot_overlap_another_session(self, tenant, rejected, person, site):
        make_session(tenant, person, site, start=13, end=15)
        with pytest.raises(ClockOverlap):
            self.edit(person, rejected, corrected_out_at=at(MONDAY, 14))

    def test_an_edit_needs_a_time(self, rejected, person):
        with pytest.raises(ClockTimeInvalid):
            self.edit(person, rejected, corrected_out_at=None)

    def test_a_refused_correction_changes_nothing(self, rejected, person):
        with pytest.raises(ClockTimeInvalid):
            self.edit(person, rejected, corrected_out_at=at(MONDAY, 7))
        assert not rejected.corrections.exists()
        assert ApprovalRequest.objects.filter(status=ApprovalRequestStatus.PENDING).count() == 0

    def test_a_second_submit_is_refused_once_reopened(self, rejected, person):
        self.edit(person, rejected)
        with pytest.raises(CorrectionNotAllowed):
            self.edit(person, rejected)


class TestAdd:
    def test_an_added_session_joins_the_reopened_slice(self, rejected, person, site, pm_a):
        office = OfficeFactory()
        correct_session(
            actor=person,
            session=rejected,
            kind=CorrectionKind.ADD,
            place=office,
            corrected_in_at=at(MONDAY, 13),
            corrected_out_at=at(MONDAY, 15),
            reason="I forgot to clock in after lunch",
        )
        sessions = list(WorkSession.objects.filter(work_day_id=rejected.work_day_id))
        assert len(sessions) == 2
        added = next(s for s in sessions if s.pk != rejected.pk)
        rejected.refresh_from_db()
        assert added.approval_request == rejected.approval_request
        assert added.approval_request.status == ApprovalRequestStatus.PENDING
        assert added.approval_request.required_user == pm_a
        assert added.in_lat is None and added.closed_by == ClosedBy.PERSON
        assert "corrected" in corrections.session_flags(added)
        [row] = added.work_day.corrections.all()
        assert row.kind == CorrectionKind.ADD and row.session is None
        assert row.location == office and row.reason.startswith("I forgot")
        assert row.reopened_request == added.approval_request

    def test_an_add_needs_a_place_and_both_times(self, rejected, person):
        with pytest.raises(Exception) as exc:
            correct_session(
                actor=person,
                session=rejected,
                kind=CorrectionKind.ADD,
                corrected_in_at=at(MONDAY, 13),
                corrected_out_at=at(MONDAY, 15),
                reason="x",
            )
        assert exc.value.code == "CLOCK_PLACE_REQUIRED"
        with pytest.raises(ClockTimeInvalid):
            correct_session(
                actor=person,
                session=rejected,
                kind=CorrectionKind.ADD,
                place=OfficeFactory(),
                corrected_in_at=at(MONDAY, 13),
                reason="x",
            )

    def test_an_add_only_on_a_rejected_slice(self, tenant, person, site, pm_a):
        s = make_session(tenant, person, site, project=make_project(site, pm_a))
        route_day(day_of(s))
        with pytest.raises(CorrectionNotAllowed):
            correct_session(
                actor=person,
                session=s,
                kind=CorrectionKind.ADD,
                place=OfficeFactory(),
                corrected_in_at=at(MONDAY, 13),
                corrected_out_at=at(MONDAY, 15),
                reason="x",
            )

    def test_an_add_cannot_overlap(self, rejected, person):
        with pytest.raises(ClockOverlap):
            correct_session(
                actor=person,
                session=rejected,
                kind=CorrectionKind.ADD,
                place=OfficeFactory(),
                corrected_in_at=at(MONDAY, 11),
                corrected_out_at=at(MONDAY, 14),
                reason="x",
            )


class TestCorrectionNotifications:
    def test_the_reopened_slice_tells_the_same_approver_tagged_corrected(
        self, rejected, person, pm_a, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=True):
            correct_session(
                actor=person,
                session=rejected,
                kind=CorrectionKind.EDIT,
                corrected_out_at=at(MONDAY, 11),
                reason="x",
            )
        events = NotificationEvent.objects.filter(
            event_key=Event.ATTENDANCE_AWAITING_APPROVAL
        ).order_by("id")
        last = events.last()
        assert last.payload["corrected"] is True
        email = NotificationDelivery.objects.get(event=last, recipient=pm_a, channel="email")
        assert email.subject.startswith("Work day waiting for your approval (corrected):")
        assert "corrected" not in (email.body.split("Flags:")[1] if "Flags:" in email.body else "")


class TestAddDay:
    @pytest.fixture
    def office(self):
        return OfficeFactory()

    def add(self, actor, person, place, **kw):
        values = {
            "date": MONDAY,
            "start": at(MONDAY, 8),
            "end": at(MONDAY, 16),
            "reason": "Phone placed him outside the area",
            **kw,
        }
        return add_day(actor=actor, person=person, place=place, **values)

    def test_a_director_adds_a_flagged_day_without_a_position(
        self, tenant, person, director, site, pm_a
    ):
        project = make_project(site, pm_a)
        session = self.add(director, person, site, project=project)

        assert session.added_by == director
        assert session.added_reason == "Phone placed him outside the area"
        assert session.closed_by == ClosedBy.PERSON
        assert session.in_lat is None and session.in_distance_m is None
        assert session.clock_in_at == at(MONDAY, 8) and session.clock_out_at == at(MONDAY, 16)
        assert "added by the Director" in corrections.session_flags(session)
        assert AuditLog.objects.filter(
            note__startswith="Added by the Director for"
        ).exists()

    def test_it_is_routed_to_the_pm_not_the_adder(self, tenant, person, director, site, pm_a):
        session = self.add(director, person, site, project=make_project(site, pm_a))
        assert session.approval_request.required_user == pm_a
        assert day_of(session).status == WorkDayStatus.PENDING
        decide(day_of(session), pm_a, True)
        assert day_of(session).status == WorkDayStatus.APPROVED

    def test_with_no_project_it_goes_to_another_director_never_the_adder(
        self, tenant, person, director, director_role, office
    ):
        second = make_user(tenant, "Dan Director", director_role)
        session = self.add(director, person, office)
        assert session.approval_request.required_role == director_role
        with pytest.raises(WorkDaySelfApproval):
            decide(day_of(session), director, True)
        decide(day_of(session), second, True)
        assert day_of(session).status == WorkDayStatus.APPROVED

    def test_with_no_other_approver_it_is_unrouted_and_the_adder_cannot_approve(
        self, tenant, person, director, office
    ):
        make_owner(tenant)
        session = self.add(director, person, office)
        assert session.approval_request is None
        assert NotificationEvent.objects.filter(event_key=Event.ATTENDANCE_UNROUTED).exists()

    def test_a_pm_who_is_also_the_adder_is_not_the_approver(
        self, tenant, person, director, director_role, site, office
    ):
        # The Director also manages the project: the add must go to the other Director.
        second = make_user(tenant, "Dan Director", director_role)
        project = make_project(site, manager=director)
        session = self.add(director, person, site, project=project)
        assert session.approval_request.required_role == director_role
        assert second.pk != director.pk

    def test_only_a_director_may_add(self, tenant, person, site, pm_a, director):
        with pytest.raises(WorkDayAddNotAllowed) as exc:
            self.add(pm_a, person, site)
        assert exc.value.code == "WORK_DAY_ADD_NOT_ALLOWED" and exc.value.status_code == 403
        assert not WorkSession.objects.exists()

    def test_nobody_may_add_when_no_director_role_is_set(self, tenant, person, site, pm_a):
        with pytest.raises(WorkDayAddNotAllowed):
            self.add(pm_a, person, site)

    def test_a_director_cannot_add_their_own_day(self, director, site):
        with pytest.raises(WorkDayAddNotAllowed):
            self.add(director, director, site)

    def test_a_reason_is_required(self, person, director, site):
        with pytest.raises(CorrectionReasonRequired):
            self.add(director, person, site, reason=" ")

    def test_times_must_be_on_the_date_and_not_in_the_future(self, person, director, site):
        with pytest.raises(ClockTimeInvalid):
            self.add(director, person, site, end=at(MONDAY + timedelta(days=1), 1))
        with pytest.raises(ClockTimeInvalid):
            self.add(
                director,
                person,
                site,
                now=at(MONDAY, 12),
                end=at(MONDAY, 16),
            )

    def test_it_cannot_overlap_the_persons_own_session(self, tenant, person, director, site):
        make_session(tenant, person, site, start=9, end=11)
        with pytest.raises(ClockOverlap):
            self.add(director, person, site)

    def test_the_place_must_be_clockable(self, person, director, site):
        site.latitude = None
        site.longitude = None
        site.save()
        with pytest.raises(Exception) as exc:
            self.add(director, person, site)
        assert exc.value.code == "PLACE_HAS_NO_COORDINATES"

    def test_it_joins_an_existing_open_slice(self, tenant, person, director, site, pm_a):
        project = make_project(site, pm_a)
        first = make_session(tenant, person, site, start=6, end=7, project=project)
        route_day(day_of(first))
        added = self.add(director, person, site, project=project, start=at(MONDAY, 9),
                         end=at(MONDAY, 11))  # fmt: skip
        first.refresh_from_db()
        assert added.approval_request == first.approval_request

    def test_a_rejected_added_day_can_be_corrected_by_the_person(
        self, tenant, person, director, site, pm_a
    ):
        session = self.add(director, person, site, project=make_project(site, pm_a))
        decide(day_of(session), pm_a, False, "Not convinced")
        correct_session(
            actor=person,
            session=session,
            kind=CorrectionKind.EDIT,
            corrected_out_at=at(MONDAY, 15),
            reason="Left at three",
        )
        session.refresh_from_db()
        assert session.approval_request.status == ApprovalRequestStatus.PENDING
