"""T10.10 — naming the job a gate pass is for (§4.7, §5.4; O5).

The job is an **attribution**, not a destination. F1's exactly-one-destination
constraint is untouched — a pass still goes to one place — and this says which
job it is for, so the material can be costed to a project.

The checks here exist because a wrong attribution is close to invisible. It does
not stop the material leaving; it shows up months later as a margin nobody can
explain on a PO nobody remembers.
"""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from accounts.factories import UserFactory
from dispatch.models import GateOut, GateOutPurpose
from jobs.models import Job, JobStatus
from locations.factories import YardFactory
from network.factories import ProjectFactory, SiteFactory


@pytest.fixture
def yard(tenant):
    return YardFactory(name="Main yard")


@pytest.fixture
def technician(tenant):
    return UserFactory(organization=tenant, full_name="Tom Technician")


@pytest.fixture
def site(tenant):
    return SiteFactory(internal_ref="SLV-3001", name="Kileleshwa")


@pytest.fixture
def po_project(tenant):
    return ProjectFactory(
        reference="WO-7001",
        po_number="PO-700",
        manager=UserFactory(organization=tenant),
        contract_value=Decimal("100000.00"),
        cost_budget=Decimal("80000.00"),
    )


@pytest.fixture
def job(tenant, site, technician, po_project):
    return Job.objects.create(
        organization=tenant,
        reference="JOB-A01",
        client=site.client,
        site=site,
        project=po_project,
        assignee=technician,
    )


def pass_to_site(tenant, yard, technician, site, **kwargs):
    return GateOut(
        organization=tenant,
        purpose_type=GateOutPurpose.INSTALLATION,
        from_location=yard,
        site=site,
        custody_holder=technician,
        requested_by=technician,
        **kwargs,
    )


@pytest.mark.django_db
class TestAttribution:
    def test_a_pass_may_name_a_job_at_its_destination_site(
        self, tenant, yard, technician, site, job, po_project
    ):
        gate_out = pass_to_site(tenant, yard, technician, site, job=job)
        gate_out.save()

        assert gate_out.project_attribution == po_project

    def test_a_pass_with_no_job_is_not_project_material(
        self, tenant, yard, technician, site
    ):
        """O5: behaves exactly as it did before Epic O."""
        gate_out = pass_to_site(tenant, yard, technician, site)
        gate_out.save()

        assert gate_out.project_attribution is None

    def test_a_job_at_another_site_is_refused(
        self, tenant, yard, technician, site, job
    ):
        elsewhere = SiteFactory(internal_ref="SLV-3002", name="Karen")
        gate_out = pass_to_site(tenant, yard, technician, elsewhere, job=job)

        with pytest.raises(ValidationError, match="different site"):
            gate_out.save()

    def test_a_closed_job_is_refused(self, tenant, yard, technician, site, job):
        """O3: its delivery cost is settled; nothing more can be issued to it."""
        job.status = JobStatus.CLOSED
        job.closed_at = timezone.now()
        job.save()

        gate_out = pass_to_site(tenant, yard, technician, site, job=job)
        with pytest.raises(ValidationError, match="settled"):
            gate_out.save()

    def test_a_job_on_a_closed_project_is_refused(
        self, tenant, yard, technician, site, job, po_project
    ):
        po_project.status = "CLOSED"
        po_project.closed_at = timezone.now()
        po_project.save()

        gate_out = pass_to_site(tenant, yard, technician, site, job=job)
        with pytest.raises(ValidationError, match="figures are final"):
            gate_out.save()

    def test_a_job_on_an_unpriced_project_attributes_to_it(
        self, tenant, yard, technician, site, job
    ):
        """A project without a PO is the old work order; it still groups work."""
        job.project = ProjectFactory(reference="WO-7002")
        job.save()

        gate_out = pass_to_site(tenant, yard, technician, site, job=job)
        gate_out.save()

        assert gate_out.project_attribution == job.project

    def test_a_pass_addressed_to_a_project_attributes_to_it(
        self, tenant, yard, technician, po_project
    ):
        """Both routes resolve the same way, so routing and costing agree."""
        gate_out = GateOut(
            organization=tenant,
            purpose_type=GateOutPurpose.INSTALLATION,
            from_location=yard,
            project=po_project,
            custody_holder=technician,
            requested_by=technician,
        )
        gate_out.save()

        assert gate_out.project_attribution == po_project
