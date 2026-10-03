"""T11.1 — the box tables and their guarantees (§4.15.2; P1, P8, P10)."""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError
from django.utils import timezone

from catalogue.factories import ItemTypeFactory
from catalogue.models import TrackingMode
from core.numbering import DEFAULT_PREFIXES, DocumentType, allocate_number
from locations.factories import YardFactory
from network.factories import ClientFactory
from stock.factories import SerialUnitFactory
from stock.models import (
    Box,
    BoxAction,
    BoxBulkContent,
    BoxEvent,
    BoxSource,
    BoxStatus,
    Condition,
)


@pytest.fixture
def yard(tenant):
    return YardFactory(name="Main yard")


@pytest.fixture
def bulk_item(tenant):
    return ItemTypeFactory(name="Jumper 3m", default_tracking_mode=TrackingMode.BULK)


def make_box(yard, code="BX-1", **extra):
    fields = {
        "code": code,
        "source": BoxSource.INTERNAL,
        "current_node": yard.node,
    }
    fields.update(extra)
    return Box.objects.create(**fields)


class TestBox:
    def test_a_code_is_unique_regardless_of_case(self, tenant, yard):
        make_box(yard, "Ab-12")

        with pytest.raises(IntegrityError), transaction.atomic():
            make_box(yard, "aB-12")

    def test_a_closed_box_still_holds_its_code(self, tenant, yard):
        """P5: a closed box cannot be reused."""
        make_box(yard, "AB-12", status=BoxStatus.CLOSED, closed_at=timezone.now())

        with pytest.raises(IntegrityError), transaction.atomic():
            make_box(yard, "ab-12")

    @pytest.mark.parametrize("depth", [0, 4])
    def test_depth_outside_one_to_three_is_refused(self, tenant, yard, depth):
        with pytest.raises(IntegrityError), transaction.atomic():
            make_box(yard, depth=depth)

    @pytest.mark.parametrize("depth", [1, 2, 3])
    def test_depth_one_to_three_is_allowed(self, tenant, yard, depth):
        assert make_box(yard, depth=depth).depth == depth

    def test_a_closed_box_must_say_when(self, tenant, yard):
        with pytest.raises(IntegrityError), transaction.atomic():
            make_box(yard, status=BoxStatus.CLOSED)

    def test_a_box_can_nest_in_another(self, tenant, yard):
        outer = make_box(yard, "OUT")
        inner = make_box(yard, "IN", parent=outer, depth=2)

        assert list(outer.children.all()) == [inner]


class TestBoxBulkContent:
    def test_a_claim_must_be_positive(self, tenant, yard, bulk_item):
        box = make_box(yard)

        for quantity in (Decimal("0"), Decimal("-1")):
            with pytest.raises(IntegrityError), transaction.atomic():
                BoxBulkContent.objects.create(
                    box=box, item_type=bulk_item, condition=Condition.NEW, quantity=quantity
                )

    def test_one_claim_per_box_item_owner_and_condition(self, tenant, yard, bulk_item):
        """Own stock has a NULL owner, so NULLs must count as equal."""
        box = make_box(yard)
        BoxBulkContent.objects.create(
            box=box, item_type=bulk_item, condition=Condition.NEW, quantity=5
        )

        with pytest.raises(IntegrityError), transaction.atomic():
            BoxBulkContent.objects.create(
                box=box, item_type=bulk_item, condition=Condition.NEW, quantity=2
            )

    def test_another_owner_or_condition_is_a_separate_claim(self, tenant, yard, bulk_item):
        box = make_box(yard)
        client = ClientFactory()
        BoxBulkContent.objects.create(
            box=box, item_type=bulk_item, condition=Condition.NEW, quantity=5
        )

        BoxBulkContent.objects.create(
            box=box,
            item_type=bulk_item,
            condition=Condition.NEW,
            quantity=2,
            owner_client=client,
        )
        BoxBulkContent.objects.create(
            box=box, item_type=bulk_item, condition=Condition.FAULTY, quantity=1
        )

        assert box.bulk_contents.count() == 3


class TestBoxEventAppendOnly:
    """P8: the trail is never edited."""

    @pytest.fixture
    def event(self, tenant, yard):
        return BoxEvent.objects.create(box=make_box(yard), action=BoxAction.CREATED)

    def test_cannot_be_modified_in_python(self, event):
        event.note = "tampered"
        with pytest.raises(ValidationError, match="append-only"):
            event.save()

    def test_cannot_be_deleted_in_python(self, event):
        with pytest.raises(ValidationError, match="append-only"):
            event.delete()

    def test_update_raises_in_the_database(self, event):
        with pytest.raises(IntegrityError, match="not permitted"), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("UPDATE stock_boxevent SET note = 'x' WHERE id = %s", [event.pk])

    def test_delete_raises_in_the_database(self, event):
        with pytest.raises(IntegrityError, match="not permitted"), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM stock_boxevent WHERE id = %s", [event.pk])

    def test_an_event_can_carry_a_unit_and_a_document(self, tenant, yard):
        unit = SerialUnitFactory(current_node=yard.node)
        event = BoxEvent.objects.create(
            box=make_box(yard),
            action=BoxAction.UNIT_IN,
            serial_unit=unit,
            document_type="GATE_IN",
            document_id="7",
            document_number="GRN-000007",
        )

        assert event.occurred_at is not None
        assert list(unit.box_events.all()) == [event]


class TestSerialUnitBox:
    def test_a_unit_can_be_in_a_box(self, tenant, yard):
        box = make_box(yard)
        unit = SerialUnitFactory(current_node=yard.node, box=box)

        assert list(box.units.all()) == [unit]

    def test_a_unit_need_not_be_in_a_box(self, tenant, yard):
        assert SerialUnitFactory(current_node=yard.node).box is None

    def test_a_box_holding_a_unit_cannot_be_deleted(self, tenant, yard):
        box = make_box(yard)
        SerialUnitFactory(current_node=yard.node, box=box)

        with pytest.raises(ProtectedError):
            box.delete()


class TestBoxNumbering:
    def test_box_numbers_use_the_bx_prefix(self, tenant):
        assert DEFAULT_PREFIXES[DocumentType.BOX] == "BX"

        first = allocate_number(DocumentType.BOX)
        second = allocate_number(DocumentType.BOX)

        assert first.startswith("BX")
        assert first != second
