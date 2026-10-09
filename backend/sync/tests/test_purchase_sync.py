"""T18.14 (purchases) — a site purchase replayed from a phone (§4.19.11; R6, R7, R9).

The queue is a second door to ``record_site_purchase``: it lands what the online
form would, once; a refusal is a recorded exception with the domain code that
the person can fix and resend; a supplier queued in the same batch is found by
its uuid; and being over budget never refuses a replay.
"""

from decimal import Decimal
from uuid import uuid4

import pytest

from accounts.factories import RoleFactory, UserFactory, UserRoleFactory
from accounts.permissions_registry import PERM
from catalogue.factories import ItemTypeFactory
from commercials.models import ExpenseStatus, PurchaseDestination, SitePurchase
from locations.factories import YardFactory
from network.factories import ProjectFactory, SiteFactory
from network.models import Supplier, SupplierStatus
from sync.models import ExceptionStatus, SubmissionStatus, SyncException
from sync.services import apply_submission

D = Decimal
pytestmark = pytest.mark.django_db


@pytest.fixture
def pm(tenant):
    return UserFactory(organization=tenant, full_name="Pippa Manager")


@pytest.fixture
def fin(tenant):
    user = UserFactory(organization=tenant, full_name="Fiona Finance")
    UserRoleFactory(user=user, role=RoleFactory(codenames=[PERM.FINANCE_APPROVE]))
    return user


@pytest.fixture
def clerk(tenant):
    return UserFactory(organization=tenant, full_name="Site Clerk")


@pytest.fixture
def project(tenant, pm):
    return ProjectFactory(
        reference="WO-8001",
        po_number="PO-800",
        manager=pm,
        contract_value=D("50000.00"),
        cost_budget=D("10000.00"),
    )


@pytest.fixture
def site(tenant, project):
    site = SiteFactory(name="Thika")
    project.sites.add(site, through_defaults={"organization_id": project.organization_id})
    return site


@pytest.fixture
def supplier(tenant, fin):
    return Supplier.objects.create(
        organization=tenant, name="Hardware Ltd", registered_by=fin,
        status=SupplierStatus.APPROVED,
    )


def send(tenant, clerk, payload, uuid=None, operation="SITE_PURCHASE"):
    uuid = uuid or uuid4()
    return apply_submission(
        organization=tenant,
        client_uuid=uuid,
        operation=operation,
        payload={**payload, "client_uuid": str(uuid)},
        submitted_by=clerk,
    )


def purchase_body(site, **extra):
    return {
        "site": site.pk,
        "purchase_date": "2026-10-05",
        "destination": "USED_AT_SITE",
        "lines": [
            {
                "item_type": None,
                "description": "Cable ties",
                "quantity": "2",
                "unit_price": "100.00",
            }
        ],
        "photos_expected": 2,
        **extra,
    }


class TestPurchaseReplay:
    def test_it_lands_what_the_online_form_would(
        self, tenant, clerk, site, supplier, project, fin
    ):
        submission, replay = send(tenant, clerk, purchase_body(site, supplier=supplier.pk))

        assert not replay and submission.status == SubmissionStatus.APPLIED
        made = SitePurchase.objects.get()
        assert submission.document_id == str(made.pk)
        assert submission.document_type == SitePurchase._meta.label
        assert made.client_uuid == submission.client_uuid
        assert made.amount == D("200.00") and made.project == project
        assert made.status == ExpenseStatus.PENDING_PM
        assert made.recorded_by == clerk and made.photos_expected == 2
        assert made.supplier == supplier

    def test_a_replay_returns_the_same_row(self, tenant, clerk, site, supplier, fin):
        uuid = uuid4()
        first, _ = send(tenant, clerk, purchase_body(site, supplier=supplier.pk), uuid)
        second, replay = send(tenant, clerk, purchase_body(site, supplier=supplier.pk), uuid)

        assert replay and second.pk == first.pk
        assert SitePurchase.objects.count() == 1

    def test_a_yard_purchase_travels_with_its_lines(
        self, tenant, clerk, site, supplier, fin
    ):
        yard = YardFactory()
        item = ItemTypeFactory(name="Sync clamp", uom="ea")
        body = purchase_body(
            site,
            supplier=supplier.pk,
            destination="INTO_YARD",
            receive_into=yard.pk,
            lines=[{"item_type": item.pk, "description": "", "quantity": "4", "unit_price": "25"}],
        )
        submission, _ = send(tenant, clerk, body)

        assert submission.status == SubmissionStatus.APPLIED
        made = SitePurchase.objects.get()
        assert made.destination == PurchaseDestination.INTO_YARD
        assert made.receive_into == yard and made.lines.get().item_type == item

    def test_a_supplier_queued_in_the_same_batch_is_found_by_uuid(
        self, tenant, clerk, site, fin
    ):
        queued = uuid4()
        sent, _ = send(
            tenant, clerk, {"name": "Roadside Traders"}, queued, operation="SUPPLIER"
        )
        assert sent.status == SubmissionStatus.APPLIED

        submission, _ = send(
            tenant, clerk, purchase_body(site, supplier_client_uuid=str(queued))
        )

        assert submission.status == SubmissionStatus.APPLIED
        made = SitePurchase.objects.get()
        assert made.supplier.name == "Roadside Traders"
        assert made.supplier.status == SupplierStatus.PENDING  # buyable, not payable

    def test_an_unknown_supplier_uuid_is_refused_as_an_exception(
        self, tenant, clerk, site, fin
    ):
        submission, _ = send(tenant, clerk, purchase_body(site, supplier_client_uuid=str(uuid4())))

        assert submission.status == SubmissionStatus.REJECTED
        assert SitePurchase.objects.count() == 0
        exception = SyncException.objects.get()
        assert exception.status == ExceptionStatus.OPEN
        assert "supplier" in str(exception.details)

    def test_no_supplier_at_all_is_refused(self, tenant, clerk, site, fin):
        submission, _ = send(tenant, clerk, purchase_body(site))

        assert submission.status == SubmissionStatus.REJECTED

    def test_domain_refusals_carry_their_code(self, tenant, clerk, site, supplier, fin):
        yard = YardFactory()
        free_text = purchase_body(
            site, supplier=supplier.pk, destination="INTO_YARD", receive_into=yard.pk
        )
        submission, _ = send(tenant, clerk, free_text)
        assert submission.status == SubmissionStatus.REJECTED
        assert SyncException.objects.get().code == "SITE_PURCHASE_YARD_NEEDS_CATALOGUE"

        Supplier.objects.filter(pk=supplier.pk).update(status=SupplierStatus.REJECTED)
        other, _ = send(tenant, clerk, purchase_body(site, supplier=supplier.pk))
        assert other.status == SubmissionStatus.REJECTED
        assert SyncException.objects.filter(code="SUPPLIER_NOT_USABLE").exists()

    def test_a_correction_closes_the_old_refusal(self, tenant, clerk, site, supplier, fin):
        bad = purchase_body(site, supplier=supplier.pk, lines=[])
        first, _ = send(tenant, clerk, bad)
        assert first.status == SubmissionStatus.REJECTED

        fixed, _ = send(
            tenant, clerk,
            purchase_body(
                site, supplier=supplier.pk, supersedes_client_uuid=str(first.client_uuid)
            ),
        )

        assert fixed.status == SubmissionStatus.APPLIED
        exception = SyncException.objects.get()
        assert exception.status == ExceptionStatus.RESOLVED
        assert exception.replacement_submission == fixed

    def test_over_budget_is_flagged_never_refused(
        self, tenant, clerk, site, supplier, project, fin
    ):
        big = purchase_body(
            site, supplier=supplier.pk,
            lines=[
                {"item_type": None, "description": "Gen", "quantity": "1", "unit_price": "12000"}
            ],
        )
        submission, _ = send(tenant, clerk, big)

        assert submission.status == SubmissionStatus.APPLIED
        made = SitePurchase.objects.get()
        assert made.over_budget_by == D("2000.00") and made.over_budget_reason == ""

    def test_no_other_approver_is_a_refusal_not_a_crash(self, tenant, clerk, site):
        alone = Supplier.objects.create(
            organization=tenant, name="Solo Ltd", registered_by=clerk,
            status=SupplierStatus.APPROVED,
        )
        submission, _ = send(tenant, clerk, purchase_body(site, supplier=alone.pk))

        assert submission.status == SubmissionStatus.REJECTED
        assert SyncException.objects.get().code == "FINANCE_NO_OTHER_APPROVER"


class TestBundleHeadroom:
    """T18.14 (bundle) — per-project budget headroom, only for who may see cost (R9)."""

    def get(self, tenant, user, settings):
        from rest_framework.test import APIClient

        settings.TENANT_BASE_DOMAIN = "localhost"
        client = APIClient(HTTP_HOST="silvertech.localhost")
        client.force_authenticate(user)
        return client.get("/api/v1/sync/bundle").json()["project_headroom"]

    def grant(self, tenant, *codenames):
        user = UserFactory(organization=tenant)
        UserRoleFactory(user=user, role=RoleFactory(codenames=list(codenames)))
        return user

    def test_a_margin_holder_gets_budget_less_spent_and_pending(
        self, tenant, clerk, site, supplier, project, settings
    ):
        send(
            tenant, clerk,
            purchase_body(
                site, supplier=supplier.pk,
                lines=[
                    {
                        "item_type": None, "description": "Cable",
                        "quantity": "2", "unit_price": "1500",
                    }
                ],
            ),
        )
        viewer = self.grant(tenant, PERM.PROJECT_VIEW_MARGIN)

        rows = self.get(tenant, viewer, settings)

        assert rows == [{"id": project.pk, "headroom": "7000.00"}]

    def test_a_manager_sees_only_their_own_projects(self, tenant, site, project, settings):
        ProjectFactory(
            reference="WO-8002", manager=UserFactory(organization=tenant), cost_budget=D("500.00")
        )
        manager = self.grant(tenant, PERM.PROJECT_VIEW_COST)
        project.manager = manager
        project.save()

        rows = self.get(tenant, manager, settings)

        assert [r["id"] for r in rows] == [project.pk]

    def test_nobody_without_cost_access_gets_a_figure(self, tenant, clerk, project, settings):
        assert self.get(tenant, clerk, settings) == []

    def test_a_project_with_no_budget_is_left_out(self, tenant, project, settings):
        project.po_number = ""
        project.cost_budget = None
        project.save()
        viewer = self.grant(tenant, PERM.PROJECT_VIEW_MARGIN)

        assert self.get(tenant, viewer, settings) == []
