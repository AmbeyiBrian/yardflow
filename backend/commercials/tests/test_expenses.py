"""T10.15 — expenses (§4.14; O16, D29).

This is the only cost line in Epic O with no ledger movement and no contract
behind it — a receipt and somebody's word. Everything here follows from that:
a second person approves it before it counts, and once it counts nobody can
quietly change it.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from accounts.factories import UserFactory
from commercials.models import (
    DEFAULT_EXPENSE_CATEGORIES,
    ExpenseCategory,
    ExpenseStatus,
    ProjectExpense,
)
from commercials.services import (
    ExpenseNotDecidable,
    NotTheProjectManager,
    decide_expense,
    reverse_expense,
    seed_expense_categories,
)
from network.factories import ProjectFactory


@pytest.fixture
def manager(tenant):
    return UserFactory(organization=tenant, full_name="Pippa Manager")


@pytest.fixture
def technician(tenant):
    return UserFactory(organization=tenant, full_name="Tom Technician")


@pytest.fixture
def po_project(tenant, manager):
    return ProjectFactory(
        reference="WO-9601",
        po_number="PO-960",
        manager=manager,
        contract_value=Decimal("100000.00"),
        cost_budget=Decimal("80000.00"),
    )


@pytest.fixture
def category(tenant):
    return ExpenseCategory.objects.create(
        organization=tenant, name="Transport and fuel", code="TRANSPORT"
    )


def record_expense(tenant, project, category, who, amount="4500.00"):
    return ProjectExpense.objects.create(
        organization=tenant,
        project=project,
        category=category,
        amount=Decimal(amount),
        incurred_on=date(2026, 4, 3),
        description="Fuel, Nairobi to Nakuru",
        recorded_by=who,
    )


@pytest.mark.django_db
class TestRecording:
    def test_anyone_may_record_one(self, tenant, po_project, category, technician):
        """A technician at a fuel station is closer to the fact than the yard."""
        expense = record_expense(tenant, po_project, category, technician)

        assert expense.status == ExpenseStatus.SUBMITTED
        assert expense.recorded_by == technician

    def test_it_counts_for_nothing_until_approved(
        self, tenant, po_project, category, technician
    ):
        record_expense(tenant, po_project, category, technician)

        approved = ProjectExpense.objects.filter(
            project=po_project, status=ExpenseStatus.APPROVED
        )
        assert approved.count() == 0


@pytest.mark.django_db
class TestDeciding:
    def test_the_manager_approves(self, tenant, po_project, category, technician, manager):
        expense = record_expense(tenant, po_project, category, technician)
        decide_expense(expense, actor=manager, approved=True)
        expense.refresh_from_db()

        assert expense.status == ExpenseStatus.APPROVED
        assert expense.decided_by == manager
        assert expense.decided_at is not None

    def test_nobody_else_decides(self, tenant, po_project, category, technician):
        expense = record_expense(tenant, po_project, category, technician)

        with pytest.raises(NotTheProjectManager):
            decide_expense(expense, actor=technician, approved=True)

    def test_rejecting_needs_a_reason(self, tenant, po_project, category, technician, manager):
        expense = record_expense(tenant, po_project, category, technician)

        with pytest.raises(ExpenseNotDecidable, match="needs a reason"):
            decide_expense(expense, actor=manager, approved=False)

    def test_it_cannot_be_decided_twice(
        self, tenant, po_project, category, technician, manager
    ):
        expense = record_expense(tenant, po_project, category, technician)
        decide_expense(expense, actor=manager, approved=True)
        expense.refresh_from_db()

        with pytest.raises(ExpenseNotDecidable, match="already"):
            decide_expense(expense, actor=manager, approved=True)

    def test_a_decision_must_record_when(self, tenant, po_project, category, technician):
        expense = record_expense(tenant, po_project, category, technician)
        expense.status = ExpenseStatus.APPROVED

        with pytest.raises(IntegrityError), transaction.atomic():
            expense.save()


@pytest.mark.django_db
class TestAnApprovedExpenseIsFinal:
    def approved(self, tenant, po_project, category, technician, manager, amount="4500.00"):
        expense = record_expense(tenant, po_project, category, technician, amount)
        decide_expense(expense, actor=manager, approved=True)
        expense.refresh_from_db()
        return expense

    def test_it_cannot_be_edited(self, tenant, po_project, category, technician, manager):
        expense = self.approved(tenant, po_project, category, technician, manager)
        expense.amount = Decimal("99999.00")

        with pytest.raises(ValidationError, match="cannot be changed"):
            expense.save()

    def test_it_is_never_deleted(self, tenant, po_project, category, technician, manager):
        expense = self.approved(tenant, po_project, category, technician, manager)

        with pytest.raises(ValidationError, match="never deleted"):
            expense.delete()

    def test_a_submitted_one_may_still_be_corrected(
        self, tenant, po_project, category, technician
    ):
        """Nothing has counted yet, so nothing is being rewritten."""
        expense = record_expense(tenant, po_project, category, technician)
        expense.amount = Decimal("4600.00")
        expense.save()

        expense.refresh_from_db()
        assert expense.amount == Decimal("4600.00")


@pytest.mark.django_db
class TestReversal:
    def approved(self, tenant, po_project, category, technician, manager):
        expense = record_expense(tenant, po_project, category, technician)
        decide_expense(expense, actor=manager, approved=True)
        expense.refresh_from_db()
        return expense

    def test_it_records_the_opposite_rather_than_editing(
        self, tenant, po_project, category, technician, manager
    ):
        expense = self.approved(tenant, po_project, category, technician, manager)
        reversal = reverse_expense(
            expense, actor=manager, reason="Charged to the wrong PO."
        )

        assert reversal.reverses == expense
        assert reversal.status == ExpenseStatus.APPROVED
        assert reversal.signed_amount == Decimal("-4500.00")
        assert expense.signed_amount == Decimal("4500.00")

    def test_the_pair_nets_to_nothing(
        self, tenant, po_project, category, technician, manager
    ):
        expense = self.approved(tenant, po_project, category, technician, manager)
        reverse_expense(expense, actor=manager, reason="Wrong PO.")

        total = sum(
            item.signed_amount
            for item in ProjectExpense.objects.filter(
                project=po_project, status=ExpenseStatus.APPROVED
            )
        )
        assert total == Decimal("0.00")

    def test_a_reversal_cannot_itself_be_reversed(
        self, tenant, po_project, category, technician, manager
    ):
        expense = self.approved(tenant, po_project, category, technician, manager)
        reversal = reverse_expense(expense, actor=manager, reason="Wrong PO.")

        with pytest.raises(ExpenseNotDecidable, match="cannot itself be reversed"):
            reverse_expense(reversal, actor=manager, reason="Changed my mind.")

    def test_it_cannot_be_reversed_twice(
        self, tenant, po_project, category, technician, manager
    ):
        expense = self.approved(tenant, po_project, category, technician, manager)
        reverse_expense(expense, actor=manager, reason="Wrong PO.")

        with pytest.raises(ExpenseNotDecidable, match="already been reversed"):
            reverse_expense(expense, actor=manager, reason="Again.")

    def test_reversing_needs_a_reason(
        self, tenant, po_project, category, technician, manager
    ):
        expense = self.approved(tenant, po_project, category, technician, manager)

        with pytest.raises(ExpenseNotDecidable, match="needs a reason"):
            reverse_expense(expense, actor=manager, reason="")


@pytest.mark.django_db
class TestSeeding:
    def test_a_new_tenant_gets_somewhere_to_file(self, tenant):
        ExpenseCategory.objects.all().delete()
        seed_expense_categories(tenant)

        assert ExpenseCategory.objects.count() == len(DEFAULT_EXPENSE_CATEGORIES)

    def test_seeding_twice_adds_nothing(self, tenant):
        ExpenseCategory.objects.all().delete()
        seed_expense_categories(tenant)
        seed_expense_categories(tenant)

        assert ExpenseCategory.objects.count() == len(DEFAULT_EXPENSE_CATEGORIES)
