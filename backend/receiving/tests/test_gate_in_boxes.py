"""T11.7 — a gate-in receives boxes (§4.15.5; P1, P2, P8, P9, P10)."""

from decimal import Decimal

import pytest

from catalogue.factories import ItemTypeFactory
from catalogue.models import TrackingMode
from receiving.models import GateInBox, GateInLine, GateInSerial, GateInSource
from receiving.services import GateInNotReady, post_gate_in, validate_for_posting, void_gate_in
from receiving.tests import test_gate_in as base
from stock.models import (
    Box,
    BoxAction,
    BoxBulkContent,
    BoxEvent,
    BoxStatus,
    Condition,
    SerialUnit,
)
from stock.queries import find_by_identifier
from stock.verification import verify_ledger

add_bulk_line = base.add_bulk_line
draft = base.draft
yard = base.yard
storekeeper = base.storekeeper


def serialized_item(name):
    return ItemTypeFactory(name=name, default_tracking_mode=TrackingMode.SERIALIZED)


def add_serialized_line(gate_in, item, serials, *, condition=Condition.NEW, **kwargs):
    line = GateInLine.objects.create(
        organization=gate_in.organization,
        gate_in=gate_in,
        item_type=item,
        tracking_mode=TrackingMode.SERIALIZED,
        quantity=Decimal(len(serials)),
        uom=item.uom,
        condition=condition,
        **kwargs,
    )
    for number, box_key in serials:
        GateInSerial.objects.create(
            organization=gate_in.organization, line=line, serial_number=number, box_key=box_key
        )
    return line


def add_box(gate_in, key, code="", parent_key="", label_text=""):
    return GateInBox.objects.create(
        organization=gate_in.organization,
        gate_in=gate_in,
        key=key,
        code=code,
        parent_key=parent_key,
        label_text=label_text,
    )


def errors_of(gate_in):
    with pytest.raises(GateInNotReady) as caught:
        validate_for_posting(gate_in)
    return caught.value.field_errors


def ledger_is_clean(tenant):
    return verify_ledger(tenant.pk).drifts == []


@pytest.fixture
def pallet(tenant, yard):
    """A pallet holding a mixed carton (two item types of serialized units) and a
    carton of bulk."""
    gate_in = draft(tenant, yard)
    add_box(gate_in, "p", code="PAL-1")
    add_box(gate_in, "c1", code="CTN-1", parent_key="p")
    add_box(gate_in, "c2", code="", parent_key="p")
    rru = serialized_item("T117 RRU")
    antenna = serialized_item("T117 Antenna")
    add_serialized_line(gate_in, rru, [("T117-RRU-1", "c1"), ("T117-RRU-2", "c1")])
    add_serialized_line(gate_in, antenna, [("T117-ANT-1", "c1")])
    bulk = ItemTypeFactory(name="T117 clamp", default_tracking_mode=TrackingMode.BULK)
    add_bulk_line(gate_in, quantity=40, item=bulk, box_key="c2")
    return gate_in, bulk


class TestPosting:
    def test_boxes_are_made_and_filled(self, tenant, yard, storekeeper, pallet):
        gate_in, bulk = pallet

        post_gate_in(gate_in, posted_by=storekeeper)

        pal = Box.objects.get(code="PAL-1")
        carton = Box.objects.get(code="CTN-1")
        assert (pal.depth, carton.depth) == (1, 2)
        assert carton.parent == pal
        assert pal.current_node == yard.node == carton.current_node
        assert pal.gate_in == gate_in
        unit = find_by_identifier("T117-ANT-1")["object"]
        assert unit.box == carton
        assert SerialUnit.objects.filter(box=carton).count() == 3
        claim = BoxBulkContent.objects.get(item_type=bulk)
        assert (claim.quantity, claim.condition) == (Decimal("40"), Condition.NEW)
        assert ledger_is_clean(tenant)

    def test_a_blank_code_is_generated_and_written_back(self, tenant, storekeeper, pallet):
        gate_in, _bulk = pallet

        post_gate_in(gate_in, posted_by=storekeeper)

        generated = GateInBox.objects.get(gate_in=gate_in, key="c2")
        assert generated.code.startswith("BX-")
        assert Box.objects.get(code=generated.code).parent.code == "PAL-1"

    def test_the_events_carry_the_document(self, tenant, storekeeper, pallet):
        gate_in, _bulk = pallet

        post_gate_in(gate_in, posted_by=storekeeper)

        events = BoxEvent.objects.filter(box__code="CTN-1")
        assert {e.action for e in events} >= {BoxAction.CREATED, BoxAction.UNIT_IN}
        assert {e.document_number for e in events} == {gate_in.number}
        assert {e.actor_id for e in events} == {storekeeper.pk}

    def test_a_quarantined_box_lands_in_quarantine(self, tenant, yard, storekeeper):
        gate_in = draft(tenant, yard)
        add_box(gate_in, "q", code="QB-1")
        add_serialized_line(
            gate_in, serialized_item("T117 faulty"), [("T117-F-1", "q")], condition=Condition.FAULTY
        )

        post_gate_in(gate_in, posted_by=storekeeper)

        box = Box.objects.get(code="QB-1")
        assert box.current_node != yard.node
        assert SerialUnit.objects.get(serial_number="T117-F-1").box == box
        assert ledger_is_clean(tenant)


class TestVoid:
    def test_every_box_ends_closed_and_units_are_out(self, tenant, storekeeper, pallet):
        gate_in, _bulk = pallet
        post_gate_in(gate_in, posted_by=storekeeper)

        void_gate_in(gate_in, reason="Wrong delivery", voided_by=storekeeper)

        boxes = Box.objects.filter(gate_in=gate_in)
        assert boxes.count() == 3
        assert set(boxes.values_list("status", flat=True)) == {BoxStatus.CLOSED}
        assert not SerialUnit.objects.filter(box__isnull=False).exists()
        assert not BoxBulkContent.objects.exists()
        closed = BoxEvent.objects.filter(box__gate_in=gate_in, action=BoxAction.CLOSED)
        assert closed.count() == 3
        assert ledger_is_clean(tenant)


class TestValidation:
    def test_a_good_draft_validates(self, pallet):
        validate_for_posting(pallet[0])

    def test_unknown_keys_are_refused(self, tenant, yard):
        gate_in = draft(tenant, yard)
        add_box(gate_in, "a", code="A", parent_key="nope")
        add_bulk_line(gate_in, box_key="ghost")
        add_serialized_line(gate_in, serialized_item("T117 u"), [("T117-U-1", "ghost2")])

        errors = errors_of(gate_in)

        assert "boxes.0.parent_key" in errors
        assert "lines.0.box_key" in errors
        assert "lines.1.serials.0.box_key" in errors

    def test_codes_are_unique_in_the_document_case_insensitively(self, tenant, yard):
        gate_in = draft(tenant, yard)
        add_box(gate_in, "a", code="Same")
        add_box(gate_in, "b", code="SAME")
        add_bulk_line(gate_in, box_key="a")
        add_bulk_line(gate_in, box_key="b")

        errors = errors_of(gate_in)

        assert "boxes.1.code" in errors
        assert "boxes.0.code" not in errors

    def test_a_code_used_by_an_open_or_closed_box_is_refused_naming_where(
        self, tenant, yard, storekeeper
    ):
        first = draft(tenant, yard)
        add_box(first, "a", code="TAKEN-1")
        add_bulk_line(first, box_key="a")
        post_gate_in(first, posted_by=storekeeper)
        void_gate_in(first, reason="Oops", voided_by=storekeeper)  # closes TAKEN-1

        second = draft(tenant, yard)
        add_box(second, "a", code="taken-1")
        add_bulk_line(second, box_key="a")

        message = errors_of(second)["boxes.0.code"][0]

        assert "closed" in message
        assert yard.node.label in message
        assert "BOX_CODE_IN_USE" in message

    def test_a_cycle_is_refused(self, tenant, yard):
        gate_in = draft(tenant, yard)
        add_box(gate_in, "a", code="A", parent_key="b")
        add_box(gate_in, "b", code="B", parent_key="a")
        add_bulk_line(gate_in, box_key="a")

        errors = errors_of(gate_in)

        assert "BOX_CYCLE" in errors["boxes.0.parent_key"][0]

    def test_four_deep_is_refused(self, tenant, yard):
        gate_in = draft(tenant, yard)
        add_box(gate_in, "a", code="A")
        add_box(gate_in, "b", code="B", parent_key="a")
        add_box(gate_in, "c", code="C", parent_key="b")
        add_box(gate_in, "d", code="D", parent_key="c")
        add_bulk_line(gate_in, box_key="d")

        errors = errors_of(gate_in)

        assert "BOX_TOO_DEEP" in errors["boxes.3.parent_key"][0]
        assert "boxes.2.parent_key" not in errors

    def test_an_empty_box_is_refused_but_a_parent_of_a_full_one_is_not(self, tenant, yard):
        gate_in = draft(tenant, yard)
        add_box(gate_in, "a", code="A")
        add_box(gate_in, "b", code="B", parent_key="a")
        add_box(gate_in, "e", code="EMPTY")
        add_bulk_line(gate_in, box_key="b")

        errors = errors_of(gate_in)

        assert "BOX_EMPTY" in errors["boxes.2.code"][0]
        assert "boxes.0.code" not in errors

    def test_one_tree_cannot_mix_serviceable_and_quarantined(self, tenant, yard):
        gate_in = draft(tenant, yard)
        add_box(gate_in, "a", code="A")
        add_box(gate_in, "b", code="B", parent_key="a")
        add_bulk_line(gate_in, box_key="a")
        add_bulk_line(gate_in, box_key="b", condition=Condition.DAMAGED)

        errors = errors_of(gate_in)

        assert "BOX_MIXED_DESTINATIONS" in errors["boxes.0.code"][0]
        assert "boxes.1.code" not in errors  # the child alone is on one node

    def test_a_reel_line_cannot_be_boxed(self, tenant, yard):
        from receiving.models import GateInReel

        gate_in = draft(tenant, yard)
        add_box(gate_in, "a", code="A")
        item = ItemTypeFactory(default_tracking_mode=TrackingMode.REEL, uom="m")
        line = GateInLine.objects.create(
            organization=tenant,
            gate_in=gate_in,
            item_type=item,
            tracking_mode=TrackingMode.REEL,
            quantity=Decimal("100"),
            uom="m",
            box_key="a",
        )
        GateInReel.objects.create(
            organization=tenant, line=line, drum_number="T117-D1", length=Decimal("100")
        )

        assert "lines.0.box_key" in errors_of(gate_in)

    def test_a_serialized_line_takes_its_box_on_the_serials(self, tenant, yard):
        gate_in = draft(tenant, yard)
        add_box(gate_in, "a", code="A")
        add_serialized_line(gate_in, serialized_item("T117 s"), [("T117-S-1", "a")], box_key="a")

        assert "lines.0.box_key" in errors_of(gate_in)

    def test_a_returning_unit_still_in_a_box_is_refused(self, tenant, yard, storekeeper):
        first = draft(tenant, yard)
        add_box(first, "a", code="HOLDER")
        item = serialized_item("T117 ret")
        add_serialized_line(first, item, [("T117-RET-1", "a")])
        post_gate_in(first, posted_by=storekeeper)

        again = draft(tenant, yard, source_type=GateInSource.WARRANTY_RETURN)
        add_box(again, "b", code="NEWBOX")
        add_serialized_line(again, item, [("T117-RET-1", "b")])

        errors = errors_of(again)

        assert "HOLDER" in errors["lines.0.serials.T117-RET-1"][0]
