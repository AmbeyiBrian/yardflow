"""T15.3 — recording, routing, deciding and paying money out (§4.17; R1-R5).

The services are the one door for an expense or an allowance, online or replayed
from a phone, so what is pinned here is behaviour a person would notice: where an
entry lands, who may not touch it, what is refused and what the refusal says.
"""

import threading
import uuid
from datetime import date
from decimal import Decimal

import pytest
from django.db import connection, transaction
from django.utils import timezone

from accounts.factories import RoleFactory, UserFactory, UserRoleFactory
from accounts.permissions_registry import PERM
from approvals.engine import NotAnApprover, ProjectHasNoActiveManager
from approvals.models import ApprovalRequest
from commercials import finance
from commercials.costing import cost_for
from commercials.finance import (
    AllowanceLimit,
    AllowanceOverlap,
    CasualIdDuplicate,
    CasualLineInput,
    FinanceInputInvalid,
    FinanceNoOtherApprover,
    FinanceNotDecidable,
    FinanceSelfApproval,
    FloatBackedNotPayable,
    FloatNotOpen,
    FuelVehicleRequired,
    PaymentReferenceRequired,
    ProjectAmbiguous,
    ProjectNotOpen,
    RejectionReasonRequired,
    SiteHasNoOpenProject,
    SiteNotOnProject,
    TransportScopeRequired,
)
from commercials.models import (
    AllowanceRequest,
    Casual,
    ExpenseCategory,
    ExpenseKind,
    ExpenseStatus,
    ProjectExpense,
)
from commercials.services import reverse_expense
from commercials.tests.finance_helpers import approve_through, reject_at_pm
from core.models import AuditLog
from core.tenancy import tenant_context
from network.factories import ProjectFactory, SiteFactory
from network.models import ProjectStatus

D = Decimal
DAY = date(2026, 10, 5)


@pytest.fixture
def pm(tenant):
    return UserFactory(organization=tenant, full_name="Pippa Manager")


@pytest.fixture
def tech(tenant):
    return UserFactory(organization=tenant, full_name="Tom Technician")


@pytest.fixture
def project(tenant, pm):
    return ProjectFactory(
        reference="WO-9901",
        po_number="PO-990",
        manager=pm,
        contract_value=D("500000.00"),
        cost_budget=D("400000.00"),
    )


@pytest.fixture
def site(tenant, project):
    site = SiteFactory(name="Ruiru")
    project.sites.add(
        site,
        through_defaults={"organization_id": project.organization_id},
    )
    return site


@pytest.fixture
def category(tenant):
    return ExpenseCategory.objects.create(organization=tenant, name="Misc")


@pytest.fixture
def fuel(tenant):
    return ExpenseCategory.objects.create(
        organization=tenant, name="Fuel", kind=ExpenseKind.FUEL
    )


@pytest.fixture
def labour(tenant):
    return ExpenseCategory.objects.create(
        organization=tenant, name="Casual labour", kind=ExpenseKind.CASUAL_LABOUR
    )


def spend(tech, category, site, amount="1000.00", **extra):
    return finance.record_expense(
        actor=tech,
        category=category,
        amount=D(amount),
        incurred_on=DAY,
        site=site,
        **extra,
    )


def ask(actor, site, type="NIGHT_OUT", amount="2000", start=DAY, end=None, **extra):
    return finance.request_allowance(
        actor=actor,
        type=type,
        amount=D(amount),
        from_date=start,
        to_date=end or start,
        site=site,
        **extra,
    )


def levels_of(entry):
    return list(
        ApprovalRequest.objects.filter(
            document_type=entry._meta.label, document_id=str(entry.pk)
        )
        .order_by("id")
        .values_list("level", "status")
    )


def paid_float(tech, pm, finance_user, site, amount="5000"):
    float_request = ask(tech, site, type="FLOAT", amount=amount)
    approve_through(float_request, pm=pm, finance_user=finance_user)
    finance.mark_paid(float_request, actor=finance_user, reference="MPESA-F1")
    return float_request


# --------------------------------------------------------------------------
# Site to project (R1)
# --------------------------------------------------------------------------


@pytest.mark.django_db
class TestOpenProjectsOf:
    def test_lists_open_projects_by_reference(self, tenant, project, site, pm):
        other = ProjectFactory(reference="WO-9900", manager=pm)
        other.sites.add(site, through_defaults={"organization_id": other.organization_id})

        assert [p.reference for p in finance.open_projects_of(site)] == ["WO-9900", "WO-9901"]

    def test_never_raises_and_skips_closed_projects(self, tenant, project, site):
        assert finance.open_projects_of(site) == [project]
        type(project).objects.filter(pk=project.pk).update(
            status=ProjectStatus.CLOSED, closed_at=timezone.now()
        )

        assert finance.open_projects_of(site) == []


@pytest.mark.django_db
class TestResolveProject:
    def test_one_open_project_is_chosen_for_you(self, tenant, project, site):
        assert finance.resolve_project(site) == project

    def test_two_open_projects_need_a_choice_and_are_listed(self, tenant, project, site, pm):
        other = ProjectFactory(reference="WO-9902", manager=pm)
        other.sites.add(
            site,
            through_defaults={"organization_id": other.organization_id},
        )
        with pytest.raises(ProjectAmbiguous) as caught:
            finance.resolve_project(site)

        assert caught.value.status_code == 400
        listed = {item["reference"] for item in caught.value.details["candidates"]}
        assert listed == {"WO-9901", "WO-9902"}
        assert finance.resolve_project(site, other) == other

    def test_a_closed_project_does_not_count_as_a_candidate(self, tenant, project, site, pm):
        closed = ProjectFactory(reference="WO-9903", manager=pm)
        closed.sites.add(
            site,
            through_defaults={"organization_id": closed.organization_id},
        )
        type(closed).objects.filter(pk=closed.pk).update(
            status=ProjectStatus.CLOSED, closed_at=timezone.now()
        )

        assert finance.resolve_project(site) == project

    def test_a_site_with_only_closed_projects_has_none(self, tenant, project, site):
        type(project).objects.filter(pk=project.pk).update(
            status=ProjectStatus.CLOSED, closed_at=timezone.now()
        )

        with pytest.raises(SiteHasNoOpenProject):
            finance.resolve_project(site)

    def test_a_project_the_site_is_not_on_is_refused(self, tenant, project, site, pm):
        elsewhere = ProjectFactory(reference="WO-9904", manager=pm)

        with pytest.raises(SiteNotOnProject):
            finance.resolve_project(site, elsewhere)

    def test_a_project_alone_is_accepted_if_open(self, tenant, project):
        assert finance.resolve_project(None, project) == project

    def test_a_closed_project_alone_is_refused(self, tenant, project):
        type(project).objects.filter(pk=project.pk).update(
            status=ProjectStatus.CLOSED, closed_at=timezone.now()
        )
        project.refresh_from_db()

        with pytest.raises(ProjectNotOpen):
            finance.resolve_project(None, project)


# --------------------------------------------------------------------------
# Routing (R4, §4.17.3)
# --------------------------------------------------------------------------


@pytest.mark.django_db
class TestRouting:
    def test_an_ordinary_expense_waits_on_the_pm_then_finance(
        self, tenant, project, site, tech, category, finance_user
    ):
        expense = spend(tech, category, site)

        assert expense.status == ExpenseStatus.PENDING_PM
        assert expense.project == project
        assert levels_of(expense) == [(1, "PENDING"), (2, "PENDING")]

    def test_a_pm_recorded_expense_goes_straight_to_finance(
        self, tenant, project, site, pm, category, finance_user
    ):
        expense = spend(pm, category, site)

        assert expense.status == ExpenseStatus.PENDING_FINANCE
        assert levels_of(expense) == [(2, "PENDING")]

    def test_a_director_skips_the_pm_only_when_the_role_is_set(
        self, tenant, project, site, tech, category, finance_user
    ):
        director = RoleFactory(name="Director")
        UserRoleFactory(user=tech, role=director)

        assert spend(tech, category, site).status == ExpenseStatus.PENDING_PM

        tenant.settings.finance_director_role = director
        tenant.settings.save()
        assert spend(tech, category, site).status == ExpenseStatus.PENDING_FINANCE

    def test_an_allowance_routes_the_same_way(
        self, tenant, project, site, tech, pm, finance_user
    ):
        assert ask(tech, site).status == ExpenseStatus.PENDING_PM
        assert ask(pm, site).status == ExpenseStatus.PENDING_FINANCE

    def test_no_pm_refuses_and_leaves_nothing_behind(
        self, tenant, project, site, tech, category, finance_user
    ):
        type(project).objects.filter(pk=project.pk).update(po_number="", manager=None)

        with pytest.raises(ProjectHasNoActiveManager):
            spend(tech, category, site)
        with pytest.raises(ProjectHasNoActiveManager):
            ask(tech, site)

        assert ProjectExpense.objects.count() == 0
        assert AllowanceRequest.objects.count() == 0
        assert ApprovalRequest.objects.count() == 0

    def test_an_inactive_pm_refuses_too(
        self, tenant, project, site, tech, pm, category, finance_user
    ):
        pm.is_active = False
        pm.save()

        with pytest.raises(ProjectHasNoActiveManager):
            spend(tech, category, site)

    def test_a_pm_level_that_is_skipped_does_not_need_an_active_pm(
        self, tenant, project, site, category, finance_user
    ):
        """The Director's expense never asks the PM, so a missing PM is not in the way."""
        director = RoleFactory(name="Director")
        director_user = UserFactory(organization=tenant)
        UserRoleFactory(user=director_user, role=director)
        tenant.settings.finance_director_role = director
        tenant.settings.save()
        type(project).objects.filter(pk=project.pk).update(po_number="", manager=None)

        assert spend(director_user, category, site).status == ExpenseStatus.PENDING_FINANCE

    def test_nobody_but_the_recorder_can_give_the_finance_approval(
        self, tenant, project, site, tech, category
    ):
        """With no Finance holder at all, or only the recorder, it could never be approved."""
        with pytest.raises(FinanceNoOtherApprover) as caught:
            spend(tech, category, site)
        assert caught.value.status_code == 409

        role = RoleFactory(name="Finance", codenames=[PERM.FINANCE_APPROVE])
        UserRoleFactory(user=tech, role=role)
        with pytest.raises(FinanceNoOtherApprover):
            spend(tech, category, site)
        with pytest.raises(FinanceNoOtherApprover):
            ask(tech, site)

    def test_an_inactive_finance_holder_does_not_count(
        self, tenant, project, site, tech, category, finance_user
    ):
        finance_user.is_active = False
        finance_user.save()

        with pytest.raises(FinanceNoOtherApprover):
            spend(tech, category, site)

    def test_a_second_finance_holder_lets_a_finance_user_record(
        self, tenant, project, site, category, finance_user
    ):
        role = RoleFactory(name="Finance 2", codenames=[PERM.FINANCE_APPROVE])
        second = UserFactory(organization=tenant)
        UserRoleFactory(user=second, role=role)

        assert spend(finance_user, category, site).status == ExpenseStatus.PENDING_PM


# --------------------------------------------------------------------------
# Recording an expense (R1)
# --------------------------------------------------------------------------


@pytest.mark.django_db
class TestRecordExpense:
    def test_it_keeps_what_the_person_said(
        self, tenant, project, site, tech, category, finance_user
    ):
        expense = spend(
            tech,
            category,
            site,
            scope_of_work="Replace the feeder",
            description="Cable ties",
            photos_expected=2,
        )

        assert expense.site == site
        assert expense.recorded_by == tech
        assert expense.scope_of_work == "Replace the feeder"
        assert expense.photos_expected == 2

    def test_a_project_given_directly_needs_no_site(
        self, tenant, project, tech, category, finance_user
    ):
        expense = finance.record_expense(
            actor=tech, category=category, amount=D("300"), incurred_on=DAY, project=project
        )

        assert expense.site is None
        assert expense.project == project

    def test_an_ambiguous_site_is_refused_until_a_project_is_chosen(
        self, tenant, project, site, tech, pm, category, finance_user
    ):
        other = ProjectFactory(reference="WO-9905", manager=pm)
        other.sites.add(
            site,
            through_defaults={"organization_id": other.organization_id},
        )
        with pytest.raises(ProjectAmbiguous):
            spend(tech, category, site)
        assert spend(tech, category, site, project=other).project == other

    def test_a_job_on_another_project_is_refused(
        self, tenant, project, site, tech, pm, category, finance_user
    ):
        from jobs.models import Job

        elsewhere = ProjectFactory(reference="WO-9906", manager=pm)
        job_site = SiteFactory(name="Elsewhere")
        job = Job.objects.create(
            organization=tenant,
            reference="JOB-X1",
            client=job_site.client,
            site=job_site,
            project=elsewhere,
            assignee=tech,
        )

        with pytest.raises(FinanceInputInvalid):
            spend(tech, category, site, job=job)

    def test_the_amount_must_be_positive(self, tenant, project, site, tech, category, finance_user):
        with pytest.raises(FinanceInputInvalid):
            spend(tech, category, site, amount="0")

    def test_fuel_needs_the_vehicle_registration(
        self, tenant, project, site, tech, fuel, finance_user
    ):
        with pytest.raises(FuelVehicleRequired) as caught:
            spend(tech, fuel, site)
        assert "vehicle_reg" in caught.value.field_errors

        expense = spend(tech, fuel, site, vehicle_reg="KDA 123A", litres=D("40.5"))
        assert expense.vehicle_reg == "KDA 123A"
        assert expense.litres == D("40.5")

    def test_casual_labour_needs_a_casual_with_days(
        self, tenant, project, site, tech, labour, finance_user
    ):
        casual = finance.register_casual(actor=tech, name="Kamau", id_number="12345678")

        with pytest.raises(FinanceInputInvalid):
            spend(tech, labour, site)
        with pytest.raises(FinanceInputInvalid):
            spend(tech, labour, site, casual_lines=[CasualLineInput(casual, 0)])

        expense = spend(
            tech, labour, site, casual_lines=[CasualLineInput(casual, 2, D("600"))]
        )
        line = expense.casual_lines.get()
        assert (line.casual, line.days, line.amount) == (casual, 2, D("600"))

    def test_the_same_casual_cannot_be_listed_twice(
        self, tenant, project, site, tech, labour, finance_user
    ):
        casual = finance.register_casual(actor=tech, name="Kamau", id_number="12345678")

        with pytest.raises(FinanceInputInvalid):
            spend(
                tech,
                labour,
                site,
                casual_lines=[CasualLineInput(casual, 1), CasualLineInput(casual, 2)],
            )

    def test_only_casual_labour_takes_casuals(
        self, tenant, project, site, tech, category, finance_user
    ):
        casual = finance.register_casual(actor=tech, name="Kamau", id_number="12345678")

        with pytest.raises(FinanceInputInvalid):
            spend(tech, category, site, casual_lines=[CasualLineInput(casual, 1)])

    def test_a_refused_casual_expense_leaves_no_partial_row(
        self, tenant, project, site, tech, labour, finance_user
    ):
        type(project).objects.filter(pk=project.pk).update(po_number="", manager=None)
        casual = finance.register_casual(actor=tech, name="Kamau", id_number="12345678")

        with pytest.raises(ProjectHasNoActiveManager):
            spend(tech, labour, site, casual_lines=[CasualLineInput(casual, 1)])

        assert ProjectExpense.objects.count() == 0

    def test_it_is_audited(self, tenant, project, site, tech, category, finance_user):
        expense = spend(tech, category, site)

        assert AuditLog.objects.filter(
            target_type=expense._meta.label, target_id=str(expense.pk), actor=tech
        ).exists()

    def test_a_replay_returns_the_same_expense(
        self, tenant, project, site, tech, category, finance_user
    ):
        token = uuid.uuid4()
        first = spend(tech, category, site, client_uuid=token)
        again = spend(tech, category, site, client_uuid=token)

        assert again.pk == first.pk
        assert ProjectExpense.objects.count() == 1
        assert ApprovalRequest.objects.count() == 2


# --------------------------------------------------------------------------
# Floats and spending from them (R2)
# --------------------------------------------------------------------------


@pytest.mark.django_db
class TestFloats:
    def test_balance_is_amount_less_live_expenses_less_returned(
        self, tenant, project, site, tech, pm, category, finance_user
    ):
        float_request = paid_float(tech, pm, finance_user, site, "5000")
        assert finance.float_balance(float_request) == D("5000.00")

        pending = spend(tech, category, site, "1200", float_request=float_request)
        # A pending expense counts: the money is already spent.
        assert finance.float_balance(float_request) == D("3800.00")

        reject_at_pm(pending, pm=pm)
        assert finance.float_balance(float_request) == D("5000.00")

        spend(tech, category, site, "2000", float_request=float_request)
        finance.close_float(float_request, actor=finance_user, returned_amount=D("500"))
        assert finance.float_balance(float_request) == D("2500.00")

    def test_it_may_be_overspent_and_then_reads_negative(
        self, tenant, project, site, tech, pm, category, finance_user
    ):
        float_request = paid_float(tech, pm, finance_user, site, "1000")
        spend(tech, category, site, "1500", float_request=float_request)

        assert finance.float_balance(float_request) == D("-500.00")

    def test_an_expense_needs_an_open_paid_float_of_the_recorders(
        self, tenant, project, site, tech, pm, category, finance_user
    ):
        unpaid = ask(tech, site, type="FLOAT", amount="5000")
        with pytest.raises(FloatNotOpen):
            spend(tech, category, site, float_request=unpaid)

        mine = paid_float(tech, pm, finance_user, site)
        stranger = UserFactory(organization=tenant, full_name="Sam Stranger")
        with pytest.raises(FloatNotOpen):
            spend(stranger, category, site, float_request=mine)

        not_a_float = ask(tech, site, type="NIGHT_OUT")
        approve_through(not_a_float, pm=pm, finance_user=finance_user)
        finance.mark_paid(not_a_float, actor=finance_user, reference="X1")
        with pytest.raises(FloatNotOpen):
            spend(tech, category, site, float_request=not_a_float)

        finance.close_float(mine, actor=finance_user, returned_amount=D("0"))
        with pytest.raises(FloatNotOpen):
            spend(tech, category, site, float_request=mine)

    def test_closing_records_what_came_back(
        self, tenant, project, site, tech, pm, finance_user
    ):
        float_request = paid_float(tech, pm, finance_user, site)

        closed = finance.close_float(float_request, actor=finance_user, returned_amount=D("750"))

        assert closed.closed_at is not None
        assert closed.closed_by == finance_user
        assert closed.returned_amount == D("750.00")

    def test_closing_twice_or_before_payment_is_refused(
        self, tenant, project, site, tech, pm, finance_user
    ):
        unpaid = ask(tech, site, type="FLOAT", amount="5000")
        with pytest.raises(FloatNotOpen) as caught:
            finance.close_float(unpaid, actor=finance_user, returned_amount=D("0"))
        assert caught.value.status_code == 409

        paid = paid_float(tech, pm, finance_user, site)
        finance.close_float(paid, actor=finance_user, returned_amount=D("0"))
        with pytest.raises(FloatNotOpen):
            finance.close_float(paid, actor=finance_user, returned_amount=D("0"))

    def test_a_negative_return_is_refused(self, tenant, project, site, tech, pm, finance_user):
        float_request = paid_float(tech, pm, finance_user, site)

        with pytest.raises(FinanceInputInvalid):
            finance.close_float(float_request, actor=finance_user, returned_amount=D("-1"))

    def test_only_a_float_closes(self, tenant, project, site, tech, pm, finance_user):
        night = ask(tech, site, type="NIGHT_OUT")
        approve_through(night, pm=pm, finance_user=finance_user)
        finance.mark_paid(night, actor=finance_user, reference="X2")

        with pytest.raises(FloatNotOpen):
            finance.close_float(night, actor=finance_user, returned_amount=D("0"))

    def test_another_open_float_is_a_warning_not_a_block(
        self, tenant, project, site, tech, pm, finance_user, category
    ):
        first = paid_float(tech, pm, finance_user, site, "5000")
        spend(tech, category, site, "1000", float_request=first)

        second = ask(tech, site, type="FLOAT", amount="3000")  # not blocked

        warning = finance.open_float_warning(tech, exclude=second)
        assert warning == {"number": first.number, "balance": D("4000.00")}

    def test_no_warning_without_another_open_float(
        self, tenant, project, site, tech, pm, finance_user
    ):
        assert finance.open_float_warning(tech) is None

        only = paid_float(tech, pm, finance_user, site)
        assert finance.open_float_warning(tech, exclude=only) is None
        assert finance.open_float_warning(tech)["number"] == only.number

        finance.close_float(only, actor=finance_user, returned_amount=D("0"))
        assert finance.open_float_warning(tech) is None

    def test_a_pending_float_is_not_an_open_one(
        self, tenant, project, site, tech, finance_user
    ):
        ask(tech, site, type="FLOAT", amount="5000")

        assert finance.open_float_warning(tech) is None


# --------------------------------------------------------------------------
# Deciding (R4)
# --------------------------------------------------------------------------


@pytest.mark.django_db
class TestDecide:
    def test_pm_then_finance_reaches_approved(
        self, tenant, project, site, tech, pm, category, finance_user
    ):
        expense = spend(tech, category, site)

        finance.decide(expense, actor=pm, approved=True)
        assert expense.status == ExpenseStatus.PENDING_FINANCE
        assert expense.decided_at is None
        assert levels_of(expense) == [(1, "APPROVED"), (2, "PENDING")]

        finance.decide(expense, actor=finance_user, approved=True)
        expense.refresh_from_db()
        assert expense.status == ExpenseStatus.APPROVED
        assert (expense.decided_by, expense.decision_reason) == (finance_user, "")
        assert expense.decided_at is not None
        assert levels_of(expense) == [(1, "APPROVED"), (2, "APPROVED")]

    def test_a_pm_recorded_entry_needs_only_finance(
        self, tenant, project, site, pm, category, finance_user
    ):
        expense = spend(pm, category, site)

        finance.decide(expense, actor=finance_user, approved=True)

        assert expense.status == ExpenseStatus.APPROVED

    def test_an_allowance_is_decided_the_same_way(
        self, tenant, project, site, tech, pm, finance_user
    ):
        allowance = ask(tech, site)

        approve_through(allowance, pm=pm, finance_user=finance_user)

        assert allowance.status == ExpenseStatus.APPROVED

    def test_a_pm_rejection_returns_it_and_supersedes_finance(
        self, tenant, project, site, tech, pm, category, finance_user
    ):
        expense = spend(tech, category, site)

        finance.decide(expense, actor=pm, approved=False, reason="Wrong site.")

        assert expense.status == ExpenseStatus.REJECTED
        assert (expense.decided_by, expense.decision_reason) == (pm, "Wrong site.")
        assert levels_of(expense) == [(1, "REJECTED"), (2, "SUPERSEDED")]

    def test_finance_may_reject_after_the_pm_approved(
        self, tenant, project, site, tech, pm, category, finance_user
    ):
        expense = spend(tech, category, site)
        finance.decide(expense, actor=pm, approved=True)

        finance.decide(expense, actor=finance_user, approved=False, reason="No receipt.")

        assert expense.status == ExpenseStatus.REJECTED
        assert expense.decided_by == finance_user
        assert levels_of(expense) == [(1, "APPROVED"), (2, "REJECTED")]

    def test_rejecting_needs_a_reason(
        self, tenant, project, site, tech, pm, category, finance_user
    ):
        expense = spend(tech, category, site)

        with pytest.raises(RejectionReasonRequired):
            finance.decide(expense, actor=pm, approved=False, reason="  ")

    def test_the_recorder_may_not_decide_even_when_self_approval_is_on(
        self, tenant, project, site, tech, pm, category, finance_user
    ):
        tenant.settings.allow_self_approval = True
        tenant.settings.save()
        expense = spend(tech, category, site)

        # Level 1: the recorder against the PM's request.
        with pytest.raises(FinanceSelfApproval) as caught:
            finance.decide(expense, actor=tech, approved=True)
        assert caught.value.status_code == 403
        assert expense.status == ExpenseStatus.PENDING_PM

        # Level 2: a Finance holder who recorded it.
        role = RoleFactory(name="Finance 2", codenames=[PERM.FINANCE_APPROVE])
        second = UserFactory(organization=tenant)
        UserRoleFactory(user=second, role=role)
        own = spend(finance_user, category, site)
        finance.decide(own, actor=pm, approved=True)
        with pytest.raises(FinanceSelfApproval):
            finance.decide(own, actor=finance_user, approved=True)
        finance.decide(own, actor=second, approved=True)
        assert own.status == ExpenseStatus.APPROVED

    def test_a_pm_who_recorded_it_cannot_approve_at_finance_either(
        self, tenant, project, site, pm, category, finance_user
    ):
        role = RoleFactory(name="Finance 2", codenames=[PERM.FINANCE_APPROVE])
        UserRoleFactory(user=pm, role=role)
        expense = spend(pm, category, site)

        with pytest.raises(FinanceSelfApproval):
            finance.decide(expense, actor=pm, approved=True)

    def test_someone_who_is_not_the_approver_is_refused(
        self, tenant, project, site, tech, pm, category, finance_user
    ):
        other = UserFactory(organization=tenant, full_name="Olu Other")
        expense = spend(tech, category, site)

        with pytest.raises(NotAnApprover):
            finance.decide(expense, actor=other, approved=True)
        # Finance cannot jump the PM, either.
        with pytest.raises(NotAnApprover):
            finance.decide(expense, actor=finance_user, approved=True)
        assert expense.status == ExpenseStatus.PENDING_PM

    def test_a_decided_entry_cannot_be_decided_again(
        self, tenant, project, site, tech, pm, category, finance_user
    ):
        expense = spend(tech, category, site)
        approve_through(expense, pm=pm, finance_user=finance_user)

        with pytest.raises(FinanceNotDecidable) as caught:
            finance.decide(expense, actor=finance_user, approved=True)
        assert caught.value.status_code == 409

    def test_a_stale_copy_is_refused_rather_than_decided_twice(
        self, tenant, project, site, tech, pm, category, finance_user
    ):
        expense = spend(tech, category, site)
        stale = ProjectExpense.objects.get(pk=expense.pk)
        finance.decide(expense, actor=pm, approved=False, reason="No.")

        with pytest.raises(FinanceNotDecidable):
            finance.decide(stale, actor=pm, approved=True)

    def test_each_decision_is_audited(
        self, tenant, project, site, tech, pm, category, finance_user
    ):
        expense = spend(tech, category, site)
        approve_through(expense, pm=pm, finance_user=finance_user)

        actors = set(
            AuditLog.objects.filter(
                target_type=expense._meta.label, target_id=str(expense.pk)
            ).values_list("actor", flat=True)
        )
        assert {pm.pk, finance_user.pk} <= actors


# --------------------------------------------------------------------------
# Reject, then resubmit
# --------------------------------------------------------------------------


@pytest.mark.django_db
class TestResubmit:
    def test_it_goes_round_again_and_the_old_requests_stay(
        self, tenant, project, site, tech, pm, category, finance_user
    ):
        expense = spend(tech, category, site)
        reject_at_pm(expense, pm=pm, reason="Add the receipt.")

        finance.resubmit(expense, actor=tech)

        assert expense.status == ExpenseStatus.PENDING_PM
        assert (expense.decided_at, expense.decided_by, expense.decision_reason) == (
            None,
            None,
            "",
        )
        assert levels_of(expense) == [
            (1, "REJECTED"),
            (2, "SUPERSEDED"),
            (1, "PENDING"),
            (2, "PENDING"),
        ]
        expense.refresh_from_db()
        assert expense.status == ExpenseStatus.PENDING_PM

    def test_the_second_round_can_be_approved(
        self, tenant, project, site, tech, pm, category, finance_user
    ):
        expense = spend(tech, category, site)
        reject_at_pm(expense, pm=pm)
        finance.resubmit(expense, actor=tech)

        approve_through(expense, pm=pm, finance_user=finance_user)

        assert expense.status == ExpenseStatus.APPROVED

    def test_a_skipped_pm_level_stays_skipped(
        self, tenant, project, site, pm, category, finance_user
    ):
        expense = spend(pm, category, site)
        finance.decide(expense, actor=finance_user, approved=False, reason="Receipt.")

        finance.resubmit(expense, actor=pm)

        assert expense.status == ExpenseStatus.PENDING_FINANCE
        assert levels_of(expense)[-1] == (2, "PENDING")

    def test_only_the_recorder_may_resubmit(
        self, tenant, project, site, tech, pm, category, finance_user
    ):
        expense = spend(tech, category, site)
        reject_at_pm(expense, pm=pm)

        with pytest.raises(Exception) as caught:
            finance.resubmit(expense, actor=pm)
        assert caught.value.status_code == 403  # type: ignore[attr-defined]
        assert expense.status == ExpenseStatus.REJECTED

    def test_only_a_rejected_entry_resubmits(
        self, tenant, project, site, tech, category, finance_user
    ):
        expense = spend(tech, category, site)

        with pytest.raises(FinanceNotDecidable):
            finance.resubmit(expense, actor=tech)

    def test_a_pm_that_has_gone_blocks_the_resubmit(
        self, tenant, project, site, tech, pm, category, finance_user
    ):
        expense = spend(tech, category, site)
        reject_at_pm(expense, pm=pm)
        pm.is_active = False
        pm.save()
        expense = ProjectExpense.objects.get(pk=expense.pk)  # a fresh load, as a request has

        with pytest.raises(ProjectHasNoActiveManager):
            finance.resubmit(expense, actor=tech)
        expense.refresh_from_db()
        assert expense.status == ExpenseStatus.REJECTED

    def test_an_allowance_is_checked_again_and_loses_to_a_newer_overlap(
        self, tenant, project, site, tech, pm, finance_user
    ):
        first = ask(tech, site)
        reject_at_pm(first, pm=pm)
        ask(tech, site)  # takes the same day while the first sits rejected

        with pytest.raises(AllowanceOverlap):
            finance.resubmit(first, actor=tech)
        assert first.status == ExpenseStatus.REJECTED


# --------------------------------------------------------------------------
# Allowances: numbers, scope, overlap (R2, R5)
# --------------------------------------------------------------------------


@pytest.mark.django_db
class TestRequestAllowance:
    def test_numbers_run_in_the_ar_series(self, tenant, project, site, tech, finance_user):
        first = ask(tech, site, type="FLOAT")
        second = ask(tech, site, type="FLOAT")

        assert (first.number, second.number) == ("AR-000001", "AR-000002")

    def test_a_refused_request_does_not_burn_a_number(
        self, tenant, project, site, tech, finance_user
    ):
        with pytest.raises(AllowanceLimit):
            ask(tech, site, amount="100")  # below the night-out minimum

        assert ask(tech, site).number == "AR-000001"

    def test_transport_must_say_where(self, tenant, project, site, tech, finance_user):
        with pytest.raises(TransportScopeRequired):
            ask(tech, site, type="TRANSPORT", amount="400")

        assert (
            ask(tech, site, type="TRANSPORT", amount="400", transport_scope="WITHIN_NAIROBI")
            .transport_scope
            == "WITHIN_NAIROBI"
        )

    def test_a_scope_on_another_type_is_dropped(self, tenant, project, site, tech, finance_user):
        allowance = ask(tech, site, type="NIGHT_OUT", transport_scope="WITHIN_NAIROBI")

        assert allowance.transport_scope == ""

    def test_the_end_cannot_precede_the_start(self, tenant, project, site, tech, finance_user):
        with pytest.raises(FinanceInputInvalid):
            ask(tech, site, start=date(2026, 10, 7), end=date(2026, 10, 5))

    def test_it_is_audited(self, tenant, project, site, tech, finance_user):
        allowance = ask(tech, site)

        assert AuditLog.objects.filter(
            target_type=allowance._meta.label, target_id=str(allowance.pk)
        ).exists()

    def test_a_replay_returns_the_same_request_and_number(
        self, tenant, project, site, tech, finance_user
    ):
        token = uuid.uuid4()
        first = ask(tech, site, client_uuid=token)
        again = ask(tech, site, client_uuid=token)

        assert again.pk == first.pk
        assert AllowanceRequest.objects.count() == 1
        # The replay must not be judged against itself, nor burn a number.
        assert ask(tech, site, start=date(2026, 10, 20)).number == "AR-000002"


@pytest.mark.django_db
class TestOverlap:
    def test_a_shared_boundary_day_is_refused_naming_the_earlier_one(
        self, tenant, project, site, tech, finance_user
    ):
        first = ask(tech, site, start=date(2026, 10, 5), end=date(2026, 10, 7), amount="6000")

        with pytest.raises(AllowanceOverlap) as caught:
            ask(tech, site, start=date(2026, 10, 7), end=date(2026, 10, 9), amount="6000")

        assert caught.value.status_code == 409
        assert first.number in caught.value.message
        assert caught.value.details["earlier"] == first.number

    def test_the_next_day_is_fine(self, tenant, project, site, tech, finance_user):
        ask(tech, site, start=date(2026, 10, 5), end=date(2026, 10, 7), amount="6000")

        ask(tech, site, start=date(2026, 10, 8), end=date(2026, 10, 9), amount="4000")

    def test_every_blocking_status_blocks(
        self, tenant, project, site, tech, pm, finance_user
    ):
        pending_pm = ask(tech, site, start=date(2026, 11, 1))
        pending_fin = ask(tech, site, start=date(2026, 11, 2))
        finance.decide(pending_fin, actor=pm, approved=True)
        approved = ask(tech, site, start=date(2026, 11, 3))
        approve_through(approved, pm=pm, finance_user=finance_user)
        paid = ask(tech, site, start=date(2026, 11, 4))
        approve_through(paid, pm=pm, finance_user=finance_user)
        finance.mark_paid(paid, actor=finance_user, reference="R1")

        for entry in (pending_pm, pending_fin, approved, paid):
            with pytest.raises(AllowanceOverlap) as caught:
                ask(tech, site, start=entry.from_date)
            assert caught.value.details["earlier"] == entry.number

    def test_a_rejected_request_does_not_block(
        self, tenant, project, site, tech, pm, finance_user
    ):
        first = ask(tech, site)
        reject_at_pm(first, pm=pm)

        assert ask(tech, site).status == ExpenseStatus.PENDING_PM

    def test_float_and_other_are_exempt(self, tenant, project, site, tech, finance_user):
        for type_ in ("FLOAT", "OTHER"):
            ask(tech, site, type=type_, amount="1000")
            ask(tech, site, type=type_, amount="1000")

    def test_a_different_type_or_person_is_not_a_clash(
        self, tenant, project, site, tech, pm, finance_user
    ):
        ask(tech, site, type="NIGHT_OUT")

        ask(tech, site, type="TEAM_ALLOWANCE")
        ask(pm, site, type="NIGHT_OUT")

    def test_each_overlap_type_is_checked(self, tenant, project, site, tech, finance_user):
        ask(tech, site, type="TRANSPORT", amount="400", transport_scope="WITHIN_NAIROBI")
        with pytest.raises(AllowanceOverlap):
            ask(tech, site, type="TRANSPORT", amount="400", transport_scope="OUTSIDE_NAIROBI")

        ask(tech, site, type="TEAM_ALLOWANCE")
        with pytest.raises(AllowanceOverlap):
            ask(tech, site, type="TEAM_ALLOWANCE")


@pytest.mark.django_db(transaction=True)
class TestOverlapIsRaceProof:
    """Real threads on real connections — the only way to test the lock (§4.17.5).

    A single-threaded test would pass with the ``select_for_update`` removed,
    since nothing would be racing.
    """

    def test_two_simultaneous_sends_cannot_both_pass(self, organization):
        with transaction.atomic(), tenant_context(organization):
            pm = UserFactory(organization=organization)
            tech = UserFactory(organization=organization)
            approver = UserFactory(organization=organization)
            role = RoleFactory(name="Finance", codenames=[PERM.FINANCE_APPROVE])
            UserRoleFactory(user=approver, role=role)
            project = ProjectFactory(reference="WO-RACE", manager=pm)
            site = SiteFactory(name="Race")
            project.sites.add(
                site,
                through_defaults={"organization_id": project.organization_id},
            )
            # Build the objects the threads use now, so they only race on the rule.
            tech_id = tech.pk

        workers = 6
        start = threading.Barrier(workers)
        landed: list[str] = []
        refused: list[str] = []
        errors: list[BaseException] = []
        guard = threading.Lock()

        def send():
            try:
                start.wait(timeout=10)
                with transaction.atomic(), tenant_context(organization):
                    from accounts.models import User
                    from network.models import Site

                    actor = User.objects.get(pk=tech_id)
                    result = finance.request_allowance(
                        actor=actor,
                        type="NIGHT_OUT",
                        amount=D("2000"),
                        from_date=DAY,
                        to_date=DAY,
                        site=Site.objects.get(pk=site.pk),
                    )
                with guard:
                    landed.append(result.number)
            except AllowanceOverlap as exc:
                with guard:
                    refused.append(exc.code)
            except BaseException as exc:
                with guard:
                    errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=send) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        assert errors == [], f"threads failed: {errors}"
        assert len(landed) == 1, f"{len(landed)} requests for the same day passed: {landed}"
        assert refused == ["ALLOWANCE_OVERLAP"] * (workers - 1)


# --------------------------------------------------------------------------
# Limits (R5)
# --------------------------------------------------------------------------


@pytest.mark.django_db
class TestLimits:
    def test_transport_within_nairobi_defaults_to_500_a_day(
        self, tenant, project, site, tech, finance_user
    ):
        ok = ask(
            tech,
            site,
            type="TRANSPORT",
            amount="1500",
            start=date(2026, 10, 5),
            end=date(2026, 10, 7),
            transport_scope="WITHIN_NAIROBI",
        )
        assert ok.days == 3

        with pytest.raises(AllowanceLimit) as caught:
            ask(
                tech,
                site,
                type="TRANSPORT",
                amount="501",
                start=date(2026, 11, 5),
                transport_scope="WITHIN_NAIROBI",
            )

        assert caught.value.status_code == 400
        assert caught.value.details["daily"] == "501.00"
        assert caught.value.details["limit"] == "500"
        # The person reads the daily figure and the limit.
        assert "501.00" in caught.value.message
        assert "500.00" in caught.value.message

    def test_the_daily_figure_spreads_the_whole_amount_over_the_days(
        self, tenant, project, site, tech, finance_user
    ):
        with pytest.raises(AllowanceLimit) as caught:
            ask(
                tech,
                site,
                type="TRANSPORT",
                amount="1501",
                start=date(2026, 10, 5),
                end=date(2026, 10, 7),
                transport_scope="WITHIN_NAIROBI",
            )

        assert caught.value.details["daily"] == "500.33"

    def test_outside_nairobi_has_no_limit_by_default(
        self, tenant, project, site, tech, finance_user
    ):
        allowance = ask(
            tech, site, type="TRANSPORT", amount="250000", transport_scope="OUTSIDE_NAIROBI"
        )

        assert allowance.status == ExpenseStatus.PENDING_PM

    def test_a_limit_finance_sets_is_enforced(
        self, tenant, project, site, tech, finance_user
    ):
        tenant.settings.allowance_limits["TRANSPORT_OUTSIDE_NAIROBI"] = {
            "min": None,
            "max": "800",
        }
        tenant.settings.save()

        ask(tech, site, type="TRANSPORT", amount="800", transport_scope="OUTSIDE_NAIROBI")
        with pytest.raises(AllowanceLimit):
            ask(
                tech,
                site,
                type="TRANSPORT",
                amount="801",
                start=date(2026, 11, 5),
                transport_scope="OUTSIDE_NAIROBI",
            )

    def test_night_out_and_team_allowance_have_both_bounds(
        self, tenant, project, site, tech, finance_user
    ):
        for type_ in ("NIGHT_OUT", "TEAM_ALLOWANCE"):
            with pytest.raises(AllowanceLimit) as low:
                ask(tech, site, type=type_, amount="1499")
            assert low.value.details["side"] == "min"
            with pytest.raises(AllowanceLimit) as high:
                ask(tech, site, type=type_, amount="10001")
            assert high.value.details["side"] == "max"
            ask(tech, site, type=type_, amount="1500", start=date(2026, 11, 1))
            ask(tech, site, type=type_, amount="10000", start=date(2026, 12, 1))

    def test_float_and_other_are_unlimited(self, tenant, project, site, tech, finance_user):
        ask(tech, site, type="FLOAT", amount="9999999", over_budget_reason="Big job")
        ask(tech, site, type="OTHER", amount="0.01", over_budget_reason="Big job")

    def test_expenses_are_not_limit_checked(
        self, tenant, project, site, tech, category, finance_user
    ):
        made = spend(tech, category, site, "9999999", over_budget_reason="Big job")
        assert made.status == ExpenseStatus.PENDING_PM

    def test_the_limits_are_judged_on_replay_as_well(
        self, tenant, project, site, tech, finance_user
    ):
        """The rules live in the service, so a queued phone request meets them."""
        with pytest.raises(AllowanceLimit):
            ask(tech, site, amount="50", client_uuid=uuid.uuid4())

        assert AllowanceRequest.objects.count() == 0


# --------------------------------------------------------------------------
# Paying (R4, D24)
# --------------------------------------------------------------------------


@pytest.mark.django_db
class TestMarkPaid:
    def approved_expense(self, tech, pm, finance_user, category, site):
        expense = spend(tech, category, site)
        approve_through(expense, pm=pm, finance_user=finance_user)
        return expense

    def test_an_approved_expense_is_paid_with_its_reference(
        self, tenant, project, site, tech, pm, category, finance_user
    ):
        expense = self.approved_expense(tech, pm, finance_user, category, site)

        finance.mark_paid(expense, actor=finance_user, reference="  QWE123  ")

        assert expense.status == ExpenseStatus.PAID
        assert expense.payment_reference == "QWE123"
        assert (expense.paid_by, expense.paid_at is not None) == (finance_user, True)
        expense.refresh_from_db()
        assert expense.status == ExpenseStatus.PAID

    def test_an_allowance_is_paid_too(self, tenant, project, site, tech, pm, finance_user):
        allowance = ask(tech, site)
        approve_through(allowance, pm=pm, finance_user=finance_user)

        finance.mark_paid(allowance, actor=finance_user, reference="MP1")

        assert allowance.status == ExpenseStatus.PAID

    def test_a_reference_is_required(
        self, tenant, project, site, tech, pm, category, finance_user
    ):
        expense = self.approved_expense(tech, pm, finance_user, category, site)

        for blank in ("", "   "):
            with pytest.raises(PaymentReferenceRequired):
                finance.mark_paid(expense, actor=finance_user, reference=blank)
        assert expense.status == ExpenseStatus.APPROVED

    def test_only_an_approved_entry_is_paid(
        self, tenant, project, site, tech, pm, category, finance_user
    ):
        pending = spend(tech, category, site)
        with pytest.raises(FinanceNotDecidable):
            finance.mark_paid(pending, actor=finance_user, reference="X")

        paid = self.approved_expense(tech, pm, finance_user, category, site)
        finance.mark_paid(paid, actor=finance_user, reference="X")
        with pytest.raises(FinanceNotDecidable):
            finance.mark_paid(paid, actor=finance_user, reference="Y")

    def test_a_float_backed_expense_is_never_paid(
        self, tenant, project, site, tech, pm, category, finance_user
    ):
        float_request = paid_float(tech, pm, finance_user, site)
        expense = spend(tech, category, site, "800", float_request=float_request)
        approve_through(expense, pm=pm, finance_user=finance_user)

        with pytest.raises(FloatBackedNotPayable):
            finance.mark_paid(expense, actor=finance_user, reference="X")
        assert expense.status == ExpenseStatus.APPROVED

    def test_a_reversal_is_not_paid(
        self, tenant, project, site, tech, pm, category, finance_user
    ):
        expense = self.approved_expense(tech, pm, finance_user, category, site)
        reversal = reverse_expense(expense, actor=pm, reason="Wrong PO.")

        with pytest.raises(FinanceNotDecidable):
            finance.mark_paid(reversal, actor=finance_user, reference="X")

    def test_a_paid_entry_cannot_be_edited(
        self, tenant, project, site, tech, pm, category, finance_user
    ):
        from django.core.exceptions import ValidationError

        expense = self.approved_expense(tech, pm, finance_user, category, site)
        finance.mark_paid(expense, actor=finance_user, reference="X")
        expense.amount = D("1.00")

        with pytest.raises(ValidationError):
            expense.save()


# --------------------------------------------------------------------------
# Cost counts APPROVED and PAID only (§4.17.11)
# --------------------------------------------------------------------------


@pytest.mark.django_db
class TestCost:
    def test_only_approved_and_paid_reach_project_cost(
        self, tenant, project, site, tech, pm, category, finance_user
    ):
        def cost():
            return cost_for(project).expenses

        waiting_pm = spend(tech, category, site, "100")
        assert cost() == D("0.00")

        finance.decide(waiting_pm, actor=pm, approved=True)  # PENDING_FINANCE
        assert cost() == D("0.00")

        rejected = spend(tech, category, site, "200")
        reject_at_pm(rejected, pm=pm)
        assert cost() == D("0.00")

        finance.decide(waiting_pm, actor=finance_user, approved=True)
        assert cost() == D("100.00")

        finance.mark_paid(waiting_pm, actor=finance_user, reference="R")
        assert cost() == D("100.00")

        reverse_expense(waiting_pm, actor=pm, reason="Wrong PO.")
        assert cost() == D("0.00")

    def test_a_pm_approval_alone_is_not_cost(
        self, tenant, project, site, tech, pm, category, finance_user
    ):
        expense = spend(tech, category, site, "700")
        finance.decide(expense, actor=pm, approved=True)

        assert cost_for(project).expenses == D("0.00")

    def test_a_skipped_pm_level_still_needs_finance(
        self, tenant, project, site, pm, category, finance_user
    ):
        expense = spend(pm, category, site, "700")
        assert cost_for(project).expenses == D("0.00")

        finance.decide(expense, actor=finance_user, approved=True)
        assert cost_for(project).expenses == D("700.00")


@pytest.mark.django_db
class TestReverseNeedsPmOrFinance:
    def approved(self, tech, pm, finance_user, category, site):
        expense = spend(tech, category, site)
        approve_through(expense, pm=pm, finance_user=finance_user)
        return expense

    def test_the_pm_may(self, tenant, project, site, tech, pm, category, finance_user):
        expense = self.approved(tech, pm, finance_user, category, site)

        assert reverse_expense(expense, actor=pm, reason="x").status == ExpenseStatus.APPROVED

    def test_finance_may(self, tenant, project, site, tech, pm, category, finance_user):
        expense = self.approved(tech, pm, finance_user, category, site)

        reversal = reverse_expense(expense, actor=finance_user, reason="x")

        assert (reversal.status, reversal.recorded_by) == (ExpenseStatus.APPROVED, finance_user)

    def test_nobody_else_may(self, tenant, project, site, tech, pm, category, finance_user):
        expense = self.approved(tech, pm, finance_user, category, site)

        with pytest.raises(NotAnApprover):
            reverse_expense(expense, actor=tech, reason="x")


# --------------------------------------------------------------------------
# Casuals (R3)
# --------------------------------------------------------------------------


@pytest.mark.django_db
class TestCasuals:
    def test_a_casual_is_registered_once(self, tenant, tech):
        casual = finance.register_casual(
            actor=tech, name=" Kamau Njoroge ", id_number="12 345-678", phone="0712000111"
        )

        assert casual.name == "Kamau Njoroge"
        assert casual.id_number == "12 345-678"  # as typed
        assert casual.id_number_key == "12345678"
        assert casual.registered_by == tech

    def test_the_same_id_in_another_spelling_is_a_duplicate_naming_the_first(self, tenant, tech):
        first = finance.register_casual(actor=tech, name="Kamau", id_number="ab-123 456")

        with pytest.raises(CasualIdDuplicate) as caught:
            finance.register_casual(actor=tech, name="Someone Else", id_number="AB123456")

        assert caught.value.status_code == 409
        assert "Kamau" in caught.value.message
        assert caught.value.details["existing"] == {"id": first.pk, "name": "Kamau"}
        assert Casual.objects.count() == 1

    def test_a_replay_returns_the_same_casual(self, tenant, tech):
        token = uuid.uuid4()
        first = finance.register_casual(
            actor=tech, name="Kamau", id_number="111", client_uuid=token
        )
        again = finance.register_casual(
            actor=tech, name="Kamau", id_number="111", client_uuid=token
        )

        assert again.pk == first.pk

    def test_a_name_and_an_id_are_required(self, tenant, tech):
        with pytest.raises(FinanceInputInvalid):
            finance.register_casual(actor=tech, name="  ", id_number="111")
        with pytest.raises(FinanceInputInvalid):
            finance.register_casual(actor=tech, name="Kamau", id_number=" - ")
