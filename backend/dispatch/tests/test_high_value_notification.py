"""T10.11 — telling the owner when something expensive leaves (§9, §4.14; O7).

Single-signature approval on project material (D22) should not also be
unwatched. This is the watching: after the fact, blocking nothing, because O6
deliberately did not ask for a second gate.

The tests that matter most are the ones asserting it **does not block** —
a courtesy message must never be able to stop a van.
"""

from decimal import Decimal

import pytest
from django.db import transaction

from accounts.factories import UserFactory
from catalogue.factories import ItemTypeFactory
from catalogue.models import TrackingMode
from dispatch.models import GateOut, GateOutLine, GateOutPurpose, GateOutStatus
from dispatch.services import approve_gate_out, submit_gate_out
from jobs.models import Job
from locations.factories import YardFactory
from locations.nodes import external_node, node_for_location
from network.factories import ProjectFactory, SiteFactory
from notifications.models import NotificationEvent
from stock.models import MovementType
from stock.services import MovementRequest, post_movement


@pytest.fixture
def yard(tenant):
    return YardFactory(name="Main yard")


@pytest.fixture
def manager(tenant):
    return UserFactory(organization=tenant, full_name="Pippa Manager")


@pytest.fixture
def technician(tenant):
    return UserFactory(organization=tenant, full_name="Tom Technician")


@pytest.fixture
def site(tenant):
    return SiteFactory(internal_ref="SLV-5001", name="Kileleshwa")


@pytest.fixture
def job(tenant, site, technician, manager):
    project = ProjectFactory(
        reference="WO-9101",
        po_number="PO-910",
        manager=manager,
        contract_value=Decimal("100000.00"),
        cost_budget=Decimal("80000.00"),
    )
    return Job.objects.create(
        organization=tenant,
        reference="JOB-N01",
        client=site.client,
        site=site,
        project=project,
        assignee=technician,
    )


def build_pass(tenant, yard, technician, site, job, *, unit_cost, quantity="2"):
    gate_out = GateOut(
        organization=tenant,
        purpose_type=GateOutPurpose.INSTALLATION,
        from_location=yard,
        site=site,
        job=job,
        custody_holder=technician,
        requested_by=technician,
    )
    gate_out.save()
    item = ItemTypeFactory(
        default_tracking_mode=TrackingMode.BULK, unit_cost=unit_cost
    )
    with transaction.atomic():
        post_movement(
            MovementRequest(
                item_type=item,
                quantity=Decimal("100"),
                from_node=external_node(tenant.pk),
                to_node=node_for_location(yard),
                movement_type=MovementType.RECEIPT,
            )
        )
    GateOutLine.objects.create(
        organization=tenant,
        gate_out=gate_out,
        item_type=item,
        tracking_mode=TrackingMode.BULK,
        requested_qty=Decimal(quantity),
        uom=item.uom,
    )
    return gate_out


def approved_events(tenant):
    return NotificationEvent.objects.filter(event_key="project.high_value_release")


def run_through_approval(gate_out, technician, manager):
    submit_gate_out(gate_out, submitted_by=technician)
    gate_out.refresh_from_db()
    if gate_out.status == GateOutStatus.PENDING_APPROVAL:
        approve_gate_out(gate_out, actor=manager)
        gate_out.refresh_from_db()
    return gate_out


@pytest.mark.django_db
class TestTheThreshold:
    def test_a_release_above_it_tells_the_owner(
        self, tenant, yard, technician, site, job, manager
    ):
        tenant.settings.project_release_notify_above = Decimal("1000.00")
        tenant.settings.save()

        gate_out = build_pass(
            tenant, yard, technician, site, job, unit_cost=Decimal("900.00")
        )
        run_through_approval(gate_out, technician, manager)

        assert approved_events(tenant).count() == 1
        payload = approved_events(tenant).first().payload
        assert payload["value"] == "1800.00"
        assert payload["approved_by"] == "Pippa Manager"

    def test_a_release_below_it_does_not(
        self, tenant, yard, technician, site, job, manager
    ):
        tenant.settings.project_release_notify_above = Decimal("1000000.00")
        tenant.settings.save()

        gate_out = build_pass(
            tenant, yard, technician, site, job, unit_cost=Decimal("900.00")
        )
        run_through_approval(gate_out, technician, manager)

        assert approved_events(tenant).count() == 0

    def test_no_threshold_means_never(
        self, tenant, yard, technician, site, job, manager
    ):
        tenant.settings.project_release_notify_above = None
        tenant.settings.save()

        gate_out = build_pass(
            tenant, yard, technician, site, job, unit_cost=Decimal("900000.00")
        )
        run_through_approval(gate_out, technician, manager)

        assert approved_events(tenant).count() == 0

    def test_a_pass_with_no_project_never_qualifies(
        self, tenant, yard, technician, site, job, manager
    ):
        tenant.settings.project_release_notify_above = Decimal("1.00")
        tenant.settings.save()
        job.project = None
        job.save()

        gate_out = build_pass(
            tenant, yard, technician, site, job, unit_cost=Decimal("900000.00")
        )
        run_through_approval(gate_out, technician, manager)

        assert approved_events(tenant).count() == 0

    def test_an_unpriced_pass_is_not_reported_as_a_cheap_one(
        self, tenant, yard, technician, site, job, manager
    ):
        """Returning None rather than zero: unvalued is not the same as small,
        and reporting it as small is the mistake that hides the expensive one."""
        tenant.settings.project_release_notify_above = Decimal("1.00")
        tenant.settings.save()

        gate_out = build_pass(tenant, yard, technician, site, job, unit_cost=None)
        run_through_approval(gate_out, technician, manager)

        assert approved_events(tenant).count() == 0


@pytest.mark.django_db
class TestItBlocksNothing:
    def test_the_pass_is_approved_either_way(
        self, tenant, yard, technician, site, job, manager
    ):
        tenant.settings.project_release_notify_above = Decimal("1.00")
        tenant.settings.save()

        gate_out = build_pass(
            tenant, yard, technician, site, job, unit_cost=Decimal("900.00")
        )
        gate_out = run_through_approval(gate_out, technician, manager)

        assert gate_out.status == GateOutStatus.APPROVED
        assert gate_out.approved_at is not None

    def test_a_broken_valuation_does_not_refuse_the_approval(
        self, tenant, yard, technician, site, job, manager, monkeypatch
    ):
        """A courtesy message must never be able to stop a van."""
        tenant.settings.project_release_notify_above = Decimal("1.00")
        tenant.settings.save()

        from dispatch import services

        def explode(gate_out):
            raise RuntimeError("pricing is down")

        monkeypatch.setattr(services, "_requested_value", explode)

        gate_out = build_pass(
            tenant, yard, technician, site, job, unit_cost=Decimal("900.00")
        )
        gate_out = run_through_approval(gate_out, technician, manager)

        assert gate_out.status == GateOutStatus.APPROVED
        assert approved_events(tenant).count() == 0


@pytest.mark.django_db
class TestSelfApprovalIsFlaggedInThePayload:
    def test_it_says_so(self, tenant, yard, site, job, manager):
        """O6 permits it; the owner's message should not have to infer it."""
        tenant.settings.project_release_notify_above = Decimal("1.00")
        tenant.settings.save()

        gate_out = build_pass(
            tenant, yard, manager, site, job, unit_cost=Decimal("900.00")
        )
        run_through_approval(gate_out, manager, manager)

        assert approved_events(tenant).first().payload["self_approved"] is True
