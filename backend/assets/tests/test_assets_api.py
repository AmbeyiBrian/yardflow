"""T17.5, T17.6, T17.8 — asset services and endpoints, fuel by vehicle, gate-in supplier.

Pins the contract the frontend reads (``frontend/src/features/assets``):
keys, who may call what, and that history is append-only.
"""

from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from django.db import connection, transaction
from django.utils import timezone

from accounts.factories import RoleFactory, UserFactory, UserRoleFactory
from accounts.permissions_registry import PERM
from assets import services
from assets.models import Asset, AssetHandover, AssetType
from commercials import finance
from commercials.models import ExpenseCategory, ExpenseKind, ProjectExpense
from commercials.tests.api_helpers import PASSWORD, Api, error_code, results
from commercials.tests.finance_helpers import approve_through
from network.factories import ProjectFactory, SiteFactory
from network.models import Supplier, SupplierStatus
from sync.models import SubmissionStatus, SyncException
from sync.services import apply_submission

D = Decimal
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
def keeper(tenant):
    return person(tenant, "Kim Keeper", PERM.ASSET_MANAGE)


@pytest.fixture
def driver(tenant):
    return person(tenant, "Dan Driver")


@pytest.fixture
def stranger(tenant):
    return person(tenant, "Sue Stranger")


@pytest.fixture
def fin(tenant):
    return person(tenant, "Fiona Finance", PERM.FINANCE_APPROVE)


@pytest.fixture
def pm(tenant):
    return person(tenant, "Pippa Manager")


@pytest.fixture
def site(tenant, pm):
    project = ProjectFactory(reference="WO-1", manager=pm)
    site = SiteFactory(name="Ruiru")
    project.sites.add(site, through_defaults={"organization_id": project.organization_id})
    return site


@pytest.fixture
def fuel(tenant, fin):
    return ExpenseCategory.objects.create(organization=tenant, name="Fuel", kind=ExpenseKind.FUEL)


@pytest.fixture
def misc(tenant, fin):
    return ExpenseCategory.objects.create(organization=tenant, name="Misc")


@pytest.fixture
def truck(tenant, keeper, driver):
    return services.create_asset(
        actor=keeper, holder=driver, type="VEHICLE", name="Hilux", tag="KDA 123A"
    )


def asset_body(**extra):
    return {"type": "VEHICLE", "name": "Canter", "tag": "KDB 456B", **extra}


def fill(actor, category, site, vehicle=None, **extra):
    return finance.record_expense(
        actor=actor,
        category=category,
        amount=extra.pop("amount", D("1000.00")),
        incurred_on=extra.pop("incurred_on", date(2026, 10, 5)),
        site=site,
        vehicle=vehicle,
        **extra,
    )


class TestRegister:
    def test_create_writes_the_first_handover_and_reads_back_the_contract_keys(
        self, client, keeper, driver
    ):
        made = Api(client, keeper).post("assets", asset_body(holder=driver.pk, cost="900000"))

        assert made.status_code == 201, made.content
        body = made.json()
        assert body["holder"] == driver.pk
        assert body["holder_name"] == "Dan Driver"
        assert body["status"] == "ACTIVE"
        assert body["cost"] == "900000.00"
        history = Api(client, keeper).get(f"assets/{body['id']}/handovers").json()
        assert len(history) == 1
        assert history[0]["from_holder"] is None
        assert history[0]["to_holder"] == driver.pk
        assert history[0]["to_holder_name"] == "Dan Driver"

    def test_a_vehicle_with_no_holder_is_in_the_yard(self, client, keeper):
        body = Api(client, keeper).post("assets", asset_body()).json()

        assert body["holder"] is None
        assert body["holder_name"] is None
        assert Api(client, keeper).get(f"assets/{body['id']}/handovers").json() == []

    def test_only_asset_manage_writes(self, client, driver, truck):
        http = Api(client, driver)

        assert http.post("assets", asset_body()).status_code == 403
        assert http.patch(f"assets/{truck.pk}", {"name": "x"}).status_code == 403
        assert http.post(f"assets/{truck.pk}/close", {}).status_code == 403

    def test_everyone_reads_but_cost_is_withheld_from_those_without_it(
        self, client, keeper, driver, fin, truck
    ):
        Asset.objects.filter(pk=truck.pk).update(cost=D("500"), purchase_terms="Cash")

        plain = results(Api(client, driver).get("assets"))[0]
        manager = results(Api(client, keeper).get("assets"))[0]
        finance_view = results(Api(client, fin).get("assets"))[0]

        assert "cost" not in plain and "purchase_terms" not in plain
        assert manager["cost"] == "500.00"
        assert finance_view["purchase_terms"] == "Cash"

    def test_the_list_filters(self, client, keeper, driver, truck):
        other = services.create_asset(
            actor=keeper, holder=None, type="TOOL", name="Drill", tag=""
        )
        http = Api(client, keeper)

        assert [r["id"] for r in results(http.get("assets", type="TOOL"))] == [other.pk]
        assert [r["id"] for r in results(http.get("assets", holder=driver.pk))] == [truck.pk]
        assert [r["id"] for r in results(http.get("assets", search="kda123"))] == []
        assert [r["id"] for r in results(http.get("assets", search="Hilux"))] == [truck.pk]
        assert len(results(http.get("assets", status="ACTIVE"))) == 2

    def test_a_tag_is_unique_however_typed_and_names_the_other(self, client, keeper, truck):
        clash = Api(client, keeper).post("assets", asset_body(tag="kda-123a"))

        assert clash.status_code == 409
        assert error_code(clash) == "ASSET_TAG_DUPLICATE"
        assert clash.json()["error"]["details"]["asset"]["id"] == truck.pk

    def test_a_vehicle_needs_a_registration_and_others_have_no_vehicle_fields(
        self, client, keeper
    ):
        http = Api(client, keeper)

        assert http.post("assets", asset_body(tag="")).status_code == 400
        assert http.post("assets", asset_body(type="TOOL", make="Bosch")).status_code == 400

    def test_edit_changes_details_but_not_the_holder(self, client, keeper, stranger, truck):
        http = Api(client, keeper)

        assert http.patch(f"assets/{truck.pk}", {"name": "Hilux 2"}).json()["name"] == "Hilux 2"
        refused = http.patch(f"assets/{truck.pk}", {"holder": stranger.pk})
        assert refused.status_code == 400
        truck.refresh_from_db()
        assert truck.name == "Hilux 2"
        assert truck.holder.full_name == "Dan Driver"

    def test_there_is_no_delete(self, client, keeper, truck):
        assert Api(client, keeper).delete(f"assets/{truck.pk}").status_code == 405


class TestHandover:
    def test_the_holder_can_give_it_on_and_history_is_newest_first(
        self, client, keeper, driver, stranger, truck
    ):
        gave = Api(client, driver).post(
            f"assets/{truck.pk}/handover", {"to_holder": stranger.pk, "note": "Shift change"}
        )
        back = Api(client, keeper).post(f"assets/{truck.pk}/handover", {"to_holder": None})

        assert gave.status_code == 201, gave.content
        assert back.status_code == 201
        history = Api(client, keeper).get(f"assets/{truck.pk}/handovers").json()
        assert [(h["from_holder_name"], h["to_holder_name"]) for h in history] == [
            ("Sue Stranger", None),
            ("Dan Driver", "Sue Stranger"),
            (None, "Dan Driver"),
        ]
        assert history[1]["note"] == "Shift change"
        assert history[1]["handed_over_by_name"] == "Dan Driver"
        truck.refresh_from_db()
        assert truck.holder is None

    def test_a_stranger_cannot_take_it(self, client, stranger, truck):
        refused = Api(client, stranger).post(
            f"assets/{truck.pk}/handover", {"to_holder": stranger.pk}
        )

        assert refused.status_code == 403
        assert AssetHandover.objects.filter(asset=truck).count() == 1

    def test_the_manager_can_reassign_anything(self, client, keeper, stranger, truck):
        done = Api(client, keeper).post(f"assets/{truck.pk}/handover", {"to_holder": stranger.pk})

        assert done.status_code == 201

    def test_handing_to_the_current_holder_is_refused(self, client, keeper, driver, truck):
        http = Api(client, keeper)

        assert error_code(http.post(f"assets/{truck.pk}/handover", {"to_holder": driver.pk})) == (
            "ASSET_ALREADY_WITH"
        )
        http.post(f"assets/{truck.pk}/handover", {"to_holder": None})
        assert error_code(http.post(f"assets/{truck.pk}/handover", {"to_holder": None})) == (
            "ASSET_ALREADY_WITH"
        )

    def test_a_future_date_and_an_inactive_person_are_refused_but_back_dating_is_fine(
        self, client, keeper, stranger, truck
    ):
        http = Api(client, keeper)
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()

        assert error_code(
            http.post(
                f"assets/{truck.pk}/handover",
                {"to_holder": stranger.pk, "handed_over_on": tomorrow},
            )
        ) == "ASSET_DATE_IN_FUTURE"
        UserFactory._meta.model.objects.filter(pk=stranger.pk).update(is_active=False)
        assert (
            http.post(f"assets/{truck.pk}/handover", {"to_holder": stranger.pk}).status_code
            == 400
        )
        UserFactory._meta.model.objects.filter(pk=stranger.pk).update(is_active=True)
        old = http.post(
            f"assets/{truck.pk}/handover",
            {"to_holder": stranger.pk, "handed_over_on": "2026-01-02"},
        )
        assert old.status_code == 201
        assert old.json()["handed_over_on"] == "2026-01-02"

    def test_history_cannot_be_rewritten(self, truck):
        row = AssetHandover.objects.get(asset=truck)

        with pytest.raises(Exception), transaction.atomic():  # noqa: B017
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE assets_assethandover SET note = 'edited' WHERE id = %s", [row.pk]
                )

    def test_a_handover_back_to_the_original_holder_is_a_new_row(
        self, keeper, driver, stranger, truck
    ):
        services.hand_over(truck, actor=keeper, to_holder=stranger)
        services.hand_over(truck, actor=keeper, to_holder=driver)

        assert AssetHandover.objects.filter(asset=truck).count() == 3

    def test_a_second_handover_from_a_stale_view_is_refused_not_doubled(
        self, keeper, driver, stranger, truck
    ):
        """The lock re-reads the holder, so the loser sees the new one (§4.20.4)."""
        stale = Asset.objects.get(pk=truck.pk)
        services.hand_over(truck, actor=driver, to_holder=stranger)

        with pytest.raises(services.AssetAlreadyWith):
            services.hand_over(stale, actor=keeper, to_holder=stranger)
        assert AssetHandover.objects.filter(asset=truck).count() == 2


class TestClose:
    def test_close_ends_the_history_with_a_handover_to_the_yard(
        self, client, keeper, driver, truck
    ):
        today = timezone.localdate().isoformat()
        done = Api(client, keeper).post(
            f"assets/{truck.pk}/close",
            {"closed_on": today, "closed_reason": "SOLD", "closed_note": "Sold to Jo"},
        )

        assert done.status_code == 200, done.content
        body = done.json()
        assert body["status"] == "CLOSED"
        assert body["closed_reason"] == "SOLD"
        assert body["holder"] is None
        history = Api(client, keeper).get(f"assets/{truck.pk}/handovers").json()
        assert history[0]["from_holder"] == driver.pk and history[0]["to_holder"] is None
        assert history[0]["handed_over_on"] == today

    def test_a_closed_asset_is_frozen(self, client, keeper, stranger, truck):
        http = Api(client, keeper)
        http.post(
            f"assets/{truck.pk}/close", {"closed_on": "2026-10-01", "closed_reason": "WRITTEN_OFF"}
        )

        assert error_code(http.patch(f"assets/{truck.pk}", {"name": "x"})) == "ASSET_CLOSED"
        assert (
            error_code(http.post(f"assets/{truck.pk}/handover", {"to_holder": stranger.pk}))
            == "ASSET_CLOSED"
        )
        assert (
            error_code(
                http.post(
                    f"assets/{truck.pk}/close",
                    {"closed_on": "2026-10-02", "closed_reason": "SOLD"},
                )
            )
            == "ASSET_CLOSED"
        )

    def test_the_reason_is_required_and_dates_are_checked(self, client, keeper, truck):
        http = Api(client, keeper)
        Asset.objects.filter(pk=truck.pk).update(purchase_date=date(2026, 6, 1))
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()

        assert http.post(f"assets/{truck.pk}/close", {"closed_on": "2026-10-01"}).status_code == 400
        assert (
            http.post(
                f"assets/{truck.pk}/close", {"closed_on": "2026-05-01", "closed_reason": "SOLD"}
            ).status_code
            == 400
        )
        assert (
            error_code(
                http.post(
                    f"assets/{truck.pk}/close", {"closed_on": tomorrow, "closed_reason": "SOLD"}
                )
            )
            == "ASSET_DATE_IN_FUTURE"
        )


class TestFuelByVehicle:
    def test_it_matches_the_cost_set_and_shows_pending_apart(
        self, client, keeper, driver, pm, fin, site, fuel, truck
    ):
        approved = fill(driver, fuel, site, truck, amount=D("6000"), litres=D("40"))
        paid = fill(driver, fuel, site, truck, amount=D("4000"), litres=D("20"))
        fill(driver, fuel, site, truck, amount=D("999"), litres=D("5"))  # pending
        rejected = fill(driver, fuel, site, truck, amount=D("777"), litres=D("7"))
        approve_through(approved, pm=pm, finance_user=fin)
        approve_through(paid, pm=pm, finance_user=fin)
        finance.decide(rejected, actor=pm, approved=False, reason="No receipt")
        finance.mark_paid(paid, actor=fin, reference="MPESA1")

        body = Api(client, driver).get(f"assets/{truck.pk}/fuel").json()

        assert body == {
            "litres": "60.00",
            "spend": "10000.00",
            "fill_count": 2,
            "spend_per_litre": "166.67",
            "pending_spend": "999.00",
        }
        # The same figure the project's cost counts (§4.17.11).
        from commercials.costing import expense_cost

        assert D(body["spend"]) == expense_cost(approved.project)

    def test_an_unfilled_window_is_zero_and_litres_unknown(self, client, driver, truck):
        body = Api(client, driver).get(f"assets/{truck.pk}/fuel").json()

        assert body["litres"] is None
        assert body["spend"] == "0.00"
        assert body["fill_count"] == 0
        assert body["spend_per_litre"] is None
        assert body["pending_spend"] == "0.00"

    def test_the_window_bounds_by_incurred_date(
        self, client, driver, pm, fin, site, fuel, truck
    ):
        old = fill(driver, fuel, site, truck, incurred_on=date(2026, 8, 1), amount=D("100"))
        new = fill(driver, fuel, site, truck, incurred_on=date(2026, 10, 5), amount=D("200"))
        approve_through(old, pm=pm, finance_user=fin)
        approve_through(new, pm=pm, finance_user=fin)

        body = Api(client, driver).get(f"assets/{truck.pk}/fuel", **{"from": "2026-10-01"}).json()

        assert body["spend"] == "200.00"
        assert (
            Api(client, driver).get(f"assets/{truck.pk}/fuel", **{"from": "bad"}).status_code
            == 400
        )

    def test_a_reversal_takes_the_fill_back_off(
        self, client, driver, pm, fin, site, fuel, truck
    ):
        made = fill(driver, fuel, site, truck, amount=D("500"), litres=D("5"))
        approve_through(made, pm=pm, finance_user=fin)
        from commercials.services import reverse_expense

        reverse_expense(made, actor=fin, reason="Wrong vehicle")

        body = Api(client, driver).get(f"assets/{truck.pk}/fuel").json()

        assert body["spend"] == "0.00"
        assert body["fill_count"] == 0

    def test_the_ranking_is_for_the_owner_and_orders_by_spend(
        self, client, keeper, driver, pm, fin, site, fuel, truck
    ):
        small = services.create_asset(actor=keeper, type="GENERATOR", name="Gen", tag="")
        for vehicle, amount in ((truck, "100"), (small, "900")):
            entry = fill(driver, fuel, site, vehicle, amount=D(amount))
            approve_through(entry, pm=pm, finance_user=fin)

        ranked = Api(client, keeper).get("assets/fuel-summary").json()

        assert [r["name"] for r in ranked] == ["Gen", "Hilux"]
        assert ranked[0]["spend"] == "900.00"
        assert Api(client, driver).get("assets/fuel-summary").status_code == 403


class TestFuelExpense:
    def test_a_vehicle_alone_fills_the_registration(self, client, driver, site, fuel, truck):
        made = Api(client, driver).post(
            "project-expenses",
            {
                "category": fuel.pk,
                "site": site.pk,
                "amount": "1000",
                "incurred_on": "2026-10-05",
                "vehicle": truck.pk,
                "litres": "10",
            },
        )

        assert made.status_code == 201, made.content
        body = made.json()
        assert body["vehicle"] == truck.pk
        assert body["vehicle_name"] == "Hilux"
        assert body["vehicle_reg"] == "KDA 123A"

    def test_the_frontend_body_with_a_blank_registration_is_accepted(
        self, client, driver, site, fuel, truck
    ):
        made = Api(client, driver).post(
            "project-expenses",
            {
                "category": fuel.pk,
                "site": site.pk,
                "amount": "1000",
                "incurred_on": "2026-10-05",
                "vehicle": truck.pk,
                "vehicle_reg": "",
                "litres": None,
            },
        )

        assert made.status_code == 201, made.content

    def test_not_ours_keeps_the_typed_registration_and_old_payloads_work(
        self, client, driver, site, fuel
    ):
        made = Api(client, driver).post(
            "project-expenses",
            {
                "category": fuel.pk,
                "site": site.pk,
                "amount": "1000",
                "incurred_on": "2026-10-05",
                "vehicle": None,
                "vehicle_reg": "KZZ 999Z",
            },
        )

        assert made.status_code == 201, made.content
        assert made.json()["vehicle"] is None
        assert made.json()["vehicle_reg"] == "KZZ 999Z"

    def test_fuel_with_neither_is_refused(self, client, driver, site, fuel):
        refused = Api(client, driver).post(
            "project-expenses",
            {"category": fuel.pk, "site": site.pk, "amount": "1000", "incurred_on": "2026-10-05"},
        )

        assert refused.status_code == 400
        assert error_code(refused) == "FUEL_VEHICLE_REQUIRED"

    def test_a_different_typed_registration_is_a_mismatch(self, driver, site, fuel, truck):
        with pytest.raises(finance.FuelVehicleMismatch):
            fill(driver, fuel, site, truck, vehicle_reg="KXX 000X")

        made = fill(driver, fuel, site, truck, vehicle_reg="kda-123a")
        assert made.vehicle_reg == "KDA 123A"

    def test_a_generator_is_allowed_and_a_tool_is_not(self, keeper, driver, site, fuel):
        gen = services.create_asset(actor=keeper, type="GENERATOR", name="Gen", tag="")
        tool = services.create_asset(actor=keeper, type="TOOL", name="Drill", tag="")

        assert fill(driver, fuel, site, gen).vehicle == gen
        with pytest.raises(Exception) as caught:
            fill(driver, fuel, site, tool)
        assert caught.value.field_errors["vehicle"]

    def test_a_closed_asset_is_refused(self, keeper, driver, site, fuel, truck):
        services.close_asset(
            truck, actor=keeper, closed_on=date(2026, 10, 1), closed_reason="SOLD"
        )

        with pytest.raises(services.AssetClosed):
            fill(driver, fuel, site, truck)

    def test_only_fuel_names_a_vehicle(self, driver, site, misc, truck):
        with pytest.raises(Exception) as caught:
            fill(driver, misc, site, truck)
        assert caught.value.field_errors["vehicle"]

    def test_another_tenants_vehicle_does_not_exist_here(
        self, client, driver, site, fuel, other_organization
    ):
        from core.tenancy import tenant_context

        with tenant_context(other_organization):
            theirs = Asset.objects.create(
                organization=other_organization, type=AssetType.VEHICLE, name="T", tag="OTH 1"
            )

        refused = Api(client, driver).post(
            "project-expenses",
            {
                "category": fuel.pk,
                "site": site.pk,
                "amount": "1",
                "incurred_on": "2026-10-05",
                "vehicle": theirs.pk,
            },
        )

        assert refused.status_code == 400

    def test_a_queued_expense_names_the_vehicle_on_replay_and_a_closed_one_is_kept_as_an_exception(
        self, tenant, keeper, driver, fin, site, fuel, truck
    ):
        body = {
            "category": fuel.pk,
            "site": site.pk,
            "amount": "1500.00",
            "incurred_on": "2026-10-05",
            "vehicle": truck.pk,
        }
        uuid = uuid4()
        sub, _ = apply_submission(
            organization=tenant,
            client_uuid=uuid,
            operation="EXPENSE",
            payload={**body, "client_uuid": str(uuid)},
            submitted_by=driver,
        )
        assert sub.status == SubmissionStatus.APPLIED
        assert ProjectExpense.objects.get().vehicle == truck

        services.close_asset(
            truck, actor=keeper, closed_on=date(2026, 10, 6), closed_reason="SOLD"
        )
        uuid2 = uuid4()
        sub2, _ = apply_submission(
            organization=tenant,
            client_uuid=uuid2,
            operation="EXPENSE",
            payload={**body, "client_uuid": str(uuid2)},
            submitted_by=driver,
        )
        assert sub2.status == SubmissionStatus.REJECTED
        assert SyncException.objects.get().code == "ASSET_CLOSED"


@pytest.fixture
def yard(tenant):
    from locations.factories import YardFactory

    return YardFactory(name="Yard")


@pytest.fixture
def item(tenant):
    from catalogue.factories import ItemTypeFactory

    return ItemTypeFactory(name="Clamp", uom="ea")


def make_supplier(registrar, name="Kenya Cable Ltd", **extra):
    return Supplier.objects.create(
        organization=registrar.organization, name=name, registered_by=registrar, **extra
    )


def gate_in_body(yard, item, **extra):
    return {
        "source_type": "PURCHASE",
        "to_location": yard.pk,
        "received_at": timezone.now().isoformat(),
        "lines": [
            {
                "item_type": item.pk,
                "tracking_mode": "BULK",
                "quantity": "10",
                "uom": "ea",
                "condition": "NEW",
            }
        ],
        **extra,
    }


class TestGateInSupplier:
    def test_a_supplier_fills_the_name_and_reads_back_its_status(
        self, client, keeper, yard, item
    ):
        supplier = make_supplier(keeper)
        http = Api(client, keeper)

        made = http.post(
            "gate-ins", gate_in_body(yard, item, supplier=supplier.pk, supplier_name="typo")
        )

        assert made.status_code == 201, made.content
        body = made.json()
        assert body["supplier"] == supplier.pk
        assert body["supplier_name"] == "Kenya Cable Ltd"
        assert body["supplier_status"] == "PENDING"
        listed = results(http.get("gate-ins", supplier=supplier.pk))
        assert [r["id"] for r in listed] == [body["id"]]
        assert results(http.get("gate-ins", search="kenya cable"))[0]["id"] == body["id"]

    def test_text_only_still_works(self, client, keeper, yard, item):
        made = Api(client, keeper).post(
            "gate-ins", gate_in_body(yard, item, supplier_name="Old Free Text")
        )

        assert made.status_code == 201
        assert made.json()["supplier"] is None
        assert made.json()["supplier_name"] == "Old Free Text"
        assert made.json()["supplier_status"] == ""

    def test_inactive_or_rejected_is_refused_on_new_but_pending_is_fine(
        self, client, keeper, yard, item
    ):
        http = Api(client, keeper)
        inactive = make_supplier(keeper, "Dormant", is_active=False)
        rejected = make_supplier(keeper, "Bad", status=SupplierStatus.REJECTED)
        approved = make_supplier(keeper, "Good", status=SupplierStatus.APPROVED)

        for supplier in (inactive, rejected):
            refused = http.post("gate-ins", gate_in_body(yard, item, supplier=supplier.pk))
            assert refused.status_code == 400
            assert error_code(refused) == "SUPPLIER_NOT_USABLE_ON_GATE_IN"
        assert (
            http.post("gate-ins", gate_in_body(yard, item, supplier=approved.pk)).status_code
            == 201
        )

    def test_a_draft_keeps_a_supplier_that_has_since_gone_inactive(
        self, client, keeper, yard, item
    ):
        http = Api(client, keeper)
        supplier = make_supplier(keeper)
        made = http.post("gate-ins", gate_in_body(yard, item, supplier=supplier.pk)).json()
        Supplier.objects.filter(pk=supplier.pk).update(is_active=False)

        edited = http.patch(
            f"gate-ins/{made['id']}", {"supplier": supplier.pk, "notes": "late truck"}
        )

        assert edited.status_code == 200, edited.content
        assert edited.json()["supplier_name"] == "Kenya Cable Ltd"

    def test_a_queued_gate_in_with_a_supplier_posts_on_replay(
        self, tenant, keeper, yard, item
    ):
        supplier = make_supplier(keeper)
        uuid = uuid4()
        sub, _ = apply_submission(
            organization=tenant,
            client_uuid=uuid,
            operation="GATE_IN",
            payload={**gate_in_body(yard, item, supplier=supplier.pk), "client_uuid": str(uuid)},
            submitted_by=keeper,
        )

        assert sub.status == SubmissionStatus.APPLIED
        from receiving.models import GateIn

        gate_in = GateIn.objects.get()
        assert gate_in.supplier == supplier
        assert gate_in.supplier_name == "Kenya Cable Ltd"

    def test_a_queued_gate_in_naming_a_rejected_supplier_becomes_an_exception(
        self, tenant, keeper, yard, item
    ):
        rejected = make_supplier(keeper, "Bad", status=SupplierStatus.REJECTED)
        uuid = uuid4()
        sub, _ = apply_submission(
            organization=tenant,
            client_uuid=uuid,
            operation="GATE_IN",
            payload={**gate_in_body(yard, item, supplier=rejected.pk), "client_uuid": str(uuid)},
            submitted_by=keeper,
        )

        assert sub.status == SubmissionStatus.REJECTED
        assert SyncException.objects.get().code == "SUPPLIER_NOT_USABLE_ON_GATE_IN"

    def test_an_asset_may_name_its_supplier(self, client, keeper):
        supplier = make_supplier(keeper)

        made = Api(client, keeper).post("assets", asset_body(supplier=supplier.pk))

        assert made.status_code == 201, made.content
        assert made.json()["supplier_name"] == "Kenya Cable Ltd"


class TestDeactivationWithAnAsset:
    def test_a_holder_cannot_be_deactivated_while_holding(self, driver, truck):
        from custody.services import HolderStillHasMaterial, assert_can_deactivate

        with pytest.raises(HolderStillHasMaterial) as caught:
            assert_can_deactivate(driver)

        assert caught.value.details["assets"][0]["name"] == "Hilux"
