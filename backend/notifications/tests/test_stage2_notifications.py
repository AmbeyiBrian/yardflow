"""T18.13 — Stage 2 notifications and the milestone sweep (§4.19.7, §4.19.12).

Purchases and subcontract payments reuse the finance events; the PO events and
the daily milestone step are new. Pinned: who is told, in which words, that the
sweep alerts once per state, that it repeats overdue every seven days and
catches up a missed run, and that nothing crosses tenants.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.factories import RoleFactory, UserFactory, UserRoleFactory
from accounts.permissions_registry import PERM
from approvals import engine
from commercials import finance
from commercials.models import (
    MilestoneCondition,
    MilestoneInvoice,
    MilestoneReceipt,
    MilestoneShare,
    ProjectMilestone,
    PurchaseDestination,
    SitePurchase,
    Subcontract,
    SubcontractPayment,
)
from commercials.po import attach_po
from core import sweeps
from core.tenancy import tenant_context
from network.factories import ProjectFactory, SiteFactory
from network.models import Subcontractor
from notifications.events import emit, emit_yard_delivery_expected, render_body
from notifications.matrix import (
    Channel,
    Event,
    Recipient,
    channels_for,
    default_channels_config,
    default_matrix_config,
    recipients_for,
)
from notifications.models import NotificationDelivery, NotificationEvent
from notifications.views import resource_for

D = Decimal
TODAY = timezone.localdate()


@pytest.fixture(autouse=True)
def seeded_matrix(tenant):
    settings = tenant.settings
    settings.notification_matrix = default_matrix_config()
    settings.notification_channels = default_channels_config()
    settings.save()


@pytest.fixture(autouse=True)
def deliver_at_once(monkeypatch):
    """Run the after-commit dispatch inline, so a test reads its deliveries."""
    monkeypatch.setattr("notifications.events.transaction.on_commit", lambda func: func())


def person(tenant, name, *codenames):
    user = UserFactory(
        organization=tenant, full_name=name, email=f"{name.split()[0].lower()}@x.co.ke"
    )
    if codenames:
        UserRoleFactory(user=user, role=RoleFactory(name=f"R-{name}", codenames=list(codenames)))
    return user


@pytest.fixture
def pm(tenant):
    return person(tenant, "Pippa Manager")


@pytest.fixture
def tech(tenant):
    return person(tenant, "Tom Tech")


@pytest.fixture
def fin(tenant):
    return person(tenant, "Fiona Finance", PERM.FINANCE_APPROVE)


@pytest.fixture
def store(tenant):
    return person(tenant, "Sam Store", PERM.GATE_IN_POST)


@pytest.fixture
def project(tenant, pm):
    return ProjectFactory(
        reference="WO-1813",
        po_number="PO-1813",
        manager=pm,
        contract_value=D("100000.00"),
        cost_budget=D("70000.00"),
        po_issue_date=TODAY - timedelta(days=60),
        payment_terms_days=30,
    )


def who(event_key, channel=None):
    found = NotificationDelivery.objects.filter(event__event_key=event_key)
    if channel:
        found = found.filter(channel=channel)
    return {d.recipient.pk for d in found}


def purchase(tenant, project, by, **fields):
    values = {
        "organization": tenant,
        "project": project,
        "site": SiteFactory(name="Ruiru"),
        "purchase_date": TODAY,
        "amount": D("2500.00"),
        "recorded_by": by,
        "number": "SP-0001",
    }
    values.update(fields)
    item = SitePurchase.objects.create(**values)
    finance._route(item, by)
    return item


def payment(tenant, project, by):
    contract = Subcontract.objects.create(
        organization=tenant,
        project=project,
        subcontractor=Subcontractor.objects.create(organization=tenant, name="Acme Civils"),
        number="SC-1",
        contract_value=D("50000.00"),
        created_by=by,
    )
    return SubcontractPayment.objects.create(
        organization=tenant,
        subcontract=contract,
        amount=D("1000.00"),
        paid_on=TODAY,
        reference="INV-1",
        recorded_by=by,
    )


@pytest.mark.django_db
class TestMatrixAndRoutes:
    def test_defaults_follow_the_design(self, tenant):
        assert recipients_for(tenant, Event.PO_MILESTONE_DUE) == [Recipient.FINANCE_APPROVERS]
        assert set(channels_for(tenant, Event.PO_MILESTONE_DUE)) == {Channel.IN_APP, Channel.EMAIL}
        assert recipients_for(tenant, Event.PO_MILESTONE_OVERDUE) == [
            Recipient.FINANCE_APPROVERS,
            Recipient.PROJECT_MANAGER,
        ]
        assert channels_for(tenant, Event.PO_ATTACHED) == [Channel.IN_APP]
        assert recipients_for(tenant, Event.PURCHASE_YARD_DELIVERY_EXPECTED) == [
            Recipient.STOREKEEPERS
        ]
        for key in (
            Event.PO_MILESTONE_DUE,
            Event.PO_MILESTONE_OVERDUE,
            Event.PO_ATTACHED,
            Event.PURCHASE_YARD_DELIVERY_EXPECTED,
        ):
            assert Channel.SMS not in channels_for(tenant, key)

    def test_links(self):
        assert resource_for("commercials.SitePurchase", "4") == "/money/purchases/4"
        assert resource_for("network.Project", "9", Event.PO_MILESTONE_DUE) == "/projects/9"
        assert resource_for("commercials.SubcontractPayment", "3") == "/approvals"


@pytest.mark.django_db
class TestSitePurchaseEvents:
    def test_waiting_goes_to_the_pm_then_finance_and_never_the_recorder(
        self, tenant, project, pm, tech, fin
    ):
        item = purchase(tenant, project, tech)
        emit(Event.FINANCE_AWAITING_APPROVAL, item)
        assert who(Event.FINANCE_AWAITING_APPROVAL, Channel.IN_APP) == {pm.pk}

        finance.decide(item, actor=pm, approved=True)
        assert who(Event.FINANCE_AWAITING_APPROVAL, Channel.IN_APP) == {pm.pk, fin.pk}

    def test_approved_rejected_and_paid_tell_the_recorder(self, tenant, project, pm, tech, fin):
        item = purchase(tenant, project, tech)
        finance.decide(item, actor=pm, approved=True)
        finance.decide(item, actor=fin, approved=True)
        assert who(Event.FINANCE_APPROVED) == {tech.pk}
        finance.mark_paid(item, actor=fin, reference="MPESA1")
        assert who(Event.FINANCE_PAID) == {tech.pk}

        other = purchase(tenant, project, tech, number="SP-0002")
        finance.decide(other, actor=pm, approved=False, reason="Too dear")
        assert who(Event.FINANCE_REJECTED) == {tech.pk}

    def test_wording(self, tenant, project, tech):
        item = purchase(tenant, project, tech)
        event = emit(Event.FINANCE_AWAITING_APPROVAL, item)
        body = render_body(event)
        assert body.startswith("Site purchase SP-0001 waiting for your approval: KES 2,500")
        assert "Ruiru" in body
        assert "used at the site" in body

    def test_a_yard_purchase_says_it_creates_a_delivery(self, tenant, project, tech):
        item = purchase(tenant, project, tech)
        item.destination = PurchaseDestination.INTO_YARD
        event = emit(Event.FINANCE_AWAITING_APPROVAL, item)
        assert "creates a delivery" in render_body(event)

    def test_the_over_budget_flag_is_in_the_approver_payload(self, tenant, project, tech):
        item = purchase(
            tenant,
            project,
            tech,
            over_budget_by=D("1200.00"),
            over_budget_reason="Urgent repair",
        )
        event = emit(Event.FINANCE_AWAITING_APPROVAL, item)
        assert event.payload["over_budget_by"] == "1200.00"
        assert event.payload["over_budget_reason"] == "Urgent repair"
        body = render_body(event)
        assert "Over the project's budget by KES 1,200" in body
        assert "Urgent repair" in body

    def test_no_over_budget_keys_when_within_budget(self, tenant, project, tech):
        event = emit(Event.FINANCE_AWAITING_APPROVAL, purchase(tenant, project, tech))
        assert "over_budget_by" not in event.payload

    def test_yard_delivery_expected_goes_to_storekeepers(self, tenant, project, tech, store):
        emit_yard_delivery_expected(purchase(tenant, project, tech))
        assert who(Event.PURCHASE_YARD_DELIVERY_EXPECTED) == {store.pk}
        delivery = NotificationDelivery.objects.get(
            event__event_key=Event.PURCHASE_YARD_DELIVERY_EXPECTED
        )
        assert "delivery is expected" in delivery.body


@pytest.mark.django_db
class TestSubcontractPaymentEvents:
    def test_waiting_goes_to_the_pm_with_the_contract_figures(self, tenant, project, pm, tech):
        item = payment(tenant, project, tech)
        engine.create_requests(item, requested_by=tech)
        event = emit(Event.FINANCE_AWAITING_APPROVAL, item)

        assert who(Event.FINANCE_AWAITING_APPROVAL, Channel.IN_APP) == {pm.pk}
        assert who(Event.FINANCE_AWAITING_APPROVAL, Channel.EMAIL) == {pm.pk}
        assert event.payload["contract_value"] == "50000.00"
        assert event.payload["work_done"] == "0.00"
        assert event.payload["paid"] == "0.00"
        body = render_body(event)
        assert body.startswith("Subcontract payment waiting for your approval: KES 1,000 to Acme")
        assert "contract value KES 50,000" in body

    def test_decided_tells_the_recorder(self, tenant, project, tech):
        item = payment(tenant, project, tech)
        emit(Event.FINANCE_APPROVED, item)
        emit(Event.FINANCE_REJECTED, item)
        assert who(Event.FINANCE_APPROVED) == {tech.pk}
        assert who(Event.FINANCE_REJECTED) == {tech.pk}


def milestone(project, sequence=1, **fields):
    values = {
        "organization_id": project.organization_id,
        "project": project,
        "sequence": sequence,
        "name": f"M{sequence}",
        "share_type": MilestoneShare.PERCENT,
        "share_value": D("30"),
        "condition": MilestoneCondition.NONE,
    }
    values.update(fields)
    return ProjectMilestone.objects.create(**values)


def invoice(ms, on, amount="30000.00"):
    return MilestoneInvoice.objects.create(
        organization_id=ms.organization_id,
        milestone=ms,
        invoice_number="INV-1",
        invoice_date=on,
        amount=D(amount),
        recorded_by=UserFactory(organization_id=ms.organization_id),
    )


def events(key):
    return NotificationEvent.objects.filter(event_key=key)


def sweep(tenant):
    return sweeps._sweep_milestones(tenant.pk)


@pytest.mark.django_db
class TestMilestoneSweep:
    def test_due_alerts_finance_once_however_often_it_runs(self, tenant, project, pm, fin):
        ms = milestone(project)
        assert sweep(tenant) == {"due": 1, "overdue": 0}
        assert sweep(tenant) == {"due": 0, "overdue": 0}
        [event] = events(Event.PO_MILESTONE_DUE)
        assert event.target_type == "network.Project"
        assert event.target_id == str(project.pk)
        assert who(Event.PO_MILESTONE_DUE, Channel.IN_APP) == {fin.pk}
        assert who(Event.PO_MILESTONE_DUE, Channel.EMAIL) == {fin.pk}
        ms.refresh_from_db()
        assert ms.due_notified_on == TODAY
        body = NotificationDelivery.objects.get(
            event__event_key=Event.PO_MILESTONE_DUE, channel=Channel.IN_APP
        ).body
        assert body == 'Milestone "M1" (KES 30,000) on PO-1813 is due. Raise the invoice.'

    def test_a_missed_run_catches_up(self, tenant, project):
        ms = milestone(
            project,
            condition=MilestoneCondition.DATE,
            condition_date=TODAY - timedelta(days=9),
        )
        assert sweep(tenant)["due"] == 1
        ms.refresh_from_db()
        assert ms.due_notified_on == TODAY

    def test_not_yet_due_and_invoiced_say_nothing(self, tenant, project):
        milestone(
            project,
            1,
            condition=MilestoneCondition.DATE,
            condition_date=TODAY + timedelta(days=1),
        )
        invoiced = milestone(project, 2)
        invoice(invoiced, TODAY - timedelta(days=5))
        assert sweep(tenant) == {"due": 0, "overdue": 0}

    def test_overdue_tells_finance_and_copies_the_pm_in_app_only(self, tenant, project, pm, fin):
        ms = milestone(project)
        invoice(ms, TODAY - timedelta(days=45))
        assert sweep(tenant) == {"due": 0, "overdue": 1}

        assert who(Event.PO_MILESTONE_OVERDUE, Channel.IN_APP) == {fin.pk, pm.pk}
        assert who(Event.PO_MILESTONE_OVERDUE, Channel.EMAIL) == {fin.pk}
        ms.refresh_from_db()
        assert ms.overdue_notified_on == TODAY
        body = NotificationDelivery.objects.get(recipient=fin, channel=Channel.IN_APP).body
        assert "overdue" in body and "KES 30,000 is still unpaid" in body

    def test_overdue_repeats_every_seven_days_and_not_before(self, tenant, project):
        ms = milestone(project)
        invoice(ms, TODAY - timedelta(days=45))
        ms.overdue_notified_on = TODAY - timedelta(days=6)
        ms.save()
        assert sweep(tenant)["overdue"] == 0

        ms.overdue_notified_on = TODAY - timedelta(days=7)
        ms.save()
        assert sweep(tenant)["overdue"] == 1
        assert sweep(tenant)["overdue"] == 0

    def test_a_paid_milestone_stops_the_chasing(self, tenant, project):
        ms = milestone(project)
        invoice(ms, TODAY - timedelta(days=45))
        MilestoneReceipt.objects.create(
            organization_id=ms.organization_id,
            milestone=ms,
            received_on=TODAY - timedelta(days=1),
            amount=D("30000.00"),
            recorded_by=UserFactory(organization_id=ms.organization_id),
        )
        assert sweep(tenant) == {"due": 0, "overdue": 0}

    def test_closed_projects_are_skipped(self, tenant, pm):
        closed = ProjectFactory(
            reference="WO-C",
            po_number="PO-C",
            manager=pm,
            status="CLOSED",
            closed_at=timezone.now(),
            contract_value=D("1000"),
            cost_budget=D("500"),
            po_issue_date=TODAY - timedelta(days=5),
        )
        milestone(closed)
        assert sweep(tenant) == {"due": 0, "overdue": 0}

    def test_sweep_tenant_runs_it_and_a_failure_does_not_stop_the_rest(
        self, tenant, project, monkeypatch
    ):
        milestone(project)
        result = sweeps.sweep_tenant.run(organization_id=str(tenant.pk))
        assert result["milestones"] == {"due": 1, "overdue": 0}

        def boom(_):
            raise RuntimeError("x")

        monkeypatch.setattr(sweeps, "_sweep_milestones", boom)
        result = sweeps.sweep_tenant.run(organization_id=str(tenant.pk))
        assert result["milestones"] == "failed"
        assert result["asset_expiries"] == 0

    def test_it_does_not_cross_tenants(self, tenant, other_organization, project, fin):
        milestone(project)
        with tenant_context(other_organization.pk):
            assert sweeps._sweep_milestones(other_organization.pk) == {"due": 0, "overdue": 0}
        assert not events(Event.PO_MILESTONE_DUE).exists()


@pytest.mark.django_db
class TestAttachPoNotice:
    def test_finance_is_told_in_app_only_and_not_the_pm(self, tenant, pm, fin):
        no_po = ProjectFactory(reference="WO-X", po_number="", manager=pm)
        attach_po(
            no_po,
            po_number="PO-77",
            po_issue_date=date(2026, 10, 1),
            contract_value=D("90000.00"),
            cost_budget=D("60000.00"),
            actor=fin,
        )
        assert who(Event.PO_ATTACHED, Channel.IN_APP) == {fin.pk}
        assert who(Event.PO_ATTACHED, Channel.EMAIL) == set()
        [event] = events(Event.PO_ATTACHED)
        assert event.target_type == "network.Project"
        assert event.payload["po_number"] == "PO-77"
        body = render_body(event)
        assert "PO PO-77 (KES 90,000)" in body and no_po.reference in body
