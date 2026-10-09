"""T18.7, T18.11 (purchases) — site purchases: services, delivery, endpoints (§4.19.3; R7, R9).

The rule under test: money reaches project cost once, by one route. A USED_AT_SITE
purchase is cost on approval; an INTO_YARD one makes a single draft delivery and
the goods become cost when issued, so the purchase itself is never costed.
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from accounts.factories import RoleFactory, UserFactory, UserRoleFactory
from accounts.permissions_registry import PERM
from approvals.engine import NotAnApprover
from catalogue.factories import ItemTypeFactory
from commercials import budget, finance
from commercials.costing import cost_for
from commercials.finance import (
    OverBudgetReasonRequired,
    PurchaseLineInput,
    SitePurchaseYardNeedsCatalogue,
    SupplierNotUsableOnPurchase,
)
from commercials.models import (
    ExpenseStatus,
    PurchaseDestination,
    SitePurchase,
    SitePurchaseLine,
)
from commercials.services import SitePurchaseReceived, reverse_purchase
from commercials.tests.api_helpers import PASSWORD, Api, error_code, results
from commercials.tests.finance_helpers import approve_through
from core.models import Attachment
from locations.models import Location, LocationType
from network.factories import ProjectFactory, SiteFactory
from network.models import Supplier, SupplierStatus
from network.suppliers import SupplierNotApproved
from receiving.models import DocumentStatus, GateIn, GateInSource
from receiving.services import YardDeliveryFailed, draft_gate_in_for_purchase

D = Decimal
S = ExpenseStatus
DAY = date(2026, 10, 5)
INTO_YARD = PurchaseDestination.INTO_YARD
AT_SITE = PurchaseDestination.USED_AT_SITE
pytestmark = pytest.mark.django_db


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
def tech(tenant):
    return person(tenant, "Tom Technician")


@pytest.fixture
def fin(tenant):
    return person(tenant, "Fiona Finance", PERM.FINANCE_APPROVE)


@pytest.fixture
def project(tenant, pm):
    return ProjectFactory(
        reference="WO-7001",
        po_number="PO-700",
        manager=pm,
        contract_value=D("50000.00"),
        cost_budget=D("10000.00"),
    )


@pytest.fixture
def site(tenant, project):
    site = SiteFactory(name="Ruiru")
    project.sites.add(site, through_defaults={"organization_id": project.organization_id})
    return site


@pytest.fixture
def yard(tenant):
    return Location.objects.create(organization=tenant, name="Yard", type=LocationType.YARD)


@pytest.fixture
def supplier(tenant, fin):
    return Supplier.objects.create(
        organization=tenant,
        name="Hardware Ltd",
        registered_by=fin,
        status=SupplierStatus.APPROVED,
    )


@pytest.fixture
def clamp(tenant):
    return ItemTypeFactory(name="Clamp", uom="ea")


def free_line(price="100.00", qty="2"):
    return PurchaseLineInput(
        quantity=D(qty), unit_price=D(price), description="Cable ties"
    )


def catalogue_line(item, price="100.00", qty="3"):
    return PurchaseLineInput(quantity=D(qty), unit_price=D(price), item_type=item)


def record(tech, site, supplier, lines, **kw):
    kw.setdefault("purchase_date", DAY)
    return finance.record_site_purchase(
        actor=tech, site=site, supplier=supplier, lines=lines, **kw
    )


class TestRecording:
    def test_a_site_purchase_is_recorded_and_routed(
        self, tenant, project, site, supplier, tech, pm, fin
    ):
        made = record(tech, site, supplier, [free_line("100.50", "2")])

        assert made.number.startswith("SP")
        assert made.project == project
        assert made.amount == D("201.00")
        assert made.status == S.PENDING_PM
        assert made.destination == AT_SITE and made.receive_into is None
        assert made.lines.get().uom == ""
        assert made.supplier == supplier

    def test_the_line_total_rounds_to_two_places_and_amount_is_the_sum(
        self, tenant, site, supplier, tech, fin
    ):
        made = record(
            tech, site, supplier,
            [free_line("0.33", "3.5"), free_line("10.00", "0.5")],
        )
        # 3.5 x 0.33 = 1.155 -> 1.16 (half up); 0.5 x 10 = 5.00
        assert made.amount == D("6.16")
        assert [line.total for line in made.lines.all()] == [D("1.16"), D("5.00")]
        with pytest.raises(finance.FinanceInputInvalid):
            record(tech, site, supplier, [free_line("0.335", "3")])

    def test_into_yard_needs_a_place_and_catalogue_items(
        self, tenant, site, supplier, tech, fin, yard, clamp
    ):
        with pytest.raises(finance.FinanceInputInvalid) as missing:
            record(tech, site, supplier, [catalogue_line(clamp)], destination=INTO_YARD)
        assert "receive_into" in missing.value.field_errors

        with pytest.raises(SitePurchaseYardNeedsCatalogue) as caught:
            record(
                tech, site, supplier, [free_line()],
                destination=INTO_YARD, receive_into=yard,
            )
        assert caught.value.code == "SITE_PURCHASE_YARD_NEEDS_CATALOGUE"

        vehicle = Location.objects.create(
            organization=tenant, name="Van", type=LocationType.VEHICLE, vehicle_reg="KAA 001A"
        )
        with pytest.raises(finance.FinanceInputInvalid):
            record(
                tech, site, supplier, [catalogue_line(clamp)],
                destination=INTO_YARD, receive_into=vehicle,
            )
        assert SitePurchase.objects.count() == 0

    def test_a_store_is_an_allowed_place_and_used_at_site_drops_it(
        self, tenant, site, supplier, tech, fin, yard, clamp
    ):
        store = Location.objects.create(
            organization=tenant, name="Store", type=LocationType.STORE, parent=yard
        )
        made = record(
            tech, site, supplier, [catalogue_line(clamp)],
            destination=INTO_YARD, receive_into=store,
        )
        assert made.receive_into == store and made.lines.get().uom == "ea"

        at_site = record(tech, site, supplier, [free_line()], receive_into=yard)
        assert at_site.receive_into is None

    @pytest.mark.parametrize(
        "lines",
        [
            [],
            [PurchaseLineInput(quantity=D("0"), unit_price=D("1"), description="x")],
            [PurchaseLineInput(quantity=D("1"), unit_price=D("-1"), description="x")],
            [PurchaseLineInput(quantity=D("1"), unit_price=D("1"))],
            [PurchaseLineInput(quantity=D("1"), unit_price=D("0"), description="free")],
        ],
    )
    def test_bad_lines_are_refused(self, tenant, site, supplier, tech, fin, lines):
        with pytest.raises(finance.FinanceInputInvalid):
            record(tech, site, supplier, lines)
        assert SitePurchase.objects.count() == 0

    def test_a_pending_supplier_may_be_named_a_rejected_or_inactive_one_may_not(
        self, tenant, site, supplier, tech, fin
    ):
        Supplier.objects.filter(pk=supplier.pk).update(status=SupplierStatus.PENDING)
        supplier.refresh_from_db()
        assert record(tech, site, supplier, [free_line()]).supplier == supplier

        Supplier.objects.filter(pk=supplier.pk).update(status=SupplierStatus.REJECTED)
        supplier.refresh_from_db()
        with pytest.raises(SupplierNotUsableOnPurchase):
            record(tech, site, supplier, [free_line()])

        Supplier.objects.filter(pk=supplier.pk).update(
            status=SupplierStatus.APPROVED, is_active=False
        )
        supplier.refresh_from_db()
        with pytest.raises(SupplierNotUsableOnPurchase):
            record(tech, site, supplier, [free_line()])

    def test_it_is_idempotent_on_client_uuid(self, tenant, site, supplier, tech, fin):
        key = uuid.uuid4()
        first = record(tech, site, supplier, [free_line()], client_uuid=key)
        again = record(tech, site, supplier, [free_line("999")], client_uuid=key)

        assert again.pk == first.pk and SitePurchase.objects.count() == 1
        assert SitePurchaseLine.objects.count() == 1

    def test_nobody_else_to_approve_refuses(self, tenant, site, tech):
        supplier = Supplier.objects.create(
            organization=tenant, name="Solo Ltd", registered_by=tech,
            status=SupplierStatus.APPROVED,
        )
        with pytest.raises(finance.FinanceNoOtherApprover):
            record(tech, site, supplier, [free_line()])


class TestApprovalAndCost:
    def test_pm_then_finance_and_used_at_site_is_cost_once(
        self, tenant, project, site, supplier, tech, pm, fin
    ):
        made = record(tech, site, supplier, [free_line("450.00", "1")])
        finance.decide(made, actor=pm, approved=True)
        assert made.status == S.PENDING_FINANCE
        assert cost_for(project).purchases == D("0.00")

        finance.decide(made, actor=fin, approved=True)
        assert made.status == S.APPROVED and made.gate_in is None
        assert cost_for(project).purchases == D("450.00")
        assert cost_for(project).total == D("450.00")

    def test_the_pm_skips_their_own_level(self, tenant, project, site, supplier, pm, fin):
        made = record(pm, site, supplier, [free_line()])
        assert made.status == S.PENDING_FINANCE

    def test_the_recorder_cannot_decide_it(self, tenant, site, supplier, tech, pm, fin):
        made = record(tech, site, supplier, [free_line()])
        with pytest.raises(finance.FinanceSelfApproval):
            finance.decide(made, actor=tech, approved=True)

    def test_rejection_needs_a_reason_and_can_be_resubmitted(
        self, tenant, site, supplier, tech, pm, fin
    ):
        made = record(tech, site, supplier, [free_line()])
        with pytest.raises(finance.RejectionReasonRequired):
            finance.decide(made, actor=pm, approved=False)
        finance.decide(made, actor=pm, approved=False, reason="No receipt.")
        assert made.status == S.REJECTED and made.decision_reason == "No receipt."

        finance.resubmit(made, actor=tech)
        assert made.status == S.PENDING_PM and made.decided_at is None

    def test_resubmit_refuses_a_supplier_rejected_meanwhile(
        self, tenant, site, supplier, tech, pm, fin
    ):
        made = record(tech, site, supplier, [free_line()])
        finance.decide(made, actor=pm, approved=False, reason="Redo.")
        Supplier.objects.filter(pk=supplier.pk).update(status=SupplierStatus.REJECTED)
        made = SitePurchase.objects.get(pk=made.pk)

        with pytest.raises(SupplierNotUsableOnPurchase):
            finance.resubmit(made, actor=tech)


class TestDraftDelivery:
    def yard_purchase(self, tenant, site, supplier, tech, yard, clamp, pm, fin):
        made = record(
            tech, site, supplier, [catalogue_line(clamp, "200.00", "3")],
            destination=INTO_YARD, receive_into=yard,
        )
        return approve_through(made, pm=pm, finance_user=fin)

    def test_approval_makes_one_draft_with_the_source_link(
        self, tenant, project, site, supplier, tech, pm, fin, yard, clamp
    ):
        made = self.yard_purchase(tenant, site, supplier, tech, yard, clamp, pm, fin)

        gate_in = made.gate_in
        assert made.status == S.APPROVED and gate_in is not None
        assert gate_in.status == DocumentStatus.DRAFT and gate_in.number == ""
        assert gate_in.source_type == GateInSource.PURCHASE
        assert gate_in.supplier == supplier and gate_in.supplier_name == "Hardware Ltd"
        assert gate_in.for_site == site and gate_in.to_location == yard
        assert gate_in.notes == f"From site purchase {made.number}"
        line = gate_in.lines.get()
        assert (line.item_type, line.quantity, line.uom) == (clamp, D("3.000"), "ea")
        assert line.for_site == site and line.owner_type == "OWN"
        assert GateIn.objects.count() == 1

    def test_it_is_never_costed_twice(
        self, tenant, project, site, supplier, tech, pm, fin, yard, clamp
    ):
        made = self.yard_purchase(tenant, site, supplier, tech, yard, clamp, pm, fin)

        # Not cost from the purchase; committed until the delivery posts.
        assert cost_for(project).purchases == D("0.00")
        assert cost_for(project).total == D("0.00")
        position = budget.budget_position(project)
        assert position.committed == D("600.00") and position.spent == D("0.00")
        assert made.gate_in.site_purchase == made

    def test_a_repeated_call_returns_the_same_draft(
        self, tenant, site, supplier, tech, pm, fin, yard, clamp
    ):
        made = self.yard_purchase(tenant, site, supplier, tech, yard, clamp, pm, fin)
        first = made.gate_in

        fresh = SitePurchase.objects.get(pk=made.pk)
        assert draft_gate_in_for_purchase(fresh, fin).pk == first.pk
        assert GateIn.objects.count() == 1

    def test_a_failed_delivery_undoes_the_approval(
        self, tenant, site, supplier, tech, pm, fin, yard, clamp, monkeypatch
    ):
        made = record(
            tech, site, supplier, [catalogue_line(clamp)],
            destination=INTO_YARD, receive_into=yard,
        )
        finance.decide(made, actor=pm, approved=True)

        def boom(*args, **kwargs):
            raise YardDeliveryFailed()

        monkeypatch.setattr("receiving.services.draft_gate_in_for_purchase", boom)
        with pytest.raises(YardDeliveryFailed):
            finance.decide(made, actor=fin, approved=True)

        stored = SitePurchase.objects.get(pk=made.pk)
        assert stored.status == S.PENDING_FINANCE and stored.decided_at is None
        assert GateIn.objects.count() == 0

    def test_an_item_that_cannot_be_received_fails_whole(
        self, tenant, site, supplier, tech, pm, fin, yard, clamp
    ):
        made = record(
            tech, site, supplier, [catalogue_line(clamp)],
            destination=INTO_YARD, receive_into=yard,
        )
        # A line that lost its catalogue item cannot become a gate-in line.
        SitePurchaseLine.objects.filter(purchase=made).update(
            item_type=None, description="gone"
        )
        finance.decide(made, actor=pm, approved=True)
        with pytest.raises(YardDeliveryFailed):
            finance.decide(made, actor=fin, approved=True)

        assert SitePurchase.objects.get(pk=made.pk).status == S.PENDING_FINANCE
        assert GateIn.objects.count() == 0


class TestPaying:
    def approved_purchase(self, site, supplier, tech, pm, fin):
        made = record(tech, site, supplier, [free_line("100.00", "1")])
        return approve_through(made, pm=pm, finance_user=fin)

    def test_paying_is_blocked_until_the_supplier_is_approved(
        self, tenant, site, supplier, tech, pm, fin
    ):
        Supplier.objects.filter(pk=supplier.pk).update(status=SupplierStatus.PENDING)
        supplier.refresh_from_db()
        made = self.approved_purchase(site, supplier, tech, pm, fin)

        with pytest.raises(SupplierNotApproved) as caught:
            finance.mark_paid(made, actor=fin, reference="MP1")
        assert caught.value.code == "SUPPLIER_NOT_APPROVED"
        assert SitePurchase.objects.get(pk=made.pk).status == S.APPROVED

        Supplier.objects.filter(pk=supplier.pk).update(status=SupplierStatus.APPROVED)
        made = SitePurchase.objects.get(pk=made.pk)
        paid = finance.mark_paid(made, actor=fin, reference="MP1")
        assert paid.status == S.PAID and paid.payment_reference == "MP1"

    def test_a_deactivated_supplier_cannot_be_paid(self, tenant, site, supplier, tech, pm, fin):
        made = self.approved_purchase(site, supplier, tech, pm, fin)
        Supplier.objects.filter(pk=supplier.pk).update(is_active=False)
        made = SitePurchase.objects.get(pk=made.pk)
        with pytest.raises(SupplierNotApproved):
            finance.mark_paid(made, actor=fin, reference="MP1")

    def test_a_reference_is_required(self, tenant, site, supplier, tech, pm, fin):
        made = self.approved_purchase(site, supplier, tech, pm, fin)
        with pytest.raises(finance.PaymentReferenceRequired):
            finance.mark_paid(made, actor=fin, reference=" ")


class TestReversal:
    def test_an_approved_site_purchase_is_reversed_to_nil_cost(
        self, tenant, project, site, supplier, tech, pm, fin
    ):
        made = approve_through(
            record(tech, site, supplier, [free_line("450.00", "1")]), pm=pm, finance_user=fin
        )
        reversal = reverse_purchase(made, actor=pm, reason="Wrong site")

        assert reversal.reverses == made and reversal.status == S.APPROVED
        assert reversal.signed_amount == D("-450.00")
        assert cost_for(project).purchases == D("0.00")
        with pytest.raises(Exception, match="already been reversed"):
            reverse_purchase(made, actor=pm, reason="again")
        with pytest.raises(Exception, match="cannot itself"):
            reverse_purchase(reversal, actor=pm, reason="x")

    def test_a_reversed_purchase_is_not_paid(self, tenant, site, supplier, tech, pm, fin):
        made = approve_through(record(tech, site, supplier, [free_line()]), pm=pm, finance_user=fin)
        reverse_purchase(made, actor=pm, reason="Wrong site")
        made = SitePurchase.objects.get(pk=made.pk)

        with pytest.raises(finance.FinanceNotDecidable):
            finance.mark_paid(made, actor=fin, reference="MP1")

    def test_only_the_pm_or_finance_reverses(self, tenant, site, supplier, tech, pm, fin):
        made = approve_through(record(tech, site, supplier, [free_line()]), pm=pm, finance_user=fin)
        with pytest.raises(NotAnApprover):
            reverse_purchase(made, actor=tech, reason="oops")

    def test_a_draft_delivery_goes_with_the_reversal(
        self, tenant, project, site, supplier, tech, pm, fin, yard, clamp
    ):
        made = approve_through(
            record(
                tech, site, supplier, [catalogue_line(clamp, "200.00", "3")],
                destination=INTO_YARD, receive_into=yard,
            ),
            pm=pm, finance_user=fin,
        )
        assert GateIn.objects.count() == 1
        reverse_purchase(made, actor=fin, reason="Cancelled order")

        assert GateIn.objects.count() == 0
        assert budget.budget_position(project).committed == D("0.00")

    def test_a_posted_delivery_blocks_it(
        self, tenant, site, supplier, tech, pm, fin, yard, clamp
    ):
        made = approve_through(
            record(
                tech, site, supplier, [catalogue_line(clamp)],
                destination=INTO_YARD, receive_into=yard,
            ),
            pm=pm, finance_user=fin,
        )
        GateIn.objects.filter(pk=made.gate_in_id).update(
            status=DocumentStatus.POSTED, number="GRN-1"
        )
        made = SitePurchase.objects.get(pk=made.pk)

        with pytest.raises(SitePurchaseReceived) as caught:
            reverse_purchase(made, actor=fin, reason="Cancelled")
        assert caught.value.code == "SITE_PURCHASE_RECEIVED"
        assert SitePurchase.objects.filter(reverses__isnull=False).count() == 0


class TestOverBudget:
    def test_online_it_needs_a_reason_and_records_it(
        self, tenant, project, site, supplier, tech, pm, fin
    ):
        big = [free_line("12000.00", "1")]
        with pytest.raises(OverBudgetReasonRequired):
            record(tech, site, supplier, big)
        assert SitePurchase.objects.count() == 0

        made = record(tech, site, supplier, big, over_budget_reason="Emergency")
        assert made.over_budget_by == D("2000.00")
        assert made.over_budget_reason == "Emergency"

    def test_a_replay_flags_it_and_never_refuses(
        self, tenant, project, site, supplier, tech, pm, fin
    ):
        made = record(tech, site, supplier, [free_line("12000.00", "1")], offline=True)

        assert made.over_budget_by == D("2000.00") and made.over_budget_reason == ""
        assert made.status == S.PENDING_PM


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


def body(site, supplier, **extra):
    return {
        "site": site.pk,
        "supplier": supplier.pk,
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
        "photos_expected": 1,
        "client_uuid": str(uuid.uuid4()),
        **extra,
    }


class TestEndpoints:
    def test_create_and_read_the_contract_fields(
        self, client, tenant, project, site, supplier, tech, pm, fin
    ):
        response = Api(client, tech).post("site-purchases", body(site, supplier))

        assert response.status_code == 201, response.content
        data = response.json()
        assert data["number"].startswith("SP")
        assert data["status"] == "PENDING_PM" and data["amount"] == "200.00"
        assert data["supplier_name"] == "Hardware Ltd"
        assert data["supplier_status"] == "APPROVED"
        assert data["site_name"] == "Ruiru"
        assert data["project_reference"] == "PO-700"
        assert data["recorded_by_name"] == "Tom Technician"
        assert data["receive_into"] is None and data["receive_into_name"] == ""
        assert data["gate_in"] is None and data["is_over_budget"] is False
        assert data["pm_level_skipped"] is False
        assert data["paid_at"] is None and data["payment_reference"] == ""
        line = data["lines"][0]
        assert line["line_total"] == "200.00" and line["item_type_name"] == ""
        assert line["description"] == "Cable ties" and line["uom"] == ""

    def test_the_same_client_uuid_returns_the_same_row(
        self, client, tenant, site, supplier, tech, pm, fin
    ):
        http = Api(client, tech)
        payload = body(site, supplier)
        first = http.post("site-purchases", payload).json()
        second = http.post("site-purchases", payload).json()

        assert first["id"] == second["id"]
        assert SitePurchase.objects.count() == 1

    def test_the_yard_rules_come_back_with_their_codes(
        self, client, tenant, site, supplier, tech, pm, fin, yard
    ):
        http = Api(client, tech)
        no_place = http.post("site-purchases", body(site, supplier, destination="INTO_YARD"))
        assert no_place.status_code == 400
        assert error_code(no_place) == "FINANCE_INPUT_INVALID"

        free_text = http.post(
            "site-purchases",
            body(site, supplier, destination="INTO_YARD", receive_into=yard.pk),
        )
        assert error_code(free_text) == "SITE_PURCHASE_YARD_NEEDS_CATALOGUE"

    def test_a_missing_supplier_is_a_field_error(self, client, tenant, site, supplier, tech, fin):
        payload = body(site, supplier)
        del payload["supplier"]
        response = Api(client, tech).post("site-purchases", payload)

        assert response.status_code == 400

    def test_over_budget_online_and_the_overrun_is_gated(
        self, client, tenant, project, site, supplier, tech, pm, fin
    ):
        big = body(
            site, supplier,
            lines=[
                {"item_type": None, "description": "Gen", "quantity": "1", "unit_price": "12000"}
            ],
        )
        refused = Api(client, tech).post("site-purchases", big)
        assert error_code(refused) == "OVER_BUDGET_REASON_REQUIRED"

        made = Api(client, tech).post("site-purchases", {**big, "over_budget_reason": "Urgent"})
        assert made.status_code == 201
        mine = made.json()
        assert mine["is_over_budget"] is True and mine["over_budget_reason"] == "Urgent"
        assert "over_budget_by" not in mine  # the recorder sees the reason, not the figure

        seen = Api(client, pm).get(f"site-purchases/{mine['id']}").json()
        assert seen["over_budget_by"] == "2000.00"

    def test_the_full_route_pending_decide_pay(
        self, client, tenant, project, site, supplier, tech, pm, fin
    ):
        made = Api(client, tech).post("site-purchases", body(site, supplier)).json()
        pk = made["id"]

        pm_http, fin_http = Api(client, pm), Api(client, fin)
        assert [r["id"] for r in results(pm_http.get("site-purchases/pending"))] == [pk]
        assert results(fin_http.get("site-purchases/pending")) == []

        step = pm_http.post(f"site-purchases/{pk}/decide", {"approved": True})
        assert step.status_code == 200 and step.json()["status"] == "PENDING_FINANCE"
        assert results(pm_http.get("site-purchases/pending")) == []
        assert [r["id"] for r in results(fin_http.get("site-purchases/pending"))] == [pk]

        done = fin_http.post(f"site-purchases/{pk}/decide", {"approved": True}).json()
        assert done["status"] == "APPROVED" and done["decided_at"]

        payable = results(fin_http.get("site-purchases", payable="true"))
        assert [r["id"] for r in payable] == [pk]
        paid = fin_http.post(
            f"site-purchases/{pk}/mark-paid",
            {"payment_reference": "MP123", "paid_at": "2026-10-06T08:00:00Z"},
        ).json()
        assert paid["status"] == "PAID" and paid["payment_reference"] == "MP123"
        assert paid["paid_at"].startswith("2026-10-06")
        assert results(fin_http.get("site-purchases", payable="true")) == []

    def test_paying_an_unapproved_supplier_is_a_409_with_the_code(
        self, client, tenant, site, supplier, tech, pm, fin
    ):
        Supplier.objects.filter(pk=supplier.pk).update(status=SupplierStatus.PENDING)
        pk = Api(client, tech).post("site-purchases", body(site, supplier)).json()["id"]
        Api(client, pm).post(f"site-purchases/{pk}/decide", {"approved": True})
        Api(client, fin).post(f"site-purchases/{pk}/decide", {"approved": True})

        listed = results(Api(client, fin).get("site-purchases", payable="true"))
        assert listed[0]["supplier_status"] == "PENDING"  # shown, so the note can be too
        response = Api(client, fin).post(
            f"site-purchases/{pk}/mark-paid", {"payment_reference": "MP1"}
        )
        assert response.status_code == 409
        assert error_code(response) == "SUPPLIER_NOT_APPROVED"

    def test_a_yard_approval_exposes_the_gate_in_and_its_source_number(
        self, client, tenant, site, supplier, tech, pm, fin, yard, clamp
    ):
        made = Api(client, tech).post(
            "site-purchases",
            body(
                site, supplier, destination="INTO_YARD", receive_into=yard.pk,
                lines=[
                    {"item_type": clamp.pk, "description": "", "quantity": "5", "unit_price": "10"}
                ],
            ),
        ).json()
        assert made["receive_into_name"] == "Yard"
        assert made["lines"][0]["item_type_name"] == "Clamp" and made["lines"][0]["uom"] == "ea"
        Api(client, pm).post(f"site-purchases/{made['id']}/decide", {"approved": True})
        done = Api(client, fin).post(
            f"site-purchases/{made['id']}/decide", {"approved": True}
        ).json()

        assert done["gate_in"] is not None
        grn = Api(client, fin).get(f"gate-ins/{done['gate_in']}").json()
        assert grn["status"] == "DRAFT" and grn["source_type"] == "PURCHASE"
        assert grn["source_purchase_number"] == made["number"]
        assert grn["supplier_name"] == "Hardware Ltd"

        other = GateIn.objects.create(
            organization=tenant, source_type=GateInSource.PURCHASE, to_location=yard,
            received_at=done["decided_at"],
        )
        plain = Api(client, fin).get(f"gate-ins/{other.pk}").json()
        assert plain["source_purchase_number"] is None

    def test_reject_then_resubmit(self, client, tenant, site, supplier, tech, pm, fin):
        pk = Api(client, tech).post("site-purchases", body(site, supplier)).json()["id"]
        rejected = Api(client, pm).post(
            f"site-purchases/{pk}/decide", {"approved": False, "reason": "Photo please"}
        ).json()
        assert rejected["status"] == "REJECTED"
        assert rejected["decision_reason"] == "Photo please"

        again = Api(client, tech).post(f"site-purchases/{pk}/resubmit")
        assert again.status_code == 200 and again.json()["status"] == "PENDING_PM"
        stranger = person(tenant, "Sam Stranger")
        assert Api(client, stranger).post(f"site-purchases/{pk}/resubmit").status_code in (
            403, 404, 409,
        )

    def test_pm_skipped_shows_when_the_pm_recorded_it(
        self, client, tenant, site, supplier, pm, fin
    ):
        made = Api(client, pm).post("site-purchases", body(site, supplier)).json()
        assert made["status"] == "PENDING_FINANCE" and made["pm_level_skipped"] is True

    def test_permissions_and_visibility(self, client, tenant, site, supplier, tech, pm, fin):
        mine = Api(client, tech).post("site-purchases", body(site, supplier)).json()
        stranger = person(tenant, "Sam Stranger")
        bystander = Api(client, stranger)

        assert results(bystander.get("site-purchases")) == []
        assert bystander.get(f"site-purchases/{mine['id']}").status_code == 404
        assert [r["id"] for r in results(Api(client, tech).get("site-purchases", mine="true"))] == [
            mine["id"]
        ]
        for http in (Api(client, pm), Api(client, fin)):
            assert [r["id"] for r in results(http.get("site-purchases"))] == [mine["id"]]
        # The Finance queue is Finance's.
        assert Api(client, tech).get("site-purchases", payable="true").status_code == 403
        # Only Finance marks paid.
        assert (
            Api(client, tech)
            .post(f"site-purchases/{mine['id']}/mark-paid", {"payment_reference": "x"})
            .status_code
            == 403
        )

    def test_patch_is_the_recorders_while_pending_pm(
        self, client, tenant, site, supplier, tech, pm, fin
    ):
        pk = Api(client, tech).post("site-purchases", body(site, supplier)).json()["id"]

        by_pm = Api(client, pm).patch(f"site-purchases/{pk}", {"photos_expected": 3})
        assert by_pm.status_code == 403
        done = Api(client, tech).patch(f"site-purchases/{pk}", {"photos_expected": 3})
        assert done.status_code == 200 and done.json()["photos_expected"] == 3
        Api(client, pm).post(f"site-purchases/{pk}/decide", {"approved": True})
        late = Api(client, tech).patch(f"site-purchases/{pk}", {"photos_expected": 4})
        assert late.status_code == 409

    def test_reverse_over_http(self, client, tenant, project, site, supplier, tech, pm, fin):
        pk = Api(client, tech).post("site-purchases", body(site, supplier)).json()["id"]
        Api(client, pm).post(f"site-purchases/{pk}/decide", {"approved": True})
        Api(client, fin).post(f"site-purchases/{pk}/decide", {"approved": True})

        assert (
            Api(client, tech).post(f"site-purchases/{pk}/reverse", {"reason": "x"}).status_code
            == 403
        )
        reversed_ = Api(client, pm).post(f"site-purchases/{pk}/reverse", {"reason": "Wrong"})
        assert reversed_.status_code == 201
        assert reversed_.json()["is_reversal"] is True and reversed_.json()["reverses"] == pk
        assert results(Api(client, fin).get("site-purchases", payable="true")) == []

    def test_the_approvals_list_carries_the_purchase(
        self, client, tenant, site, supplier, tech, pm, fin
    ):
        Api(client, tech).post("site-purchases", body(site, supplier))
        pending = Api(client, pm).get("approvals/pending")

        assert pending.status_code == 200
        kinds = [row["document"].get("kind") for row in results(pending)]
        assert "PURCHASE" in kinds


class TestReceiptAttachments:
    def upload(self, http, target_id, **extra):
        photo = SimpleUploadedFile("r.jpg", b"\xff\xd8\xff\xd9 jpeg", content_type="image/jpeg")
        return http.upload(
            {
                "target_type": "commercials.SitePurchase",
                "target_id": str(target_id),
                "file": photo,
                "caption": "Receipt",
                **extra,
            }
        )

    def test_the_recorder_adds_receipts_until_it_is_approved(
        self, client, tenant, site, supplier, tech, pm, fin
    ):
        pk = Api(client, tech).post("site-purchases", body(site, supplier)).json()["id"]
        stranger = person(tenant, "Sam Stranger")

        assert self.upload(Api(client, tech), pk).status_code == 201
        assert Attachment.objects.filter(target_type="commercials.SitePurchase").count() == 1
        assert self.upload(Api(client, stranger), pk).status_code in (403, 404)

        Api(client, pm).post(f"site-purchases/{pk}/decide", {"approved": True})
        Api(client, fin).post(f"site-purchases/{pk}/decide", {"approved": True})
        late = self.upload(Api(client, tech), pk)
        assert late.status_code == 409 and error_code(late) == "ATTACHMENT_LOCKED"

        shown = results(
            Api(client, pm).get(
                "attachments", target_type="commercials.SitePurchase", target_id=pk
            )
        )
        assert len(shown) == 1

        attachment = Attachment.objects.get(target_type="commercials.SitePurchase")
        assert Api(client, tech).delete(f"attachments/{attachment.pk}").status_code == 409
