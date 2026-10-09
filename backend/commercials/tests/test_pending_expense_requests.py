"""T15.2 — the data migration giving in-flight expenses their requests (§4.17.11).

T15.1 moved ``SUBMITTED`` to ``PENDING_PM`` without requests. This one addresses
them, following the routing rules, and must be safe to run twice. The migration
function is called directly against the live app registry, which is what a
historical registry resolves to while no later migration has changed these
tables.
"""

from datetime import date
from decimal import Decimal
from importlib import import_module

import pytest
from django.apps import apps
from django.utils import timezone

from accounts.factories import RoleFactory, UserFactory, UserRoleFactory
from approvals.models import ApprovalRequest
from commercials.models import ExpenseCategory, ExpenseStatus, ProjectExpense
from network.factories import ProjectFactory

migration = import_module("commercials.migrations.0006_requests_for_pending_expenses")


def run():
    migration.create_requests(apps, None)


@pytest.fixture
def pm(tenant):
    return UserFactory(organization=tenant)


@pytest.fixture
def recorder(tenant):
    return UserFactory(organization=tenant)


@pytest.fixture
def project(tenant, pm):
    return ProjectFactory(
        reference="WO-9951",
        po_number="PO-995",
        manager=pm,
        contract_value=Decimal("1000.00"),
        cost_budget=Decimal("800.00"),
    )


@pytest.fixture
def category(tenant):
    return ExpenseCategory.objects.create(organization=tenant, name="Misc")


def pending_expense(tenant, project, category, who):
    return ProjectExpense.objects.create(
        organization=tenant,
        project=project,
        category=category,
        amount=Decimal("500.00"),
        incurred_on=date(2026, 10, 1),
        recorded_by=who,
    )


def requests_for(expense):
    return list(
        ApprovalRequest.objects.filter(
            document_type="commercials.ProjectExpense", document_id=str(expense.pk)
        ).order_by("level")
    )


@pytest.mark.django_db
class TestPendingExpensesGetRequests:
    def test_an_ordinary_expense_gets_both_levels(
        self, tenant, project, category, recorder, pm
    ):
        expense = pending_expense(tenant, project, category, recorder)

        run()

        first, second = requests_for(expense)
        assert (first.level, first.required_user) == (1, pm)
        assert (second.level, second.required_permission) == (2, "finance.approve")
        assert first.due_at is None and second.due_at is None
        assert first.requested_by == recorder
        expense.refresh_from_db()
        assert expense.status == ExpenseStatus.PENDING_PM

    def test_a_pm_recorded_expense_skips_level_one(self, tenant, project, category, pm):
        expense = pending_expense(tenant, project, category, pm)

        run()

        (only,) = requests_for(expense)
        assert (only.level, only.required_permission) == (2, "finance.approve")
        expense.refresh_from_db()
        assert expense.status == ExpenseStatus.PENDING_FINANCE

    def test_a_director_recorded_expense_skips_level_one(
        self, tenant, project, category, recorder
    ):
        director = RoleFactory(name="Director")
        UserRoleFactory(user=recorder, role=director)
        tenant.settings.finance_director_role = director
        tenant.settings.save()
        expense = pending_expense(tenant, project, category, recorder)

        run()

        assert [r.level for r in requests_for(expense)] == [2]
        expense.refresh_from_db()
        assert expense.status == ExpenseStatus.PENDING_FINANCE

    def test_a_project_with_no_manager_is_left_alone(self, tenant, category, recorder):
        """D28: nobody to address level 1 to, and no routing round the PM."""
        expense = pending_expense(
            tenant, ProjectFactory(reference="WO-9952"), category, recorder
        )

        run()

        assert requests_for(expense) == []
        expense.refresh_from_db()
        assert expense.status == ExpenseStatus.PENDING_PM

    def test_decided_expenses_are_untouched(self, tenant, project, category, recorder):
        approved = pending_expense(tenant, project, category, recorder)
        ProjectExpense.objects.filter(pk=approved.pk).update(
            status=ExpenseStatus.APPROVED, decided_at=timezone.now()
        )

        run()

        assert requests_for(approved) == []

    def test_running_it_twice_adds_nothing(self, tenant, project, category, recorder, pm):
        expense = pending_expense(tenant, project, category, recorder)
        skipped = pending_expense(tenant, project, category, pm)

        run()
        run()

        assert len(requests_for(expense)) == 2
        assert len(requests_for(skipped)) == 1

    def test_an_expense_that_already_has_requests_is_skipped(
        self, tenant, project, category, recorder
    ):
        expense = pending_expense(tenant, project, category, recorder)
        ApprovalRequest.objects.create(
            organization=tenant,
            document_type="commercials.ProjectExpense",
            document_id=str(expense.pk),
            level=2,
            required_permission="finance.approve",
        )

        run()

        assert [r.level for r in requests_for(expense)] == [2]
