"""T10.13 — costing a person's time from the closeout (§4.9, §4.14; O15, D26).

R1 in the requirements is the risk this carries: labour cost rests on a field
somebody fills in, and D31 lets a storekeeper fill it in on a technician's
behalf. None of that can be fixed here. What can be done is refusing to turn a
missing figure into a flattering one — hence `rate_source = NONE` rather than a
zero rate, and hence the over-a-day flag rather than silence.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from accounts.factories import RoleFactory, UserFactory
from catalogue.factories import ItemTypeFactory
from catalogue.models import TrackingMode
from jobs.models import (
    CloseoutAction,
    Job,
    JobCloseout,
    JobCloseoutLine,
    JobLabour,
    RateSource,
)
from jobs.services import submit_closeout
from locations.nodes import external_node, node_for_user
from network.factories import SiteFactory
from stock.models import MovementType
from stock.services import MovementRequest, post_movement


@pytest.fixture
def technician(tenant):
    return UserFactory(
        organization=tenant, full_name="Tom Technician", day_rate=Decimal("2000.00")
    )


@pytest.fixture
def job(tenant, technician):
    site = SiteFactory(internal_ref="SLV-6001", name="Kileleshwa")
    return Job.objects.create(
        organization=tenant,
        reference="JOB-L01",
        client=site.client,
        site=site,
        assignee=technician,
    )


def give_to(tenant, holder):
    """Put a unit of stock in someone's custody, so a CONSUME line can post."""
    from django.db import transaction

    item = ItemTypeFactory(default_tracking_mode=TrackingMode.BULK)
    with transaction.atomic():
        post_movement(
            MovementRequest(
                item_type=item,
                quantity=Decimal("10"),
                from_node=external_node(tenant.pk),
                to_node=node_for_user(holder),
                movement_type=MovementType.RECEIPT,
            )
        )
    return item


def closeout_with_labour(tenant, job, person, *, days, work_date=date(2026, 4, 1)):
    closeout = JobCloseout.objects.create(
        organization=tenant, job=job, submitted_by=person
    )
    item = give_to(tenant, job.assignee)
    JobCloseoutLine.objects.create(
        organization=tenant,
        closeout=closeout,
        action=CloseoutAction.CONSUMED,
        item_type=item,
        quantity=Decimal("1"),
        uom=item.uom,
    )
    JobLabour.objects.create(
        organization=tenant,
        job=job,
        closeout=closeout,
        person=person,
        work_date=work_date,
        days=Decimal(str(days)),
    )
    return closeout


@pytest.mark.django_db
class TestTheRateIsCaptured:
    def test_submitting_captures_the_persons_own_rate(self, tenant, job, technician):
        closeout = closeout_with_labour(tenant, job, technician, days="1.0")
        submit_closeout(closeout, submitted_by=technician)

        entry = JobLabour.objects.get(closeout=closeout)
        assert entry.day_rate == Decimal("2000.00")
        assert entry.rate_source == RateSource.USER
        assert entry.cost == Decimal("2000.00")

    def test_a_later_rate_change_does_not_move_it(self, tenant, job, technician):
        """D27: the same rule the ledger's unit_cost follows."""
        closeout = closeout_with_labour(tenant, job, technician, days="1.0")
        submit_closeout(closeout, submitted_by=technician)

        technician.day_rate = Decimal("9999.00")
        technician.save()

        entry = JobLabour.objects.get(closeout=closeout)
        assert entry.day_rate == Decimal("2000.00")

    def test_a_person_with_no_rate_is_uncosted_not_free(self, tenant, job):
        """O15: a job costing nothing to deliver reads as delivered for free."""
        casual = UserFactory(organization=tenant, full_name="Cas Casual", day_rate=None)
        closeout = closeout_with_labour(tenant, job, casual, days="1.0")
        submit_closeout(closeout, submitted_by=casual)

        entry = JobLabour.objects.get(closeout=closeout)
        assert entry.day_rate is None
        assert entry.rate_source == RateSource.NONE
        assert entry.cost is None

    def test_the_role_rate_is_used_when_the_person_has_none(self, tenant, job):
        person = UserFactory(organization=tenant, day_rate=None)
        person.user_roles.create(
            organization=tenant, role=RoleFactory(day_rate=Decimal("1500.00"))
        )
        closeout = closeout_with_labour(tenant, job, person, days="2.0")
        submit_closeout(closeout, submitted_by=person)

        entry = JobLabour.objects.get(closeout=closeout)
        assert entry.rate_source == RateSource.ROLE
        assert entry.cost == Decimal("3000.00")

    def test_half_days_work(self, tenant, job, technician):
        closeout = closeout_with_labour(tenant, job, technician, days="0.5")
        submit_closeout(closeout, submitted_by=technician)

        assert JobLabour.objects.get(closeout=closeout).cost == Decimal("1000.00")


@pytest.mark.django_db
class TestTheOverADayCheck:
    def second_job(self, tenant, technician):
        site = SiteFactory(internal_ref="SLV-6002", name="Karen")
        return Job.objects.create(
            organization=tenant,
            reference="JOB-L02",
            client=site.client,
            site=site,
            assignee=technician,
        )

    def test_one_day_across_two_jobs_is_not_flagged(self, tenant, job, technician):
        first = closeout_with_labour(tenant, job, technician, days="0.5")
        submit_closeout(first, submitted_by=technician)

        other = self.second_job(tenant, technician)
        second = closeout_with_labour(tenant, other, technician, days="0.5")
        submit_closeout(second, submitted_by=technician)

        assert JobLabour.objects.filter(overlaps_day=True).count() == 0

    def test_more_than_a_day_is_flagged_and_not_refused(self, tenant, job, technician):
        """O15: a refusal would block a late closeout because of an earlier one."""
        first = closeout_with_labour(tenant, job, technician, days="1.0")
        submit_closeout(first, submitted_by=technician)

        other = self.second_job(tenant, technician)
        second = closeout_with_labour(tenant, other, technician, days="1.0")
        submit_closeout(second, submitted_by=technician)

        assert JobLabour.objects.get(closeout=second).overlaps_day is True
        assert JobLabour.objects.get(closeout=second).cost == Decimal("2000.00")

    def test_different_dates_do_not_interfere(self, tenant, job, technician):
        first = closeout_with_labour(
            tenant, job, technician, days="1.0", work_date=date(2026, 4, 1)
        )
        submit_closeout(first, submitted_by=technician)

        other = self.second_job(tenant, technician)
        second = closeout_with_labour(
            tenant, other, technician, days="1.0", work_date=date(2026, 4, 2)
        )
        submit_closeout(second, submitted_by=technician)

        assert JobLabour.objects.filter(overlaps_day=True).count() == 0


@pytest.mark.django_db
class TestConstraints:
    def test_one_entry_per_person_per_day_per_job(self, tenant, job, technician):
        JobLabour.objects.create(
            organization=tenant,
            job=job,
            person=technician,
            work_date=date(2026, 4, 1),
            days=Decimal("1.0"),
        )

        with pytest.raises(IntegrityError), transaction.atomic():
            JobLabour.objects.create(
                organization=tenant,
                job=job,
                person=technician,
                work_date=date(2026, 4, 1),
                days=Decimal("1.0"),
            )

    def test_a_closeout_with_no_labour_is_fine(self, tenant, job, technician):
        """A delivery dropped at a site has no days to report, and requiring
        them would produce invented ones."""
        closeout = JobCloseout.objects.create(
            organization=tenant, job=job, submitted_by=technician
        )
        item = give_to(tenant, job.assignee)
        JobCloseoutLine.objects.create(
            organization=tenant,
            closeout=closeout,
            action=CloseoutAction.CONSUMED,
            item_type=item,
            quantity=Decimal("1"),
            uom=item.uom,
        )

        submit_closeout(closeout, submitted_by=technician)

        assert JobLabour.objects.filter(closeout=closeout).count() == 0
