"""T10.14 — the manager accepting what a closeout cost (§4.9; O8).

The whole point of this task is the thing it does **not** do. Stock moves when
the storekeeper confirms, whatever the manager thinks about the bill. A ledger
that waited for a financial signature would stop being a record of what
happened, and the yard's figures would silently lag the yard by however long a
PM took to look at their phone.

So a query here is a request for a corrected closeout, not a retraction. Posted
material stays posted; a genuine error is undone by a reversal, which leaves
both the mistake and the correction visible.
"""

from decimal import Decimal

import pytest
from django.db import transaction

from accounts.factories import UserFactory
from catalogue.factories import ItemTypeFactory
from catalogue.models import TrackingMode
from core.exceptions import DomainError
from jobs.models import (
    CloseoutAction,
    CostAcceptance,
    Job,
    JobCloseout,
    JobCloseoutLine,
)
from jobs.services import accept_closeout_cost, submit_closeout
from locations.nodes import external_node, node_for_user
from network.factories import ProjectFactory, SiteFactory
from stock.models import MovementType, StockMovement
from stock.services import MovementRequest, post_movement


@pytest.fixture
def manager(tenant):
    return UserFactory(organization=tenant, full_name="Pippa Manager")


@pytest.fixture
def technician(tenant):
    return UserFactory(organization=tenant, full_name="Tom Technician")


@pytest.fixture
def po_project(tenant, manager):
    return ProjectFactory(
        reference="WO-9501",
        po_number="PO-950",
        manager=manager,
        contract_value=Decimal("100000.00"),
        cost_budget=Decimal("80000.00"),
    )


@pytest.fixture
def job(tenant, technician, po_project):
    site = SiteFactory(internal_ref="SLV-7001", name="Kileleshwa")
    return Job.objects.create(
        organization=tenant,
        reference="JOB-C01",
        client=site.client,
        site=site,
        project=po_project,
        assignee=technician,
    )


def submitted_closeout(tenant, job):
    closeout = JobCloseout.objects.create(
        organization=tenant, job=job, submitted_by=job.assignee
    )
    item = ItemTypeFactory(default_tracking_mode=TrackingMode.BULK)
    with transaction.atomic():
        post_movement(
            MovementRequest(
                item_type=item,
                quantity=Decimal("10"),
                from_node=external_node(tenant.pk),
                to_node=node_for_user(job.assignee),
                movement_type=MovementType.RECEIPT,
            )
        )
    JobCloseoutLine.objects.create(
        organization=tenant,
        closeout=closeout,
        action=CloseoutAction.CONSUMED,
        item_type=item,
        quantity=Decimal("3"),
        uom=item.uom,
    )
    submit_closeout(closeout, submitted_by=job.assignee)
    closeout.refresh_from_db()
    return closeout


@pytest.mark.django_db
class TestTheLedgerDoesNotWait:
    def test_material_posts_before_the_manager_has_seen_it(self, tenant, job):
        closeout = submitted_closeout(tenant, job)

        assert closeout.cost_acceptance == CostAcceptance.PENDING
        assert StockMovement.objects.filter(movement_type=MovementType.CONSUME).exists()

    def test_querying_retracts_nothing(self, tenant, job, manager):
        """A query asks for a corrected closeout, not for the record back."""
        closeout = submitted_closeout(tenant, job)
        posted = StockMovement.objects.filter(movement_type=MovementType.CONSUME).count()

        accept_closeout_cost(
            closeout, actor=manager, accepted=False, reason="Three drums, not four."
        )
        closeout.refresh_from_db()

        assert closeout.cost_acceptance == CostAcceptance.QUERIED
        assert (
            StockMovement.objects.filter(movement_type=MovementType.CONSUME).count()
            == posted
        )


@pytest.mark.django_db
class TestWhoDecides:
    def test_the_manager_may_accept(self, tenant, job, manager):
        closeout = submitted_closeout(tenant, job)
        accept_closeout_cost(closeout, actor=manager, accepted=True)
        closeout.refresh_from_db()

        assert closeout.cost_acceptance == CostAcceptance.ACCEPTED
        assert closeout.cost_decided_by == manager
        assert closeout.cost_decided_at is not None

    def test_nobody_else_may(self, tenant, job, technician):
        closeout = submitted_closeout(tenant, job)

        with pytest.raises(DomainError, match="Only the manager"):
            accept_closeout_cost(closeout, actor=technician, accepted=True)

    def test_a_query_needs_a_reason(self, tenant, job, manager):
        closeout = submitted_closeout(tenant, job)

        with pytest.raises(DomainError, match="needs a reason"):
            accept_closeout_cost(closeout, actor=manager, accepted=False)

    def test_it_cannot_be_decided_twice(self, tenant, job, manager):
        closeout = submitted_closeout(tenant, job)
        accept_closeout_cost(closeout, actor=manager, accepted=True)
        closeout.refresh_from_db()

        with pytest.raises(DomainError, match="not waiting"):
            accept_closeout_cost(closeout, actor=manager, accepted=True)


@pytest.mark.django_db
class TestWhenNobodyIsBudgeting:
    def test_a_job_with_no_project_needs_no_acceptance(self, tenant, job):
        """Inventing a step for work nobody budgets is friction with nothing
        behind it."""
        job.project = None
        job.save()
        closeout = submitted_closeout(tenant, job)

        assert closeout.cost_acceptance == CostAcceptance.NOT_REQUIRED

    def test_an_unpriced_project_needs_no_acceptance(self, tenant, job):
        job.project = ProjectFactory(reference="WO-9502")
        job.save()
        closeout = submitted_closeout(tenant, job)

        assert closeout.cost_acceptance == CostAcceptance.NOT_REQUIRED
