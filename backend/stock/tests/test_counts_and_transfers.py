"""T3.13–T3.16 — as-of-date stock, history, transfers and counts (E2–E5, M1)."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import transaction
from django.utils import timezone

from accounts.factories import UserFactory
from catalogue.factories import ItemTypeFactory
from catalogue.models import TrackingMode
from core.exceptions import AlreadyPosted
from locations.factories import StoreFactory, VehicleFactory, YardFactory
from locations.models import Location, LocationType
from locations.nodes import consumed_node, external_node, node_for_site, node_for_user
from network.factories import ClientFactory, SiteFactory
from stock.counting import (
    ApprovalRequired,
    CountNotReady,
    TransferNotAllowed,
    add_count_line,
    is_inside_perimeter,
    post_stock_count,
    transfer_stock,
)
from stock.factories import ReelFactory, SerialUnitFactory
from stock.models import (
    Condition,
    MovementType,
    OwnerType,
    StockCount,
    StockCountStatus,
)
from stock.queries import (
    below_minimum_stock,
    client_owned_position,
    custody_holdings,
    find_by_identifier,
    reel_history,
    serial_history,
    stock_as_at,
    stock_on_hand,
)
from stock.services import MovementRequest, balance_at, post_movement


@pytest.fixture
def yard(tenant):
    return YardFactory(name="Main yard")


@pytest.fixture
def storekeeper(tenant):
    return UserFactory(organization=tenant, full_name="Sara Storekeeper")


def receive(tenant, item, node, quantity, *, owner_client=None, condition=Condition.NEW, at=None):
    with transaction.atomic():
        return post_movement(
            MovementRequest(
                item_type=item,
                quantity=Decimal(str(quantity)),
                from_node=external_node(tenant.pk, client=owner_client),
                to_node=node,
                movement_type=MovementType.RECEIPT,
                owner_type=OwnerType.CLIENT if owner_client else OwnerType.OWN,
                owner_client=owner_client,
                condition=condition,
                occurred_at=at,
            )
        )


class TestStockOnHand:
    """E1: "do we have it?"."""

    def test_available_stock_excludes_quarantine(self, tenant, yard):
        item = ItemTypeFactory()
        quarantine = Location.objects.get(parent=yard, type=LocationType.QUARANTINE)
        receive(tenant, item, yard.node, 10)
        receive(tenant, item, quarantine.node, 3, condition=Condition.FAULTY)

        rows = stock_on_hand(item_type=item)

        assert {row.node_id for row in rows} == {yard.node.pk}

    def test_including_unavailable_shows_quarantine_too(self, tenant, yard):
        """A storekeeper deciding what to repair needs to see it."""
        item = ItemTypeFactory()
        quarantine = Location.objects.get(parent=yard, type=LocationType.QUARANTINE)
        receive(tenant, item, quarantine.node, 3, condition=Condition.FAULTY)

        rows = stock_on_hand(item_type=item, available_only=False)

        assert quarantine.node.pk in {row.node_id for row in rows}

    def test_client_owned_stock_is_reported_separately(self, tenant, yard):
        """E1: "client-owned stock is visually distinct from own stock"."""
        item = ItemTypeFactory()
        safaricom = ClientFactory(name="Safaricom")
        receive(tenant, item, yard.node, 10)
        receive(tenant, item, yard.node, 4, owner_client=safaricom)

        own = stock_on_hand(item_type=item).filter(owner_client__isnull=True)
        theirs = stock_on_hand(item_type=item, owner_client=safaricom)

        assert own.get().quantity == Decimal("10")
        assert theirs.get().quantity == Decimal("4")

    def test_zero_balances_are_not_reported_as_stock(self, tenant, yard):
        """A row that fell to zero is not "stock on hand"."""
        item = ItemTypeFactory()
        receive(tenant, item, yard.node, 5)
        with transaction.atomic():
            post_movement(
                MovementRequest(
                    item_type=item,
                    quantity=Decimal("5"),
                    from_node=yard.node,
                    to_node=consumed_node(tenant.pk),
                    movement_type=MovementType.CONSUME,
                )
            )

        assert stock_on_hand(item_type=item).count() == 0


class TestStockAsAtADate:
    """T3.13, M1: "stock on hand — current, and as at any past date"."""

    def test_stock_as_at_a_past_date_ignores_later_movements(self, tenant, yard):
        """The figure must not change when something happens afterwards."""
        item = ItemTypeFactory()
        two_weeks_ago = timezone.now() - timedelta(days=14)
        yesterday = timezone.now() - timedelta(days=1)

        receive(tenant, item, yard.node, 100, at=two_weeks_ago)
        with transaction.atomic():
            post_movement(
                MovementRequest(
                    item_type=item,
                    quantity=Decimal("40"),
                    from_node=yard.node,
                    to_node=consumed_node(tenant.pk),
                    movement_type=MovementType.CONSUME,
                    occurred_at=yesterday,
                )
            )

        a_week_ago = timezone.now() - timedelta(days=7)
        rows = stock_as_at(tenant.pk, a_week_ago, item_type=item)

        assert len(rows) == 1
        assert rows[0]["quantity"] == Decimal("100")

    def test_stock_as_at_now_matches_the_cache(self, tenant, yard):
        """The ledger and the cache must agree about the present.

        If they ever disagree, one of them is wrong — which is what
        verify_ledger exists to catch (T3.6).
        """
        item = ItemTypeFactory()
        receive(tenant, item, yard.node, 100)
        with transaction.atomic():
            post_movement(
                MovementRequest(
                    item_type=item,
                    quantity=Decimal("40"),
                    from_node=yard.node,
                    to_node=consumed_node(tenant.pk),
                    movement_type=MovementType.CONSUME,
                )
            )

        rows = stock_as_at(tenant.pk, timezone.now(), item_type=item)
        ledger_figure = next(row["quantity"] for row in rows if row["node_id"] == yard.node.pk)

        assert ledger_figure == balance_at(yard.node, item) == Decimal("60")

    def test_it_uses_when_material_arrived_not_when_it_was_typed_in(self, tenant, yard):
        """A delivery entered the next morning belongs to the day it arrived.

        Using the posting time would make yesterday's figure change overnight,
        which is exactly what an auditor would question.
        """
        item = ItemTypeFactory()
        arrived = timezone.now() - timedelta(days=3)
        receive(tenant, item, yard.node, 50, at=arrived)

        # As at two days ago — after arrival, before it was keyed in today.
        rows = stock_as_at(tenant.pk, timezone.now() - timedelta(days=2), item_type=item)

        assert any(row["quantity"] == Decimal("50") for row in rows)

    def test_before_anything_arrived_there_is_no_stock(self, tenant, yard):
        item = ItemTypeFactory()
        receive(tenant, item, yard.node, 50, at=timezone.now() - timedelta(days=1))

        rows = stock_as_at(tenant.pk, timezone.now() - timedelta(days=10), item_type=item)

        assert rows == []


class TestHistory:
    """T3.14, E2: the query an operator audit actually asks for."""

    def test_a_serial_shows_every_event_in_order(self, tenant, yard, storekeeper):
        """T3.14: "received, issued, installed and recovered shows all four"."""
        unit = SerialUnitFactory(current_node=external_node(tenant.pk))
        site = SiteFactory()
        technician = node_for_user(UserFactory(organization=tenant))

        steps = [
            (external_node(tenant.pk), yard.node, MovementType.RECEIPT),
            (yard.node, technician, MovementType.ISSUE),
            (technician, node_for_site(site), MovementType.INSTALL),
            (node_for_site(site), yard.node, MovementType.RETURN),
        ]
        for source, destination, movement_type in steps:
            with transaction.atomic():
                post_movement(
                    MovementRequest(
                        item_type=unit.item_type,
                        quantity=Decimal("1"),
                        from_node=source,
                        to_node=destination,
                        movement_type=movement_type,
                        tracking_mode=TrackingMode.SERIALIZED,
                        serial_unit=unit,
                        posted_by=storekeeper,
                    )
                )

        history = list(serial_history(unit.serial_number))

        assert [m.movement_type for m in history] == [
            MovementType.RECEIPT,
            MovementType.ISSUE,
            MovementType.INSTALL,
            MovementType.RETURN,
        ]
        assert history[0].from_node.type == "EXTERNAL"
        assert history[2].to_node.site == site

    def test_a_drum_shows_each_issue(self, tenant, yard):
        reel = ReelFactory(current_node=yard.node, initial_length=500, remaining_length=500)
        with transaction.atomic():
            post_movement(
                MovementRequest(
                    item_type=reel.item_type,
                    quantity=Decimal("500"),
                    from_node=external_node(tenant.pk),
                    to_node=yard.node,
                    movement_type=MovementType.RECEIPT,
                    tracking_mode=TrackingMode.REEL,
                    reel=reel,
                    uom="m",
                )
            )
        for length in (100, 50):
            with transaction.atomic():
                post_movement(
                    MovementRequest(
                        item_type=reel.item_type,
                        quantity=Decimal(str(length)),
                        from_node=yard.node,
                        to_node=consumed_node(tenant.pk),
                        movement_type=MovementType.CONSUME,
                        tracking_mode=TrackingMode.REEL,
                        reel=reel,
                        uom="m",
                    )
                )

        assert reel_history(reel.drum_number).count() == 3


class TestScanningAnIdentifier:
    """D7, E1: a storekeeper with a barcode should not have to pick a mode."""

    def test_a_serial_number_resolves_to_its_unit(self, tenant, yard):
        unit = SerialUnitFactory(serial_number="RRU-0001", current_node=yard.node)

        found = find_by_identifier("RRU-0001")

        assert found["kind"] == "serial_unit"
        assert found["object"] == unit

    def test_an_asset_tag_resolves_to_its_unit(self, tenant, yard):
        unit = SerialUnitFactory(
            serial_number="NO-PLATE-1", asset_tag="SLV-RRU-000123", current_node=yard.node
        )

        found = find_by_identifier("SLV-RRU-000123")

        assert found["object"] == unit

    def test_a_drum_number_resolves_to_its_drum(self, tenant, yard):
        reel = ReelFactory(drum_number="D-0007", current_node=yard.node)

        found = find_by_identifier("D-0007")

        assert found["kind"] == "reel"
        assert found["object"] == reel

    def test_lookup_is_case_insensitive(self, tenant, yard):
        SerialUnitFactory(serial_number="RRU-0001", current_node=yard.node)

        assert find_by_identifier("rru-0001") is not None

    def test_an_unknown_identifier_returns_nothing(self, tenant, yard):
        assert find_by_identifier("NOTHING-LIKE-THIS") is None


class TestInternalTransfers:
    """E4: material moving inside the yard stays visible; leaving needs a pass."""

    def test_a_transfer_between_stores_posts_directly(self, tenant, yard, storekeeper):
        item = ItemTypeFactory()
        store = StoreFactory(parent=yard, name="Bonded store")
        receive(tenant, item, yard.node, 40)

        transfer_stock(
            organization=tenant,
            item_type=item,
            quantity=Decimal("15"),
            from_location=yard,
            to_location=store,
            performed_by=storekeeper,
        )

        assert balance_at(yard.node, item) == Decimal("25")
        assert balance_at(store.node, item) == Decimal("15")

    def test_a_transfer_to_a_vehicle_is_refused(self, tenant, yard, storekeeper):
        """The rule that keeps the approval control meaningful.

        A vehicle is outside the perimeter. If loading a pickup were a transfer,
        it would be an unapproved way out of the yard — routing around the exact
        control the system exists to provide (E4).
        """
        item = ItemTypeFactory()
        vehicle = VehicleFactory(name="Pickup 1")
        receive(tenant, item, yard.node, 40)

        with pytest.raises(TransferNotAllowed, match="gate-out"):
            transfer_stock(
                organization=tenant,
                item_type=item,
                quantity=Decimal("5"),
                from_location=yard,
                to_location=vehicle,
                performed_by=storekeeper,
            )

        assert balance_at(yard.node, item) == Decimal("40")

    def test_the_refusal_names_the_alternative(self, tenant, yard, storekeeper):
        """A message that says "not allowed" without saying what to do instead
        just gets worked around."""
        item = ItemTypeFactory()
        receive(tenant, item, yard.node, 5)

        with pytest.raises(TransferNotAllowed) as caught:
            transfer_stock(
                organization=tenant,
                item_type=item,
                quantity=Decimal("1"),
                from_location=yard,
                to_location=VehicleFactory(),
                performed_by=storekeeper,
            )

        assert caught.value.code == "TRANSFER_REQUIRES_GATE_OUT"
        assert "gate-out" in str(caught.value)

    @pytest.mark.parametrize(
        ("location_type", "inside"),
        [
            (LocationType.YARD, True),
            (LocationType.STORE, True),
            (LocationType.QUARANTINE, True),
            (LocationType.VEHICLE, False),
        ],
    )
    def test_what_counts_as_inside_the_perimeter(self, tenant, location_type, inside):
        location = Location(type=location_type)

        assert is_inside_perimeter(location) is inside

    def test_transferring_more_than_is_there_is_refused(self, tenant, yard, storekeeper):
        from stock.services import InsufficientStock

        item = ItemTypeFactory()
        store = StoreFactory(parent=yard)
        receive(tenant, item, yard.node, 5)

        with pytest.raises(InsufficientStock):
            transfer_stock(
                organization=tenant,
                item_type=item,
                quantity=Decimal("10"),
                from_location=yard,
                to_location=store,
                performed_by=storekeeper,
            )


class TestStockCounts:
    """E5: a count document, and adjustments with a mandatory reason."""

    def _count(self, tenant, yard, storekeeper):
        return StockCount.objects.create(
            organization=tenant,
            location=yard,
            counted_at=timezone.now(),
            counted_by=storekeeper,
        )

    def test_a_line_captures_what_the_system_expected(self, tenant, yard, storekeeper):
        """A count is a statement about a moment.

        Recomputing the expectation at posting would silently absorb anything
        that moved while counting was happening — the very discrepancy the count
        exists to find.
        """
        item = ItemTypeFactory()
        receive(tenant, item, yard.node, 40)
        count = self._count(tenant, yard, storekeeper)

        line = add_count_line(count, item, Decimal("38"), reason="Two units unaccounted")

        assert line.expected_quantity == Decimal("40")
        assert line.counted_quantity == Decimal("38")
        assert line.variance == Decimal("-2")

    def test_posting_a_shortfall_reduces_the_balance(self, tenant, yard, storekeeper):
        item = ItemTypeFactory()
        receive(tenant, item, yard.node, 40)
        count = self._count(tenant, yard, storekeeper)
        add_count_line(count, item, Decimal("38"), reason="Two units unaccounted")

        post_stock_count(count, posted_by=storekeeper)

        assert balance_at(yard.node, item) == Decimal("38")

    def test_posting_a_surplus_increases_the_balance(self, tenant, yard, storekeeper):
        item = ItemTypeFactory()
        receive(tenant, item, yard.node, 40)
        count = self._count(tenant, yard, storekeeper)
        add_count_line(count, item, Decimal("43"), reason="Three found behind the racking")

        post_stock_count(count, posted_by=storekeeper)

        assert balance_at(yard.node, item) == Decimal("43")

    def test_a_variance_needs_a_reason(self, tenant, yard, storekeeper):
        """E5: "adjustment movements with a mandatory reason"."""
        item = ItemTypeFactory()
        receive(tenant, item, yard.node, 40)
        count = self._count(tenant, yard, storekeeper)
        add_count_line(count, item, Decimal("38"), reason="")

        with pytest.raises(CountNotReady) as caught:
            post_stock_count(count, posted_by=storekeeper)

        assert "lines.0.reason" in caught.value.field_errors

    def test_a_line_that_matched_needs_no_reason(self, tenant, yard, storekeeper):
        """Demanding one would train people to type "ok" and stop reading."""
        item = ItemTypeFactory()
        receive(tenant, item, yard.node, 40)
        count = self._count(tenant, yard, storekeeper)
        add_count_line(count, item, Decimal("40"))

        post_stock_count(count, posted_by=storekeeper)

        assert count.status == StockCountStatus.POSTED

    def test_posting_writes_adjust_movements_carrying_the_reason(
        self, tenant, yard, storekeeper
    ):
        from stock.models import StockMovement

        item = ItemTypeFactory()
        receive(tenant, item, yard.node, 40)
        count = self._count(tenant, yard, storekeeper)
        add_count_line(count, item, Decimal("38"), reason="Two units unaccounted")

        post_stock_count(count, posted_by=storekeeper)

        adjustment = StockMovement.objects.get(movement_type=MovementType.ADJUST)
        assert adjustment.note == "Two units unaccounted"
        assert adjustment.document_number == count.number

    def test_a_count_gets_a_number_only_when_posted(self, tenant, yard, storekeeper):
        item = ItemTypeFactory()
        receive(tenant, item, yard.node, 40)
        count = self._count(tenant, yard, storekeeper)
        assert count.number == ""

        add_count_line(count, item, Decimal("40"))
        post_stock_count(count, posted_by=storekeeper)

        assert count.number.startswith("SC-")

    def test_posting_twice_is_refused(self, tenant, yard, storekeeper):
        item = ItemTypeFactory()
        receive(tenant, item, yard.node, 40)
        count = self._count(tenant, yard, storekeeper)
        add_count_line(count, item, Decimal("40"))
        post_stock_count(count, posted_by=storekeeper)

        with pytest.raises(AlreadyPosted):
            post_stock_count(count, posted_by=storekeeper)

    def test_an_empty_count_cannot_be_posted(self, tenant, yard, storekeeper):
        count = self._count(tenant, yard, storekeeper)

        with pytest.raises(CountNotReady, match="at least one"):
            post_stock_count(count, posted_by=storekeeper)


class TestClientOwnedAdjustmentsNeedApproval:
    """E5: "adjustments to client-owned stock always require approval,
    regardless of category"."""

    def test_a_client_owned_variance_cannot_post_without_approval(
        self, tenant, yard, storekeeper
    ):
        """Writing off an operator's property is not a storekeeper's decision."""
        item = ItemTypeFactory()
        safaricom = ClientFactory(name="Safaricom")
        receive(tenant, item, yard.node, 10, owner_client=safaricom)

        count = StockCount.objects.create(
            organization=tenant, location=yard, counted_at=timezone.now()
        )
        add_count_line(
            count, item, Decimal("8"), owner_client=safaricom, reason="Two missing"
        )

        with pytest.raises(ApprovalRequired, match="client-owned"):
            post_stock_count(count, posted_by=storekeeper)

        assert balance_at(yard.node, item, owner_client=safaricom) == Decimal("10")

    def test_an_approved_client_owned_count_posts(self, tenant, yard, storekeeper):
        item = ItemTypeFactory()
        safaricom = ClientFactory(name="Safaricom")
        receive(tenant, item, yard.node, 10, owner_client=safaricom)

        count = StockCount.objects.create(
            organization=tenant, location=yard, counted_at=timezone.now()
        )
        add_count_line(
            count, item, Decimal("8"), owner_client=safaricom, reason="Two missing"
        )

        post_stock_count(count, posted_by=storekeeper, approved=True)

        assert balance_at(yard.node, item, owner_client=safaricom) == Decimal("8")

    def test_an_own_stock_count_needs_no_approval(self, tenant, yard, storekeeper):
        item = ItemTypeFactory()
        receive(tenant, item, yard.node, 10)
        count = StockCount.objects.create(
            organization=tenant, location=yard, counted_at=timezone.now()
        )
        add_count_line(count, item, Decimal("9"), reason="One missing")

        post_stock_count(count, posted_by=storekeeper)

        assert count.status == StockCountStatus.POSTED


class TestCustodyAndClientPosition:
    def test_custody_is_the_balance_at_a_person_node(self, tenant, yard):
        """§4.10, I1: no separate custody ledger, so the two can never disagree."""
        item = ItemTypeFactory()
        technician = UserFactory(organization=tenant, full_name="Tom Technician")
        receive(tenant, item, yard.node, 10)
        with transaction.atomic():
            post_movement(
                MovementRequest(
                    item_type=item,
                    quantity=Decimal("3"),
                    from_node=yard.node,
                    to_node=node_for_user(technician),
                    movement_type=MovementType.ISSUE,
                )
            )

        holdings = custody_holdings(user=technician)

        assert holdings.get().quantity == Decimal("3")

    def test_the_client_position_covers_every_location(self, tenant, yard):
        """M1: an operator asks about all of their material, wherever it is."""
        item = ItemTypeFactory()
        safaricom = ClientFactory(name="Safaricom")
        store = StoreFactory(parent=yard)
        receive(tenant, item, yard.node, 10, owner_client=safaricom)
        receive(tenant, item, store.node, 5, owner_client=safaricom)

        rows = client_owned_position(client=safaricom)

        assert sum(row.quantity for row in rows) == Decimal("15")

    def test_own_stock_is_not_in_the_client_position(self, tenant, yard):
        item = ItemTypeFactory()
        receive(tenant, item, yard.node, 10)

        assert client_owned_position().count() == 0


class TestMinimumStockAlerts:
    """E6, T3.17: alert when an item falls below its reorder level."""

    def test_an_item_below_its_minimum_is_reported(self, tenant, yard):
        item = ItemTypeFactory(name="Jumper", min_stock_qty=Decimal("20"))
        receive(tenant, item, yard.node, 5)

        low = below_minimum_stock(tenant.pk)

        assert len(low) == 1
        assert low[0]["item_type"] == "Jumper"
        assert low[0]["shortfall"] == Decimal("15")

    def test_an_item_at_its_minimum_is_not_reported(self, tenant, yard):
        item = ItemTypeFactory(min_stock_qty=Decimal("20"))
        receive(tenant, item, yard.node, 20)

        assert below_minimum_stock(tenant.pk) == []

    def test_items_with_no_minimum_set_are_ignored(self, tenant, yard):
        """E6 is per-item and opt-in: no minimum means no opinion."""
        item = ItemTypeFactory(min_stock_qty=None)
        receive(tenant, item, yard.node, 1)

        assert below_minimum_stock(tenant.pk) == []

    def test_quarantined_stock_does_not_count_towards_the_minimum(self, tenant, yard):
        """Faulty stock cannot fill an order, so it must not mask a shortage."""
        item = ItemTypeFactory(min_stock_qty=Decimal("10"))
        quarantine = Location.objects.get(parent=yard, type=LocationType.QUARANTINE)
        receive(tenant, item, quarantine.node, 50, condition=Condition.FAULTY)

        low = below_minimum_stock(tenant.pk)

        assert len(low) == 1
        assert low[0]["available"] == Decimal("0")
