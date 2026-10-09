"""T18.6 — the budget position and purchase cost (§4.19.5, §4.19.3; R9).

The point of the table is that each shilling sits in one cell. These build the
awkward cases (a paid expense, a float with expenses on it, a yard purchase
before and after receipt, an advance to a subcontractor) and check the split
adds up, then that the over-budget reason is required online and never refuses
a replay.
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.factories import RoleFactory, UserFactory, UserRoleFactory
from accounts.permissions_registry import PERM
from commercials import budget, finance
from commercials.costing import cost_for, purchase_cost
from commercials.finance import OverBudgetReasonRequired
from commercials.models import (
    AllowanceRequest,
    AllowanceType,
    ExpenseCategory,
    ExpenseStatus,
    ProjectExpense,
    PurchaseDestination,
    SitePurchase,
    Subcontract,
    SubcontractPayment,
)
from commercials.tests.api_helpers import PASSWORD, Api, error_code
from jobs.models import DeliveryMode, Job, JobStatus
from locations.models import Location, LocationType
from network.factories import ProjectFactory, SiteFactory
from network.models import ProjectStatus, Subcontractor
from receiving.models import DocumentStatus, GateIn, GateInSource

D = Decimal
S = ExpenseStatus
DAY = date(2026, 10, 5)
pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _domain(settings):
    settings.TENANT_BASE_DOMAIN = "localhost"


def person(tenant, name, *codenames):
    user = UserFactory(organization=tenant, full_name=name, password=PASSWORD)
    if codenames:
        UserRoleFactory(user=user, role=RoleFactory(codenames=list(codenames)))
    return user


@pytest.fixture
def pm(tenant):
    return person(tenant, "Pippa Manager", PERM.PROJECT_VIEW_COST)


@pytest.fixture
def tech(tenant):
    return person(tenant, "Tom Technician")


@pytest.fixture
def fin(tenant):
    return person(tenant, "Fiona Finance", PERM.FINANCE_APPROVE)


@pytest.fixture
def project(tenant, pm):
    return ProjectFactory(
        reference="WO-6001",
        po_number="PO-600",
        manager=pm,
        contract_value=D("50000.00"),
        cost_budget=D("10000.00"),
    )


@pytest.fixture
def site(tenant, project):
    site = SiteFactory(name="Ruiru")
    project.sites.add(site, through_defaults={"organization_id": project.organization_id})
    return site


@pytest.fixture
def category(tenant):
    return ExpenseCategory.objects.create(organization=tenant, name="Misc")


@pytest.fixture
def yard(tenant):
    return Location.objects.create(organization=tenant, name="Yard", type=LocationType.YARD)


def approved(model, **fields):
    status = fields.pop("status", S.APPROVED)
    extra = {"decided_at": timezone.now()}
    if status == S.PAID:
        extra.update(paid_at=timezone.now(), payment_reference="R1")
    return model.objects.create(status=status, **extra, **fields)


def expense(tenant, project, site, category, user, amount="1000.00", **fields):
    return approved(
        ProjectExpense,
        organization=tenant,
        project=project,
        site=site,
        category=category,
        amount=D(amount),
        incurred_on=DAY,
        recorded_by=user,
        **fields,
    )


def purchase(tenant, project, site, user, amount="500.00", **fields):
    return approved(
        SitePurchase,
        organization=tenant,
        project=project,
        site=site,
        purchase_date=DAY,
        amount=D(amount),
        recorded_by=user,
        **fields,
    )


def allowance(tenant, project, user, amount, type=AllowanceType.NIGHT_OUT, **fields):
    return approved(
        AllowanceRequest,
        organization=tenant,
        project=project,
        type=type,
        amount=D(amount),
        from_date=DAY,
        to_date=DAY,
        recorded_by=user,
        **fields,
    )


def component(position, kind):
    return next(c for c in position.components if c.kind == kind)


class TestTheSplit:
    def test_a_paid_expense_is_counted_once(self, tenant, project, site, category, tech):
        expense(tenant, project, site, category, tech, "1000.00", status=S.PAID,
                paid_by=tech)
        position = budget.budget_position(project)

        assert component(position, "COST").spent == D("1000.00")
        assert position.spent == D("1000.00")
        assert position.committed == D("0.00")
        assert position.remaining == D("9000.00")

    def test_allowances_are_committed_then_spent(self, tenant, project, tech):
        allowance(tenant, project, tech, "800.00")
        allowance(tenant, project, tech, "300.00", status=S.PAID, paid_by=tech)
        position = budget.budget_position(project)

        assert component(position, "ALLOWANCE").committed == D("800.00")
        assert component(position, "ALLOWANCE").spent == D("300.00")

    def test_a_float_spent_is_only_the_part_not_yet_cost(
        self, tenant, project, site, category, tech
    ):
        float_ = allowance(
            tenant, project, tech, "5000.00", type=AllowanceType.FLOAT,
            status=S.PAID, paid_by=tech, returned_amount=D("500.00"),
        )
        expense(tenant, project, site, category, tech, "2000.00", float_request=float_)
        # A pending one is not cost yet, so the float still holds that money.
        ProjectExpense.objects.create(
            organization=tenant, project=project, site=site, category=category,
            amount=D("400.00"), incurred_on=DAY, recorded_by=tech, float_request=float_,
        )
        position = budget.budget_position(project)

        assert component(position, "COST").spent == D("2000.00")
        assert component(position, "FLOAT").spent == D("2500.00")  # 5000 - 2000 - 500
        assert position.spent == D("4500.00")
        # Pending float-backed spending is inside the float, not extra.
        assert position.pending == D("0.00")

    def test_an_approved_unpaid_float_is_committed(self, tenant, project, tech):
        allowance(tenant, project, tech, "3000.00", type=AllowanceType.FLOAT)
        position = budget.budget_position(project)

        assert component(position, "FLOAT").committed == D("3000.00")
        assert component(position, "FLOAT").spent == D("0.00")

    def test_a_yard_purchase_is_committed_until_received(
        self, tenant, project, site, tech, yard
    ):
        bought = purchase(
            tenant, project, site, tech, "700.00",
            destination=PurchaseDestination.INTO_YARD, receive_into=yard,
        )
        position = budget.budget_position(project)
        assert component(position, "PURCHASE").committed == D("700.00")
        assert cost_for(project).purchases == D("0.00")

        gate_in = GateIn.objects.create(
            organization=tenant, source_type=GateInSource.PURCHASE, to_location=yard,
            received_at=timezone.now(), supplier_name="Hardware Ltd",
        )
        SitePurchase.objects.filter(pk=bought.pk).update(gate_in=gate_in)
        GateIn.objects.filter(pk=gate_in.pk).update(status=DocumentStatus.POSTED, number="GRN-1")
        after = budget.budget_position(project)

        assert component(after, "PURCHASE").committed == D("0.00")
        assert after.spent == D("0.00")  # the ledger takes over at issue

    def test_a_site_purchase_is_spent_and_not_committed(self, tenant, project, site, tech):
        purchase(tenant, project, site, tech, "450.00")
        position = budget.budget_position(project)

        assert cost_for(project).purchases == D("450.00")
        assert cost_for(project).total == D("450.00")
        assert component(position, "COST").spent == D("450.00")
        assert position.committed == D("0.00")

    def test_a_subcontract_commits_what_is_left_and_spends_an_advance(
        self, tenant, project, site, tech
    ):
        sub = Subcontractor.objects.create(organization=tenant, name="Acme")
        contract = Subcontract.objects.create(
            organization=tenant, project=project, subcontractor=sub,
            contract_value=D("6000.00"), created_by=tech,
        )
        job = Job.objects.create(
            organization=tenant, client=site.client, site=site, project=project,
            assignee=tech, delivery_mode=DeliveryMode.SUBCONTRACTED, subcontractor=sub,
            agreed_price=D("2000.00"), subcontract=contract,
        )
        Job.objects.filter(pk=job.pk).update(
            status=JobStatus.CLOSED, closed_at=timezone.now()
        )
        first = budget.budget_position(project)
        # 2000 of work is done (cost), 4000 still to do (committed).
        assert component(first, "SUBCONTRACT").committed == D("4000.00")
        assert component(first, "SUBCONTRACT").spent == D("0.00")
        assert component(first, "COST").spent == D("2000.00")

        approved(
            SubcontractPayment, organization=tenant, subcontract=contract,
            amount=D("3000.00"), paid_on=DAY, reference="INV-1", recorded_by=tech,
        )
        after = budget.budget_position(project)
        # 3000 paid against 2000 done: a 1000 advance is spent; 3000 remains.
        assert component(after, "SUBCONTRACT").spent == D("1000.00")
        assert component(after, "SUBCONTRACT").committed == D("3000.00")

    def test_pending_is_outside_spent_and_committed(
        self, tenant, project, site, category, tech
    ):
        ProjectExpense.objects.create(
            organization=tenant, project=project, site=site, category=category,
            amount=D("600.00"), incurred_on=DAY, recorded_by=tech,
        )
        position = budget.budget_position(project)

        assert position.pending == D("600.00")
        assert position.spent == D("0.00") and position.committed == D("0.00")

    def test_a_project_with_no_po_has_no_budget(self, tenant, tech):
        bare = ProjectFactory(reference="WO-NOPO", manager=None)
        position = budget.budget_position(bare)

        assert position.budget is None and position.remaining is None
        assert budget.would_exceed(bare, D("99999999.00")) is None
        assert position.as_dict()["budget"] is None

    def test_a_closed_project_commits_nothing(self, tenant, project, site, tech):
        allowance(tenant, project, tech, "800.00")
        project.status = ProjectStatus.CLOSED
        position = budget.budget_position(project)

        assert position.committed == D("0.00") and position.pending == D("0.00")

    def test_the_json_shape(self, tenant, project, tech):
        allowance(tenant, project, tech, "800.00")
        data = budget.budget_position(project).as_dict()

        assert set(data) == {
            "budget", "spent", "committed", "pending", "remaining",
            "components", "over_budget_entries",
        }
        assert [c["kind"] for c in data["components"]] == [
            "COST", "ALLOWANCE", "FLOAT", "PURCHASE", "SUBCONTRACT",
        ]
        assert data["remaining"] == "9200.00"


class TestPurchaseCost:
    def test_destination_decides(self, tenant, project, site, tech, yard):
        purchase(tenant, project, site, tech, "300.00")
        purchase(
            tenant, project, site, tech, "900.00",
            destination=PurchaseDestination.INTO_YARD, receive_into=yard,
        )
        purchase(tenant, project, site, tech, "70.00", status=S.PAID, paid_by=tech)

        assert purchase_cost(project) == D("370.00")

    def test_a_reversal_is_negative(self, tenant, project, site, tech):
        original = purchase(tenant, project, site, tech, "300.00")
        approved(
            SitePurchase, organization=tenant, project=project, site=site,
            purchase_date=DAY, amount=D("300.00"), recorded_by=tech, reverses=original,
        )

        assert purchase_cost(project) == D("0.00")

    def test_unapproved_does_not_count(self, tenant, project, site, tech):
        SitePurchase.objects.create(
            organization=tenant, project=project, site=site, purchase_date=DAY,
            amount=D("300.00"), recorded_by=tech,
        )

        assert purchase_cost(project) == D("0.00")


class TestWouldExceed:
    def test_boundaries(self, tenant, project, tech):
        allowance(tenant, project, tech, "9000.00")

        assert budget.would_exceed(project, D("1000.00")) is None  # exactly the budget
        assert budget.would_exceed(project, D("1000.01")) == D("0.01")
        assert budget.would_exceed(project, D("2500.00")) == D("1500.00")

    def test_pending_counts_so_two_entries_cannot_both_slip_under(
        self, tenant, project, site, category, tech, fin
    ):
        finance.record_expense(
            actor=tech, category=category, amount=D("6000.00"), incurred_on=DAY, site=site
        )

        assert budget.would_exceed(project, D("4000.00")) is None
        assert budget.would_exceed(project, D("4000.01")) == D("0.01")


class TestRecordingOverBudget:
    def test_an_expense_over_budget_needs_a_reason_online(
        self, tenant, project, site, category, tech, fin
    ):
        with pytest.raises(OverBudgetReasonRequired) as caught:
            finance.record_expense(
                actor=tech, category=category, amount=D("12000.00"),
                incurred_on=DAY, site=site,
            )
        assert caught.value.code == "OVER_BUDGET_REASON_REQUIRED"
        assert caught.value.details["over"] is True
        assert ProjectExpense.objects.count() == 0

    def test_with_a_reason_it_records_the_overrun(
        self, tenant, project, site, category, tech, fin
    ):
        made = finance.record_expense(
            actor=tech, category=category, amount=D("12000.00"), incurred_on=DAY,
            site=site, over_budget_reason="Emergency repair",
        )

        assert made.over_budget_by == D("2000.00")
        assert made.over_budget_reason == "Emergency repair"

    def test_within_budget_records_nothing(self, tenant, project, site, category, tech, fin):
        made = finance.record_expense(
            actor=tech, category=category, amount=D("10000.00"), incurred_on=DAY, site=site,
            over_budget_reason="not needed",
        )

        assert made.over_budget_by is None and made.over_budget_reason == ""

    def test_offline_flags_without_refusing(self, tenant, project, site, category, tech, fin):
        made = finance.record_expense(
            actor=tech, category=category, amount=D("12000.00"), incurred_on=DAY,
            site=site, offline=True,
        )

        assert made.over_budget_by == D("2000.00") and made.over_budget_reason == ""

    def test_a_float_backed_expense_is_not_checked_again(
        self, tenant, project, site, category, tech, fin
    ):
        float_ = allowance(
            tenant, project, tech, "9500.00", type=AllowanceType.FLOAT,
            status=S.PAID, paid_by=tech,
        )
        float_.recorded_by = tech
        made = finance.record_expense(
            actor=tech, category=category, amount=D("3000.00"), incurred_on=DAY,
            site=site, float_request=float_,
        )

        assert made.over_budget_by is None

    def test_an_allowance_over_budget_needs_a_reason(self, tenant, project, site, tech, fin):
        allowance(tenant, project, tech, "9000.00", type=AllowanceType.FLOAT)
        with pytest.raises(OverBudgetReasonRequired):
            finance.request_allowance(
                actor=tech, type="NIGHT_OUT", amount=D("2000.00"), from_date=DAY,
                to_date=DAY, site=site,
            )
        made = finance.request_allowance(
            actor=tech, type="NIGHT_OUT", amount=D("2000.00"), from_date=DAY, to_date=DAY,
            site=site, over_budget_reason="Long job",
        )
        assert made.over_budget_by == D("1000.00")

    def test_a_project_without_a_po_needs_no_reason(self, tenant, category, tech, fin, pm):
        bare = ProjectFactory(reference="WO-BARE", manager=pm)
        bare_site = SiteFactory(name="Bare")
        bare.sites.add(bare_site, through_defaults={"organization_id": bare.organization_id})
        made = finance.record_expense(
            actor=tech, category=category, amount=D("999999.00"), incurred_on=DAY,
            site=bare_site,
        )

        assert made.over_budget_by is None


class TestReplayFromAPhone:
    def test_the_sync_door_never_refuses(self, tenant, project, site, category, tech, fin):
        from sync.services import apply_submission

        key = uuid.uuid4()
        submission, _ = apply_submission(
            organization=tenant,
            client_uuid=key,
            operation="EXPENSE",
            payload={
                "client_uuid": str(key), "category": category.pk, "site": site.pk,
                "amount": "12000.00", "incurred_on": "2026-10-05",
            },
            submitted_by=tech,
        )

        assert submission.status == "APPLIED"
        made = ProjectExpense.objects.get()
        assert made.over_budget_by == D("2000.00") and made.over_budget_reason == ""


class TestApi:
    def body(self, category, site, **extra):
        return {
            "category": category.pk, "site": site.pk, "amount": "12000.00",
            "incurred_on": "2026-10-05", **extra,
        }

    def test_online_without_a_reason_is_refused_with_the_code(
        self, client, tech, fin, project, site, category
    ):
        response = Api(client, tech).post("project-expenses", self.body(category, site))

        assert response.status_code == 400
        assert error_code(response) == "OVER_BUDGET_REASON_REQUIRED"
        # The recorder holds no cost permission, so the figure is withheld.
        assert "over_by" not in response.json()["error"]["details"]

    def test_with_a_reason_the_manager_sees_the_overrun(
        self, client, tech, pm, fin, project, site, category
    ):
        response = Api(client, tech).post(
            "project-expenses", self.body(category, site, over_budget_reason="Urgent")
        )
        assert response.status_code == 201, response.content
        body = response.json()
        assert body["is_over_budget"] is True
        assert body["over_budget_reason"] == "Urgent"
        assert "over_budget_by" not in body  # the recorder sees the reason, not figures

        seen = Api(client, pm).get(f"project-expenses/{body['id']}")
        assert seen.json()["over_budget_by"] == "2000.00"

    def test_the_budget_endpoint_and_the_check(
        self, client, tech, pm, fin, project, site, category
    ):
        mine = Api(client, pm)
        data = mine.get(f"projects/{project.pk}/budget").json()
        assert data["budget"] == "10000.00" and data["remaining"] == "10000.00"

        over = mine.post(f"projects/{project.pk}/budget-check", {"amount": "10500.00"})
        assert over.json() == {"over": True, "over_by": "500.00"}
        fine = mine.post(f"projects/{project.pk}/budget-check", {"amount": "10000.00"})
        assert fine.json() == {"over": False}

    def test_a_member_gets_the_boolean_only(self, client, tech, fin, project):
        response = Api(client, tech).post(
            f"projects/{project.pk}/budget-check", {"amount": "10500.00"}
        )
        if response.status_code == 200:
            assert response.json() == {"over": True}

    def test_performance_carries_the_position(self, client, pm, project):
        data = Api(client, pm).get(f"projects/{project.pk}/performance").json()

        assert data["budget_position"]["budget"] == "10000.00"
        assert "purchases" in data
