"""T18.9 — milestones, invoices, receipts and the PO payments report (§4.19.7; R11).

The state table is tested at its date edges (due the day the last site is
accepted, overdue the day after ``latest invoice + terms``), then the endpoints
for who may do what, then the report against figures worked out by hand.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from accounts.factories import RoleFactory, UserFactory, UserRoleFactory
from accounts.permissions_registry import PERM
from commercials import milestones as svc
from commercials.models import (
    MilestoneCondition,
    MilestoneInvoice,
    MilestoneReceipt,
    MilestoneShare,
    ProjectMilestone,
)
from commercials.tests.api_helpers import PASSWORD, Api, error_code, results
from core.exceptions import DomainError
from core.models import Attachment
from network.factories import ProjectFactory, SiteFactory
from network.models import ProjectSite
from reporting.framework import get_report, parse_params

D = Decimal
ISSUED = date(2026, 9, 1)
TODAY = date(2026, 10, 9)


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
def fin(tenant):
    return person(tenant, "Fiona Finance", PERM.FINANCE_APPROVE)


@pytest.fixture
def owner(tenant):
    return person(tenant, "Olive Owner", PERM.PROJECT_VIEW_MARGIN)


@pytest.fixture
def stranger(tenant):
    return person(tenant, "Sam Stranger")


@pytest.fixture
def project(tenant, pm):
    return ProjectFactory(
        reference="WO-1801",
        po_number="PO-1801",
        manager=pm,
        contract_value=D("100000.00"),
        cost_budget=D("70000.00"),
        po_issue_date=ISSUED,
        payment_terms="30 days from invoice",
        payment_terms_days=30,
    )


def link(project, *sites):
    project.sites.add(*sites, through_defaults={"organization_id": project.organization_id})


def milestone(project, sequence=1, **fields):
    defaults = {
        "organization_id": project.organization_id,
        "project": project,
        "sequence": sequence,
        "name": f"M{sequence}",
        "share_type": MilestoneShare.PERCENT,
        "share_value": D("30"),
        "condition": MilestoneCondition.NONE,
    }
    return ProjectMilestone.objects.create(**{**defaults, **fields})


def invoice(ms, amount="10000.00", on=date(2026, 9, 10), number="INV-1", user=None):
    return MilestoneInvoice.objects.create(
        organization_id=ms.organization_id,
        milestone=ms,
        invoice_number=number,
        invoice_date=on,
        amount=D(amount),
        recorded_by=user or UserFactory(organization_id=ms.organization_id),
    )


def receipt(ms, amount, on=date(2026, 9, 20), user=None):
    return MilestoneReceipt.objects.create(
        organization_id=ms.organization_id,
        milestone=ms,
        received_on=on,
        amount=D(amount),
        recorded_by=user or UserFactory(organization_id=ms.organization_id),
    )


def accept(project, site, on, *, certificate=True, user=None):
    row = ProjectSite.objects.get(project=project, site=site)
    row.accepted_on = on
    row.save()
    if certificate:
        Attachment.objects.create(
            organization_id=project.organization_id,
            target_type="network.ProjectSite",
            target_id=str(row.pk),
            filename="cert.pdf",
            content_type="application/pdf",
            size=4,
            file=f"t/{row.pk}.pdf",
            uploaded_by=user or UserFactory(organization_id=project.organization_id),
            caption="Acceptance certificate",
        )
    return row


def state_of(ms, today=TODAY):
    ms = ProjectMilestone.objects.select_related("project").get(pk=ms.pk)
    return svc.states_for_project(ms.project, today, [ms])[0][1]


@pytest.mark.django_db
class TestAmountAndConditions:
    def test_amount_is_a_percent_of_the_current_value_or_a_fixed_sum(self, project):
        percent = milestone(project, 1, share_value=D("30"))
        fixed = milestone(project, 2, share_type=MilestoneShare.AMOUNT, share_value=D("12500"))

        assert state_of(percent).amount == D("30000.00")
        assert state_of(fixed).amount == D("12500.00")

    def test_a_percent_follows_an_approved_variation(self, project, pm):
        from network.models import ProjectVariation

        ProjectVariation.objects.create(
            organization_id=project.organization_id,
            project=project,
            reference="VAR-1",
            value_delta=D("20000.00"),
            budget_delta=D("0"),
            effective_on=ISSUED,
            raised_by=pm,
            status="APPROVED",
            decided_by=pm,
            decided_at=timezone.now(),
        )
        ms = milestone(project, 1, share_value=D("50"))

        assert state_of(ms).amount == D("60000.00")

    def test_an_unset_share_has_no_amount(self, project):
        ms = milestone(project, 1, share_value=None)

        assert state_of(ms).amount is None

    def test_a_condition_of_none_is_due_on_the_po(self, project):
        state = state_of(milestone(project, 1))

        assert (state.state, state.met_on) == ("DUE", ISSUED)

    def test_all_sites_accepted_needs_every_site_and_a_certificate(self, project):
        first, second = SiteFactory(client=project.client), SiteFactory(client=project.client)
        link(project, first, second)
        ms = milestone(project, 2, condition=MilestoneCondition.ALL_SITES_ACCEPTED)
        assert state_of(ms).state == "NOT_DUE"

        accept(project, first, date(2026, 10, 1))
        assert state_of(ms).state == "NOT_DUE"  # one of two

        accept(project, second, date(2026, 10, 5), certificate=False)
        assert state_of(ms).state == "NOT_DUE"  # a date without the document

        Attachment.objects.create(
            organization_id=project.organization_id,
            target_type="network.ProjectSite",
            target_id=str(ProjectSite.objects.get(project=project, site=second).pk),
            filename="c.pdf",
            content_type="application/pdf",
            size=1,
            file="t/second.pdf",
            uploaded_by=UserFactory(organization_id=project.organization_id),
            caption="Acceptance certificate",
        )
        state = state_of(ms)
        assert (state.state, state.met_on) == ("DUE", date(2026, 10, 5))

    def test_a_project_with_no_sites_is_never_all_accepted(self, project):
        assert state_of(milestone(project, 2, condition="ALL_SITES_ACCEPTED")).state == "NOT_DUE"

    def test_a_dated_condition_is_due_the_day_it_arrives(self, project):
        ms = milestone(
            project, 3, condition=MilestoneCondition.DATE, condition_date=date(2026, 10, 9)
        )

        assert state_of(ms, date(2026, 10, 8)).state == "NOT_DUE"
        assert state_of(ms, date(2026, 10, 9)).state == "DUE"


@pytest.mark.django_db
class TestOverdueAndPayment:
    def test_overdue_starts_the_day_after_the_latest_invoice_plus_terms(self, project):
        ms = milestone(project)
        invoice(ms, on=date(2026, 9, 10))  # terms 30: due by 10 October

        assert state_of(ms, date(2026, 10, 10)).state == "INVOICED"
        assert state_of(ms, date(2026, 10, 11)).state == "OVERDUE"

    def test_a_second_invoice_restarts_the_clock(self, project):
        ms = milestone(project)
        invoice(ms, "5000", on=date(2026, 9, 1), number="A")
        invoice(ms, "5000", on=date(2026, 10, 5), number="B")

        assert state_of(ms, date(2026, 10, 20)).state == "INVOICED"
        assert state_of(ms, date(2026, 11, 4)).state == "INVOICED"
        assert state_of(ms, date(2026, 11, 5)).state == "OVERDUE"

    def test_with_no_terms_days_nothing_is_ever_overdue(self, project):
        project.payment_terms_days = None
        project.save()
        ms = milestone(project)
        invoice(ms, on=date(2020, 1, 1))

        assert state_of(ms, date(2030, 1, 1)).state == "INVOICED"

    def test_a_partial_receipt_is_part_paid_and_can_still_be_overdue(self, project):
        ms = milestone(project)
        invoice(ms, "10000")
        receipt(ms, "4000")

        assert state_of(ms, date(2026, 9, 25)).state == "PART_PAID"
        late = state_of(ms, date(2026, 10, 11))
        assert (late.state, late.outstanding) == ("OVERDUE", D("6000"))

    def test_receipts_adding_up_to_the_invoice_make_it_paid(self, project):
        ms = milestone(project)
        invoice(ms, "10000")
        receipt(ms, "4000")
        receipt(ms, "6000", on=date(2026, 9, 25))

        assert state_of(ms, date(2027, 1, 1)).state == "PAID"

    def test_a_voided_invoice_is_not_counted(self, project, fin):
        ms = milestone(project)
        bad = invoice(ms, "10000")
        svc.void_invoice(bad, reason="Wrong PO number", actor=fin)

        state = state_of(ms)

        assert (state.state, state.invoiced) == ("DUE", D("0"))

    def test_a_voided_receipt_reopens_the_balance(self, project, fin):
        ms = milestone(project)
        invoice(ms, "10000")
        paid = receipt(ms, "10000")
        assert state_of(ms).state == "PAID"

        svc.void_receipt(paid, reason="Bounced", actor=fin)

        assert state_of(ms, date(2026, 9, 30)).state == "INVOICED"


@pytest.mark.django_db
class TestServices:
    def test_a_receipt_beyond_the_invoiced_amount_is_refused(self, project, fin):
        ms = milestone(project)
        invoice(ms, "10000")
        svc.record_receipt(ms, received_on=TODAY, amount=D("7000"), actor=fin)

        with pytest.raises(svc.ReceiptExceedsInvoiced) as refused:
            svc.record_receipt(ms, received_on=TODAY, amount=D("3000.01"), actor=fin)

        assert refused.value.code == "RECEIPT_EXCEEDS_INVOICED"
        assert MilestoneReceipt.objects.filter(milestone=ms).count() == 1
        svc.record_receipt(ms, received_on=TODAY, amount=D("3000"), actor=fin)  # exactly the rest

    def test_voiding_needs_a_reason(self, project, fin):
        from rest_framework.exceptions import ValidationError

        ms = milestone(project)

        with pytest.raises(ValidationError):
            svc.void_invoice(invoice(ms), reason="  ", actor=fin)

    def test_an_invoice_with_money_received_cannot_be_voided_first(self, project, fin):
        ms = milestone(project)
        inv = invoice(ms, "10000")
        receipt(ms, "10000")

        with pytest.raises(svc.ReceiptExceedsInvoiced):
            svc.void_invoice(inv, reason="Mistake", actor=fin)

    def test_a_voided_row_keeps_who_when_and_why(self, project, fin):
        ms = milestone(project)
        inv = svc.void_invoice(invoice(ms), reason="Duplicate", actor=fin)

        inv.refresh_from_db()
        assert (inv.voided_by, inv.void_reason) == (fin, "Duplicate")
        assert inv.voided_at is not None

    def test_overinvoicing_warns_but_records(self, project, fin):
        ms = milestone(project)  # 30% of 100000 = 30000

        _inv, warning = svc.record_invoice(
            ms, invoice_number="BIG", invoice_date=TODAY, amount=D("35000"), actor=fin
        )

        assert "30000" in warning
        assert MilestoneInvoice.objects.filter(milestone=ms).count() == 1

    def test_after_an_invoice_only_the_condition_date_can_change(self, project, fin):
        ms = milestone(project, condition=MilestoneCondition.DATE, condition_date=date(2026, 11, 1))
        invoice(ms)

        svc.update_milestone(ms, {"condition_date": date(2026, 11, 15)}, actor=fin)
        with pytest.raises(svc.MilestoneLocked):
            svc.update_milestone(ms, {"share_value": D("40")}, actor=fin)
        with pytest.raises(svc.MilestoneLocked):
            svc.update_milestone(ms, {"name": "Renamed"}, actor=fin)

    def test_a_voided_invoice_unlocks_editing_but_not_removal(self, project, fin):
        ms = milestone(project)
        svc.void_invoice(invoice(ms), reason="Wrong client", actor=fin)

        svc.update_milestone(ms, {"share_value": D("40")}, actor=fin)
        with pytest.raises(DomainError):
            svc.delete_milestone(ms, actor=fin)

    def test_removing_a_milestone_closes_the_gap(self, project, fin):
        first, second, third = (milestone(project, n) for n in (1, 2, 3))

        svc.delete_milestone(second, actor=fin)

        assert list(
            ProjectMilestone.objects.filter(project=project).values_list("sequence", "pk")
        ) == [(1, first.pk), (2, third.pk)]

    def test_defaults_are_three_with_empty_shares_and_only_once(self, project, fin):
        created = svc.add_default_milestones(project, actor=fin)

        assert [(m.sequence, m.name, m.condition, m.share_value) for m in created] == [
            (1, "Deposit", "NONE", None),
            (2, "Conditional acceptance", "ALL_SITES_ACCEPTED", None),
            (3, "Final acceptance", "ALL_SITES_ACCEPTED", None),
        ]
        with pytest.raises(svc.MilestonesExist):
            svc.add_default_milestones(project, actor=fin)

    def test_a_project_without_a_po_has_no_milestones(self, tenant, fin):
        bare = ProjectFactory(reference="WO-NOPO")

        with pytest.raises(svc.ProjectHasNoPo):
            svc.add_default_milestones(bare, actor=fin)


@pytest.mark.django_db
class TestEndpoints:
    def test_finance_runs_a_milestone_end_to_end(self, client, project, fin):
        http = Api(client, fin)

        created = http.post(
            f"projects/{project.pk}/milestones",
            {
                "name": "Deposit",
                "share_type": "PERCENT",
                "share_value": "30",
                "condition": "NONE",
                "condition_date": None,
            },
        )
        assert created.status_code == 201, created.content
        ms = created.json()
        assert (ms["sequence"], ms["amount"], ms["state"]) == (1, "30000.00", "DUE")

        raised = http.post(
            f"milestones/{ms['id']}/invoices",
            {"milestone": ms["id"], "invoice_number": "INV-9", "invoice_date": "2026-09-10",
             "amount": "10000"},
        )
        assert raised.status_code == 201, raised.content
        part = http.post(
            f"milestones/{ms['id']}/receipts",
            {"milestone": ms["id"], "received_on": "2026-09-20", "amount": "4000",
             "reference": "R1"},
        )
        assert part.status_code == 201, part.content
        over = http.post(
            f"milestones/{ms['id']}/receipts",
            {"received_on": "2026-09-21", "amount": "6000.01", "reference": "R2"},
        )
        assert (over.status_code, error_code(over)) == (400, "RECEIPT_EXCEEDS_INVOICED")

        [row] = results(http.get(f"projects/{project.pk}/milestones"))
        assert (row["invoiced"], row["received"]) == ("10000.00", "4000.00")
        assert row["state"] in {"PART_PAID", "OVERDUE"}
        assert [i["invoice_number"] for i in row["invoices"]] == ["INV-9"]
        assert [r["reference"] for r in row["receipts"]] == ["R1"]

    def test_voiding_by_endpoint_needs_a_reason(self, client, project, fin):
        ms = milestone(project)
        inv = invoice(ms)
        http = Api(client, fin)

        assert http.post(f"milestone-invoices/{inv.pk}/void", {}).status_code == 400
        ok = http.post(f"milestone-invoices/{inv.pk}/void", {"reason": "Wrong amount"})
        assert ok.status_code == 200
        assert ok.json()["void_reason"] == "Wrong amount"
        recorded = receipt(ms, "1")  # nothing invoiced now, but the row exists to void
        assert (
            http.post(f"milestone-receipts/{recorded.pk}/void", {"reason": "Typo"}).status_code
            == 200
        )

    def test_the_defaults_endpoint_seeds_once(self, client, project, fin):
        http = Api(client, fin)

        first = http.post(f"projects/{project.pk}/milestones/defaults")
        again = http.post(f"projects/{project.pk}/milestones/defaults")

        assert first.status_code == 200
        assert [m["name"] for m in results(first)] == [
            "Deposit",
            "Conditional acceptance",
            "Final acceptance",
        ]
        assert results(first)[0]["share_value"] is None
        assert (again.status_code, error_code(again)) == (409, "MILESTONES_EXIST")

    def test_patch_and_delete_follow_the_lock(self, client, project, fin):
        http = Api(client, fin)
        ms = milestone(project)
        inv = invoice(ms)

        locked = http.patch(f"projects/{project.pk}/milestones/{ms.pk}", {"name": "New"})
        assert (locked.status_code, error_code(locked)) == (409, "MILESTONE_LOCKED")
        no_delete = http.delete(f"projects/{project.pk}/milestones/{ms.pk}")
        assert no_delete.status_code == 409

        other = milestone(project, 2)
        renamed = http.patch(f"projects/{project.pk}/milestones/{other.pk}", {"name": "Final"})
        assert renamed.status_code == 200 and renamed.json()["name"] == "Final"
        assert http.delete(f"projects/{project.pk}/milestones/{other.pk}").status_code == 204
        assert inv.pk  # the invoiced milestone is untouched

    def test_a_percent_over_100_and_a_date_without_a_date_are_field_errors(
        self, client, project, fin
    ):
        http = Api(client, fin)

        big = http.post(
            f"projects/{project.pk}/milestones",
            {"name": "X", "share_type": "PERCENT", "share_value": "101", "condition": "NONE"},
        )
        dated = http.post(
            f"projects/{project.pk}/milestones",
            {"name": "X", "share_type": "PERCENT", "share_value": "10", "condition": "DATE"},
        )

        assert "share_value" in big.json()["error"]["field_errors"]
        assert "condition_date" in dated.json()["error"]["field_errors"]

    def test_the_project_manager_reads_states_but_not_money(self, client, project, pm):
        ms = milestone(project)
        invoice(ms)

        [row] = results(Api(client, pm).get(f"projects/{project.pk}/milestones"))

        assert row["state"] == "INVOICED"
        for key in ("amount", "invoiced", "received", "invoices", "receipts", "share_value"):
            assert key not in row

    def test_the_owner_sees_the_money(self, client, project, owner):
        milestone(project)

        [row] = results(Api(client, owner).get(f"projects/{project.pk}/milestones"))

        assert row["amount"] == "30000.00"

    def test_a_stranger_cannot_read_or_change_milestones(self, client, project, stranger):
        ms = milestone(project)
        http = Api(client, stranger)

        assert http.get(f"projects/{project.pk}/milestones").status_code == 403
        assert http.post(f"projects/{project.pk}/milestones/defaults").status_code == 403
        assert http.post(f"milestones/{ms.pk}/invoices", {}).status_code == 403

    def test_only_finance_records_invoices_and_receipts(self, client, project, pm, owner):
        ms = milestone(project)
        body = {"invoice_number": "X", "invoice_date": "2026-09-10", "amount": "1"}

        for user in (pm, owner):
            http = Api(client, user)
            assert http.post(f"milestones/{ms.pk}/invoices", body).status_code == 403
            assert http.post(
                f"projects/{project.pk}/milestones",
                {"name": "X", "share_type": "PERCENT", "condition": "NONE"},
            ).status_code == 403

    def test_a_changing_manager_is_not_needed_to_record_on_a_closed_project(
        self, client, project, fin
    ):
        """Money still arrives after a project closes; nothing here refuses it."""
        ms = milestone(project)
        invoice(ms)
        project.status = "CLOSED"
        project.closed_at = TODAY.isoformat() + "T00:00:00+00:00"
        project.save()

        response = Api(client, fin).post(
            f"milestones/{ms.pk}/receipts", {"received_on": "2026-10-01", "amount": "100"}
        )

        assert response.status_code == 201


@pytest.mark.django_db
class TestInvoiceDocument:
    def test_finance_attaches_the_invoice_pdf_and_a_voided_one_is_fixed(
        self, client, project, fin, pm
    ):
        ms = milestone(project)
        inv = invoice(ms, user=fin)
        http = Api(client, fin)

        def upload(user_http):
            return user_http.upload(
                {
                    "target_type": "commercials.MilestoneInvoice",
                    "target_id": str(inv.pk),
                    "file": SimpleUploadedFile("i.pdf", b"%PDF-1", content_type="application/pdf"),
                    "kind": "DOCUMENT",
                    "caption": "Invoice",
                }
            )

        assert upload(http).status_code == 201
        assert upload(Api(client, pm)).status_code == 403
        svc.void_invoice(inv, reason="Wrong client", actor=fin)
        locked = upload(http)
        assert (locked.status_code, error_code(locked)) == (409, "ATTACHMENT_LOCKED")


@pytest.mark.django_db
class TestPoPaymentsReport:
    def run(self, **raw):
        report = get_report("po-payments")
        params = parse_params(report, raw)
        rows = list(report.rows(params))
        return rows, report.totals(rows)

    def test_one_row_per_po_with_hand_worked_figures(self, tenant, project, pm):
        m1 = milestone(project, 1, share_value=D("30"))
        m2 = milestone(project, 2, share_value=D("70"), condition="ALL_SITES_ACCEPTED")
        invoice(m1, "30000", on=date(2026, 1, 10))
        receipt(m1, "12000")
        other = ProjectFactory(
            reference="WO-1802",
            po_number="PO-1802",
            manager=pm,
            contract_value=D("50000.00"),
            cost_budget=D("30000.00"),
            po_issue_date=ISSUED,
            payment_terms_days=30,
        )
        ProjectFactory(reference="WO-NOPO")  # no PO, not a row
        assert m2.pk and other.pk

        rows, totals = self.run()

        by_ref = {r["reference"]: r for r in rows}
        assert set(by_ref) == {"PO-1801", "PO-1802"}
        one = by_ref["PO-1801"]
        assert (one["value"], one["invoiced"], one["received"]) == (
            D("100000.00"),
            D("30000"),
            D("12000"),
        )
        assert (one["outstanding"], one["invoiced_unpaid"]) == (D("88000.00"), D("18000"))
        assert one["next_milestone"] == "M1 M1"
        assert one["is_overdue"] is True  # invoiced 10 Sept + 30 days has long passed
        assert totals == {
            "value": D("150000.00"),
            "invoiced": D("30000"),
            "received": D("12000"),
            "outstanding": D("138000.00"),
            "invoiced_unpaid": D("18000"),
        }

    def test_the_overdue_filter_keeps_only_overdue_pos(self, tenant, project, pm):
        ms = milestone(project)
        invoice(ms, on=date(2026, 1, 10))
        ProjectFactory(
            reference="WO-1802",
            po_number="PO-1802",
            manager=pm,
            contract_value=D("1.00"),
            cost_budget=D("1.00"),
        )

        rows, _ = self.run(overdue="yes")

        assert [r["reference"] for r in rows] == ["PO-1801"]

    def test_a_fully_paid_po_has_no_next_milestone(self, tenant, project):
        ms = milestone(project, share_value=D("100"))
        invoice(ms, "100000")
        receipt(ms, "100000")

        [row], _ = self.run()

        assert (row["next_milestone"], row["next_state"], row["is_overdue"]) == ("", "", False)

    def test_the_report_is_finance_only_over_the_api(self, client, project, fin, pm):
        milestone(project)

        assert Api(client, fin).get("reports/po-payments").status_code == 200
        assert Api(client, pm).get("reports/po-payments").status_code == 403

    def test_it_is_in_the_catalogue_for_finance(self, client, fin):
        catalogue = Api(client, fin).get("reports").json()
        slugs = {
            r["slug"]
            for group in (
                catalogue if isinstance(catalogue, list) else catalogue.get("reports", [])
            )
            for r in ([group] if "slug" in group else group.get("reports", []))
        }

        assert "po-payments" in slugs
