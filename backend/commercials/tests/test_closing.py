"""T10.20 — closing a project and freezing what it said (§4.14; O13).

Two facts sit side by side here and are easy to mistake for a contradiction:

* the **ledger stays live** after a project closes, because a reversal posted
  next month is a true correction and must land;
* the **reported figures do not**, because a statement that rewrites itself is
  not a statement.

The test that matters is the one posting a reversal after close and asserting
the reported margin has not moved.
"""

from decimal import Decimal

import pytest
from django.db import transaction

from accounts.factories import UserFactory
from catalogue.factories import ItemTypeFactory
from catalogue.models import TrackingMode
from commercials.closing import (
    NotTheProjectManager,
    ProjectNotClosable,
    close_project,
    reopen_project,
)
from commercials.costing import cost_for
from commercials.models import ProjectSnapshot
from jobs.models import (
    CloseoutAction,
    Job,
    JobCloseout,
    JobCloseoutLine,
    JobStatus,
)
from jobs.services import submit_closeout
from locations.nodes import external_node, node_for_user
from network.factories import ProjectFactory, SiteFactory
from network.models import ProjectStatus
from stock.models import MovementType
from stock.services import MovementRequest, post_movement


@pytest.fixture
def manager(tenant):
    return UserFactory(organization=tenant, full_name="Pippa Manager")


@pytest.fixture
def owner(tenant):
    return UserFactory(organization=tenant, full_name="Olive Owner")


@pytest.fixture
def technician(tenant):
    return UserFactory(organization=tenant, full_name="Tom Technician")


@pytest.fixture
def project(tenant, manager):
    return ProjectFactory(
        reference="WO-9301",
        po_number="PO-930",
        manager=manager,
        contract_value=Decimal("500000.00"),
        cost_budget=Decimal("300000.00"),
    )


def delivered_and_closed_job(tenant, project, technician, ref_no="SLV-9301"):
    site = SiteFactory(internal_ref=ref_no, name="Kileleshwa")
    job = Job.objects.create(
        organization=tenant,
        reference=f"JOB-{ref_no}",
        client=site.client,
        site=site,
        project=project,
        assignee=technician,
    )
    item = ItemTypeFactory(
        default_tracking_mode=TrackingMode.BULK, unit_cost=Decimal("1500.00")
    )
    with transaction.atomic():
        post_movement(
            MovementRequest(
                item_type=item,
                quantity=Decimal("10"),
                from_node=external_node(tenant.pk),
                to_node=node_for_user(technician),
                movement_type=MovementType.RECEIPT,
            )
        )
    closeout = JobCloseout.objects.create(
        organization=tenant, job=job, submitted_by=technician
    )
    JobCloseoutLine.objects.create(
        organization=tenant,
        closeout=closeout,
        action=CloseoutAction.CONSUMED,
        item_type=item,
        quantity=Decimal("4"),
        uom=item.uom,
    )
    submit_closeout(closeout, submitted_by=technician)

    from django.utils import timezone

    job.status = JobStatus.CLOSED
    job.closed_at = timezone.now()
    job.save()
    return job, item


@pytest.mark.django_db
class TestClosing:
    def test_a_clean_project_closes_without_a_reason(self, tenant, project, manager):
        closed, _ = close_project(project, actor=manager)

        assert closed.status == ProjectStatus.CLOSED
        assert closed.closed_at is not None

    def test_open_jobs_need_a_reason(self, tenant, project, manager, technician):
        site = SiteFactory(internal_ref="SLV-9302", name="Karen")
        Job.objects.create(
            organization=tenant,
            reference="JOB-OPEN",
            client=site.client,
            site=site,
            project=project,
            assignee=technician,
        )

        with pytest.raises(ProjectNotClosable, match="needs a reason"):
            close_project(project, actor=manager)

    def test_with_a_reason_it_closes_anyway(self, tenant, project, manager, technician):
        """C7: a warning, not a block. Blocking is H5's rule, for jobs."""
        site = SiteFactory(internal_ref="SLV-9303", name="Karen")
        Job.objects.create(
            organization=tenant,
            reference="JOB-OPEN2",
            client=site.client,
            site=site,
            project=project,
            assignee=technician,
        )

        closed, _ = close_project(
            project, actor=manager, reason="Operator cancelled the remaining sites."
        )

        assert closed.status == ProjectStatus.CLOSED
        assert closed.close_reason.startswith("Operator cancelled")

    def test_only_the_manager_closes_it(self, tenant, project, owner):
        with pytest.raises(NotTheProjectManager):
            close_project(project, actor=owner)

    def test_it_cannot_be_closed_twice(self, tenant, project, manager):
        close_project(project, actor=manager)
        project.refresh_from_db()

        with pytest.raises(ProjectNotClosable, match="already"):
            close_project(project, actor=manager)


@pytest.mark.django_db
class TestTheSnapshot:
    def test_closing_writes_one(self, tenant, project, manager, technician):
        delivered_and_closed_job(tenant, project, technician)
        # The technician still holds the balance of the material, so the close
        # is genuinely unreconciled and needs a reason (C7, O13).
        close_project(project, actor=manager, reason="Balance written off.")

        snapshot = ProjectSnapshot.objects.get(project=project)
        assert snapshot.figures["material"] == "6000.00"
        assert snapshot.figures["margin"] == "494000.00"
        assert snapshot.taken_by == manager

    def test_the_reported_figures_stop_moving(
        self, tenant, project, manager, technician
    ):
        """The point of the whole task.

        The reversal below is a genuine correction and the ledger takes it. What
        must not happen is the closed project quietly reporting a different
        margin than the one it was closed on.
        """
        _, item = delivered_and_closed_job(tenant, project, technician)
        close_project(project, actor=manager, reason="Balance written off.")
        project.refresh_from_db()

        reported = cost_for(project)
        assert reported.material == Decimal("6000.00")

        # A month later, somebody reverses part of that consumption.
        from stock.models import StockMovement

        original = StockMovement.objects.filter(
            movement_type=MovementType.CONSUME
        ).first()
        with transaction.atomic():
            post_movement(
                MovementRequest(
                    item_type=item,
                    quantity=original.quantity,
                    from_node=original.to_node,
                    to_node=original.from_node,
                    movement_type=MovementType.REVERSAL,
                    reversal_of=original,
                )
            )

        assert cost_for(project).material == Decimal("6000.00")

    def test_an_open_project_still_recomputes(self, tenant, project, technician):
        """Only a closed one is frozen — a live project must stay live."""
        delivered_and_closed_job(tenant, project, technician)

        assert cost_for(project).material == Decimal("6000.00")
        assert not ProjectSnapshot.objects.filter(project=project).exists()


@pytest.mark.django_db
class TestReopening:
    def test_it_needs_a_reason(self, tenant, project, manager, owner):
        close_project(project, actor=manager)
        project.refresh_from_db()

        with pytest.raises(ProjectNotClosable, match="needs a reason"):
            reopen_project(project, actor=owner, reason="")

    def test_reopening_makes_the_figures_live_again(
        self, tenant, project, manager, owner, technician
    ):
        delivered_and_closed_job(tenant, project, technician)
        close_project(project, actor=manager, reason="Balance written off.")
        project.refresh_from_db()

        reopen_project(project, actor=owner, reason="Operator added two sites.")
        project.refresh_from_db()

        assert project.status == ProjectStatus.OPEN
        assert cost_for(project).material == Decimal("6000.00")

    def test_the_old_snapshot_is_kept(self, tenant, project, manager, owner):
        """What it reported each time it closed is worth more than a tidy row."""
        close_project(project, actor=manager)
        project.refresh_from_db()
        reopen_project(project, actor=owner, reason="More scope.")
        project.refresh_from_db()
        close_project(project, actor=manager)

        assert ProjectSnapshot.objects.filter(project=project).count() == 2
