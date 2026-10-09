"""T18.10 — the PO that arrives late, site dates and the new attachment rules
(§4.19.6, §4.19.8, §4.19.9; R10, R12).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test.utils import CaptureQueriesContext

from accounts.factories import RoleFactory, UserFactory, UserRoleFactory
from accounts.permissions_registry import PERM
from commercials.models import (
    ExpenseCategory,
    ProjectExpense,
    ProjectMilestone,
    SitePurchase,
    Subcontract,
    SubcontractPayment,
)
from commercials.tests.api_helpers import PASSWORD, Api, error_code, results
from core.models import Attachment, AuditAction, AuditLog
from dispatch.models import GateOut, GateOutPurpose
from jobs.models import Job
from locations.factories import YardFactory
from network.factories import ProjectFactory, SiteFactory
from network.models import ProjectSite, Subcontractor

D = Decimal
SITE = "network.ProjectSite"
PROJECT = "network.Project"
CERT = "Acceptance certificate"


@pytest.fixture(autouse=True)
def _domain(settings):
    settings.TENANT_BASE_DOMAIN = "localhost"


def person(tenant, name, *codenames):
    user = UserFactory(organization=tenant, full_name=name, password=PASSWORD)
    if codenames:
        UserRoleFactory(user=user, role=RoleFactory(codenames=list(codenames)))
    return user


@pytest.fixture
def pm(tenant):
    return person(tenant, "Pippa Manager", PERM.PROJECT_VIEW_COST)


@pytest.fixture
def admin(tenant):
    return person(tenant, "Alex Admin", PERM.CATALOGUE_MANAGE)


@pytest.fixture
def fin(tenant):
    return person(tenant, "Fiona Finance", PERM.FINANCE_APPROVE)


@pytest.fixture
def owner(tenant):
    return person(tenant, "Olive Owner", PERM.PROJECT_VIEW_MARGIN, PERM.CATALOGUE_MANAGE)


@pytest.fixture
def stranger(tenant):
    return person(tenant, "Sam Stranger")


@pytest.fixture
def project(tenant, pm):
    """A work-order project: no PO yet (O1)."""
    return ProjectFactory(reference="WO-2001", manager=pm)


@pytest.fixture
def site(project):
    site = SiteFactory(client=project.client, internal_ref="SLV-1")
    link(project, site)
    return site


def link(project, *sites):
    project.sites.add(*sites, through_defaults={"organization_id": project.organization_id})


def utc(value: str) -> datetime:
    """The API answers in the tenant's local time; compare as instants."""
    return datetime.fromisoformat(value).astimezone(UTC)


def row_of(project, site):
    return ProjectSite.objects.get(project=project, site=site)


def pdf(name="cert.pdf"):
    return SimpleUploadedFile(name, b"%PDF-1.4 pretend", content_type="application/pdf")


def upload(http, target_type, target_id, caption="", name="doc.pdf"):
    return http.upload(
        {
            "target_type": target_type,
            "target_id": str(target_id),
            "file": pdf(name),
            "kind": "DOCUMENT",
            "caption": caption,
        }
    )


PO = {
    "po_number": "PO-9001",
    "po_issue_date": "2026-10-01",
    "contract_value": "250000.00",
    "cost_budget": "180000.00",
    "payment_terms": "30 days from invoice",
    "payment_terms_days": 30,
}


@pytest.mark.django_db
class TestAttachPo:
    def test_the_po_lands_on_the_same_row_and_history_stays(
        self, client, tenant, project, site, pm
    ):
        job = Job.objects.create(
            organization=tenant,
            reference="JOB-1",
            client=site.client,
            site=site,
            project=project,
            assignee=pm,
        )
        expense = ProjectExpense.objects.create(
            organization=tenant,
            project=project,
            category=ExpenseCategory.objects.create(organization=tenant, name="Fuel"),
            amount=D("500"),
            incurred_on=date(2026, 9, 20),
            recorded_by=pm,
        )
        note = Attachment.objects.create(
            organization=tenant,
            target_type=PROJECT,
            target_id=str(project.pk),
            file="t/wo.pdf",
            filename="wo.pdf",
            uploaded_by=pm,
        )

        response = Api(client, pm).post(f"projects/{project.pk}/attach-po", PO)

        assert response.status_code == 200, response.content
        body = response.json()
        assert body["id"] == project.pk
        assert (body["po_number"], body["po_issue_date"]) == ("PO-9001", "2026-10-01")
        assert body["po_recorded_at"] is not None
        project.refresh_from_db()
        assert project.po_number == "PO-9001"
        assert project.payment_terms_days == 30
        assert Job.objects.get(pk=job.pk).project_id == project.pk
        assert ProjectExpense.objects.get(pk=expense.pk).project_id == project.pk
        assert Attachment.objects.filter(pk=note.pk, target_id=str(project.pk)).exists()
        assert list(project.sites.all()) == [site]

    def test_it_seeds_the_default_milestones_and_audits(self, client, project, pm):
        Api(client, pm).post(f"projects/{project.pk}/attach-po", PO)

        names = list(
            ProjectMilestone.objects.filter(project=project).values_list("name", flat=True)
        )
        assert names == ["Deposit", "Conditional acceptance", "Final acceptance"]
        assert AuditLog.objects.filter(
            action=AuditAction.DOCUMENT_AMENDED, note__contains="PO PO-9001 attached"
        ).exists()

    def test_a_second_po_is_refused(self, client, project, pm):
        http = Api(client, pm)
        http.post(f"projects/{project.pk}/attach-po", PO)

        again = http.post(f"projects/{project.pk}/attach-po", {**PO, "po_number": "PO-2"})

        assert (again.status_code, error_code(again)) == (409, "PROJECT_ALREADY_HAS_PO")
        project.refresh_from_db()
        assert project.po_number == "PO-9001"
        assert ProjectMilestone.objects.filter(project=project).count() == 3

    def test_a_taken_po_number_is_a_field_error(self, client, tenant, project, pm):
        ProjectFactory(
            reference="WO-X",
            po_number="PO-9001",
            manager=pm,
            contract_value=D("1"),
            cost_budget=D("1"),
        )

        response = Api(client, pm).post(f"projects/{project.pk}/attach-po", PO)

        assert response.status_code == 400
        assert "po_number" in response.json()["error"]["field_errors"]
        project.refresh_from_db()
        assert project.po_number == ""

    def test_a_project_with_no_manager_needs_one_chosen(self, client, tenant, admin, pm):
        bare = ProjectFactory(reference="WO-BARE")
        http = Api(client, admin)

        missing = http.post(f"projects/{bare.pk}/attach-po", PO)
        chosen = http.post(f"projects/{bare.pk}/attach-po", {**PO, "manager": pm.pk})

        assert missing.status_code == 400
        assert "manager" in missing.json()["error"]["field_errors"]
        assert chosen.status_code == 200
        bare.refresh_from_db()
        assert bare.manager_id == pm.pk

    def test_a_closed_project_cannot_take_a_po(self, client, project, pm):
        project.status = "CANCELLED"
        project.closed_at = datetime(2026, 10, 1, tzinfo=UTC)
        project.save()

        response = Api(client, pm).post(f"projects/{project.pk}/attach-po", PO)

        assert (response.status_code, error_code(response)) == (409, "PROJECT_NOT_OPEN")

    def test_only_the_pm_or_catalogue_managers_may(self, client, project, admin, fin, stranger):
        assert Api(client, stranger).post(f"projects/{project.pk}/attach-po", PO).status_code == 403
        assert Api(client, fin).post(f"projects/{project.pk}/attach-po", PO).status_code == 403
        assert Api(client, admin).post(f"projects/{project.pk}/attach-po", PO).status_code == 200


@pytest.mark.django_db
class TestWithoutPoList:
    def test_the_owner_lists_open_projects_without_a_po_with_their_age(
        self, client, tenant, project, owner, pm
    ):
        ProjectFactory(
            reference="WO-HAS",
            po_number="PO-1",
            manager=pm,
            contract_value=D("1"),
            cost_budget=D("1"),
        )

        rows = results(Api(client, owner).get("projects", po="none", status="OPEN"))

        assert [r["reference"] for r in rows] == ["WO-2001"]
        assert rows[0]["days_without_po"] == 0

    def test_a_project_created_with_its_po_has_no_waiting_time(self, client, owner, project):
        response = Api(client, owner).post(
            "projects",
            {"client": project.client_id, "po_number": "PO-NEW", "manager": project.manager_id,
             "contract_value": "10", "cost_budget": "5"},
        )

        assert response.status_code == 201, response.content
        assert response.json()["po_recorded_at"] == response.json()["opened_at"]
        assert response.json()["days_without_po"] is None

    def test_anyone_without_margin_rights_is_refused_the_list(self, client, project, pm, fin):
        assert Api(client, pm).get("projects", po="none").status_code == 403
        assert Api(client, fin).get("projects", has_po="false").status_code == 403

    def test_the_ordinary_project_list_is_unchanged_for_members(self, client, project, pm):
        assert [r["reference"] for r in results(Api(client, pm).get("projects"))] == ["WO-2001"]


@pytest.mark.django_db
class TestSiteDates:
    def test_the_pm_types_the_dates_and_a_date_alone_is_not_acceptance(
        self, client, project, site, pm
    ):
        row = row_of(project, site)
        http = Api(client, pm)

        response = http.patch(
            f"project-sites/{row.pk}", {"mobilised_on": "2026-09-01", "accepted_on": "2026-10-05"}
        )

        assert response.status_code == 200, response.content
        body = response.json()
        assert (body["mobilised_on"], body["accepted_on"]) == ("2026-09-01", "2026-10-05")
        assert body["is_accepted"] is False
        assert (body["site_ref"], body["project"], body["site"]) == ("SLV-1", project.pk, site.pk)

    def test_a_certificate_makes_it_accepted(self, client, project, site, pm):
        row = row_of(project, site)
        http = Api(client, pm)
        http.patch(f"project-sites/{row.pk}", {"accepted_on": "2026-10-05"})

        assert upload(http, SITE, row.pk, CERT).status_code == 201

        [seen] = results(http.get("project-sites", project=project.pk))
        assert seen["is_accepted"] is True

    def test_a_certificate_without_a_date_is_not_accepted_either(self, client, project, site, pm):
        row = row_of(project, site)
        http = Api(client, pm)
        upload(http, SITE, row.pk, CERT)

        [seen] = results(http.get("project-sites", project=project.pk))

        assert seen["is_accepted"] is False

    def test_who_may_type_the_dates(self, client, project, site, admin, stranger, fin):
        row = row_of(project, site)

        assert Api(client, admin).patch(f"project-sites/{row.pk}", {"mobilised_on": "2026-09-01"}
                                        ).status_code == 200
        for user in (stranger, fin):
            denied = Api(client, user).patch(
                f"project-sites/{row.pk}", {"mobilised_on": "2026-09-02"}
            )
            assert denied.status_code == 403
        assert Api(client, stranger).get("project-sites", project=project.pk).status_code == 200

    def test_only_the_dates_are_writable(self, client, project, site, pm):
        row = row_of(project, site)
        other = SiteFactory(client=project.client, internal_ref="SLV-2")

        Api(client, pm).patch(f"project-sites/{row.pk}", {"site": other.pk, "project": 999})

        row.refresh_from_db()
        assert (row.site_id, row.project_id) == (site.pk, project.pk)

    def test_the_change_is_audited(self, client, project, site, pm):
        row = row_of(project, site)

        Api(client, pm).patch(f"project-sites/{row.pk}", {"accepted_on": "2026-10-05"})

        assert AuditLog.objects.filter(
            action=AuditAction.DOCUMENT_AMENDED, note__startswith="Site dates changed"
        ).exists()

    def test_a_site_with_dates_cannot_be_taken_off_the_project(
        self, client, project, site, pm, admin
    ):
        row = row_of(project, site)
        row.mobilised_on = date(2026, 9, 1)
        row.save()
        keep = SiteFactory(client=project.client, internal_ref="SLV-9")

        refused = Api(client, admin).patch(f"projects/{project.pk}", {"sites": [keep.pk]})

        assert (refused.status_code, error_code(refused)) == (409, "SITE_HAS_PROJECT_DATA")
        assert list(project.sites.all()) == [site]

    def test_a_site_without_data_can_be_swapped(self, client, project, site, admin):
        keep = SiteFactory(client=project.client, internal_ref="SLV-9")

        ok = Api(client, admin).patch(f"projects/{project.pk}", {"sites": [keep.pk]})

        assert ok.status_code == 200, ok.content
        assert list(project.sites.all()) == [keep]
        assert ProjectSite.objects.get(project=project).organization_id == project.organization_id


@pytest.mark.django_db
class TestDerivedYardDates:
    @staticmethod
    def release(tenant, project, site, when, *, job_project=True, user=None):
        yard = YardFactory(name=f"Yard {when.day}")
        holder = user or UserFactory(organization=tenant)
        job = Job.objects.create(
            organization=tenant,
            reference=f"J-{when.isoformat()}-{site.pk}",
            client=site.client,
            site=site,
            project=project if job_project else None,
            assignee=holder,
        )
        return GateOut.objects.create(
            organization=tenant,
            purpose_type=GateOutPurpose.INSTALLATION,
            from_location=yard,
            site=site,
            job=job,
            custody_holder=holder,
            requested_by=holder,
            released_at=when,
        )

    def test_first_collection_and_last_dispatch_come_from_gate_outs(
        self, client, tenant, project, site, pm
    ):
        self.release(tenant, project, site, datetime(2026, 9, 3, 9, tzinfo=UTC))
        self.release(tenant, project, site, datetime(2026, 9, 20, 15, tzinfo=UTC))
        self.release(tenant, project, site, datetime(2026, 9, 10, 8, tzinfo=UTC))

        [seen] = results(Api(client, pm).get("project-sites", project=project.pk))

        assert utc(seen["first_collection_at"]) == datetime(2026, 9, 3, 9, tzinfo=UTC)
        assert utc(seen["last_dispatch_at"]) == datetime(2026, 9, 20, 15, tzinfo=UTC)

    def test_another_projects_or_unattributed_passes_do_not_count(
        self, client, tenant, project, site, pm
    ):
        rival = ProjectFactory(reference="WO-RIVAL", manager=pm)
        link(rival, site)
        self.release(tenant, rival, site, datetime(2026, 8, 1, 9, tzinfo=UTC))
        self.release(
            tenant, project, site, datetime(2026, 8, 2, 9, tzinfo=UTC), job_project=False
        )
        self.release(tenant, project, site, datetime(2026, 9, 5, 9, tzinfo=UTC))

        [seen] = results(Api(client, pm).get("project-sites", project=project.pk))

        assert utc(seen["first_collection_at"]).date() == date(2026, 9, 5)
        assert utc(seen["last_dispatch_at"]).date() == date(2026, 9, 5)

    def test_an_unreleased_site_has_neither(self, client, project, site, pm):
        [seen] = results(Api(client, pm).get("project-sites", project=project.pk))

        assert (seen["first_collection_at"], seen["last_dispatch_at"]) == (None, None)

    def test_the_query_count_does_not_grow_with_the_sites(self, client, tenant, project, site, pm):
        http = Api(client, pm)
        http.get("project-sites", project=project.pk)  # warm caches and logins
        with CaptureQueriesContext(connection) as one:
            http.get("project-sites", project=project.pk)
        for n in range(4):
            extra = SiteFactory(client=project.client, internal_ref=f"SLV-X{n}")
            link(project, extra)
            self.release(tenant, project, extra, datetime(2026, 9, 1 + n, 9, tzinfo=UTC))

        with CaptureQueriesContext(connection) as many:
            rows = results(http.get("project-sites", project=project.pk))

        assert len(rows) == 5
        assert len(many) == len(one)


@pytest.mark.django_db
class TestAttachmentRules:
    def test_the_po_document_is_the_pm_catalogue_or_finance(
        self, client, project, pm, admin, fin, stranger
    ):
        for user in (pm, admin, fin):
            assert upload(Api(client, user), PROJECT, project.pk, "PO").status_code == 201
        denied = upload(Api(client, stranger), PROJECT, project.pk, "PO")
        assert denied.status_code == 403

    def test_the_certificate_is_the_pm_or_catalogue_only(
        self, client, project, site, pm, admin, fin, stranger
    ):
        row = row_of(project, site)

        for user in (pm, admin):
            assert upload(Api(client, user), SITE, row.pk, CERT).status_code == 201
        for user in (fin, stranger):
            assert upload(Api(client, user), SITE, row.pk, CERT).status_code == 403

    def test_a_pm_of_another_project_may_not(self, client, tenant, project, site):
        rival_pm = person(tenant, "Rita Rival")
        ProjectFactory(reference="WO-OTHER", manager=rival_pm)
        row = row_of(project, site)

        assert upload(Api(client, rival_pm), SITE, row.pk, CERT).status_code == 403
        assert upload(Api(client, rival_pm), PROJECT, project.pk, "PO").status_code == 403

    def test_the_subcontract_is_the_pm_or_finance(self, client, tenant, project, pm, fin, admin):
        contract = Subcontract.objects.create(
            organization=tenant,
            project=project,
            subcontractor=Subcontractor.objects.create(organization=tenant, name="Civil Co"),
            contract_value=D("1000"),
            created_by=pm,
        )

        for user in (pm, fin):
            sent = upload(Api(client, user), "commercials.Subcontract", contract.pk)
            assert sent.status_code == 201
        assert upload(Api(client, admin), "commercials.Subcontract", contract.pk).status_code == 403

    def test_a_reference_document_stays_replaceable_by_its_uploader(self, client, project, pm):
        http = Api(client, pm)
        first = upload(http, PROJECT, project.pk, "PO").json()

        assert http.delete(f"attachments/{first['id']}").status_code == 204

    def test_a_subcontract_payment_invoice_is_the_recorders_while_pending(
        self, client, tenant, project, pm, fin, admin
    ):
        contract = Subcontract.objects.create(
            organization=tenant,
            project=project,
            subcontractor=Subcontractor.objects.create(organization=tenant, name="Civil Co"),
            contract_value=D("1000"),
            created_by=pm,
        )
        payment = SubcontractPayment.objects.create(
            organization=tenant,
            subcontract=contract,
            amount=D("100"),
            paid_on=date(2026, 10, 1),
            reference="MP1",
            recorded_by=fin,
        )
        label = "commercials.SubcontractPayment"

        assert upload(Api(client, admin), label, payment.pk, "Invoice").status_code == 403
        mine = upload(Api(client, fin), label, payment.pk, "Invoice")
        assert mine.status_code == 201
        payment.status = "APPROVED"
        payment.decided_by = pm
        payment.decided_at = datetime(2026, 10, 2, tzinfo=UTC)
        payment.save()
        locked = upload(Api(client, fin), label, payment.pk, "Invoice")
        assert (locked.status_code, error_code(locked)) == (409, "ATTACHMENT_LOCKED")
        assert Api(client, fin).delete(f"attachments/{mine.json()['id']}").status_code == 409

    def test_a_site_purchase_receipt_is_the_recorders_while_open(
        self, client, tenant, project, site, pm, fin
    ):
        purchase = SitePurchase.objects.create(
            organization=tenant,
            project=project,
            site=site,
            purchase_date=date(2026, 10, 1),
            amount=D("500"),
            recorded_by=pm,
        )
        label = "commercials.SitePurchase"

        assert upload(Api(client, fin), label, purchase.pk, "Receipt").status_code == 403
        assert upload(Api(client, pm), label, purchase.pk, "Receipt").status_code == 201

    def test_the_milestone_invoice_target_is_listed_for_finance(self, client, fin, stranger):
        targets = Api(client, fin).get("attachment-targets").json()["targets"]
        denied = Api(client, stranger).get("attachment-targets").json()["targets"]

        assert targets["commercials.MilestoneInvoice"] is True
        assert denied["commercials.MilestoneInvoice"] is False
        assert denied[PROJECT] is True  # "the PM" is decided on the record, not here
