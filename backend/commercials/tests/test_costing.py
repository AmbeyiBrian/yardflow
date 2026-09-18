"""T10.17 — what a project cost, derived rather than typed (§4.14, §10; O11, D23).

Three properties are worth more than the arithmetic, and each has a test here:

1. **Repricing an item type does not move a posted figure** (D27). A margin that
   changes months after a PO closed is worth nothing in front of a client.
2. **A late return reduces the loss by itself.** Loss is a query over
   expectations still open on closed jobs, not a posted write-off, so kit that
   turns up is simply no longer missing — nobody has to find an entry and
   reverse it.
3. **Unvalued is not cheap.** A movement with no captured cost is counted as
   unvalued, so the report can say the figure is incomplete instead of stating
   a confident understatement.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.db import transaction
from django.utils import timezone

from accounts.factories import UserFactory
from catalogue.factories import ItemTypeFactory
from catalogue.models import TrackingMode
from commercials.costing import cost_for, performance_for
from commercials.models import ExpenseCategory, ProjectExpense
from commercials.services import decide_expense
from jobs.models import (
    CloseoutAction,
    DeliveryMode,
    Job,
    JobCloseout,
    JobCloseoutLine,
    JobLabour,
    JobStatus,
)
from jobs.services import submit_closeout
from locations.nodes import external_node, node_for_user
from network.factories import ProjectFactory, SiteFactory
from network.models import Subcontractor
from stock.models import MovementType
from stock.services import MovementRequest, post_movement


@pytest.fixture
def manager(tenant):
    return UserFactory(organization=tenant, full_name="Pippa Manager")


@pytest.fixture
def technician(tenant):
    return UserFactory(
        organization=tenant, full_name="Tom Technician", day_rate=Decimal("2000.00")
    )


@pytest.fixture
def project(tenant, manager):
    return ProjectFactory(
        reference="WO-9801",
        po_number="PO-980",
        manager=manager,
        contract_value=Decimal("500000.00"),
        cost_budget=Decimal("300000.00"),
    )


def make_job(tenant, project, technician, reference, ref_no="SLV-8001"):
    site = SiteFactory(internal_ref=ref_no, name="Kileleshwa")
    return Job.objects.create(
        organization=tenant,
        reference=reference,
        client=site.client,
        site=site,
        project=project,
        assignee=technician,
    )


def issue_to(tenant, holder, *, unit_cost, quantity="10"):
    item = ItemTypeFactory(
        default_tracking_mode=TrackingMode.BULK, unit_cost=unit_cost
    )
    with transaction.atomic():
        post_movement(
            MovementRequest(
                item_type=item,
                quantity=Decimal(quantity),
                from_node=external_node(tenant.pk),
                to_node=node_for_user(holder),
                movement_type=MovementType.RECEIPT,
            )
        )
    return item


def consume(tenant, job, item, quantity, *, days=None, work_date=date(2026, 5, 1)):
    closeout = JobCloseout.objects.create(
        organization=tenant, job=job, submitted_by=job.assignee
    )
    JobCloseoutLine.objects.create(
        organization=tenant,
        closeout=closeout,
        action=CloseoutAction.CONSUMED,
        item_type=item,
        quantity=Decimal(quantity),
        uom=item.uom,
    )
    if days is not None:
        JobLabour.objects.create(
            organization=tenant,
            job=job,
            closeout=closeout,
            person=job.assignee,
            work_date=work_date,
            days=Decimal(days),
        )
    submit_closeout(closeout, submitted_by=job.assignee)
    return closeout


@pytest.mark.django_db
class TestTheFourCostLines:
    def test_a_full_lifecycle_sums_to_the_hand_figure(
        self, tenant, project, technician, manager
    ):
        job = make_job(tenant, project, technician, "JOB-K01")
        item = issue_to(tenant, technician, unit_cost=Decimal("1500.00"))
        consume(tenant, job, item, "4", days="2.0")

        # A second job, subcontracted and closed.
        subbed = make_job(tenant, project, technician, "JOB-K02", ref_no="SLV-8002")
        subbed.delivery_mode = DeliveryMode.SUBCONTRACTED
        subbed.subcontractor = Subcontractor.objects.create(
            organization=tenant, name="Rigging Co"
        )
        subbed.agreed_price = Decimal("45000.00")
        subbed.save()
        subbed.status = JobStatus.CLOSED
        subbed.closed_at = timezone.now()
        subbed.save()

        expense = ProjectExpense.objects.create(
            organization=tenant,
            project=project,
            category=ExpenseCategory.objects.create(
                organization=tenant, name="Transport and fuel"
            ),
            amount=Decimal("4500.00"),
            incurred_on=date(2026, 5, 2),
            recorded_by=technician,
        )
        decide_expense(expense, actor=manager, approved=True)

        cost = cost_for(project)

        assert cost.material == Decimal("6000.00")  # 4 × 1500
        assert cost.labour == Decimal("4000.00")  # 2 × 2000
        assert cost.subcontractor == Decimal("45000.00")
        assert cost.expenses == Decimal("4500.00")
        assert cost.total == Decimal("59500.00")

    def test_an_unapproved_expense_counts_for_nothing(
        self, tenant, project, technician
    ):
        ProjectExpense.objects.create(
            organization=tenant,
            project=project,
            category=ExpenseCategory.objects.create(
                organization=tenant, name="Transport and fuel"
            ),
            amount=Decimal("9999.00"),
            incurred_on=date(2026, 5, 2),
            recorded_by=technician,
        )

        assert cost_for(project).expenses == Decimal("0.00")

    def test_an_open_subcontracted_job_counts_for_nothing(
        self, tenant, project, technician
    ):
        """O3: on closing, not on award — or mid-project margin means nothing."""
        job = make_job(tenant, project, technician, "JOB-K03")
        job.delivery_mode = DeliveryMode.SUBCONTRACTED
        job.subcontractor = Subcontractor.objects.create(
            organization=tenant, name="Rigging Co"
        )
        job.agreed_price = Decimal("45000.00")
        job.save()

        assert cost_for(project).subcontractor == Decimal("0.00")


@pytest.mark.django_db
class TestRepricingDoesNotRewriteHistory:
    def test_the_figure_holds_after_the_catalogue_changes(
        self, tenant, project, technician
    ):
        """D27, and the reason the valuation lives on the movement at all."""
        job = make_job(tenant, project, technician, "JOB-K04")
        item = issue_to(tenant, technician, unit_cost=Decimal("1500.00"))
        consume(tenant, job, item, "4")

        before = cost_for(project).material
        item.unit_cost = Decimal("99999.00")
        item.save()

        assert cost_for(project).material == before == Decimal("6000.00")


@pytest.mark.django_db
class TestUnvaluedIsNotCheap:
    def test_a_movement_with_no_price_is_counted_as_unvalued(
        self, tenant, project, technician
    ):
        job = make_job(tenant, project, technician, "JOB-K05")
        item = issue_to(tenant, technician, unit_cost=None)
        consume(tenant, job, item, "4")

        cost = cost_for(project)

        assert cost.material == Decimal("0.00")
        assert cost.unvalued_movements == 1
        assert cost.is_fully_valued is False

    def test_a_fully_priced_project_says_so(self, tenant, project, technician):
        job = make_job(tenant, project, technician, "JOB-K06")
        item = issue_to(tenant, technician, unit_cost=Decimal("1500.00"))
        consume(tenant, job, item, "4", days="1.0")

        assert cost_for(project).is_fully_valued is True

    def test_a_closed_in_house_job_with_no_days_is_named(
        self, tenant, project, technician
    ):
        """A job delivered for nothing is the flattery O12 exists to catch."""
        job = make_job(tenant, project, technician, "JOB-K07")
        item = issue_to(tenant, technician, unit_cost=Decimal("1500.00"))
        consume(tenant, job, item, "1")
        job.status = JobStatus.CLOSED
        job.closed_at = timezone.now()
        job.save()

        cost = cost_for(project)

        assert cost.jobs_closed_without_labour == 1
        assert cost.is_fully_valued is False


@pytest.mark.django_db
class TestPerformance:
    def test_margin_is_value_less_cost(self, tenant, project, technician):
        job = make_job(tenant, project, technician, "JOB-K08")
        item = issue_to(tenant, technician, unit_cost=Decimal("1500.00"))
        consume(tenant, job, item, "4")

        result = performance_for(project)

        assert result.contract_value == Decimal("500000.00")
        assert result.margin == Decimal("494000.00")
        assert result.is_over_budget is False

    def test_going_over_budget_is_reported_not_blocked(
        self, tenant, project, technician, manager
    ):
        """O12: flagged, never blocking. Nothing here refuses anything."""
        project.cost_budget = Decimal("1000.00")
        project.save()

        job = make_job(tenant, project, technician, "JOB-K09")
        item = issue_to(tenant, technician, unit_cost=Decimal("1500.00"))
        consume(tenant, job, item, "4")

        result = performance_for(project)

        assert result.is_over_budget is True
        assert result.budget_variance == Decimal("-5000.00")

    def test_progress_is_jobs_closed_over_jobs_opened(
        self, tenant, project, technician
    ):
        first = make_job(tenant, project, technician, "JOB-K10")
        make_job(tenant, project, technician, "JOB-K11", ref_no="SLV-8011")
        first.status = JobStatus.CLOSED
        first.closed_at = timezone.now()
        first.save()

        assert performance_for(project).progress_percent == Decimal("50.00")


@pytest.mark.django_db
class TestLossHealsItself:
    """The distinctive claim in §4.14, and the reason loss is a query.

    Loss is derived from expectations still open on **closed** jobs, not posted
    as a write-off. So kit that turns up two months later is simply no longer
    missing: the figure drops with nobody finding an entry and reversing it.
    """

    def expectation_for(self, tenant, project, technician, *, unit_cost, quantity="5"):
        from custody.models import CustodyExpectation, ExpectationStatus
        from dispatch.models import GateOut, GateOutLine, GateOutPurpose
        from locations.factories import YardFactory
        from locations.nodes import node_for_location

        job = make_job(tenant, project, technician, "JOB-LOSS", ref_no="SLV-8900")
        yard = YardFactory(name="Loss yard")
        item = ItemTypeFactory(
            default_tracking_mode=TrackingMode.BULK, unit_cost=unit_cost
        )
        with transaction.atomic():
            post_movement(
                MovementRequest(
                    item_type=item,
                    quantity=Decimal("20"),
                    from_node=external_node(tenant.pk),
                    to_node=node_for_location(yard),
                    movement_type=MovementType.RECEIPT,
                )
            )

        gate_out = GateOut(
            organization=tenant,
            purpose_type=GateOutPurpose.INSTALLATION,
            from_location=yard,
            site=job.site,
            job=job,
            custody_holder=technician,
            requested_by=technician,
        )
        gate_out.save()
        line = GateOutLine.objects.create(
            organization=tenant,
            gate_out=gate_out,
            item_type=item,
            tracking_mode=TrackingMode.BULK,
            requested_qty=Decimal(quantity),
            uom=item.uom,
        )
        # The ISSUE movement is what carries the captured valuation the loss
        # query reads (D27).
        with transaction.atomic():
            post_movement(
                MovementRequest(
                    item_type=item,
                    quantity=Decimal(quantity),
                    from_node=node_for_location(yard),
                    to_node=node_for_user(technician),
                    movement_type=MovementType.ISSUE,
                    document_type="dispatch.GateOut",
                    document_id=str(gate_out.pk),
                    document_line_id=str(line.pk),
                )
            )
        expectation = CustodyExpectation.objects.create(
            organization=tenant,
            holder=technician,
            gate_out_line=line,
            item_type=item,
            quantity=Decimal(quantity),
            status=ExpectationStatus.OPEN,
        )
        return job, expectation

    def close(self, job):
        job.status = JobStatus.CLOSED
        job.closed_at = timezone.now()
        job.save()

    def test_an_open_expectation_on_a_closed_job_is_a_loss(
        self, tenant, project, technician
    ):
        job, _ = self.expectation_for(
            tenant, project, technician, unit_cost=Decimal("800.00")
        )
        self.close(job)

        assert cost_for(project).material_loss == Decimal("4000.00")

    def test_while_the_job_is_open_it_is_exposure_not_cost(
        self, tenant, project, technician
    ):
        """O11: it becomes cost when a closeout says what happened to it."""
        self.expectation_for(tenant, project, technician, unit_cost=Decimal("800.00"))

        cost = cost_for(project)
        assert cost.material_loss == Decimal("0.00")
        assert cost.exposure == Decimal("4000.00")

    def test_a_late_return_reduces_the_loss_with_no_edit(
        self, tenant, project, technician
    ):
        job, expectation = self.expectation_for(
            tenant, project, technician, unit_cost=Decimal("800.00")
        )
        self.close(job)
        assert cost_for(project).material_loss == Decimal("4000.00")

        # Two months later, three of the five turn up and are booked in.
        expectation.returned_quantity = Decimal("3")
        expectation.save()

        assert cost_for(project).material_loss == Decimal("1600.00")

    def test_everything_coming_back_clears_it(self, tenant, project, technician):
        job, expectation = self.expectation_for(
            tenant, project, technician, unit_cost=Decimal("800.00")
        )
        self.close(job)

        expectation.returned_quantity = Decimal("5")
        expectation.save()

        assert cost_for(project).material_loss == Decimal("0.00")
