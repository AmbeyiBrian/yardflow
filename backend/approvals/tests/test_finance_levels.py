"""T15.2 — routing and deciding money-out entries (§4.17.3; R4, D22, D28).

Two levels for an expense or an allowance: the project's PM, then whoever holds
``finance.approve``. What these pin is who may *not* act: the recorder at either
level whatever the tenant's self-approval setting says, a delegate, and anyone
who merely holds a role with a similar name.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.factories import DelegationFactory, RoleFactory, UserFactory, UserRoleFactory
from accounts.permissions_registry import PERM
from approvals.engine import (
    NotAnApprover,
    ProjectHasNoActiveManager,
    SelfApprovalNotAllowed,
    can_approve,
    create_requests,
    record_decision,
    required_levels,
)
from approvals.models import ApprovalDecision, ApprovalRequest, ApprovalRequestStatus
from commercials.models import (
    AllowanceRequest,
    AllowanceType,
    ExpenseCategory,
    ProjectExpense,
)
from network.factories import ProjectFactory


@pytest.fixture
def pm(tenant):
    return UserFactory(organization=tenant, full_name="Pippa Manager")


@pytest.fixture
def recorder(tenant):
    return UserFactory(organization=tenant, full_name="Tom Technician")


@pytest.fixture
def finance_role(tenant):
    return RoleFactory(name="Finance", codenames=[PERM.FINANCE_APPROVE])


@pytest.fixture
def finance_user(tenant, finance_role):
    user = UserFactory(organization=tenant, full_name="Fiona Finance")
    UserRoleFactory(user=user, role=finance_role)
    return user


@pytest.fixture
def project(tenant, pm):
    return ProjectFactory(
        reference="WO-9801",
        po_number="PO-980",
        manager=pm,
        contract_value=Decimal("100000.00"),
        cost_budget=Decimal("80000.00"),
    )


@pytest.fixture
def category(tenant):
    return ExpenseCategory.objects.create(organization=tenant, name="Misc")


def expense_by(tenant, project, category, who):
    return ProjectExpense.objects.create(
        organization=tenant,
        project=project,
        category=category,
        amount=Decimal("1000.00"),
        incurred_on=date(2026, 10, 1),
        recorded_by=who,
    )


def allowance_by(tenant, project, who):
    return AllowanceRequest.objects.create(
        organization=tenant,
        number="AR-000001",
        type=AllowanceType.FLOAT,
        amount=Decimal("5000.00"),
        from_date=date(2026, 10, 1),
        to_date=date(2026, 10, 1),
        project=project,
        recorded_by=who,
    )


def shape(levels):
    """(level, who) pairs, with the addressee reduced to something comparable."""
    return [(level.level, level.user or level.permission or level.role) for level in levels]


@pytest.mark.django_db
class TestRouting:
    def test_an_ordinary_expense_goes_to_the_pm_then_finance(
        self, tenant, project, category, recorder, pm
    ):
        levels = required_levels(expense_by(tenant, project, category, recorder))

        assert shape(levels) == [(1, pm), (2, "finance.approve")]

    def test_an_allowance_routes_the_same_way(self, tenant, project, recorder, pm):
        levels = required_levels(allowance_by(tenant, project, recorder))

        assert shape(levels) == [(1, pm), (2, "finance.approve")]

    def test_a_pm_recording_their_own_entry_skips_the_pm_level(
        self, tenant, project, category, pm
    ):
        levels = required_levels(expense_by(tenant, project, category, pm))

        assert shape(levels) == [(2, "finance.approve")]

    def test_a_director_skips_the_pm_level_when_the_role_is_set(
        self, tenant, project, category, recorder
    ):
        director = RoleFactory(name="Director")
        UserRoleFactory(user=recorder, role=director)
        tenant.settings.finance_director_role = director
        tenant.settings.save()

        levels = required_levels(expense_by(tenant, project, category, recorder))

        assert shape(levels) == [(2, "finance.approve")]

    def test_nobody_skips_when_the_director_role_is_unset(
        self, tenant, project, category, recorder, pm
    ):
        director = RoleFactory(name="Director")
        UserRoleFactory(user=recorder, role=director)
        assert tenant.settings.finance_director_role is None

        levels = required_levels(expense_by(tenant, project, category, recorder))

        assert shape(levels) == [(1, pm), (2, "finance.approve")]

    def test_holding_some_other_role_does_not_skip(
        self, tenant, project, category, recorder, pm
    ):
        tenant.settings.finance_director_role = RoleFactory(name="Director")
        tenant.settings.save()
        UserRoleFactory(user=recorder, role=RoleFactory(name="Storekeeper"))

        levels = required_levels(expense_by(tenant, project, category, recorder))

        assert [level.level for level in levels] == [1, 2]

    def test_a_project_without_a_manager_is_refused(self, tenant, category, recorder):
        """D28: no fallback approver, so no routing round the PM."""
        project = ProjectFactory(reference="WO-9802")

        with pytest.raises(ProjectHasNoActiveManager):
            required_levels(expense_by(tenant, project, category, recorder))

    def test_an_inactive_manager_is_refused(self, tenant, project, category, recorder, pm):
        pm.is_active = False
        pm.save()

        with pytest.raises(ProjectHasNoActiveManager):
            required_levels(expense_by(tenant, project, category, recorder))

    def test_the_director_skip_still_needs_no_manager(self, tenant, category, recorder):
        """A skipped PM level does not look at the manager at all."""
        director = RoleFactory(name="Director")
        UserRoleFactory(user=recorder, role=director)
        tenant.settings.finance_director_role = director
        tenant.settings.save()
        project = ProjectFactory(reference="WO-9803")

        levels = required_levels(expense_by(tenant, project, category, recorder))

        assert shape(levels) == [(2, "finance.approve")]


@pytest.mark.django_db
class TestCreatingRequests:
    def test_requests_carry_the_addressee_and_never_a_due_date(
        self, tenant, project, category, recorder, pm
    ):
        expense = expense_by(tenant, project, category, recorder)

        first, second = create_requests(expense, requested_by=recorder)

        assert (first.level, first.required_user) == (1, pm)
        assert first.required_permission == "" and first.required_role is None
        assert (second.level, second.required_permission) == (2, "finance.approve")
        assert second.required_user is None and second.required_role is None
        # D22/R4: nothing escalates, so the sweep never picks these up.
        assert first.due_at is None and second.due_at is None
        assert first.document_type == "commercials.ProjectExpense"
        assert first.requested_by == recorder


@pytest.mark.django_db
class TestNoSelfApproval:
    @pytest.mark.parametrize("allow_self", [False, True])
    def test_the_recorder_cannot_approve_at_the_pm_level(
        self, tenant, project, category, pm, finance_user, allow_self
    ):
        """The O6 exception (a PM approving their own) does not carry over."""
        tenant.settings.allow_self_approval = allow_self
        tenant.settings.save()
        # Routing would skip level 1 for a PM-recorded entry, so the request is
        # built by hand: this is the case the O6 exception would have allowed.
        expense = expense_by(tenant, project, category, pm)
        request = ApprovalRequest.objects.create(
            organization=tenant,
            document_type="commercials.ProjectExpense",
            document_id=str(expense.pk),
            level=1,
            required_user=pm,
            requested_by=pm,
        )

        assert can_approve(pm, request, document=expense) == (False, "self")

    @pytest.mark.parametrize("allow_self", [False, True])
    def test_the_recorder_cannot_approve_at_the_finance_level(
        self, tenant, project, category, pm, finance_user, allow_self
    ):
        """A finance user's own entry still needs *someone else* at level 2."""
        tenant.settings.allow_self_approval = allow_self
        tenant.settings.save()
        expense = expense_by(tenant, project, category, finance_user)
        request = create_requests(expense, requested_by=finance_user)[-1]

        assert can_approve(finance_user, request, document=expense) == (False, "self")

        record_decision(expense, actor=pm, decision=ApprovalDecision.APPROVED)
        with pytest.raises(SelfApprovalNotAllowed):
            record_decision(
                expense, actor=finance_user, decision=ApprovalDecision.APPROVED
            )

    def test_the_recorder_is_refused_even_without_a_document(
        self, tenant, project, category, finance_user
    ):
        """The request remembers who asked, so a caller that forgot the document is still safe."""
        expense = expense_by(tenant, project, category, finance_user)
        request = create_requests(expense, requested_by=finance_user)[-1]

        assert can_approve(finance_user, request) == (False, "self")


@pytest.mark.django_db
class TestPermissionLevel:
    def test_a_holder_of_the_permission_may_approve(
        self, tenant, project, category, recorder, finance_user
    ):
        expense = expense_by(tenant, project, category, recorder)
        level_two = create_requests(expense, requested_by=recorder)[-1]

        assert can_approve(finance_user, level_two, document=expense) == (True, "")

    def test_someone_without_it_may_not(self, tenant, project, category, recorder):
        expense = expense_by(tenant, project, category, recorder)
        level_two = create_requests(expense, requested_by=recorder)[-1]
        stranger = UserFactory(organization=tenant)

        assert can_approve(stranger, level_two, document=expense) == (False, "permission")

    def test_a_role_with_the_same_name_but_not_the_permission_does_not_count(
        self, tenant, project, category, recorder
    ):
        expense = expense_by(tenant, project, category, recorder)
        level_two = create_requests(expense, requested_by=recorder)[-1]
        impostor = UserFactory(organization=tenant)
        UserRoleFactory(user=impostor, role=RoleFactory(name="Finance 2"))

        assert can_approve(impostor, level_two, document=expense)[0] is False

    def test_the_gate_out_blanket_permission_is_not_a_finance_override(
        self, tenant, project, category, recorder
    ):
        """B4 lets it act on role levels; a permission level is a different thing."""
        expense = expense_by(tenant, project, category, recorder)
        level_two = create_requests(expense, requested_by=recorder)[-1]
        approver = UserFactory(organization=tenant)
        UserRoleFactory(
            user=approver, role=RoleFactory(codenames=[PERM.GATE_OUT_APPROVE])
        )

        assert can_approve(approver, level_two, document=expense)[0] is False

    def test_a_delegation_does_not_lend_it(
        self, tenant, project, category, recorder, finance_user
    ):
        """D22: a signature is not lendable."""
        expense = expense_by(tenant, project, category, recorder)
        level_two = create_requests(expense, requested_by=recorder)[-1]
        delegate = UserFactory(organization=tenant)
        delegation = DelegationFactory(
            from_user=finance_user,
            to_user=delegate,
            role=finance_user.user_roles.first().role,
            starts_at=timezone.now() - timedelta(hours=1),
            ends_at=timezone.now() + timedelta(hours=1),
        )
        assert delegation.pk

        assert can_approve(delegate, level_two, document=expense)[0] is False

    def test_an_inactive_holder_may_not(
        self, tenant, project, category, recorder, finance_user
    ):
        expense = expense_by(tenant, project, category, recorder)
        level_two = create_requests(expense, requested_by=recorder)[-1]
        finance_user.is_active = False
        finance_user.save()

        assert can_approve(finance_user, level_two, document=expense)[0] is False

    def test_only_the_named_pm_may_answer_level_one(
        self, tenant, project, category, recorder, pm, finance_user
    ):
        expense = expense_by(tenant, project, category, recorder)
        level_one = create_requests(expense, requested_by=recorder)[0]

        assert can_approve(pm, level_one, document=expense) == (True, "")
        assert can_approve(finance_user, level_one, document=expense) == (
            False,
            "not_the_manager",
        )


@pytest.mark.django_db
class TestTheTwoLevelsEndToEnd:
    def test_pm_then_finance_resolves_both(
        self, tenant, project, category, recorder, pm, finance_user
    ):
        expense = expense_by(tenant, project, category, recorder)
        create_requests(expense, requested_by=recorder)

        decided, still_open = record_decision(
            expense, actor=pm, decision=ApprovalDecision.APPROVED
        )
        assert decided.level == 1 and still_open is not None and still_open.level == 2

        # Finance cannot jump the queue, and cannot be answered by the PM.
        with pytest.raises(NotAnApprover):
            record_decision(expense, actor=pm, decision=ApprovalDecision.APPROVED)

        decided, still_open = record_decision(
            expense, actor=finance_user, decision=ApprovalDecision.APPROVED
        )
        assert decided.level == 2 and still_open is None
        assert not ApprovalRequest.objects.filter(
            status=ApprovalRequestStatus.PENDING
        ).exists()

    def test_the_action_is_not_marked_self_approved(
        self, tenant, project, category, recorder, pm
    ):
        expense = expense_by(tenant, project, category, recorder)
        create_requests(expense, requested_by=recorder)

        decided, _ = record_decision(expense, actor=pm, decision=ApprovalDecision.APPROVED)

        assert decided.actions.get().self_approved is False

    def test_a_rejection_supersedes_the_finance_level(
        self, tenant, project, category, recorder, pm
    ):
        expense = expense_by(tenant, project, category, recorder)
        create_requests(expense, requested_by=recorder)

        record_decision(
            expense, actor=pm, decision=ApprovalDecision.REJECTED, reason="Not ours"
        )

        statuses = list(
            ApprovalRequest.objects.order_by("level").values_list("status", flat=True)
        )
        assert statuses == [
            ApprovalRequestStatus.REJECTED,
            ApprovalRequestStatus.SUPERSEDED,
        ]
