"""T15.1 — the finance models and their guards (§4.17.2, §4.17.3; R1-R4).

Nothing here routes or decides: the services and the engine come later. These
tests pin what the *tables* refuse, so a later bug in a service cannot quietly
rewrite an approved entry.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.factories import UserFactory
from accounts.models import Role
from accounts.permissions_registry import DEFAULT_ROLES, PERM
from commercials.models import (
    COSTED_STATUSES,
    AllowanceRequest,
    AllowanceType,
    Casual,
    ExpenseCasualLine,
    ExpenseCategory,
    ExpenseKind,
    ExpenseStatus,
    ProjectExpense,
)
from commercials.services import seed_expense_categories
from core.models import OrganizationSettings
from core.numbering import DEFAULT_PREFIXES, DocumentType
from network.factories import ProjectFactory, SiteFactory

S = ExpenseStatus


@pytest.fixture
def recorder(tenant):
    return UserFactory(organization=tenant)


@pytest.fixture
def project(tenant, recorder):
    return ProjectFactory(
        reference="WO-9701",
        po_number="PO-970",
        manager=UserFactory(organization=tenant),
        contract_value=Decimal("100000.00"),
        cost_budget=Decimal("80000.00"),
    )


@pytest.fixture
def category(tenant):
    return ExpenseCategory.objects.create(organization=tenant, name="Misc")


def make_expense(tenant, project, category, who, **fields):
    defaults = {
        "organization": tenant,
        "project": project,
        "category": category,
        "amount": Decimal("1000.00"),
        "incurred_on": date(2026, 10, 1),
        "recorded_by": who,
    }
    return ProjectExpense.objects.create(**{**defaults, **fields})


def reload(expense):
    return ProjectExpense.objects.get(pk=expense.pk)


def move(expense, status, **fields):
    expense.status = status
    for name, value in fields.items():
        setattr(expense, name, value)
    expense.save()


@pytest.mark.django_db
class TestExpenseStatusMoves:
    def test_it_starts_waiting_on_the_pm(self, tenant, project, category, recorder):
        assert make_expense(tenant, project, category, recorder).status == S.PENDING_PM

    def test_the_whole_happy_path(self, tenant, project, category, recorder):
        expense = reload(make_expense(tenant, project, category, recorder))

        move(expense, S.PENDING_FINANCE)
        move(expense, S.APPROVED, decided_at=timezone.now())
        move(
            expense,
            S.PAID,
            paid_at=timezone.now(),
            paid_by=recorder,
            payment_reference="QJ12345",
        )

        assert reload(expense).status == S.PAID

    def test_a_pm_approval_cannot_skip_finance(
        self, tenant, project, category, recorder
    ):
        expense = reload(make_expense(tenant, project, category, recorder))

        with pytest.raises(ValidationError, match="cannot move"):
            move(expense, S.APPROVED, decided_at=timezone.now())

    def test_it_may_be_born_at_finance_when_the_pm_level_is_skipped(
        self, tenant, project, category, recorder
    ):
        expense = make_expense(
            tenant, project, category, recorder, status=S.PENDING_FINANCE
        )

        assert reload(expense).status == S.PENDING_FINANCE

    @pytest.mark.parametrize("level", [S.PENDING_PM, S.PENDING_FINANCE])
    def test_either_level_may_reject(self, tenant, project, category, recorder, level):
        expense = reload(
            make_expense(tenant, project, category, recorder, status=level)
        )

        move(expense, S.REJECTED, decided_at=timezone.now())

        assert reload(expense).status == S.REJECTED

    @pytest.mark.parametrize("first", [S.PENDING_PM, S.PENDING_FINANCE])
    def test_a_rejected_one_resubmits_to_its_first_open_level(
        self, tenant, project, category, recorder, first
    ):
        expense = reload(
            make_expense(
                tenant,
                project,
                category,
                recorder,
                status=S.REJECTED,
                decided_at=timezone.now(),
            )
        )

        move(expense, first, decided_at=None)

        assert reload(expense).status == first

    def test_a_rejected_one_cannot_be_approved_directly(
        self, tenant, project, category, recorder
    ):
        expense = reload(
            make_expense(
                tenant,
                project,
                category,
                recorder,
                status=S.REJECTED,
                decided_at=timezone.now(),
            )
        )

        with pytest.raises(ValidationError, match="cannot move"):
            move(expense, S.APPROVED)

    def test_an_approved_one_cannot_go_back(self, tenant, project, category, recorder):
        expense = reload(
            make_expense(
                tenant,
                project,
                category,
                recorder,
                status=S.APPROVED,
                decided_at=timezone.now(),
            )
        )

        with pytest.raises(ValidationError, match="cannot move"):
            move(expense, S.PENDING_FINANCE, decided_at=None)

    def test_the_decision_check_wants_a_time(self, tenant, project, category, recorder):
        expense = make_expense(tenant, project, category, recorder)

        with pytest.raises(IntegrityError), transaction.atomic():
            ProjectExpense.objects.filter(pk=expense.pk).update(status=S.PAID)

    def test_a_waiting_one_may_not_carry_a_decision_time(
        self, tenant, project, category, recorder
    ):
        with pytest.raises(IntegrityError), transaction.atomic():
            make_expense(
                tenant, project, category, recorder, decided_at=timezone.now()
            )


@pytest.mark.django_db
class TestApprovedExpensesAreFrozen:
    def approved(self, tenant, project, category, recorder):
        return reload(
            make_expense(
                tenant,
                project,
                category,
                recorder,
                status=S.APPROVED,
                decided_at=timezone.now(),
            )
        )

    def test_the_amount_cannot_change(self, tenant, project, category, recorder):
        expense = self.approved(tenant, project, category, recorder)
        expense.amount = Decimal("1.00")

        with pytest.raises(ValidationError, match="cannot be changed"):
            expense.save()

    def test_the_description_cannot_change(self, tenant, project, category, recorder):
        expense = self.approved(tenant, project, category, recorder)
        expense.description = "Rewritten"

        with pytest.raises(ValidationError, match="cannot be changed"):
            expense.save()

    def test_only_the_payment_columns_move_with_the_status(
        self, tenant, project, category, recorder
    ):
        expense = self.approved(tenant, project, category, recorder)

        move(
            expense,
            S.PAID,
            paid_at=timezone.now(),
            paid_by=recorder,
            payment_reference="QJ1",
        )

        paid = reload(expense)
        assert paid.payment_reference == "QJ1"
        assert paid.status == S.PAID

    def test_payment_cannot_carry_another_edit_with_it(
        self, tenant, project, category, recorder
    ):
        expense = self.approved(tenant, project, category, recorder)
        expense.amount = Decimal("2.00")

        with pytest.raises(ValidationError, match="cannot be changed"):
            move(expense, S.PAID, paid_at=timezone.now(), payment_reference="QJ1")

    def test_a_paid_one_is_final(self, tenant, project, category, recorder):
        expense = self.approved(tenant, project, category, recorder)
        move(expense, S.PAID, paid_at=timezone.now(), payment_reference="QJ1")
        expense.payment_reference = "OTHER"

        with pytest.raises(ValidationError, match="cannot be changed"):
            expense.save()

    def test_a_waiting_one_may_still_be_corrected(
        self, tenant, project, category, recorder
    ):
        expense = reload(make_expense(tenant, project, category, recorder))
        expense.amount = Decimal("1200.00")
        expense.save()

        assert reload(expense).amount == Decimal("1200.00")

    def test_it_is_never_deleted(self, tenant, project, category, recorder):
        expense = make_expense(tenant, project, category, recorder)

        with pytest.raises(ValidationError, match="never deleted"):
            expense.delete()

    def test_cost_counts_approved_and_paid_only(self):
        assert set(COSTED_STATUSES) == {S.APPROVED, S.PAID}

    def test_the_engine_can_read_the_recorder_as_requested_by(
        self, tenant, project, category, recorder
    ):
        expense = make_expense(tenant, project, category, recorder)

        assert expense.requested_by_id == recorder.pk


@pytest.mark.django_db
class TestExpenseColumns:
    def test_client_uuid_is_unique_per_organization(
        self, tenant, project, category, recorder
    ):
        import uuid

        key = uuid.uuid4()
        make_expense(tenant, project, category, recorder, client_uuid=key)

        with pytest.raises(IntegrityError), transaction.atomic():
            make_expense(tenant, project, category, recorder, client_uuid=key)

    def test_many_may_have_no_client_uuid(self, tenant, project, category, recorder):
        make_expense(tenant, project, category, recorder)
        make_expense(tenant, project, category, recorder)

    def test_fuel_and_site_fields_round_trip(self, tenant, project, recorder):
        fuel = ExpenseCategory.objects.create(
            organization=tenant, name="Fuel", kind=ExpenseKind.FUEL
        )
        site = SiteFactory()
        expense = make_expense(
            tenant,
            project,
            fuel,
            recorder,
            site=site,
            vehicle_reg="KDA 123A",
            litres=Decimal("40.50"),
            scope_of_work="Install RRU",
            photos_expected=2,
        )

        loaded = reload(expense)
        assert loaded.category.kind == ExpenseKind.FUEL
        assert (loaded.site, loaded.litres, loaded.photos_expected) == (
            site,
            Decimal("40.50"),
            2,
        )


@pytest.mark.django_db
class TestCasual:
    def make(self, tenant, recorder, id_number, **fields):
        return Casual.objects.create(
            organization=tenant,
            name="Juma",
            id_number=id_number,
            registered_by=recorder,
            **fields,
        )

    @pytest.mark.parametrize(
        ("typed", "key"),
        [
            ("12345678", "12345678"),
            (" 12 345-678 ", "12345678"),
            ("ab-12 cd", "AB12CD"),
            ("a1b2", "A1B2"),
        ],
    )
    def test_the_key_is_normalised(self, tenant, recorder, typed, key):
        casual = self.make(tenant, recorder, typed)

        assert Casual.objects.get(pk=casual.pk).id_number_key == key
        assert casual.id_number == typed

    def test_the_same_id_written_differently_is_the_same_person(
        self, tenant, recorder
    ):
        self.make(tenant, recorder, "12345678")

        with pytest.raises(IntegrityError), transaction.atomic():
            self.make(tenant, recorder, "12-345 678")

    def test_another_organization_may_hold_the_same_id(
        self, tenant, other_organization, recorder
    ):
        from core.tenancy import tenant_context

        self.make(tenant, recorder, "12345678")
        with tenant_context(other_organization):
            other_user = UserFactory(organization=other_organization)
            Casual.objects.create(
                organization=other_organization,
                name="Juma",
                id_number="12345678",
                registered_by=other_user,
            )

    def test_editing_the_id_refreshes_the_key(self, tenant, recorder):
        casual = self.make(tenant, recorder, "111")
        casual.id_number = "222-x"
        casual.save(update_fields=["id_number"])

        assert Casual.objects.get(pk=casual.pk).id_number_key == "222X"

    def test_lines_freeze_with_the_expense(self, tenant, project, category, recorder):
        casual = self.make(tenant, recorder, "999")
        expense = make_expense(tenant, project, category, recorder)
        line = ExpenseCasualLine.objects.create(
            organization=tenant, expense=expense, casual=casual, days=2
        )

        line.days = 3
        line.save()  # still pending: allowed

        expense = reload(expense)
        move(expense, S.PENDING_FINANCE)
        move(expense, S.APPROVED, decided_at=timezone.now())

        line.days = 9
        with pytest.raises(ValidationError, match="frozen"):
            line.save()
        with pytest.raises(ValidationError, match="frozen"):
            line.delete()

    def test_a_casual_works_at_least_a_day(self, tenant, project, category, recorder):
        casual = self.make(tenant, recorder, "888")
        expense = make_expense(tenant, project, category, recorder)

        with pytest.raises(IntegrityError), transaction.atomic():
            ExpenseCasualLine.objects.create(
                organization=tenant, expense=expense, casual=casual, days=0
            )


@pytest.mark.django_db
class TestAllowanceRequest:
    def make(self, tenant, project, recorder, **fields):
        defaults = {
            "organization": tenant,
            "project": project,
            "type": AllowanceType.NIGHT_OUT,
            "amount": Decimal("3000.00"),
            "from_date": date(2026, 10, 1),
            "to_date": date(2026, 10, 2),
            "recorded_by": recorder,
        }
        return AllowanceRequest.objects.create(**{**defaults, **fields})

    def test_days_counts_both_ends(self, tenant, project, recorder):
        request = self.make(tenant, project, recorder)

        assert request.days == 2

    def test_a_single_day_is_one(self, tenant, project, recorder):
        request = self.make(
            tenant, project, recorder, to_date=date(2026, 10, 1)
        )

        assert request.days == 1

    def test_days_across_a_month_end(self, tenant, project, recorder):
        request = self.make(
            tenant,
            project,
            recorder,
            from_date=date(2026, 10, 30),
            to_date=date(2026, 10, 30) + timedelta(days=3),
        )

        assert request.days == 4

    def test_it_cannot_end_before_it_starts(self, tenant, project, recorder):
        with pytest.raises(IntegrityError), transaction.atomic():
            self.make(
                tenant, project, recorder, to_date=date(2026, 9, 30)
            )

    def test_only_transport_has_a_scope(self, tenant, project, recorder):
        with pytest.raises(IntegrityError), transaction.atomic():
            self.make(tenant, project, recorder, transport_scope="WITHIN_NAIROBI")

        self.make(
            tenant,
            project,
            recorder,
            type=AllowanceType.TRANSPORT,
            transport_scope="WITHIN_NAIROBI",
        )

    def test_the_requested_by_alias(self, tenant, project, recorder):
        assert self.make(tenant, project, recorder).requested_by_id == recorder.pk

    def test_it_follows_the_same_moves_and_freezes(self, tenant, project, recorder):
        request = AllowanceRequest.objects.get(pk=self.make(tenant, project, recorder).pk)

        with pytest.raises(ValidationError, match="cannot move"):
            request.status = S.APPROVED
            request.decided_at = timezone.now()
            request.save()

        request = AllowanceRequest.objects.get(pk=request.pk)
        request.status = S.PENDING_FINANCE
        request.save()
        request.status = S.APPROVED
        request.decided_at = timezone.now()
        request.save()

        request.amount = Decimal("1.00")
        with pytest.raises(ValidationError, match="cannot be changed"):
            request.save()

    def test_a_paid_float_may_still_be_closed(self, tenant, project, recorder):
        request = AllowanceRequest.objects.get(
            pk=self.make(
                tenant,
                project,
                recorder,
                type=AllowanceType.FLOAT,
                status=S.PAID,
                decided_at=timezone.now(),
            ).pk
        )

        request.closed_at = timezone.now()
        request.closed_by = recorder
        request.returned_amount = Decimal("250.00")
        request.save()

        request.amount = Decimal("1.00")
        with pytest.raises(ValidationError, match="cannot be changed"):
            request.save()

    def test_the_number_is_unique_once_given(self, tenant, project, recorder):
        self.make(tenant, project, recorder, number="AR-000001")
        self.make(tenant, project, recorder)  # unnumbered ones may repeat
        self.make(tenant, project, recorder)

        with pytest.raises(IntegrityError), transaction.atomic():
            self.make(tenant, project, recorder, number="AR-000001")

    def test_the_series_exists(self):
        assert DocumentType.ALLOWANCE == "ALLOWANCE"
        assert DEFAULT_PREFIXES[DocumentType.ALLOWANCE] == "AR"

    def test_an_expense_may_point_at_a_float(self, tenant, project, category, recorder):
        float_request = self.make(
            tenant, project, recorder, type=AllowanceType.FLOAT
        )
        expense = make_expense(
            tenant, project, category, recorder, float_request=float_request
        )

        assert list(float_request.expenses.all()) == [expense]


@pytest.mark.django_db
class TestSettingsAndSeeds:
    def test_allowance_limits_default(self, tenant):
        settings = OrganizationSettings.objects.get(organization=tenant)

        assert settings.allowance_limits == {
            "TRANSPORT_WITHIN_NAIROBI": {"min": None, "max": "500"},
            "TRANSPORT_OUTSIDE_NAIROBI": {"min": None, "max": None},
            "NIGHT_OUT": {"min": "1500", "max": "10000"},
            "TEAM_ALLOWANCE": {"min": "1500", "max": "10000"},
        }
        assert settings.finance_director_role is None

    def test_each_tenant_gets_its_own_limits(self, tenant, other_organization):
        mine = OrganizationSettings.objects.get(organization=tenant)
        mine.allowance_limits["NIGHT_OUT"]["max"] = "1"
        mine.save()

        theirs = OrganizationSettings.objects.get(organization=other_organization)
        assert theirs.allowance_limits["NIGHT_OUT"]["max"] == "10000"

    def test_the_finance_role_is_seeded(self):
        assert set(DEFAULT_ROLES["Finance"]) == {
            PERM.FINANCE_APPROVE,
            PERM.PROJECT_VIEW_COST,
            PERM.REPORT_VIEW_ALL,
        }
        assert PERM.FINANCE_APPROVE in DEFAULT_ROLES["Owner"]
        assert PERM.FINANCE_APPROVE not in DEFAULT_ROLES["Project manager"]

    def test_a_provisioned_tenant_has_the_finance_role(self, tenant):
        from core.provisioning import seed_default_roles

        seed_default_roles(tenant)

        role = Role.objects.get(organization=tenant, name="Finance")
        assert PERM.FINANCE_APPROVE in set(
            role.permissions.values_list("codename", flat=True)
        )

    def test_the_new_categories_are_seeded_with_their_kinds(self, tenant):
        ExpenseCategory.objects.all().delete()
        seed_expense_categories(tenant)

        kinds = dict(ExpenseCategory.objects.values_list("name", "kind"))
        assert kinds["Fuel"] == ExpenseKind.FUEL
        assert kinds["Casual labour"] == ExpenseKind.CASUAL_LABOUR
        assert kinds["Team allowance"] == ExpenseKind.GENERAL
        assert kinds["Transport"] == ExpenseKind.GENERAL
        assert kinds["Transport and fuel"] == ExpenseKind.GENERAL
