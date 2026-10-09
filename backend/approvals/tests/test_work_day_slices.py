"""T16.3 — the approval engine's work-day slices (§4.18.5, R13).

A day can need two approvers at once: one level-1 request per slice, answered
in parallel. These pin the six engine changes, and that nothing changes for
gate-outs or finance entries.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.factories import RoleFactory, UserFactory, UserRoleFactory
from accounts.permissions_registry import PERM
from approvals.addressing import open_requests_addressed_to
from approvals.engine import (
    NotAnApprover,
    NothingToApprove,
    SelfApprovalNotAllowed,
    WorkDaySlice,
    can_approve,
    create_requests,
    next_pending_request,
    readdress_project_requests,
    record_decision,
    required_levels,
)
from approvals.models import ApprovalDecision, ApprovalRequest, ApprovalRequestStatus
from attendance.models import WorkDay, WorkSession
from network.factories import ProjectFactory, SiteFactory
from notifications.events import _resolve_group
from notifications.matrix import Recipient

NOW = timezone.now().replace(microsecond=0)


@pytest.fixture
def person(tenant):
    return UserFactory(organization=tenant, full_name="Wanjiru Worker")


@pytest.fixture
def pm_a(tenant):
    return UserFactory(organization=tenant, full_name="Pam A")


@pytest.fixture
def pm_b(tenant):
    return UserFactory(organization=tenant, full_name="Pete B")


@pytest.fixture
def director_role(tenant):
    return RoleFactory(name="Director")


@pytest.fixture
def director(tenant, director_role):
    user = UserFactory(organization=tenant, full_name="Dora Director")
    UserRoleFactory(user=user, role=director_role)
    return user


@pytest.fixture
def work_day(tenant, person):
    return WorkDay.objects.create(organization=tenant, person=person, date=date(2026, 3, 2))


@pytest.fixture
def slices(work_day, person, pm_a, pm_b):
    first = create_requests(
        work_day, requested_by=person, work_day_slice=WorkDaySlice(user=pm_a)
    )[0]
    second = create_requests(
        work_day, requested_by=person, work_day_slice=WorkDaySlice(user=pm_b)
    )[0]
    return first, second


def blanket_role():
    return RoleFactory(name="Approver", codenames=[PERM.GATE_OUT_APPROVE])


class TestRequiredLevels:
    def test_a_slice_is_one_level_one_request_with_no_due_date(self, work_day, pm_a):
        levels = required_levels(work_day, work_day_slice=WorkDaySlice(user=pm_a))
        assert [(item.level, item.user) for item in levels] == [(1, pm_a)]

        (request,) = create_requests(work_day, work_day_slice=WorkDaySlice(user=pm_a))
        assert request.level == 1
        assert request.required_user == pm_a
        assert request.due_at is None

    def test_a_director_slice_is_addressed_by_role_and_never_escalates(
        self, work_day, director_role
    ):
        (request,) = create_requests(work_day, work_day_slice=WorkDaySlice(role=director_role))
        assert request.required_role == director_role
        assert request.required_user is None
        assert request.due_at is None

    def test_two_slices_are_parallel_level_one_requests(self, work_day, slices):
        assert sorted(item.level for item in slices) == [1, 1]
        assert ApprovalRequest.objects.filter(document_id=str(work_day.pk)).count() == 2

    def test_a_slice_needs_exactly_one_addressee(self, work_day, pm_a, director_role):
        with pytest.raises(ValueError):
            required_levels(work_day)
        with pytest.raises(ValueError):
            required_levels(work_day, work_day_slice=WorkDaySlice())
        with pytest.raises(ValueError):
            required_levels(
                work_day, work_day_slice=WorkDaySlice(user=pm_a, role=director_role)
            )


class TestCanApprove:
    def test_the_owner_of_the_day_is_refused_even_as_the_named_pm(self, tenant, work_day, person):
        """A PM clocking in is their own day's person: the O6 exception must not apply."""
        (request,) = create_requests(
            work_day, requested_by=person, work_day_slice=WorkDaySlice(user=person)
        )
        assert can_approve(person, request, document=work_day) == (False, "self")
        # Same answer from the request alone, as the pending list asks it.
        assert can_approve(person, request) == (False, "self")

    def test_self_approval_setting_does_not_open_it(self, tenant, work_day, person, director_role):
        tenant.settings.allow_self_approval = True
        tenant.settings.save()
        UserRoleFactory(user=person, role=director_role)
        (request,) = create_requests(
            work_day, requested_by=person, work_day_slice=WorkDaySlice(role=director_role)
        )
        assert can_approve(person, request, document=work_day) == (False, "self")

    def test_only_the_named_pm_may_answer_their_slice(self, work_day, slices, pm_a, pm_b):
        first, second = slices
        assert can_approve(pm_a, first, document=work_day) == (True, "")
        assert can_approve(pm_b, first, document=work_day) == (False, "not_the_manager")
        assert can_approve(pm_a, second, document=work_day) == (False, "not_the_manager")

    def test_the_gate_out_blanket_does_not_reach_a_director_slice(
        self, work_day, person, director_role, director
    ):
        blanket = UserFactory(organization=work_day.organization)
        UserRoleFactory(user=blanket, role=blanket_role())
        (request,) = create_requests(
            work_day, requested_by=person, work_day_slice=WorkDaySlice(role=director_role)
        )
        assert can_approve(blanket, request, document=work_day) == (False, "role")
        assert can_approve(director, request, document=work_day) == (True, "")


class TestRecordDecision:
    def test_a_work_day_decision_names_its_slice(self, work_day, slices, pm_a):
        with pytest.raises(ValueError):
            record_decision(work_day, actor=pm_a, decision=ApprovalDecision.APPROVED)

    def test_next_pending_request_takes_a_slice(self, work_day, slices):
        first, second = slices
        assert next_pending_request(work_day, approval_request=second) == second
        record_decision(
            work_day, actor=second.required_user, decision=ApprovalDecision.APPROVED,
            approval_request=second,
        )
        assert next_pending_request(work_day, approval_request=second) is None
        assert next_pending_request(work_day, approval_request=first) == first

    def test_approving_one_slice_leaves_the_other_pending(self, work_day, slices, pm_a):
        first, second = slices
        decided, following = record_decision(
            work_day, actor=pm_a, decision=ApprovalDecision.APPROVED, approval_request=first
        )
        assert decided.status == ApprovalRequestStatus.APPROVED
        assert following is None
        second.refresh_from_db()
        assert second.status == ApprovalRequestStatus.PENDING

    def test_a_rejected_slice_supersedes_only_itself(self, work_day, slices, pm_a):
        first, second = slices
        record_decision(
            work_day, actor=pm_a, decision=ApprovalDecision.REJECTED,
            reason="Not on site", approval_request=first,
        )
        first.refresh_from_db()
        second.refresh_from_db()
        assert first.status == ApprovalRequestStatus.REJECTED
        assert second.status == ApprovalRequestStatus.PENDING

    def test_the_wrong_manager_cannot_decide_a_slice(self, work_day, slices, pm_b):
        first, _second = slices
        with pytest.raises(NotAnApprover):
            record_decision(
                work_day, actor=pm_b, decision=ApprovalDecision.APPROVED, approval_request=first
            )

    def test_the_day_owner_cannot_decide_their_own_slice(self, work_day, person):
        (request,) = create_requests(
            work_day, requested_by=person, work_day_slice=WorkDaySlice(user=person)
        )
        with pytest.raises(SelfApprovalNotAllowed):
            record_decision(
                work_day, actor=person, decision=ApprovalDecision.APPROVED,
                approval_request=request,
            )

    def test_a_decided_slice_cannot_be_decided_again(self, work_day, slices, pm_a):
        first, _second = slices
        record_decision(
            work_day, actor=pm_a, decision=ApprovalDecision.APPROVED, approval_request=first
        )
        with pytest.raises(NothingToApprove):
            record_decision(
                work_day, actor=pm_a, decision=ApprovalDecision.APPROVED, approval_request=first
            )


class TestAddressing:
    def test_a_pm_sees_only_their_own_slice(self, work_day, slices, pm_a, pm_b):
        first, second = slices
        base = ApprovalRequest.objects.all()
        assert list(open_requests_addressed_to(pm_a, base)) == [first]
        assert list(open_requests_addressed_to(pm_b, base)) == [second]

    def test_slices_do_not_queue_behind_each_other(self, work_day, slices, pm_b):
        # Both are level 1: the second is not hidden by the first being open.
        assert open_requests_addressed_to(pm_b, ApprovalRequest.objects.all()).count() == 1

    def test_the_gate_out_blanket_excludes_work_days(
        self, tenant, work_day, person, director_role, director
    ):
        blanket = UserFactory(organization=tenant)
        UserRoleFactory(user=blanket, role=blanket_role())
        create_requests(
            work_day, requested_by=person, work_day_slice=WorkDaySlice(role=director_role)
        )
        base = ApprovalRequest.objects.all()
        assert open_requests_addressed_to(blanket, base).count() == 0
        assert open_requests_addressed_to(director, base).count() == 1

    def test_the_blanket_still_sees_other_role_levels(self, tenant):
        """Gate-out routing is unchanged: a role-addressed level is visible to the blanket."""
        role = RoleFactory(name="Supervisor")
        ApprovalRequest.objects.create(
            organization=tenant, document_type="gateout.GateOut", document_id="1",
            document_number="GO-1", level=1, required_role=role,
        )
        blanket = UserFactory(organization=tenant)
        UserRoleFactory(user=blanket, role=blanket_role())
        assert open_requests_addressed_to(blanket, ApprovalRequest.objects.all()).count() == 1


class TestNotification:
    def test_every_open_slice_approver_is_told(self, tenant, work_day, slices, pm_a, pm_b):
        told = _resolve_group(tenant, Recipient.LEVEL_APPROVERS, work_day)
        assert {user.pk for user in told} == {pm_a.pk, pm_b.pk}

    def test_a_decided_slice_is_no_longer_told(self, tenant, work_day, slices, pm_a, pm_b):
        first, _second = slices
        record_decision(
            work_day, actor=pm_a, decision=ApprovalDecision.APPROVED, approval_request=first
        )
        assert _resolve_group(tenant, Recipient.LEVEL_APPROVERS, work_day) == [pm_b]

    def test_the_person_is_never_told_to_approve_their_own_day(
        self, tenant, work_day, person, pm_a
    ):
        create_requests(work_day, requested_by=person, work_day_slice=WorkDaySlice(user=person))
        create_requests(work_day, requested_by=person, work_day_slice=WorkDaySlice(user=pm_a))
        assert _resolve_group(tenant, Recipient.LEVEL_APPROVERS, work_day) == [pm_a]


class TestReaddress:
    def make_session(self, tenant, person, work_day, project, request):
        return WorkSession.objects.create(
            organization=tenant, person=person, work_day=work_day, local_date=work_day.date,
            site=SiteFactory(), project=project, approval_request=request,
            clock_in_at=NOW, clock_in_received_at=NOW, clock_out_at=NOW + timedelta(hours=8),
        )

    def test_reassigning_a_pm_moves_only_that_projects_slice(
        self, tenant, work_day, person, pm_a, pm_b
    ):
        new_pm = UserFactory(organization=tenant, full_name="New PM")
        project_a = ProjectFactory(
            reference="WO-1601", po_number="PO-1601", manager=pm_a,
            contract_value=Decimal("1000.00"), cost_budget=Decimal("800.00"),
        )
        project_b = ProjectFactory(
            reference="WO-1602", po_number="PO-1602", manager=pm_b,
            contract_value=Decimal("1000.00"), cost_budget=Decimal("800.00"),
        )
        first = create_requests(
            work_day, requested_by=person, work_day_slice=WorkDaySlice(user=pm_a)
        )[0]
        second = create_requests(
            work_day, requested_by=person, work_day_slice=WorkDaySlice(user=pm_b)
        )[0]
        session = self.make_session(tenant, person, work_day, project_a, first)
        self.make_session(tenant, person, work_day, project_b, second)

        project_a.manager = new_pm
        project_a.save()
        moved = readdress_project_requests(project_a, old_manager_id=pm_a.pk)

        assert moved == 1
        first.refresh_from_db()
        second.refresh_from_db()
        assert first.required_user == new_pm
        assert second.required_user == pm_b
        session.refresh_from_db()
        assert session.project_id == project_a.pk


class TestOtherRoutingUnchanged:
    def test_a_gate_out_style_request_still_supersedes_every_later_level(self, tenant):
        """The one-chain behaviour: rejecting level 1 supersedes the rest."""
        # Covered end to end by the gate-out and finance suites; this pins that a
        # non-work-day document needs no slice and no approval_request argument.
        role = RoleFactory(name="Chain")
        approver = UserFactory(organization=tenant)
        UserRoleFactory(user=approver, role=role)

        class Doc:
            pk = 77
            organization_id = tenant.pk
            organization = tenant
            number = "D-77"
            requested_by_id = None

            class _meta:
                label = "stock.Chain"

        one = ApprovalRequest.objects.create(
            organization=tenant, document_type="stock.Chain", document_id="77",
            document_number="D-77", level=1, required_role=role,
        )
        two = ApprovalRequest.objects.create(
            organization=tenant, document_type="stock.Chain", document_id="77",
            document_number="D-77", level=2, required_role=role,
        )
        record_decision(Doc, actor=approver, decision=ApprovalDecision.REJECTED, reason="No")
        one.refresh_from_db()
        two.refresh_from_db()
        assert one.status == ApprovalRequestStatus.REJECTED
        assert two.status == ApprovalRequestStatus.SUPERSEDED
