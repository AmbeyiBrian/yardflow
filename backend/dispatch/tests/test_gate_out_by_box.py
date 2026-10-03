"""T11.9 - gate-out by box (design 4.15.7, 4.15.8; P5, P6, P9, P10).

A pass can be picked from a box. Submit refuses a unit that is elsewhere, not in
stock or already promised to another open pass, and a box line beyond its claim;
release draws the claim and lets the ledger hooks close what empties; both read
sides carry the box chains the release scan matches against.
"""

from decimal import Decimal

import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.factories import UserFactory
from catalogue.factories import ItemTypeFactory
from catalogue.models import TrackingMode
from core.provisioning import provision_tenant
from core.tenancy import tenant_context
from dispatch.documents import gate_pass_context
from dispatch.models import GateOut, GateOutLineSerial, GateOutStatus
from dispatch.services import (
    UnitNotAvailable,
    cancel_gate_out,
    release_gate_out,
    submit_gate_out,
)
from dispatch.tests.test_gate_out import add_line, make_gate_out, stock_in
from locations.factories import YardFactory
from locations.nodes import external_node
from stock.models import (
    Box,
    BoxBulkContent,
    BoxSource,
    BoxStatus,
    Condition,
    MovementType,
    SerialUnit,
    SerialUnitStatus,
)
from stock.services import BoxClaimShort, MovementRequest, post_movement
from stock.verification import verify_ledger


@pytest.fixture
def yard(tenant):
    return YardFactory(name="Box yard")


@pytest.fixture
def people(tenant):
    return UserFactory(organization=tenant), UserFactory(organization=tenant)


@pytest.fixture
def radio(tenant):
    return ItemTypeFactory(name="Box radio", default_tracking_mode=TrackingMode.SERIALIZED)


@pytest.fixture
def jumper(tenant):
    return ItemTypeFactory(name="Box jumper", default_tracking_mode=TrackingMode.BULK)


def receive_units(org, node, item, count, prefix="BX"):
    units = []
    for n in range(count):
        unit = SerialUnit.objects.create(
            organization=org,
            item_type=item,
            serial_number=f"{prefix}-{n + 1}",
            asset_tag=f"{prefix}-TAG-{n + 1}",
            current_node=external_node(org.pk),
        )
        with transaction.atomic():
            post_movement(
                MovementRequest(
                    item_type=item,
                    quantity=Decimal("1"),
                    from_node=external_node(org.pk),
                    to_node=node,
                    movement_type=MovementType.RECEIPT,
                    tracking_mode=TrackingMode.SERIALIZED,
                    serial_unit=unit,
                )
            )
        unit.refresh_from_db()
        units.append(unit)
    return units


def make_box(code, node, parent=None):
    return Box.objects.create(
        code=code,
        source=BoxSource.INTERNAL,
        current_node=node,
        parent=parent,
        depth=parent.depth + 1 if parent else 1,
    )


def put(units, box):
    SerialUnit.objects.filter(pk__in=[u.pk for u in units]).update(box=box)


def claim(box, item, quantity, condition=Condition.NEW):
    return BoxBulkContent.objects.create(
        box=box, item_type=item, condition=condition, quantity=Decimal(quantity)
    )


def name(org, line, units):
    for unit in units:
        GateOutLineSerial.objects.create(organization=org, line=line, serial_unit=unit)


def unit_line(org, gate_out, item, units, box=None):
    line = add_line(
        gate_out, item, len(units), tracking_mode=TrackingMode.SERIALIZED, box=box
    )
    name(org, line, units)
    return line


def no_drift(org):
    result = verify_ledger(org.pk)
    assert result.ok, result.summary()


class TestReleaseFromABox:
    def test_a_whole_box_released_leaves_it_and_its_pallet_closed(
        self, tenant, yard, people, radio
    ):
        pallet = make_box("PAL-1", yard.node)
        carton = make_box("CTN-1", yard.node, parent=pallet)
        units = receive_units(tenant, yard.node, radio, 3)
        put(units, carton)
        gate_out = make_gate_out(tenant, yard, *people)
        line = unit_line(tenant, gate_out, radio, units, box=carton)

        submit_gate_out(gate_out, submitted_by=people[0])
        gate_out.refresh_from_db()
        assert gate_out.status == GateOutStatus.APPROVED
        release_gate_out(gate_out, released_by=people[0])

        carton.refresh_from_db()
        pallet.refresh_from_db()
        assert carton.status == BoxStatus.CLOSED
        assert pallet.status == BoxStatus.CLOSED
        assert all(SerialUnit.objects.get(pk=u.pk).box_id is None for u in units)
        line.refresh_from_db()
        assert line.released_qty == 3
        no_drift(tenant)

    def test_one_unit_released_leaves_the_rest_in_the_box(self, tenant, yard, people, radio):
        carton = make_box("CTN-2", yard.node)
        units = receive_units(tenant, yard.node, radio, 3)
        put(units, carton)
        gate_out = make_gate_out(tenant, yard, *people)
        unit_line(tenant, gate_out, radio, units[:1], box=carton)

        submit_gate_out(gate_out, submitted_by=people[0])
        gate_out.refresh_from_db()
        release_gate_out(gate_out, released_by=people[0])

        carton.refresh_from_db()
        assert carton.status == BoxStatus.OPEN
        assert SerialUnit.objects.get(pk=units[0].pk).box_id is None
        assert SerialUnit.objects.filter(box=carton).count() == 2
        no_drift(tenant)

    def test_a_bulk_box_line_draws_the_claim_and_closes_the_emptied_box(
        self, tenant, yard, people, jumper
    ):
        stock_in(tenant, yard.node, jumper, 10)
        carton = make_box("CTN-3", yard.node)
        claim(carton, jumper, 10)
        gate_out = make_gate_out(tenant, yard, *people)
        add_line(gate_out, jumper, 4, box=carton)

        submit_gate_out(gate_out, submitted_by=people[0])
        gate_out.refresh_from_db()
        release_gate_out(gate_out, released_by=people[0])
        assert BoxBulkContent.objects.get(box=carton).quantity == Decimal("6")
        carton.refresh_from_db()
        assert carton.status == BoxStatus.OPEN

        second = make_gate_out(tenant, yard, *people)
        add_line(second, jumper, 6, box=carton)
        submit_gate_out(second, submitted_by=people[0])
        second.refresh_from_db()
        release_gate_out(second, released_by=people[0])
        carton.refresh_from_db()
        assert carton.status == BoxStatus.CLOSED
        assert not BoxBulkContent.objects.filter(box=carton).exists()
        no_drift(tenant)


class TestSubmitRefusals:
    def test_a_unit_somewhere_else_is_refused_by_name(self, tenant, yard, people, radio):
        elsewhere = YardFactory(name="Other yard")
        stray = receive_units(tenant, elsewhere.node, radio, 1, prefix="FAR")
        # The yard holds a radio too, so the balance check passes and it is the
        # *named* unit that is wrong.
        receive_units(tenant, yard.node, radio, 1, prefix="NEAR")
        gate_out = make_gate_out(tenant, yard, *people)
        unit_line(tenant, gate_out, radio, stray)
        with pytest.raises(UnitNotAvailable, match="FAR-1") as caught:
            submit_gate_out(gate_out, submitted_by=people[0])
        assert caught.value.code == "UNIT_NOT_AVAILABLE"
        assert caught.value.status_code == 409
        assert "Other yard" in caught.value.message

    def test_a_unit_not_in_stock_is_refused(self, tenant, yard, people, radio):
        units = receive_units(tenant, yard.node, radio, 1, prefix="QUA")
        SerialUnit.objects.filter(pk=units[0].pk).update(status=SerialUnitStatus.QUARANTINED)
        gate_out = make_gate_out(tenant, yard, *people)
        unit_line(tenant, gate_out, radio, units)
        with pytest.raises(UnitNotAvailable, match="QUA-1 is quarantined"):
            submit_gate_out(gate_out, submitted_by=people[0])

    def test_a_unit_on_another_open_pass_is_refused_naming_that_pass(
        self, tenant, yard, people, radio
    ):
        units = receive_units(tenant, yard.node, radio, 2, prefix="DUP")
        first = make_gate_out(tenant, yard, *people)
        unit_line(tenant, first, radio, units)
        submit_gate_out(first, submitted_by=people[0])

        second = make_gate_out(tenant, yard, *people)
        unit_line(tenant, second, radio, units[:1])
        with pytest.raises(UnitNotAvailable, match=f"DUP-1 is already on gate pass {first.number}"):
            submit_gate_out(second, submitted_by=people[0])

    def test_a_draft_does_not_hold_a_unit_but_a_cancelled_pass_lets_it_go(
        self, tenant, yard, people, radio
    ):
        units = receive_units(tenant, yard.node, radio, 1, prefix="DFT")
        draft = make_gate_out(tenant, yard, *people)
        unit_line(tenant, draft, radio, units)
        # The draft above holds nothing, so another can take the unit.
        other = make_gate_out(tenant, yard, *people)
        unit_line(tenant, other, radio, units)
        submit_gate_out(other, submitted_by=people[0])
        with pytest.raises(UnitNotAvailable):
            submit_gate_out(draft, submitted_by=people[0])

        cancel_gate_out(other, reason="not needed", cancelled_by=people[0])
        submit_gate_out(draft, submitted_by=people[0])

    def test_a_bulk_box_line_beyond_the_claim_is_refused(
        self, tenant, yard, people, jumper
    ):
        stock_in(tenant, yard.node, jumper, 20)
        carton = make_box("CTN-4", yard.node)
        claim(carton, jumper, 5)
        gate_out = make_gate_out(tenant, yard, *people)
        add_line(gate_out, jumper, 6, box=carton)
        with pytest.raises(BoxClaimShort, match="CTN-4 holds 5"):
            submit_gate_out(gate_out, submitted_by=people[0])

    def test_another_open_pass_on_the_same_claim_counts_against_it(
        self, tenant, yard, people, jumper
    ):
        stock_in(tenant, yard.node, jumper, 20)
        carton = make_box("CTN-5", yard.node)
        claim(carton, jumper, 5)
        first = make_gate_out(tenant, yard, *people)
        add_line(first, jumper, 3, box=carton)
        submit_gate_out(first, submitted_by=people[0])

        second = make_gate_out(tenant, yard, *people)
        add_line(second, jumper, 3, box=carton)
        with pytest.raises(BoxClaimShort, match=f"CTN-5.*{first.number}"):
            submit_gate_out(second, submitted_by=people[0])

        small = make_gate_out(tenant, yard, *people)
        add_line(small, jumper, 2, box=carton)
        submit_gate_out(small, submitted_by=people[0])


@pytest.fixture
def signed_in(db, client, settings):
    settings.TENANT_BASE_DOMAIN = "localhost"
    result = provision_tenant(
        name="Boxco", slug="boxco", owner_email="owner@boxco.co.ke"
    )
    owner = result["owner"]
    owner.set_password("a good long password")
    owner.save()
    client.defaults["HTTP_HOST"] = "boxco.localhost"
    token = client.post(
        reverse("v1:auth:login"),
        {"identifier": "owner@boxco.co.ke", "password": "a good long password"},
        content_type="application/json",
    ).json()["access"]
    return client, {"HTTP_AUTHORIZATION": f"Bearer {token}"}, result["organization"], owner


class TestLineValidation:
    @pytest.fixture
    def world(self, signed_in):
        http, auth, org, owner = signed_in
        with tenant_context(org):
            from network.factories import SiteFactory

            yard = YardFactory(name="Api yard")
            site = SiteFactory()
            radio = ItemTypeFactory(
                name="Api radio", default_tracking_mode=TrackingMode.SERIALIZED
            )
            jumper = ItemTypeFactory(name="Api jumper", default_tracking_mode=TrackingMode.BULK)
            units = receive_units(org, yard.node, radio, 2, prefix="API")
            stranger = receive_units(org, yard.node, radio, 1, prefix="LOOSE")
            carton = make_box("API-CTN", yard.node)
            put(units, carton)
            stock_in(org, yard.node, jumper, 5)
            return {
                "http": http,
                "auth": auth,
                "owner": owner,
                "yard": yard,
                "site": site,
                "radio": radio,
                "jumper": jumper,
                "units": units,
                "stranger": stranger[0],
                "carton": carton,
                "org": org,
            }

    def post(self, w, line, yard=None):
        return w["http"].post(
            reverse("v1:gate-out-list"),
            {
                "purpose_type": "INSTALLATION",
                "from_location": (yard or w["yard"]).pk,
                "site": w["site"].pk,
                "custody_holder": w["owner"].pk,
                "lines": [line],
            },
            content_type="application/json",
            **w["auth"],
        )

    def serial_line(self, w, units, box):
        return {
            "item_type": w["radio"].pk,
            "tracking_mode": "SERIALIZED",
            "requested_qty": str(len(units)),
            "uom": "ea",
            "box": box.pk,
            "serials": [{"serial_unit": u.pk} for u in units],
        }

    def test_a_box_line_is_accepted_and_echoed(self, world):
        w = world
        response = self.post(w, self.serial_line(w, w["units"], w["carton"]))
        assert response.status_code == 201, response.content
        line = response.json()["lines"][0]
        assert line["box"] == w["carton"].pk
        assert line["box_code"] == "API-CTN"
        assert line["box_path"] == ["API-CTN"]

    def test_a_unit_outside_the_box_is_refused(self, world):
        w = world
        response = self.post(w, self.serial_line(w, [w["stranger"]], w["carton"]))
        assert response.status_code == 400
        assert "LOOSE-1" in response.content.decode()

    def test_a_closed_box_is_refused(self, world):
        w = world
        with tenant_context(w["org"]):
            Box.objects.filter(pk=w["carton"].pk).update(
                status=BoxStatus.CLOSED, closed_at=timezone.now()
            )
        response = self.post(w, self.serial_line(w, w["units"], w["carton"]))
        assert response.status_code == 400
        assert "closed" in response.content.decode()

    def test_a_box_at_another_location_is_refused(self, world):
        w = world
        with tenant_context(w["org"]):
            other = YardFactory(name="Elsewhere")
        response = self.post(w, self.serial_line(w, w["units"], w["carton"]), yard=other)
        assert response.status_code == 400
        assert "not at Elsewhere" in response.content.decode()

    def test_a_reel_line_cannot_have_a_box(self, world):
        w = world
        response = self.post(
            w,
            {
                "item_type": w["jumper"].pk,
                "tracking_mode": "REEL",
                "requested_qty": "5",
                "uom": "m",
                "box": w["carton"].pk,
            },
        )
        assert response.status_code == 400
        assert "reel" in response.content.decode().lower()

    def test_a_bulk_line_needs_a_claim_in_the_box(self, world):
        w = world
        response = self.post(
            w,
            {
                "item_type": w["jumper"].pk,
                "tracking_mode": "BULK",
                "requested_qty": "2",
                "uom": "ea",
                "box": w["carton"].pk,
            },
        )
        assert response.status_code == 400
        assert "holds no" in response.content.decode()

    def test_a_missing_box_is_refused(self, world):
        w = world
        line = self.serial_line(w, w["units"], w["carton"])
        line["box"] = 99999999
        assert self.post(w, line).status_code == 400


class TestReadSides:
    @pytest.fixture
    def world(self, signed_in):
        http, auth, org, owner = signed_in
        with tenant_context(org):
            yard = YardFactory(name="Read yard")
            radio = ItemTypeFactory(
                name="Read radio", default_tracking_mode=TrackingMode.SERIALIZED
            )
            jumper = ItemTypeFactory(name="Read jumper", default_tracking_mode=TrackingMode.BULK)
            pallet = make_box("PAL-9", yard.node)
            carton = make_box("CTN-9", yard.node, parent=pallet)
            units = receive_units(org, yard.node, radio, 10, prefix="RD")
            put(units[:8], carton)
            put(units[8:9], pallet)
            # units[9] stays loose.
            stock_in(org, yard.node, jumper, 4)
            drum = make_box("BAG-9", yard.node)
            claim(drum, jumper, 4)

            from network.factories import SiteFactory

            gate_out = GateOut.objects.create(
                organization=org,
                from_location=yard,
                site=SiteFactory(),
                purpose_type="INSTALLATION",
                custody_holder=owner,
                requested_by=owner,
            )
            unit_line(org, gate_out, radio, units, box=pallet)
            add_line(gate_out, jumper, 2, box=drum)
            submit_gate_out(gate_out, submitted_by=owner)
            GateOut.objects.filter(pk=gate_out.pk).update(status=GateOutStatus.APPROVED)
            gate_out.refresh_from_db()
            return {"http": http, "auth": auth, "gate_out": gate_out, "org": org, "units": units}

    def check_contract(self, lines, units):
        serial_line, bulk_line = lines
        assert serial_line["box_path"] == ["PAL-9"]
        assert serial_line["box_code"] == "PAL-9"
        assert bulk_line["box_path"] == ["BAG-9"]
        by_serial = {s["serial_number"]: s for s in serial_line["serials"]}
        assert by_serial["RD-1"]["box_path"] == ["PAL-9", "CTN-9"]
        assert by_serial["RD-9"]["box_path"] == ["PAL-9"]
        assert by_serial["RD-10"]["box_path"] == []
        sample = by_serial["RD-1"]
        assert sample["asset_tag"] == "RD-TAG-1"
        assert sample["released"] is False
        assert sample["serial_unit"] == units[0].pk
        assert "id" in sample

    def test_the_detail_carries_the_box_chains(self, world):
        w = world
        response = w["http"].get(
            reverse("v1:gate-out-detail", args=[w["gate_out"].pk]), **w["auth"]
        )
        assert response.status_code == 200
        self.check_contract(response.json()["lines"], w["units"])

    def test_the_releasable_bundle_carries_the_same_fields(self, world):
        w = world
        response = w["http"].get(reverse("v1:sync-bundle"), **w["auth"])
        assert response.status_code == 200
        rows = [r for r in response.json()["releasable_gate_outs"] if r["id"] == w["gate_out"].pk]
        assert rows
        self.check_contract(rows[0]["lines"], w["units"])

    def test_a_unit_that_moved_boxes_shows_its_current_chain(self, world):
        w = world
        with tenant_context(w["org"]):
            SerialUnit.objects.filter(pk=w["units"][0].pk).update(box=None)
        response = w["http"].get(
            reverse("v1:gate-out-detail", args=[w["gate_out"].pk]), **w["auth"]
        )
        by_serial = {s["serial_number"]: s for s in response.json()["lines"][0]["serials"]}
        assert by_serial["RD-1"]["box_path"] == []

    def test_box_paths_cost_a_constant_number_of_queries(self, world):
        w = world
        url = reverse("v1:gate-out-detail", args=[w["gate_out"].pk])
        bundle = reverse("v1:sync-bundle")
        w["http"].get(url, **w["auth"])  # warm caches
        with CaptureQueriesContext(connection) as detail:
            w["http"].get(url, **w["auth"])
        with CaptureQueriesContext(connection) as sync_bundle:
            w["http"].get(bundle, **w["auth"])

        # Add more units in more boxes and the count must not move.
        with tenant_context(w["org"]):
            line = w["gate_out"].lines.get(tracking_mode=TrackingMode.SERIALIZED)
            more = receive_units(
                w["org"], line.gate_out.from_location.node, line.item_type, 10, prefix="MORE"
            )
            for index, unit in enumerate(more):
                put([unit], make_box(f"EXTRA-{index}", line.gate_out.from_location.node))
            name(w["org"], line, more)
        with CaptureQueriesContext(connection) as detail_more:
            w["http"].get(url, **w["auth"])
        with CaptureQueriesContext(connection) as bundle_more:
            w["http"].get(bundle, **w["auth"])

        assert len(detail_more) == len(detail)
        assert len(bundle_more) == len(sync_bundle)


class TestGatePassDocument:
    def test_lines_are_grouped_under_their_box_with_every_unit_listed(
        self, tenant, yard, people, radio, jumper
    ):
        pallet = make_box("PAL-7", yard.node)
        carton = make_box("CTN-7", yard.node, parent=pallet)
        units = receive_units(tenant, yard.node, radio, 3, prefix="DOC")
        put(units[:2], carton)
        stock_in(tenant, yard.node, jumper, 3)
        gate_out = make_gate_out(tenant, yard, *people)
        unit_line(tenant, gate_out, radio, units[:2], box=carton)
        unit_line(tenant, gate_out, radio, units[2:])
        add_line(gate_out, jumper, 3)

        context = gate_pass_context(gate_out)
        groups = context["line_groups"]
        assert [g["box"] for g in groups] == ["", "PAL-7 › CTN-7"]
        assert len(groups[0]["lines"]) == 2
        boxed = groups[1]["lines"][0]
        assert boxed["serials"] == ["DOC-1", "DOC-2"]
        assert len(context["lines"]) == 3
