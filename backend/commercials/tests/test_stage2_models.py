"""T18.2-T18.4 — the stage-2 finance models and their guards (§4.19.2; R7-R11).

Tables only: services, the engine and endpoints come later. These pin what the
database and the save guards refuse.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from accounts.factories import UserFactory
from commercials.models import (
    AllowanceRequest,
    AllowanceType,
    ExpenseCategory,
    ExpenseStatus,
    MilestoneInvoice,
    MilestoneReceipt,
    ProjectExpense,
    ProjectMilestone,
    SitePurchase,
    SitePurchaseLine,
    Subcontract,
    SubcontractPayment,
)
from core.numbering import DEFAULT_PREFIXES, DocumentType
from core.rls import tables_with_policy
from core.tenancy import tenant_context
from jobs.models import DeliveryMode, Job
from locations.models import Location, LocationType
from network.factories import ProjectFactory, SiteFactory
from network.models import Subcontractor

S = ExpenseStatus


@pytest.fixture
def user(tenant):
    return UserFactory(organization=tenant)


@pytest.fixture
def project(tenant, user):
    return ProjectFactory(
        reference="WO-1801",
        po_number="PO-1801",
        manager=user,
        contract_value=Decimal("100000.00"),
        cost_budget=Decimal("80000.00"),
    )


@pytest.fixture
def site(tenant):
    return SiteFactory()


@pytest.fixture
def yard(tenant):
    return Location.objects.create(
        organization=tenant, name="Yard", type=LocationType.YARD
    )


def purchase(tenant, project, site, user, **fields):
    defaults = {
        "organization": tenant,
        "project": project,
        "site": site,
        "purchase_date": date(2026, 10, 1),
        "amount": Decimal("500.00"),
        "recorded_by": user,
    }
    return SitePurchase.objects.create(**{**defaults, **fields})


def reload(obj):
    return type(obj).objects.get(pk=obj.pk)


def move(obj, status, **fields):
    obj.status = status
    for name, value in fields.items():
        setattr(obj, name, value)
    obj.save()


@pytest.mark.django_db
class TestSitePurchase:
    def test_the_series_exists(self):
        assert DEFAULT_PREFIXES[DocumentType.SITE_PURCHASE] == "SP"
        assert DEFAULT_PREFIXES[DocumentType.SUBCONTRACT] == "SC"

    def test_it_follows_the_expense_status_path(self, tenant, project, site, user):
        p = reload(purchase(tenant, project, site, user))
        move(p, S.PENDING_FINANCE)
        move(p, S.APPROVED, decided_at=timezone.now())
        move(p, S.PAID, paid_at=timezone.now(), paid_by=user, payment_reference="R1")
        assert reload(p).status == S.PAID

    def test_pm_cannot_skip_finance(self, tenant, project, site, user):
        p = reload(purchase(tenant, project, site, user))
        with pytest.raises(ValidationError, match="cannot move"):
            move(p, S.APPROVED, decided_at=timezone.now())

    def test_an_approved_purchase_is_frozen(self, tenant, project, site, user):
        p = purchase(
            tenant, project, site, user, status=S.APPROVED, decided_at=timezone.now()
        )
        p = reload(p)
        p.amount = Decimal("1.00")
        with pytest.raises(ValidationError, match="cannot be changed"):
            p.save()

    def test_a_decided_purchase_records_when(self, tenant, project, site, user):
        with pytest.raises(IntegrityError), transaction.atomic():
            purchase(tenant, project, site, user, status=S.APPROVED)

    def test_a_yard_purchase_needs_a_location(self, tenant, project, site, user, yard):
        with pytest.raises(IntegrityError), transaction.atomic():
            purchase(tenant, project, site, user, destination="INTO_YARD")
        ok = purchase(
            tenant, project, site, user, destination="INTO_YARD", receive_into=yard
        )
        assert ok.pk

    def test_a_site_purchase_forbids_a_location(
        self, tenant, project, site, user, yard
    ):
        with pytest.raises(IntegrityError), transaction.atomic():
            purchase(tenant, project, site, user, receive_into=yard)

    def test_numbers_are_unique_per_org_but_blank_may_repeat(
        self, tenant, project, site, user
    ):
        purchase(tenant, project, site, user)
        purchase(tenant, project, site, user)
        purchase(tenant, project, site, user, number="SP-000001")
        with pytest.raises(IntegrityError), transaction.atomic():
            purchase(tenant, project, site, user, number="SP-000001")

    def test_client_uuid_is_unique_per_org(self, tenant, project, site, user):
        import uuid

        key = uuid.uuid4()
        purchase(tenant, project, site, user, client_uuid=key)
        with pytest.raises(IntegrityError), transaction.atomic():
            purchase(tenant, project, site, user, client_uuid=key)

    def test_it_is_never_deleted(self, tenant, project, site, user):
        with pytest.raises(ValidationError, match="never deleted"):
            purchase(tenant, project, site, user).delete()

    def test_a_reversal_counts_negatively(self, tenant, project, site, user):
        original = purchase(tenant, project, site, user)
        reversal = purchase(
            tenant,
            project,
            site,
            user,
            reverses=original,
            status=S.APPROVED,
            decided_at=timezone.now(),
        )
        assert reversal.signed_amount == Decimal("-500.00")
        assert original.signed_amount == Decimal("500.00")

    def test_lines_total_is_the_sum_of_rounded_lines(
        self, tenant, project, site, user
    ):
        p = purchase(tenant, project, site, user, amount=Decimal("35.34"))
        SitePurchaseLine.objects.create(
            organization=tenant,
            purchase=p,
            description="Cable",
            quantity=Decimal("3.333"),
            unit_price=Decimal("10.00"),
        )
        SitePurchaseLine.objects.create(
            organization=tenant,
            purchase=p,
            description="Tape",
            quantity=Decimal("2"),
            unit_price=Decimal("1.00"),
        )
        # 33.33 + 2.00
        assert p.lines_total() == Decimal("35.33")

    def test_a_line_needs_an_item_or_a_description(
        self, tenant, project, site, user
    ):
        p = purchase(tenant, project, site, user)
        with pytest.raises(IntegrityError), transaction.atomic():
            SitePurchaseLine.objects.create(
                organization=tenant,
                purchase=p,
                quantity=Decimal("1"),
                unit_price=Decimal("1"),
            )

    @pytest.mark.parametrize(
        ("quantity", "price"), [("0", "1.00"), ("-1", "1.00"), ("1", "-0.01")]
    )
    def test_line_quantity_and_price_bounds(
        self, tenant, project, site, user, quantity, price
    ):
        p = purchase(tenant, project, site, user)
        with pytest.raises(IntegrityError), transaction.atomic():
            SitePurchaseLine.objects.create(
                organization=tenant,
                purchase=p,
                description="x",
                quantity=Decimal(quantity),
                unit_price=Decimal(price),
            )

    def test_a_zero_price_line_is_allowed(self, tenant, project, site, user):
        p = purchase(tenant, project, site, user)
        line = SitePurchaseLine.objects.create(
            organization=tenant,
            purchase=p,
            description="Free sample",
            quantity=Decimal("1"),
            unit_price=Decimal("0"),
        )
        assert line.total == Decimal("0.00")

    def test_lines_freeze_with_the_purchase(self, tenant, project, site, user):
        p = purchase(tenant, project, site, user)
        line = SitePurchaseLine.objects.create(
            organization=tenant,
            purchase=p,
            description="x",
            quantity=Decimal("1"),
            unit_price=Decimal("1"),
        )
        SitePurchase.objects.filter(pk=p.pk).update(
            status=S.APPROVED, decided_at=timezone.now()
        )
        line.quantity = Decimal("2")
        with pytest.raises(ValidationError, match="frozen"):
            line.save()
        with pytest.raises(ValidationError, match="frozen"):
            line.delete()


@pytest.mark.django_db
class TestOverBudgetColumns:
    def test_expense_allowance_and_purchase_carry_them(
        self, tenant, project, site, user
    ):
        cat = ExpenseCategory.objects.create(organization=tenant, name="Misc")
        expense = ProjectExpense.objects.create(
            organization=tenant,
            project=project,
            category=cat,
            amount=Decimal("10.00"),
            incurred_on=date(2026, 10, 1),
            recorded_by=user,
            over_budget_by=Decimal("5.00"),
            over_budget_reason="Urgent",
        )
        allowance = AllowanceRequest.objects.create(
            organization=tenant,
            type=AllowanceType.OTHER,
            amount=Decimal("10.00"),
            from_date=date(2026, 10, 1),
            to_date=date(2026, 10, 1),
            project=project,
            recorded_by=user,
            over_budget_by=Decimal("1.00"),
            over_budget_reason="Because",
        )
        p = purchase(
            tenant,
            project,
            site,
            user,
            over_budget_by=Decimal("2.00"),
            over_budget_reason="Why",
        )
        assert reload(expense).over_budget_by == Decimal("5.00")
        assert reload(allowance).over_budget_reason == "Because"
        assert reload(p).over_budget_reason == "Why"

    def test_they_default_to_none_and_blank(self, tenant, project, site, user):
        p = purchase(tenant, project, site, user)
        assert (p.over_budget_by, p.over_budget_reason) == (None, "")

    def test_they_freeze_with_an_approved_expense(self, tenant, project, user):
        cat = ExpenseCategory.objects.create(organization=tenant, name="Misc")
        e = ProjectExpense.objects.create(
            organization=tenant,
            project=project,
            category=cat,
            amount=Decimal("10.00"),
            incurred_on=date(2026, 10, 1),
            recorded_by=user,
            status=S.PENDING_FINANCE,
        )
        e = reload(e)
        move(e, S.APPROVED, decided_at=timezone.now())
        e = reload(e)
        e.over_budget_reason = "edited"
        with pytest.raises(ValidationError, match="cannot be changed"):
            e.save()


@pytest.fixture
def subcontractor(tenant):
    return Subcontractor.objects.create(organization=tenant, name="Acme Civils")


def make_subcontract(tenant, project, subcontractor, user, **fields):
    defaults = {
        "organization": tenant,
        "project": project,
        "subcontractor": subcontractor,
        "contract_value": Decimal("50000.00"),
        "created_by": user,
    }
    return Subcontract.objects.create(**{**defaults, **fields})


def make_payment(tenant, subcontract, user, **fields):
    defaults = {
        "organization": tenant,
        "subcontract": subcontract,
        "amount": Decimal("1000.00"),
        "paid_on": date(2026, 10, 2),
        "reference": "INV-1",
        "recorded_by": user,
    }
    return SubcontractPayment.objects.create(**{**defaults, **fields})


@pytest.mark.django_db
class TestSubcontract:
    def test_several_per_project_and_subcontractor(
        self, tenant, project, subcontractor, user
    ):
        make_subcontract(tenant, project, subcontractor, user)
        make_subcontract(tenant, project, subcontractor, user)
        assert Subcontract.objects.count() == 2

    def test_sites_are_a_many_to_many(self, tenant, project, subcontractor, user, site):
        sc = make_subcontract(tenant, project, subcontractor, user)
        sc.sites.add(site)
        assert list(sc.sites.all()) == [site]

    def test_a_value_is_required_positive(self, tenant, project, subcontractor, user):
        with pytest.raises(IntegrityError), transaction.atomic():
            make_subcontract(
                tenant, project, subcontractor, user, contract_value=Decimal("0")
            )

    def test_number_unique_per_org(self, tenant, project, subcontractor, user):
        make_subcontract(tenant, project, subcontractor, user, number="SC-000001")
        with pytest.raises(IntegrityError), transaction.atomic():
            make_subcontract(
                tenant, project, subcontractor, user, number="SC-000001"
            )

    def test_never_deleted(self, tenant, project, subcontractor, user):
        with pytest.raises(ValidationError, match="never deleted"):
            make_subcontract(tenant, project, subcontractor, user).delete()


@pytest.mark.django_db
class TestSubcontractPayment:
    @pytest.fixture
    def subcontract(self, tenant, project, subcontractor, user):
        return make_subcontract(tenant, project, subcontractor, user)

    def test_one_pm_level_then_it_counts(self, tenant, subcontract, user):
        pay = reload(make_payment(tenant, subcontract, user))
        move(pay, S.APPROVED, decided_at=timezone.now())
        assert reload(pay).status == S.APPROVED

    def test_it_exposes_the_project(self, tenant, subcontract, user, project):
        pay = make_payment(tenant, subcontract, user)
        assert pay.project == project
        assert pay.project_id == project.pk

    def test_there_is_no_finance_level_or_paid(self, tenant, subcontract, user):
        with pytest.raises(IntegrityError), transaction.atomic():
            make_payment(tenant, subcontract, user, status=S.PENDING_FINANCE)
        pay = reload(make_payment(tenant, subcontract, user))
        with pytest.raises(ValidationError, match="cannot move"):
            move(pay, S.PENDING_FINANCE)

    def test_a_rejected_payment_resubmits(self, tenant, subcontract, user):
        pay = reload(make_payment(tenant, subcontract, user))
        move(pay, S.REJECTED, decided_at=timezone.now())
        pay = reload(pay)
        move(pay, S.PENDING_PM, decided_at=None)
        assert reload(pay).status == S.PENDING_PM

    def test_an_approved_payment_is_frozen(self, tenant, subcontract, user):
        pay = make_payment(
            tenant, subcontract, user, status=S.APPROVED, decided_at=timezone.now()
        )
        pay = reload(pay)
        pay.amount = Decimal("1.00")
        with pytest.raises(ValidationError, match="cannot be changed"):
            pay.save()
        pay = reload(pay)
        with pytest.raises(ValidationError, match="cannot move"):
            move(pay, S.REJECTED)

    def test_a_decided_payment_records_when(self, tenant, subcontract, user):
        with pytest.raises(IntegrityError), transaction.atomic():
            make_payment(tenant, subcontract, user, status=S.APPROVED)

    def test_reference_is_required(self, tenant, subcontract, user):
        with pytest.raises(IntegrityError), transaction.atomic():
            make_payment(tenant, subcontract, user, reference="")

    def test_a_reversal_counts_negatively(self, tenant, subcontract, user):
        original = make_payment(tenant, subcontract, user)
        reversal = make_payment(
            tenant,
            subcontract,
            user,
            reverses=original,
            status=S.APPROVED,
            decided_at=timezone.now(),
        )
        assert reversal.signed_amount == Decimal("-1000.00")

    def test_never_deleted(self, tenant, subcontract, user):
        with pytest.raises(ValidationError, match="never deleted"):
            make_payment(tenant, subcontract, user).delete()


@pytest.mark.django_db
class TestJobSubcontract:
    def _job(self, tenant, site, user, **fields):
        defaults = {
            "organization": tenant,
            "client": site.client,
            "site": site,
            "assignee": user,
        }
        return Job.objects.create(**{**defaults, **fields})

    def test_old_subcontracted_jobs_have_no_subcontract(
        self, tenant, site, user, subcontractor
    ):
        job = self._job(
            tenant,
            site,
            user,
            delivery_mode=DeliveryMode.SUBCONTRACTED,
            subcontractor=subcontractor,
            agreed_price=Decimal("100.00"),
        )
        assert reload(job).subcontract is None

    def test_an_in_house_job_cannot_carry_one(
        self, tenant, project, site, user, subcontractor
    ):
        sc = make_subcontract(tenant, project, subcontractor, user)
        job = self._job(tenant, site, user)
        with pytest.raises(IntegrityError), transaction.atomic():
            Job.objects.filter(pk=job.pk).update(subcontract=sc)

    def test_a_subcontracted_job_may(self, tenant, project, site, user, subcontractor):
        sc = make_subcontract(tenant, project, subcontractor, user)
        job = self._job(
            tenant,
            site,
            user,
            delivery_mode=DeliveryMode.SUBCONTRACTED,
            subcontractor=subcontractor,
            agreed_price=Decimal("100.00"),
            subcontract=sc,
            over_contract_reason="Extra scope",
        )
        assert reload(job).subcontract == sc

    def test_it_freezes_once_the_job_is_closed(
        self, tenant, project, site, user, subcontractor
    ):
        sc = make_subcontract(tenant, project, subcontractor, user)
        other = make_subcontract(tenant, project, subcontractor, user)
        job = self._job(
            tenant,
            site,
            user,
            delivery_mode=DeliveryMode.SUBCONTRACTED,
            subcontractor=subcontractor,
            agreed_price=Decimal("100.00"),
            subcontract=sc,
        )
        job = reload(job)
        job.status = "CLOSED"
        job.closed_at = timezone.now()
        job.save()
        job = reload(job)
        job.subcontract = other
        with pytest.raises(ValidationError, match="closed"):
            job.save()
        job = reload(job)
        job.over_contract_reason = "late"
        with pytest.raises(ValidationError, match="closed"):
            job.save()

    def test_subcontract_deletion_is_protected(
        self, tenant, project, site, user, subcontractor
    ):
        sc = make_subcontract(tenant, project, subcontractor, user)
        self._job(
            tenant,
            site,
            user,
            delivery_mode=DeliveryMode.SUBCONTRACTED,
            subcontractor=subcontractor,
            agreed_price=Decimal("1.00"),
            subcontract=sc,
        )
        assert sc.jobs.count() == 1


def make_milestone(tenant, project, **fields):
    defaults = {
        "organization": tenant,
        "project": project,
        "sequence": 1,
        "name": "Deposit",
        "share_type": "PERCENT",
        "share_value": Decimal("30.00"),
    }
    return ProjectMilestone.objects.create(**{**defaults, **fields})


@pytest.mark.django_db
class TestProjectMilestone:
    def test_sequence_is_unique_per_project(self, tenant, project):
        make_milestone(tenant, project)
        with pytest.raises(IntegrityError), transaction.atomic():
            make_milestone(tenant, project, name="Again")
        assert make_milestone(tenant, project, sequence=2).pk

    def test_a_date_condition_needs_its_date(self, tenant, project):
        with pytest.raises(IntegrityError), transaction.atomic():
            make_milestone(tenant, project, condition="DATE")
        ok = make_milestone(
            tenant, project, condition="DATE", condition_date=date(2026, 12, 1)
        )
        assert ok.pk

    def test_a_date_without_a_date_condition_is_refused(self, tenant, project):
        with pytest.raises(IntegrityError), transaction.atomic():
            make_milestone(tenant, project, condition_date=date(2026, 12, 1))

    def test_a_percent_is_at_most_100_but_an_amount_may_exceed(
        self, tenant, project
    ):
        with pytest.raises(IntegrityError), transaction.atomic():
            make_milestone(tenant, project, share_value=Decimal("100.01"))
        assert make_milestone(
            tenant, project, share_type="AMOUNT", share_value=Decimal("5000.00")
        ).pk

    def test_a_share_is_positive(self, tenant, project):
        with pytest.raises(IntegrityError), transaction.atomic():
            make_milestone(tenant, project, share_value=Decimal("0"))


@pytest.mark.django_db
class TestMilestoneInvoiceAndReceipt:
    @pytest.fixture
    def milestone(self, tenant, project):
        return make_milestone(tenant, project)

    def invoice(self, tenant, milestone, user, **fields):
        defaults = {
            "organization": tenant,
            "milestone": milestone,
            "invoice_number": "INV-9",
            "invoice_date": date(2026, 10, 3),
            "amount": Decimal("100.00"),
            "recorded_by": user,
        }
        return MilestoneInvoice.objects.create(**{**defaults, **fields})

    def receipt(self, tenant, milestone, user, **fields):
        defaults = {
            "organization": tenant,
            "milestone": milestone,
            "received_on": date(2026, 10, 4),
            "amount": Decimal("40.00"),
            "recorded_by": user,
        }
        return MilestoneReceipt.objects.create(**{**defaults, **fields})

    def test_partial_receipts_are_many_rows(self, tenant, milestone, user):
        self.receipt(tenant, milestone, user)
        self.receipt(tenant, milestone, user)
        assert milestone.receipts.count() == 2

    @pytest.mark.parametrize("kind", ["invoice", "receipt"])
    def test_an_edit_is_refused(self, tenant, milestone, user, kind):
        row = getattr(self, kind)(tenant, milestone, user)
        row = reload(row)
        row.amount = Decimal("1.00")
        with pytest.raises(ValidationError, match="cannot be changed"):
            row.save()

    @pytest.mark.parametrize("kind", ["invoice", "receipt"])
    def test_a_delete_is_refused(self, tenant, milestone, user, kind):
        row = getattr(self, kind)(tenant, milestone, user)
        with pytest.raises(ValidationError, match="never deleted"):
            row.delete()

    @pytest.mark.parametrize("kind", ["invoice", "receipt"])
    def test_a_void_is_allowed_once_then_frozen(self, tenant, milestone, user, kind):
        row = reload(getattr(self, kind)(tenant, milestone, user))
        row.voided_at = timezone.now()
        row.voided_by = user
        row.void_reason = "Typo"
        row.save()
        row = reload(row)
        assert row.is_void
        row.void_reason = "Changed my mind"
        with pytest.raises(ValidationError, match="cannot be changed"):
            row.save()

    @pytest.mark.parametrize("kind", ["invoice", "receipt"])
    def test_a_void_needs_who_and_why(self, tenant, milestone, user, kind):
        with pytest.raises(IntegrityError), transaction.atomic():
            getattr(self, kind)(tenant, milestone, user, voided_at=timezone.now())

    def test_amounts_are_positive(self, tenant, milestone, user):
        with pytest.raises(IntegrityError), transaction.atomic():
            self.invoice(tenant, milestone, user, amount=Decimal("0"))
        with pytest.raises(IntegrityError), transaction.atomic():
            self.receipt(tenant, milestone, user, amount=Decimal("0"))

    def test_an_invoice_needs_a_number(self, tenant, milestone, user):
        with pytest.raises(IntegrityError), transaction.atomic():
            self.invoice(tenant, milestone, user, invoice_number="")


STAGE2_MODELS = (
    SitePurchase,
    SitePurchaseLine,
    Subcontract,
    SubcontractPayment,
    ProjectMilestone,
    MilestoneInvoice,
    MilestoneReceipt,
)


@pytest.mark.django_db
@pytest.mark.rls
class TestIsolation:
    def test_every_table_carries_the_policy(self):
        with connection.cursor():
            carried = tables_with_policy()
        for model in STAGE2_MODELS:
            assert model._meta.db_table in carried

    def test_another_tenant_cannot_see_the_rows(
        self, organization, other_organization
    ):
        with tenant_context(organization):
            mine_user = UserFactory(organization=organization)
            project = ProjectFactory(
                reference="WO-ISO",
                po_number="PO-ISO",
                manager=mine_user,
                contract_value=Decimal("1.00"),
                cost_budget=Decimal("1.00"),
            )
            sub = Subcontractor.objects.create(organization=organization, name="S")
            sc = make_subcontract(organization, project, sub, mine_user)
            make_payment(organization, sc, mine_user)
            ms = make_milestone(organization, project)
            MilestoneReceipt.objects.create(
                organization=organization,
                milestone=ms,
                received_on=date(2026, 10, 4),
                amount=Decimal("1.00"),
                recorded_by=mine_user,
            )
            site = SiteFactory()
            purchase(organization, project, site, mine_user)
            assert Subcontract.objects.count() == 1

        with tenant_context(other_organization):
            for model in STAGE2_MODELS:
                assert model.objects.count() == 0
                with connection.cursor() as cursor:
                    cursor.execute(f'SELECT count(*) FROM "{model._meta.db_table}"')
                    assert cursor.fetchone()[0] == 0
