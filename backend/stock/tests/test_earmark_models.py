"""T13.1 — the earmark tables and their guarantees (§4.16.2)."""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction

from catalogue.factories import ItemTypeFactory
from catalogue.models import TrackingMode
from locations.factories import YardFactory
from network.factories import ClientFactory, SiteFactory
from stock.factories import ReelFactory, SerialUnitFactory
from stock.models import BulkEarmark, Condition, EarmarkAction, EarmarkEvent


@pytest.fixture
def yard(tenant):
    return YardFactory(name="Main yard")


@pytest.fixture
def bulk_item(tenant):
    return ItemTypeFactory(name="Jumper 3m", default_tracking_mode=TrackingMode.BULK)


def make_earmark(yard, item, site, quantity=5, **extra):
    fields = {
        "site": site,
        "node": yard.node,
        "item_type": item,
        "condition": Condition.NEW,
        "quantity": quantity,
    }
    fields.update(extra)
    return BulkEarmark.objects.create(**fields)


class TestBulkEarmark:
    def test_a_claim_must_be_positive(self, tenant, yard, bulk_item):
        site = SiteFactory()
        for quantity in (Decimal("0"), Decimal("-1")):
            with pytest.raises(IntegrityError), transaction.atomic():
                make_earmark(yard, bulk_item, site, quantity)

    def test_one_claim_per_site_lot_with_a_null_owner(self, tenant, yard, bulk_item):
        site = SiteFactory()
        make_earmark(yard, bulk_item, site)

        with pytest.raises(IntegrityError), transaction.atomic():
            make_earmark(yard, bulk_item, site, 2)

    def test_one_claim_per_site_lot_with_an_owner(self, tenant, yard, bulk_item):
        site, client = SiteFactory(), ClientFactory()
        make_earmark(yard, bulk_item, site, owner_client=client)

        with pytest.raises(IntegrityError), transaction.atomic():
            make_earmark(yard, bulk_item, site, 2, owner_client=client)

    def test_another_site_owner_or_condition_is_a_separate_claim(self, tenant, yard, bulk_item):
        site = SiteFactory()
        make_earmark(yard, bulk_item, site)

        make_earmark(yard, bulk_item, SiteFactory())
        make_earmark(yard, bulk_item, site, owner_client=ClientFactory())
        make_earmark(yard, bulk_item, site, condition=Condition.FAULTY)

        assert BulkEarmark.objects.count() == 4


class TestEarmarkEventAppendOnly:
    @pytest.fixture
    def event(self, tenant):
        return EarmarkEvent.objects.create(action=EarmarkAction.EARMARKED, site=SiteFactory())

    def test_cannot_be_modified_in_python(self, event):
        event.reason = "tampered"
        with pytest.raises(ValidationError, match="append-only"):
            event.save()

    def test_cannot_be_deleted_in_python(self, event):
        with pytest.raises(ValidationError, match="append-only"):
            event.delete()

    def test_update_raises_in_the_database(self, event):
        with pytest.raises(IntegrityError, match="not permitted"), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE stock_earmarkevent SET reason = 'x' WHERE id = %s", [event.pk]
                )

    def test_delete_raises_in_the_database(self, event):
        with pytest.raises(IntegrityError, match="not permitted"), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM stock_earmarkevent WHERE id = %s", [event.pk])

    def test_an_event_carries_both_sites_a_subject_and_a_document(self, tenant, yard):
        unit = SerialUnitFactory(current_node=yard.node)
        a, b = SiteFactory(), SiteFactory()
        event = EarmarkEvent.objects.create(
            action=EarmarkAction.DIVERTED,
            site=a,
            to_site=b,
            serial_unit=unit,
            node=yard.node,
            document_type="GATE_OUT",
            document_id="7",
            document_number="GP-000007",
            reason="Site B is short",
        )

        assert event.occurred_at is not None
        assert list(unit.earmark_events.all()) == [event]

    def test_the_site_can_be_empty_for_a_change_from_free(self, tenant):
        event = EarmarkEvent.objects.create(
            action=EarmarkAction.CHANGED, to_site=SiteFactory(), reason="Plan changed"
        )

        assert event.site is None


class TestSiteForeignKeys:
    def test_a_unit_can_be_earmarked_and_need_not_be(self, tenant, yard):
        site = SiteFactory()
        unit = SerialUnitFactory(current_node=yard.node, earmark_site=site)

        assert list(site.earmarked_serial_units.all()) == [unit]
        assert SerialUnitFactory(current_node=yard.node).earmark_site is None

    def test_a_drum_can_be_earmarked(self, tenant, yard):
        site = SiteFactory()
        reel = ReelFactory(current_node=yard.node, earmark_site=site)

        assert list(site.earmarked_reels.all()) == [reel]
