"""T18.5 — routing site purchases and subcontract payments (§4.19.3, §4.19.4; R7, R8).

A purchase routes exactly as an expense (PM then Finance, PM skipped for the PM
or the Director). A subcontract payment has one level, the project's PM, and the
recorder never answers it.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse

from accounts.factories import RoleFactory, UserFactory, UserRoleFactory
from accounts.models import Role, User, UserRole
from accounts.permissions_registry import PERM
from approvals.engine import (
    NotAnApprover,
    ProjectHasNoActiveManager,
    SelfApprovalNotAllowed,
    can_approve,
    create_requests,
    readdress_project_requests,
    record_decision,
    required_levels,
)
from approvals.models import ApprovalDecision, ApprovalRequest
from commercials.models import (
    ExpenseCategory,
    ProjectExpense,
    SitePurchase,
    Subcontract,
    SubcontractPayment,
)
from core.provisioning import provision_tenant
from core.tenancy import tenant_context
from network.factories import ProjectFactory, SiteFactory
from network.models import Subcontractor

PASSWORD = "a good long password"


def shape(levels):
    return [(level.level, level.user or level.permission or level.role) for level in levels]


@pytest.fixture
def pm(tenant):
    return UserFactory(organization=tenant, full_name="Pippa Manager")


@pytest.fixture
def recorder(tenant):
    return UserFactory(organization=tenant, full_name="Tom Technician")


@pytest.fixture
def finance_user(tenant):
    role = RoleFactory(name="Finance", codenames=[PERM.FINANCE_APPROVE])
    user = UserFactory(organization=tenant, full_name="Fiona Finance")
    UserRoleFactory(user=user, role=role)
    return user


@pytest.fixture
def project(tenant, pm):
    return ProjectFactory(
        reference="WO-9802",
        po_number="PO-981",
        manager=pm,
        contract_value=Decimal("100000.00"),
        cost_budget=Decimal("80000.00"),
    )


@pytest.fixture
def site(tenant):
    return SiteFactory()


def purchase_by(tenant, project, site, who):
    return SitePurchase.objects.create(
        organization=tenant,
        project=project,
        site=site,
        purchase_date=date(2026, 10, 1),
        amount=Decimal("500.00"),
        recorded_by=who,
    )


def payment_by(tenant, project, who):
    subcontractor = Subcontractor.objects.create(organization=tenant, name="Acme Civils")
    contract = Subcontract.objects.create(
        organization=tenant,
        project=project,
        subcontractor=subcontractor,
        contract_value=Decimal("50000.00"),
        created_by=who,
    )
    return SubcontractPayment.objects.create(
        organization=tenant,
        subcontract=contract,
        amount=Decimal("1000.00"),
        paid_on=date(2026, 10, 2),
        reference="INV-1",
        recorded_by=who,
    )


@pytest.mark.django_db
class TestPurchaseRouting:
    def test_an_ordinary_purchase_goes_to_the_pm_then_finance(
        self, tenant, project, site, recorder, pm
    ):
        levels = required_levels(purchase_by(tenant, project, site, recorder))

        assert shape(levels) == [(1, pm), (2, "finance.approve")]

    def test_a_pm_recorded_purchase_skips_the_pm_level(self, tenant, project, site, pm):
        levels = required_levels(purchase_by(tenant, project, site, pm))

        assert shape(levels) == [(2, "finance.approve")]

    def test_a_director_recorded_purchase_skips_the_pm_level(self, tenant, project, site, recorder):
        director = RoleFactory(name="Director")
        UserRoleFactory(user=recorder, role=director)
        tenant.settings.finance_director_role = director
        tenant.settings.save()

        levels = required_levels(purchase_by(tenant, project, site, recorder))

        assert shape(levels) == [(2, "finance.approve")]

    def test_an_inactive_pm_is_refused(self, tenant, project, site, recorder, pm):
        pm.is_active = False
        pm.save()

        with pytest.raises(ProjectHasNoActiveManager):
            required_levels(purchase_by(tenant, project, site, recorder))

    def test_the_recorder_cannot_approve_at_finance(self, tenant, project, site, pm, finance_user):
        purchase = purchase_by(tenant, project, site, finance_user)
        request = create_requests(purchase, requested_by=finance_user)[-1]

        assert can_approve(finance_user, request, document=purchase) == (False, "self")
        record_decision(purchase, actor=pm, decision=ApprovalDecision.APPROVED)
        with pytest.raises(SelfApprovalNotAllowed):
            record_decision(purchase, actor=finance_user, decision=ApprovalDecision.APPROVED)

    def test_the_two_levels_resolve_in_order(
        self, tenant, project, site, recorder, pm, finance_user
    ):
        purchase = purchase_by(tenant, project, site, recorder)
        create_requests(purchase, requested_by=recorder)

        decided, still_open = record_decision(
            purchase, actor=pm, decision=ApprovalDecision.APPROVED
        )
        assert decided.level == 1 and still_open is not None and still_open.level == 2
        with pytest.raises(NotAnApprover):
            record_decision(purchase, actor=pm, decision=ApprovalDecision.APPROVED)
        _, still_open = record_decision(
            purchase, actor=finance_user, decision=ApprovalDecision.APPROVED
        )
        assert still_open is None


@pytest.mark.django_db
class TestSubcontractPaymentRouting:
    def test_one_level_the_project_pm_only(self, tenant, project, recorder, pm):
        levels = required_levels(payment_by(tenant, project, recorder))

        assert shape(levels) == [(1, pm)]

    def test_there_is_no_finance_level_even_for_a_pm_recorder(self, tenant, project, pm):
        # Routing does not skip; the service refuses a PM recording (§4.19.4).
        assert shape(required_levels(payment_by(tenant, project, pm))) == [(1, pm)]

    def test_an_inactive_pm_is_refused(self, tenant, project, recorder, pm):
        payment = payment_by(tenant, project, recorder)
        pm.is_active = False
        pm.save()

        with pytest.raises(ProjectHasNoActiveManager):
            required_levels(payment)

    def test_a_project_without_a_manager_is_refused(self, tenant, project, recorder):
        payment = payment_by(tenant, project, recorder)
        # A PO project cannot be saved without a manager (a DB check), so the
        # unmanaged state is set on the in-memory project the payment reads.
        payment.subcontract.project.manager = None

        with pytest.raises(ProjectHasNoActiveManager):
            required_levels(payment)

    @pytest.mark.parametrize("allow_self", [False, True])
    def test_the_recorder_pm_cannot_approve_their_own_payment(
        self, tenant, project, pm, allow_self
    ):
        tenant.settings.allow_self_approval = allow_self
        tenant.settings.save()
        payment = payment_by(tenant, project, pm)
        request = create_requests(payment, requested_by=pm)[0]

        assert can_approve(pm, request, document=payment) == (False, "self")
        with pytest.raises(SelfApprovalNotAllowed):
            record_decision(payment, actor=pm, decision=ApprovalDecision.APPROVED)

    def test_only_the_pm_may_approve_not_finance(self, tenant, project, recorder, pm, finance_user):
        payment = payment_by(tenant, project, recorder)
        request = create_requests(payment, requested_by=recorder)[0]

        assert can_approve(finance_user, request, document=payment) == (False, "not_the_manager")
        assert can_approve(pm, request, document=payment) == (True, "")
        _, still_open = record_decision(payment, actor=pm, decision=ApprovalDecision.APPROVED)
        assert still_open is None

    def test_a_new_manager_inherits_the_open_request(self, tenant, project, recorder, pm):
        payment = payment_by(tenant, project, recorder)
        create_requests(payment, requested_by=recorder)
        new_pm = UserFactory(organization=tenant)
        project.manager = new_pm
        project.save()

        assert readdress_project_requests(project, old_manager_id=pm.pk) == 1
        assert ApprovalRequest.objects.get().required_user_id == new_pm.pk


@pytest.mark.django_db
class TestExistingRoutingUnchanged:
    def test_an_expense_still_goes_pm_then_finance(self, tenant, project, recorder, pm):
        category = ExpenseCategory.objects.create(organization=tenant, name="Misc")
        expense = ProjectExpense.objects.create(
            organization=tenant,
            project=project,
            category=category,
            amount=Decimal("1000.00"),
            incurred_on=date(2026, 10, 1),
            recorded_by=recorder,
        )

        assert shape(required_levels(expense)) == [(1, pm), (2, "finance.approve")]


# --- /approvals/pending ---------------------------------------------------


@pytest.fixture
def world(db, client, settings):
    settings.TENANT_BASE_DOMAIN = "localhost"
    result = provision_tenant(
        name="Silvertech", slug="silvertech", owner_email="owner@silvertech.co.ke"
    )
    client.defaults["HTTP_HOST"] = "silvertech.localhost"
    return client, result["organization"]


def make_user(organization, email, *, codenames=()):
    user = User.objects.create_user(
        email=email, password=PASSWORD, organization=organization, full_name=email
    )
    if codenames:
        role = Role.objects.create(organization=organization, name=f"Role {email}")
        role.set_permissions(list(codenames))
        UserRole.objects.create(organization=organization, user=user, role=role)
    return user


def pending_rows(client, email):
    token = client.post(
        reverse("v1:auth:login"),
        {"identifier": email, "password": PASSWORD},
        content_type="application/json",
    ).json()["access"]
    response = client.get(reverse("v1:approval-pending"), HTTP_AUTHORIZATION=f"Bearer {token}")
    assert response.status_code == 200, response.content
    body = response.json()
    return body["results"] if isinstance(body, dict) else body


class TestPendingShowsThemToTheRightPeople:
    def test_pm_sees_both_then_finance_sees_the_purchase_after_the_pm(self, world):
        client, organization = world
        with tenant_context(organization):
            pm = make_user(organization, "pm@silvertech.co.ke")
            recorder = make_user(organization, "tech@silvertech.co.ke")
            make_user(organization, "fin@silvertech.co.ke", codenames=[PERM.FINANCE_APPROVE])
            project = ProjectFactory(
                reference="WO-9804",
                po_number="PO-983",
                manager=pm,
                contract_value=Decimal("100000.00"),
                cost_budget=Decimal("80000.00"),
            )
            purchase = purchase_by(organization, project, SiteFactory(), recorder)
            create_requests(purchase, requested_by=recorder)
            payment = payment_by(organization, project, recorder)
            create_requests(payment, requested_by=recorder)

        pm_rows = pending_rows(client, "pm@silvertech.co.ke")
        assert {row["document_type"] for row in pm_rows} == {
            "commercials.SitePurchase",
            "commercials.SubcontractPayment",
        }
        by_type = {row["document_type"]: row["document"] for row in pm_rows}
        assert by_type["commercials.SitePurchase"]["kind"] == "PURCHASE"
        assert by_type["commercials.SitePurchase"]["amount"] == "500.00"
        assert by_type["commercials.SubcontractPayment"]["kind"] == "SUBCONTRACT_PAYMENT"
        assert by_type["commercials.SubcontractPayment"]["reference"] == "INV-1"

        # Finance waits behind the PM, and never sees the PM-only payment.
        assert pending_rows(client, "fin@silvertech.co.ke") == []
        # The recorder is nobody's approver.
        assert pending_rows(client, "tech@silvertech.co.ke") == []

        with tenant_context(organization):
            record_decision(purchase, actor=pm, decision=ApprovalDecision.APPROVED)

        finance_rows = pending_rows(client, "fin@silvertech.co.ke")
        assert [row["document_type"] for row in finance_rows] == ["commercials.SitePurchase"]
