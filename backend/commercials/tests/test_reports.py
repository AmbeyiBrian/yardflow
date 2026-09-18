"""T10.19 — the project reports (§10; O12, O15, O6).

Run through the real machinery rather than by calling ``rows`` directly, because
the point of §10's one-definition-per-report design is that the screen, the
spreadsheet and the PDF cannot disagree. A test that bypassed it would prove
nothing about the thing people will actually open.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.db import transaction
from django.utils import timezone

from accounts.factories import UserFactory
from catalogue.factories import ItemTypeFactory
from catalogue.models import TrackingMode
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
from reporting.framework import all_reports, get_report
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
        reference="WO-9401",
        po_number="PO-940",
        manager=manager,
        contract_value=Decimal("500000.00"),
        cost_budget=Decimal("300000.00"),
    )


def delivered_job(tenant, project, technician, reference, ref_no, *, days="2.0"):
    site = SiteFactory(internal_ref=ref_no, name="Kileleshwa")
    job = Job.objects.create(
        organization=tenant,
        reference=reference,
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
    if days is not None:
        JobLabour.objects.create(
            organization=tenant,
            job=job,
            closeout=closeout,
            person=technician,
            work_date=date(2026, 6, 1),
            days=Decimal(days),
        )
    submit_closeout(closeout, submitted_by=technician)
    return job


def run(slug, params=None):
    report = get_report(slug)
    return report, list(report.rows(params or {}))


@pytest.mark.django_db
class TestTheyAreRegistered:
    def test_all_four_appear_in_the_catalogue(self, tenant):
        slugs = {report.slug for report in all_reports()}

        assert {
            "project-performance",
            "projects-ranked",
            "self-approved-releases",
            "labour-gaps",
        } <= slugs

    def test_they_require_project_cost_not_general_reporting(self, tenant):
        """A report is another way to read the same figures, and a poor place
        to lose the restriction §10 puts everywhere else."""
        from accounts.permissions_registry import PERM

        for slug in ("project-performance", "projects-ranked", "labour-gaps"):
            assert get_report(slug).required_permission == PERM.PROJECT_VIEW_COST


@pytest.mark.django_db
class TestProjectPerformance:
    def test_it_reports_the_costed_figures(self, tenant, project, technician):
        delivered_job(tenant, project, technician, "JOB-R01", "SLV-9001")

        _, rows = run("project-performance")
        row = next(row for row in rows if row["reference"] == "PO-940")

        assert row["material"] == Decimal("6000.00")
        assert row["labour"] == Decimal("4000.00")
        assert row["cost_to_date"] == Decimal("10000.00")
        assert row["margin"] == Decimal("490000.00")

    def test_a_project_without_a_po_is_not_listed(self, tenant, technician):
        """O1: an unpriced project is the old work order and has no margin."""
        ProjectFactory(reference="WO-9402")

        _, rows = run("project-performance")

        assert all(row["reference"] != "WO-9402" for row in rows)

    def test_the_over_budget_flag_is_set_not_enforced(
        self, tenant, project, technician
    ):
        project.cost_budget = Decimal("100.00")
        project.save()
        delivered_job(tenant, project, technician, "JOB-R02", "SLV-9002")

        _, rows = run("project-performance")
        row = next(row for row in rows if row["reference"] == "PO-940")

        assert row["is_over_budget"] is True

    def test_it_exports_through_the_shared_machinery(
        self, tenant, project, technician
    ):
        """M2: one definition, so the file cannot disagree with the screen."""
        from reporting.exports import to_excel

        delivered_job(tenant, project, technician, "JOB-R03", "SLV-9003")
        report = get_report("project-performance")

        content = to_excel(report, {})

        # A real xlsx is a zip, and a zip starts PK.
        assert content[:2] == b"PK"


@pytest.mark.django_db
class TestProjectsRanked:
    def test_worst_margin_first(self, tenant, manager, technician):
        good = ProjectFactory(
            reference="WO-9410",
            po_number="PO-GOOD",
            manager=manager,
            contract_value=Decimal("900000.00"),
            cost_budget=Decimal("10000.00"),
        )
        poor = ProjectFactory(
            reference="WO-9411",
            po_number="PO-POOR",
            manager=manager,
            contract_value=Decimal("10000.00"),
            cost_budget=Decimal("10000.00"),
        )
        delivered_job(tenant, poor, technician, "JOB-R04", "SLV-9004")
        delivered_job(tenant, good, technician, "JOB-R05", "SLV-9005")

        _, rows = run("projects-ranked")
        references = [row["reference"] for row in rows]

        assert references.index("PO-POOR") < references.index("PO-GOOD")


@pytest.mark.django_db
class TestLabourGaps:
    def test_a_closed_job_with_no_days_is_named(self, tenant, project, technician):
        job = delivered_job(
            tenant, project, technician, "JOB-R06", "SLV-9006", days=None
        )
        job.status = JobStatus.CLOSED
        job.closed_at = timezone.now()
        job.save()

        _, rows = run("labour-gaps")

        assert any(row["finding"] == "Closed with no days recorded" for row in rows)

    def test_an_uncosted_entry_is_named(self, tenant, project):
        casual = UserFactory(organization=tenant, full_name="Cas Casual", day_rate=None)
        delivered_job(tenant, project, casual, "JOB-R07", "SLV-9007")

        _, rows = run("labour-gaps")

        assert any(row["finding"] == "No rate — uncosted" for row in rows)

    def test_a_subcontracted_job_is_not_expected_to_have_days(
        self, tenant, project, technician
    ):
        """Its cost is the agreed price; days would be double-counting."""
        job = delivered_job(
            tenant, project, technician, "JOB-R08", "SLV-9008", days=None
        )
        job.delivery_mode = DeliveryMode.SUBCONTRACTED
        job.subcontractor = Subcontractor.objects.create(
            organization=tenant, name="Rigging Co"
        )
        job.agreed_price = Decimal("1000.00")
        job.save()
        job.status = JobStatus.CLOSED
        job.closed_at = timezone.now()
        job.save()

        _, rows = run("labour-gaps")

        assert not any(row["job_reference"] == "JOB-R08" for row in rows)


@pytest.mark.django_db
class TestSelfApprovedReleases:
    def test_it_is_empty_when_nobody_has(self, tenant):
        _, rows = run("self-approved-releases")

        assert rows == []
