"""T7.1–T7.5 — the report framework, the report set and the exports (§10; M1, M2).

Two criteria are structural rather than behavioural, and both get a test that
would fail if somebody broke the structure:

* T7.1 — "adding a report requires **no changes to the export code**"
* T7.3 — "the consumption report **reconciles with T5.7**"

The first is tested by defining a report here, inside the test file, and
exporting it. If the exporters ever grow knowledge of specific reports, a report
they have never heard of stops working and this fails.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import transaction
from django.utils import timezone

from accounts.factories import UserFactory
from catalogue.factories import ItemTypeFactory
from locations.factories import YardFactory
from locations.nodes import external_node, node_for_location
from network.factories import ClientFactory, SiteFactory
from reporting.exports import to_excel, to_pdf
from reporting.framework import (
    Filter,
    Report,
    ReportError,
    all_reports,
    get_report,
    parse_params,
    quantity,
    register,
    text,
)
from stock.models import Condition, MovementType, OwnerType
from stock.services import MovementRequest, post_movement


@pytest.fixture
def yard(tenant):
    return YardFactory(name="Report yard")


@pytest.fixture
def stocked(tenant, yard):
    """Some own stock and some consignment stock in the yard."""
    ours = ItemTypeFactory(name="Report clamp", uom="ea")
    client = ClientFactory(name="Safaricom")
    theirs = ItemTypeFactory(name="Report RRU", uom="ea")

    with transaction.atomic():
        post_movement(
            MovementRequest(
                item_type=ours,
                quantity=Decimal("120"),
                from_node=external_node(tenant.pk),
                to_node=node_for_location(yard),
                movement_type=MovementType.RECEIPT,
            )
        )
        post_movement(
            MovementRequest(
                item_type=theirs,
                quantity=Decimal("4"),
                from_node=external_node(tenant.pk, client=client),
                to_node=node_for_location(yard),
                movement_type=MovementType.RECEIPT,
                owner_type=OwnerType.CLIENT,
                owner_client=client,
            )
        )
    return ours, theirs, client


class TestTheFrameworkIsGeneric:
    """T7.1's criterion, tested by exporting a report the exporters never saw."""

    def test_a_report_defined_here_exports_to_excel_and_pdf(self, tenant, stocked):
        @register
        class InventedReport(Report):
            slug = "invented-for-a-test"
            title = "Invented report"
            description = "Defined inside a test, exported by code that never saw it."
            columns = (
                text("thing", "Thing", width=20),
                quantity("amount", "Amount"),
            )
            filters = (Filter("thing", "Thing"),)

            def rows(self, params):
                yield {"thing": "first", "amount": Decimal("1.5")}
                yield {"thing": "second", "amount": Decimal("2.25")}

        try:
            report = get_report("invented-for-a-test")

            excel = to_excel(report, {})
            assert excel[:2] == b"PK", "an xlsx is a zip"
            assert len(excel) > 2_000

            content, content_type, filename = to_pdf(report, {}, organization=tenant)
            body = content.decode() if b"<html" in content else ""
            if body:
                # No WeasyPrint on this host, so the HTML fallback (§11).
                assert "Invented report" in body
                assert "3.750" in body, "the total is the sum of the column"
            else:
                assert content_type == "application/pdf"
            assert filename.startswith("invented-for-a-test")
        finally:
            # Registered reports are global, and leaving a test's report behind
            # would show up in the catalogue of every later test.
            from reporting import framework

            framework._REGISTRY.pop("invented-for-a-test", None)

    def test_a_column_formats_the_same_everywhere(self):
        """M2: the export must not disagree with the screen it came from."""
        column = quantity("q", "Q")
        assert column.format(Decimal("1")) == "1.000"
        assert column.format("2.5") == "2.500"
        assert column.format(None) == ""

    def test_a_report_without_columns_is_refused(self):
        with pytest.raises(ReportError):

            @register
            class Empty(Report):
                slug = "empty-report"
                title = "Empty"

    def test_two_reports_cannot_share_a_slug(self):
        """A slug is in URLs and in stored exports, so a collision is silent."""
        with pytest.raises(ReportError, match="both call themselves"):

            @register
            class Duplicate(Report):
                slug = "stock-on-hand"
                title = "Also stock on hand"
                columns = (text("a", "A"),)

    def test_a_required_filter_is_enforced(self):
        report = get_report("stock-as-at")
        with pytest.raises(ReportError, match="as at"):
            parse_params(report, {})

    def test_every_registered_report_declares_itself_properly(self):
        """A report missing a title or a requirement is one nobody can find."""
        for report in all_reports():
            assert report.slug
            assert report.title
            assert report.columns
            assert report.description, f"{report.slug} has no description"


class TestStockReports:
    """T7.2: "each matches a hand-computed fixture"."""

    def test_stock_on_hand_matches_what_was_received(self, tenant, stocked):
        rendered = get_report("stock-on-hand").render({})
        by_item = {row["item_type"]: row for row in rendered["rows"]}

        assert by_item["Report clamp"]["quantity"] == "120.000"
        assert by_item["Report clamp"]["owner"] == "Own stock"
        # E1: whose it is, wherever it appears.
        assert by_item["Report RRU"]["owner"] == "Safaricom"
        assert rendered["totals"]["quantity"] == "124.000"

    def test_stock_as_at_before_the_delivery_is_empty(self, tenant, stocked):
        """M1: "as at any past date" — and the past really is the past."""
        yesterday = timezone.now() - timedelta(days=1)

        rendered = get_report("stock-as-at").render({"as_at": yesterday})

        assert rendered["row_count"] == 0

    def test_stock_as_at_now_matches_stock_on_hand(self, tenant, stocked):
        """The ledger aggregation and the cache must agree about the present.

        §3.4: the ledger is the source of truth and the balances are a cache. If
        these two ever disagree, one of them is wrong — which is exactly what
        `verify_ledger` exists to catch, and what this asserts for the reports.
        """
        as_at = get_report("stock-as-at").render({"as_at": timezone.now()})
        on_hand = get_report("stock-on-hand").render({})

        def total(rendered):
            return sum(Decimal(row["quantity"]) for row in rendered["rows"])

        assert total(as_at) == total(on_hand)

    def test_movement_history_reads_chronologically(self, tenant, stocked):
        rendered = get_report("movement-history").render({})

        assert rendered["row_count"] == 2
        # Newest first: somebody opening this wants to know what just happened.
        assert rendered["rows"][0]["occurred_at"] >= rendered["rows"][1]["occurred_at"]
        # And no meaningless total across directions and units.
        assert rendered["totals"] is None

    def test_the_client_position_report_splits_the_three_states(self, tenant, stocked):
        _ours, _theirs, client = stocked

        rendered = get_report("client-position").render({"client": client.pk})

        assert rendered["row_count"] == 1
        assert rendered["rows"][0]["state"] == "We hold it"
        assert rendered["rows"][0]["quantity"] == "4.000"


class TestAccountabilityReports:
    """T7.3, including its criterion about reconciling with T5.7."""

    @pytest.fixture
    def issued_job(self, tenant, yard):
        """Material issued to a technician for a site, then partly installed."""
        from dispatch.models import GateOut, GateOutLine, GateOutPurpose
        from dispatch.services import release_gate_out, submit_gate_out
        from jobs.models import CloseoutAction, Job, JobCloseout, JobCloseoutLine
        from jobs.services import submit_closeout

        storekeeper = UserFactory(organization=tenant, full_name="Sara Storekeeper")
        technician = UserFactory(organization=tenant, full_name="Tom Technician")
        site = SiteFactory(internal_ref="RPT-1", name="Report site")
        item = ItemTypeFactory(name="Report feeder", uom="m")

        with transaction.atomic():
            post_movement(
                MovementRequest(
                    item_type=item,
                    quantity=Decimal("500"),
                    from_node=external_node(tenant.pk),
                    to_node=node_for_location(yard),
                    movement_type=MovementType.RECEIPT,
                )
            )

        gate_out = GateOut.objects.create(
            organization=tenant,
            from_location=yard,
            site=site,
            custody_holder=technician,
            requested_by=storekeeper,
            purpose_type=GateOutPurpose.INSTALLATION,
        )
        GateOutLine.objects.create(
            organization=tenant,
            gate_out=gate_out,
            item_type=item,
            tracking_mode="BULK",
            requested_qty=Decimal("200"),
            uom="m",
        )
        submit_gate_out(gate_out, submitted_by=storekeeper)
        release_gate_out(gate_out, released_by=storekeeper)

        job = Job.objects.create(
            organization=tenant,
            reference="RPT-JOB-1",
            client=site.client,
            site=site,
            assignee=technician,
        )
        closeout = JobCloseout.objects.create(
            organization=tenant, job=job, submitted_by=technician
        )
        JobCloseoutLine.objects.create(
            organization=tenant,
            closeout=closeout,
            action=CloseoutAction.INSTALLED,
            item_type=item,
            quantity=Decimal("150"),
            uom="m",
        )
        submit_closeout(closeout, submitted_by=technician)
        return site, item, technician

    def test_the_consumption_report_reconciles_with_the_reconciliation(
        self, tenant, issued_job
    ):
        """T7.3's criterion, and why the report calls `reconcile_site`.

        Same numbers because there is one implementation, not two that happen to
        agree today.
        """
        from jobs.reconciliation import reconcile_site

        site, _item, _technician = issued_job
        truth = reconcile_site(site)["items"][0]

        rendered = get_report("consumption").render({"site": site.pk})
        row = rendered["rows"][0]

        assert row["issued"] == f"{truth['issued']:.3f}"
        assert row["installed"] == f"{truth['installed']:.3f}"
        assert row["unaccounted"] == f"{truth['unaccounted']:.3f}"
        # And the figures are the ones a hand count would give: 200 out, 150 in
        # the ground, 50 still on the technician.
        assert row["issued"] == "200.000"
        assert row["installed"] == "150.000"
        assert row["unaccounted"] == "50.000"

    def test_outstanding_gate_outs_lists_only_what_has_not_left(self, tenant, issued_job):
        """F7: a fully released pass is not outstanding."""
        rendered = get_report("outstanding-gate-outs").render({})

        assert rendered["row_count"] == 0

    def test_custody_shows_what_a_technician_still_holds(self, tenant, issued_job):
        """I1: custody is a balance at a PERSON node, so it needs no second table."""
        _site, _item, technician = issued_job

        rendered = get_report("overdue-custody").render({"holder": technician.pk})

        assert rendered["row_count"] == 1
        assert rendered["rows"][0]["quantity"] == "50.000"
        assert rendered["rows"][0]["state"] == "Held"

    def test_the_installed_base_shows_what_is_in_the_ground(self, tenant, issued_job):
        site, _item, _technician = issued_job

        rendered = get_report("installed-base").render({"site": site.pk})

        assert rendered["rows"][0]["quantity"] == "150.000"


class TestRecoveryAndDisposalReports:
    """T7.4, including "a recovery from a decommissioned site appears against it"."""

    def test_a_recovery_from_a_decommissioned_site_still_reports_against_it(
        self, tenant, yard
    ):
        """T7.4's criterion, and the reason the grouping is on `origin_site`.

        A site being decommissioned is *why* material came off it. A report that
        dropped those rows would lose exactly the history somebody is looking for.
        """
        from network.models import SiteStatus
        from receiving.models import GateIn, GateInLine, GateInSource
        from receiving.services import post_gate_in

        site = SiteFactory(internal_ref="DEC-1", name="Decommissioned site")
        item = ItemTypeFactory(name="Recovered antenna", uom="ea")
        storekeeper = UserFactory(organization=tenant, full_name="Sara Storekeeper")

        gate_in = GateIn.objects.create(
            organization=tenant,
            source_type=GateInSource.RECOVERY,
            origin_site=site,
            to_location=yard,
            received_at=timezone.now(),
        )
        GateInLine.objects.create(
            organization=tenant,
            gate_in=gate_in,
            item_type=item,
            tracking_mode="BULK",
            quantity=Decimal("3"),
            uom="ea",
            condition=Condition.USED_SERVICEABLE,
        )
        post_gate_in(gate_in, posted_by=storekeeper)

        # The site is then decommissioned — after the recovery, as it happens.
        site.status = SiteStatus.DECOMMISSIONED
        site.save(update_fields=["status"])

        rendered = get_report("recoveries").render({})

        assert rendered["row_count"] == 1
        row = rendered["rows"][0]
        assert row["site"] == str(site)
        assert row["site_ref"] == "DEC-1"
        assert row["quantity"] == "3.000"

    def test_the_disposals_report_names_who_authorised_each_write_off(
        self, tenant, yard
    ):
        """M3, J3: a write-off nobody authorised is indistinguishable from theft."""
        from approvals.models import ApprovalRule
        from disposition.models import Disposal, DisposalLine, DisposalMethod
        from disposition.services import post_disposal, submit_disposal
        from locations.nodes import quarantine_location

        ApprovalRule.objects.all().delete()

        quarantine = quarantine_location(tenant.pk, yard)
        item = ItemTypeFactory(name="Written-off cable", uom="m")
        storekeeper = UserFactory(organization=tenant, full_name="Sara Storekeeper")

        with transaction.atomic():
            post_movement(
                MovementRequest(
                    item_type=item,
                    quantity=Decimal("60"),
                    from_node=external_node(tenant.pk),
                    to_node=node_for_location(quarantine),
                    movement_type=MovementType.RECEIPT,
                    condition=Condition.SCRAP,
                )
            )

        disposal = Disposal.objects.create(
            organization=tenant,
            from_location=quarantine,
            method=DisposalMethod.LICENSED_HANDLER,
            handler_name="WEEE Centre",
            handler_reference="WEEE-4410",
        )
        DisposalLine.objects.create(
            organization=tenant,
            disposal=disposal,
            item_type=item,
            quantity=Decimal("60"),
            uom="m",
            condition=Condition.SCRAP,
        )
        submit_disposal(disposal, submitted_by=storekeeper)
        post_disposal(disposal, posted_by=storekeeper)

        rendered = get_report("disposals").render({})

        row = rendered["rows"][0]
        assert row["quantity"] == "60.000"
        assert "WEEE-4410" in row["handler"]
        # Auto-approved is still an answer to "who authorised this?" (§5.2).
        assert row["authorised_by"]


class TestExports:
    """T7.5, M2."""

    def test_an_excel_export_carries_the_filters_it_was_run_with(self, tenant, stocked):
        """M2: a filtered report read as a total one is worse than no report."""
        from openpyxl import load_workbook

        _ours, _theirs, client = stocked
        report = get_report("stock-on-hand")

        content = to_excel(report, {"client": client.pk})
        workbook = load_workbook(io_bytes(content))
        sheet = workbook.active

        assert sheet.cell(row=1, column=1).value == "Stock on hand"
        assert "Safaricom" in str(sheet.cell(row=2, column=1).value)
        assert "Produced" in str(sheet.cell(row=3, column=1).value)

    def test_excel_quantities_are_numbers_not_text(self, tenant, stocked):
        """So the client receiving it can sum a column themselves (M2)."""
        from openpyxl import load_workbook

        report = get_report("stock-on-hand")
        workbook = load_workbook(io_bytes(to_excel(report, {})))
        sheet = workbook.active

        quantity_column = next(
            index
            for index, column in enumerate(report.columns, start=1)
            if column.key == "quantity"
        )
        value = sheet.cell(row=6, column=quantity_column).value

        assert isinstance(value, (int, float))
        assert sheet.cell(row=6, column=quantity_column).number_format == "#,##0.000"

    def test_a_small_report_is_not_queued(self, tenant, stocked):
        from reporting.exports import should_run_async

        assert should_run_async(get_report("stock-on-hand"), {}) is False

    def test_a_report_that_cannot_be_large_is_never_queued(self, tenant):
        from reporting.exports import should_run_async

        assert should_run_async(get_report("serial-history"), {"serial": "X"}) is False

    def test_a_large_report_is_queued(self, tenant, stocked, monkeypatch):
        """T7.5: over the threshold, it goes to a worker."""
        import reporting.exports as exports

        monkeypatch.setattr(exports, "ASYNC_ROW_THRESHOLD", 1)

        assert exports.should_run_async(get_report("movement-history"), {}) is True

    def test_a_queued_export_is_stored_with_an_expiring_link(self, tenant, stocked):
        """T7.5's second half: "and the link expires" (N-7).

        Stored as an ``Attachment`` so it is served by the one signed, expiring
        mechanism rather than a second one nobody would review.
        """
        from core.models import Attachment
        from notifications.models import NotificationEvent
        from reporting.tasks import build_export

        result = build_export(
            organization_id=str(tenant.pk),
            slug="stock-on-hand",
            fmt="xlsx",
            filters={},
            requested_by_id=None,
        )

        attachment = Attachment.objects.get(pk=result["attachment_id"])
        assert attachment.filename.startswith("stock-on-hand")
        url = attachment.download_url()
        assert "/media/" not in url
        assert "expires_in" in url

        event = NotificationEvent.objects.filter(
            event_key="report.export_ready"
        ).latest("created_at")
        assert event.payload["download_url"]


def io_bytes(content: bytes):
    import io

    return io.BytesIO(content)


class TestTheReportEndpoints:
    """§6, M1, M2 — over HTTP, because that is how a screen reaches them."""

    @pytest.fixture
    def signed_in(self, db, client, settings):
        from core.provisioning import provision_tenant

        settings.TENANT_BASE_DOMAIN = "localhost"
        result = provision_tenant(
            name="Silvertech", slug="silvertech", owner_email="owner@silvertech.co.ke"
        )
        owner = result["owner"]
        owner.set_password("a good long password")
        owner.save()

        client.defaults["HTTP_HOST"] = "silvertech.localhost"
        token = client.post(
            "/api/v1/auth/login",
            {"identifier": "owner@silvertech.co.ke", "password": "a good long password"},
            content_type="application/json",
        ).json()["access"]
        return client, token, result["organization"], owner

    def test_the_catalogue_carries_columns_and_filters(self, signed_in):
        """T7.7 builds its filter panels from this, so a new report gets a UI."""
        http, token, _organization, _owner = signed_in

        response = http.get(
            "/api/v1/reports", HTTP_AUTHORIZATION=f"Bearer {token}"
        )

        assert response.status_code == 200, response.content
        body = response.json()
        assert body["count"] >= 9, "M1 lists nine reports"
        stock = next(row for row in body["reports"] if row["slug"] == "stock-on-hand")
        assert [column["key"] for column in stock["columns"]][:2] == [
            "item_type",
            "node_label",
        ]
        assert any(filter_["resource"] == "item-types" for filter_ in stock["filters"])

    def test_a_report_returns_its_rows(self, signed_in):
        http, token, _organization, _owner = signed_in

        response = http.get(
            "/api/v1/reports/stock-on-hand", HTTP_AUTHORIZATION=f"Bearer {token}"
        )

        assert response.status_code == 200, response.content
        assert response.json()["slug"] == "stock-on-hand"

    def test_an_unknown_report_is_a_404_not_a_500(self, signed_in):
        http, token, _organization, _owner = signed_in

        response = http.get(
            "/api/v1/reports/no-such-report", HTTP_AUTHORIZATION=f"Bearer {token}"
        )

        assert response.status_code == 404

    def test_a_missing_required_filter_says_which(self, signed_in):
        """A 400 naming the filter, not a stack trace."""
        http, token, _organization, _owner = signed_in

        response = http.get(
            "/api/v1/reports/stock-as-at", HTTP_AUTHORIZATION=f"Bearer {token}"
        )

        assert response.status_code == 400
        assert "as at" in str(response.json()["error"]["field_errors"]).lower()

    def test_an_export_comes_back_as_a_file(self, signed_in):
        http, token, _organization, _owner = signed_in

        response = http.post(
            "/api/v1/reports/stock-on-hand/export",
            {"format": "xlsx"},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        assert response.status_code == 200, response.content
        assert "spreadsheetml" in response["Content-Type"]
        # An attachment, because a spreadsheet rendered inline helps nobody.
        assert response["Content-Disposition"].startswith("attachment")
        assert response.content[:2] == b"PK"

    def test_a_pdf_export_is_offered_too(self, signed_in):
        """M2 asks for both, and the PDF degrades to HTML where it must (§11)."""
        http, token, _organization, _owner = signed_in

        response = http.post(
            "/api/v1/reports/stock-on-hand/export",
            {"format": "pdf"},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        assert response.status_code == 200, response.content
        assert response["Content-Type"] in (
            "application/pdf",
            "text/html; charset=utf-8",
        )

    def test_an_unknown_format_is_refused(self, signed_in):
        http, token, _organization, _owner = signed_in

        response = http.post(
            "/api/v1/reports/stock-on-hand/export",
            {"format": "csv"},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        assert response.status_code == 400

    def test_a_user_without_the_permission_sees_no_reports(self, signed_in):
        """A catalogue offering things that 403 on the next tap is worse than empty."""
        from accounts.factories import UserFactory
        from core.tenancy import tenant_context

        http, _token, organization, _owner = signed_in

        with tenant_context(organization):
            onlooker = UserFactory(
                organization=organization, email="nobody@silvertech.co.ke"
            )
            onlooker.set_password("a good long password")
            onlooker.save()

        their_token = http.post(
            "/api/v1/auth/login",
            {"identifier": "nobody@silvertech.co.ke", "password": "a good long password"},
            content_type="application/json",
        ).json()["access"]

        catalogue = http.get(
            "/api/v1/reports", HTTP_AUTHORIZATION=f"Bearer {their_token}"
        )
        assert catalogue.json()["count"] == 0

        refused = http.get(
            "/api/v1/reports/stock-on-hand", HTTP_AUTHORIZATION=f"Bearer {their_token}"
        )
        assert refused.status_code == 403


class TestRetentionReview:
    """T7.6: "expiry produces a review list **and no data loss**"."""

    def test_nothing_in_the_retention_module_deletes(self):
        """M5 is emphatic, so this is asserted about the source itself.

        A retention job that *can* delete is one that will, one day, remove
        something an auditor then asks for. The check is crude on purpose: it
        fails if anybody adds a delete, which is the moment to have the argument.
        """
        from pathlib import Path

        import reporting.retention as retention

        source = Path(retention.__file__).read_text(encoding="utf-8")
        code = "\n".join(
            line for line in source.splitlines() if not line.strip().startswith("#")
        )
        # Both the queryset and the instance forms.
        assert ".delete()" not in code
        assert "bulk_delete" not in code

    def test_an_old_document_appears_on_the_review_list_unchanged(self, tenant, yard):
        from receiving.models import DocumentStatus, GateIn, GateInLine, GateInSource
        from receiving.services import post_gate_in
        from reporting.retention import retention_cutoff, review_list

        storekeeper = UserFactory(organization=tenant, full_name="Sara Storekeeper")
        item = ItemTypeFactory(name="Ancient clamp", uom="ea")

        gate_in = GateIn.objects.create(
            organization=tenant,
            source_type=GateInSource.PURCHASE,
            supplier_name="Long-gone supplier",
            to_location=yard,
            received_at=timezone.now() - timedelta(days=365 * 9),
        )
        GateInLine.objects.create(
            organization=tenant,
            gate_in=gate_in,
            item_type=item,
            tracking_mode="BULK",
            quantity=Decimal("5"),
            uom="ea",
        )
        post_gate_in(gate_in, posted_by=storekeeper)

        candidates = review_list(tenant)

        assert any(
            entry["document_id"] == str(gate_in.pk) for entry in candidates
        ), f"nine years old, cutoff {retention_cutoff(tenant)}"

        # And it is still there, entirely intact. That is the whole requirement.
        gate_in.refresh_from_db()
        assert gate_in.status == DocumentStatus.POSTED
        assert gate_in.lines.count() == 1
        assert GateIn.objects.filter(pk=gate_in.pk).exists()

    def test_a_recent_document_is_not_listed(self, tenant, yard):
        from receiving.models import GateIn, GateInSource
        from reporting.retention import review_list

        GateIn.objects.create(
            organization=tenant,
            source_type=GateInSource.PURCHASE,
            supplier_name="Yesterday's supplier",
            to_location=yard,
            received_at=timezone.now() - timedelta(days=3),
        )

        assert review_list(tenant) == []

    def test_something_still_open_is_flagged_as_a_problem_not_a_candidate(
        self, tenant, yard
    ):
        """A pass approved two years ago and never released is somebody's
        oversight, and retention review is when it gets noticed."""
        from dispatch.models import GateOut, GateOutLine, GateOutPurpose
        from reporting.retention import open_documents_past_retention, review_list

        storekeeper = UserFactory(organization=tenant, full_name="Sara Storekeeper")
        site = SiteFactory(internal_ref="OLD-1", name="Forgotten site")
        item = ItemTypeFactory(name="Forgotten clamp", uom="ea")

        gate_out = GateOut.objects.create(
            organization=tenant,
            from_location=yard,
            site=site,
            custody_holder=storekeeper,
            requested_by=storekeeper,
            purpose_type=GateOutPurpose.INSTALLATION,
        )
        GateOutLine.objects.create(
            organization=tenant,
            gate_out=gate_out,
            item_type=item,
            tracking_mode="BULK",
            requested_qty=Decimal("1"),
            uom="ea",
        )
        GateOut.all_objects.filter(pk=gate_out.pk).update(
            created_at=timezone.now() - timedelta(days=365 * 9)
        )

        stale = open_documents_past_retention(tenant)
        assert any(entry["document_id"] == str(gate_out.pk) for entry in stale)

        # Not offered for archiving: it has not finished.
        assert all(
            entry["document_id"] != str(gate_out.pk) for entry in review_list(tenant)
        )
