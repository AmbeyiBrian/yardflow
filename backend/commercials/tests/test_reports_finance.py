"""The three finance reports that follow a bad margin (§10; O4, O12, O16).

Each is checked against the costing module rather than against numbers typed
into the test: the expenses ledger's total must be the expense figure the
performance report shows, subcontractor spend must count what the project cost
counts, and the months of the budget report must add up to cost to date. A
report that disagreed with the figure beside it would be worse than no report.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from accounts.factories import UserFactory
from commercials.costing import cost_for
from commercials.models import ExpenseCategory, ProjectExpense
from commercials.services import decide_expense
from jobs.models import DeliveryMode, Job, JobStatus
from network.factories import ProjectFactory, SiteFactory
from network.models import Subcontractor
from reporting.framework import get_report, parse_params


def run(slug: str, raw: dict) -> dict:
    """What the API returns: the view parses the filters, then renders.

    `render` gives dict rows with formatted values, which is what a screen or a
    test reads. `table` gives the export's ordered cells and is not for reading
    by key.
    """
    report = get_report(slug)
    return report.render(parse_params(report, raw))


@pytest.fixture
def manager(tenant):
    return UserFactory(organization=tenant, full_name="Pippa Manager")


@pytest.fixture
def technician(tenant):
    return UserFactory(organization=tenant, full_name="Tom Technician")


@pytest.fixture
def project(tenant, manager):
    return ProjectFactory(
        reference="WO-7701",
        po_number="PO-770",
        manager=manager,
        contract_value=Decimal("500000.00"),
        cost_budget=Decimal("60000.00"),
    )


def _job(tenant, project, technician, reference, *, ref_no):
    site = SiteFactory(internal_ref=ref_no, name="Ruiru")
    return Job.objects.create(
        organization=tenant,
        reference=reference,
        client=site.client,
        site=site,
        project=project,
        assignee=technician,
    )


def _expense(tenant, project, technician, amount, on, *, category="Transport"):
    return ProjectExpense.objects.create(
        organization=tenant,
        project=project,
        category=ExpenseCategory.objects.get_or_create(organization=tenant, name=category)[0],
        amount=Decimal(amount),
        incurred_on=on,
        recorded_by=technician,
    )


def _subcontract(tenant, job, name, price, *, closed_on=None):
    job.delivery_mode = DeliveryMode.SUBCONTRACTED
    job.subcontractor, _ = Subcontractor.objects.get_or_create(organization=tenant, name=name)
    job.agreed_price = Decimal(price)
    job.save()
    if closed_on is not None:
        job.status = JobStatus.CLOSED
        job.closed_at = datetime(closed_on.year, closed_on.month, closed_on.day, 12, tzinfo=UTC)
        job.save()
    return job


@pytest.mark.django_db
class TestExpensesLedger:
    def test_every_claim_is_a_row_and_only_approved_ones_total(
        self, tenant, project, technician, manager
    ):
        approved = _expense(tenant, project, technician, "4500.00", date(2026, 5, 2))
        decide_expense(approved, actor=manager, approved=True)
        rejected = _expense(tenant, project, technician, "9999.00", date(2026, 5, 3))
        decide_expense(rejected, actor=manager, approved=False, reason="No receipt.")
        _expense(tenant, project, technician, "120.00", date(2026, 5, 4))  # still waiting

        table = run("expenses-ledger", {})

        # The display wording belongs to the model; the test pins the meaning.
        # Rendered rows carry the model's own wording for each status; the test
        # pins that all three outcomes appear, not how each is phrased.
        statuses = [row["status"] for row in table["rows"]]
        assert len(statuses) == 3
        assert sum(status.startswith("Approved") for status in statuses) == 1
        assert sum(status.startswith("Rejected") for status in statuses) == 1
        # The total is the figure the performance report shows, and nothing else.
        assert table["totals"]["amount"] == "4,500.00"
        assert Decimal(table["totals"]["amount"].replace(",", "")) == cost_for(project).expenses

    def test_the_status_filter_narrows(self, tenant, project, technician, manager):
        rejected = _expense(tenant, project, technician, "9999.00", date(2026, 5, 3))
        decide_expense(rejected, actor=manager, approved=False, reason="No.")
        _expense(tenant, project, technician, "1.00", date(2026, 5, 4))

        table = run("expenses-ledger", {"status": "REJECTED"})

        assert [row["status"].split(" ")[0] for row in table["rows"]] == ["Rejected"]

    def test_a_date_window_applies_to_when_it_was_incurred(
        self, tenant, project, technician
    ):
        _expense(tenant, project, technician, "1.00", date(2026, 4, 30))
        _expense(tenant, project, technician, "2.00", date(2026, 5, 15))

        table = run("expenses-ledger",
            {"date_from": "2026-05-01", "date_to": "2026-05-31"}
        )

        assert [row["amount"] for row in table["rows"]] == ["2.00"]


@pytest.mark.django_db
class TestSubcontractorSpend:
    def test_delivered_follows_the_costing_rule_and_open_is_committed(
        self, tenant, project, technician
    ):
        done = _job(tenant, project, technician, "JOB-S1", ref_no="SLV-1")
        _subcontract(tenant, done, "Rigging Co", "45000.00", closed_on=date(2026, 5, 20))
        pending = _job(tenant, project, technician, "JOB-S2", ref_no="SLV-2")
        _subcontract(tenant, pending, "Rigging Co", "30000.00")

        table = run("subcontractor-spend", {})

        (row,) = table["rows"]
        assert row["name"] == "Rigging Co"
        assert row["jobs_total"] == "2"
        assert row["jobs_closed"] == "1"
        # What the project cost counts (O11) — closed jobs only.
        assert row["delivered"] == "45,000.00"
        assert Decimal(row["delivered"].replace(",", "")) == cost_for(project).subcontractor
        # Money already promised on work not yet delivered.
        assert row["committed"] == "30,000.00"

    def test_deactivated_contractors_are_hidden_unless_asked_for(
        self, tenant, project, technician
    ):
        job = _job(tenant, project, technician, "JOB-S3", ref_no="SLV-3")
        _subcontract(tenant, job, "Gone Ltd", "1000.00", closed_on=date(2026, 5, 1))
        Subcontractor.objects.filter(name="Gone Ltd").update(is_active=False)

        assert run("subcontractor-spend", {})["rows"] == []
        assert len(run("subcontractor-spend", {"register": "all"})["rows"]) == 1


@pytest.mark.django_db
class TestBudgetByMonth:
    def test_months_add_up_to_cost_to_date_and_show_when_it_went_over(
        self, tenant, project, technician, manager
    ):
        # Budget is 60,000. May: 4,500 expense. June: 45,000 subcontract.
        # July: 20,000 subcontract — that is the month it goes over.
        approved = _expense(tenant, project, technician, "4500.00", date(2026, 5, 2))
        decide_expense(approved, actor=manager, approved=True)
        _subcontract(
            tenant,
            _job(tenant, project, technician, "JOB-B1", ref_no="SLV-11"),
            "Rigging Co",
            "45000.00",
            closed_on=date(2026, 6, 10),
        )
        _subcontract(
            tenant,
            _job(tenant, project, technician, "JOB-B2", ref_no="SLV-12"),
            "Rigging Co",
            "20000.00",
            closed_on=date(2026, 7, 3),
        )

        table = run("budget-by-month", {"project": project.pk})
        rows = table["rows"]

        assert [row["month"] for row in rows] == ["2026-05", "2026-06", "2026-07"]
        assert [row["month_cost"] for row in rows] == ["4,500.00", "45,000.00", "20,000.00"]
        assert [row["cumulative"] for row in rows] == ["4,500.00", "49,500.00", "69,500.00"]
        assert [row["is_over_budget"] for row in rows] == ["no", "no", "yes"]
        # The months are the performance figure, not a different one.
        assert Decimal(rows[-1]["cumulative"].replace(",", "")) == cost_for(project).total

    def test_a_window_hides_rows_without_restarting_the_running_total(
        self, tenant, project, technician, manager
    ):
        approved = _expense(tenant, project, technician, "4500.00", date(2026, 5, 2))
        decide_expense(approved, actor=manager, approved=True)
        _subcontract(
            tenant,
            _job(tenant, project, technician, "JOB-B3", ref_no="SLV-13"),
            "Rigging Co",
            "45000.00",
            closed_on=date(2026, 6, 10),
        )

        rows = run("budget-by-month",
            {"project": project.pk, "date_from": "2026-06-01"}
        )["rows"]

        assert [row["month"] for row in rows] == ["2026-06"]
        # May is hidden, but June's cost to date still includes it.
        assert rows[0]["cumulative"] == "49,500.00"


@pytest.mark.django_db
class TestCategories:
    def test_every_report_belongs_to_a_known_group(self, tenant):
        from reporting.framework import CATEGORIES, all_reports

        for report in all_reports():
            assert report.category in CATEGORIES, report.slug
        assert {r.category for r in all_reports() if r.slug.startswith("stock-valuation")} == {
            "Finance"
        }
