"""T10.5 — how a job is delivered, and what that costs (§4.14; O3, O4).

O3 puts delivery on the **job**, not the project, because a PO awarded in bulk
is allocated afterwards — by region, by capacity — so one PO routinely has both
in-house and subcontracted work under it.

The two constraints here exist for the same reason: a cost that is not recorded
is a cost the project does not carry, and a project that carries less than it
should looks more profitable than it is.
"""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.factories import UserFactory
from jobs.models import DeliveryMode, Job, JobStatus
from network.factories import SiteFactory
from network.models import Subcontractor


@pytest.fixture
def contractor(tenant):
    return Subcontractor.objects.create(organization=tenant, name="Rigging Co")


@pytest.fixture
def job(tenant):
    site = SiteFactory(internal_ref="SLV-2001", name="Kileleshwa")
    return Job.objects.create(
        organization=tenant,
        reference="JOB-D01",
        client=site.client,
        site=site,
        assignee=UserFactory(organization=tenant),
    )


@pytest.mark.django_db
class TestDeliveryMode:
    def test_a_job_is_delivered_in_house_by_default(self, tenant, job):
        assert job.delivery_mode == DeliveryMode.IN_HOUSE
        assert job.subcontractor is None
        assert job.agreed_price is None

    def test_a_subcontracted_job_names_a_contractor_and_a_price(
        self, tenant, job, contractor
    ):
        job.delivery_mode = DeliveryMode.SUBCONTRACTED
        job.subcontractor = contractor
        job.agreed_price = Decimal("45000.00")
        job.save()

        job.refresh_from_db()
        assert job.subcontractor == contractor
        assert job.agreed_price == Decimal("45000.00")

    def test_subcontracted_without_a_price_is_refused(self, tenant, job, contractor):
        """A contractor with no price contributes nothing and flatters the PO."""
        job.delivery_mode = DeliveryMode.SUBCONTRACTED
        job.subcontractor = contractor

        with pytest.raises(IntegrityError), transaction.atomic():
            job.save()

    def test_subcontracted_without_a_contractor_is_refused(self, tenant, job):
        """A price with no party cannot be rolled up by contractor."""
        job.delivery_mode = DeliveryMode.SUBCONTRACTED
        job.agreed_price = Decimal("1.00")

        with pytest.raises(IntegrityError), transaction.atomic():
            job.save()

    def test_in_house_may_not_carry_a_contractor(self, tenant, job, contractor):
        job.subcontractor = contractor

        with pytest.raises(IntegrityError), transaction.atomic():
            job.save()

    def test_a_referenced_contractor_cannot_be_deleted(self, tenant, job, contractor):
        """O4: last year's cost still names them."""
        job.delivery_mode = DeliveryMode.SUBCONTRACTED
        job.subcontractor = contractor
        job.agreed_price = Decimal("1.00")
        job.save()

        from django.db.models import ProtectedError

        with pytest.raises(ProtectedError), transaction.atomic():
            contractor.delete()


@pytest.mark.django_db
class TestAClosedJobsCostIsSettled:
    def close(self, job):
        job.status = JobStatus.CLOSED
        job.closed_at = timezone.now()
        job.save()
        job.refresh_from_db()
        return job

    def test_the_price_cannot_be_changed_after_closing(self, tenant, job, contractor):
        job.delivery_mode = DeliveryMode.SUBCONTRACTED
        job.subcontractor = contractor
        job.agreed_price = Decimal("45000.00")
        job.save()
        job = self.close(job)

        job.agreed_price = Decimal("90000.00")
        with pytest.raises(ValidationError, match="already been counted"):
            job.save()

    def test_the_mode_cannot_be_changed_after_closing(self, tenant, job):
        job = self.close(job)
        job.delivery_mode = DeliveryMode.SUBCONTRACTED

        with pytest.raises(ValidationError, match="already been counted"):
            job.save()

    def test_a_closed_job_may_still_be_saved_for_other_reasons(self, tenant, job):
        """The guard is about delivery cost, not about the row being frozen."""
        job = self.close(job)
        job.description = "Corrected description"
        job.save()

        job.refresh_from_db()
        assert job.description == "Corrected description"

    def test_an_open_job_may_change_freely(self, tenant, job, contractor):
        job.delivery_mode = DeliveryMode.SUBCONTRACTED
        job.subcontractor = contractor
        job.agreed_price = Decimal("1.00")
        job.save()

        job.agreed_price = Decimal("2.00")
        job.save()

        job.refresh_from_db()
        assert job.agreed_price == Decimal("2.00")
