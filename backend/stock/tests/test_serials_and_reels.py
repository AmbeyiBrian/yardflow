"""T3.3, T3.4 — serialized units and reels (§3.5; D3, D4, D12, E2, E3)."""

import threading
from decimal import Decimal

import pytest
from django.db import IntegrityError, connection, transaction

from accounts.factories import UserFactory
from catalogue.factories import ItemTypeFactory
from catalogue.models import TrackingMode
from locations.factories import YardFactory
from locations.models import Location, LocationType
from locations.nodes import consumed_node, external_node, node_for_site, node_for_user, scrap_node
from network.factories import SiteFactory
from stock.factories import ReelFactory, SerialUnitFactory
from stock.models import (
    MovementType,
    ReelStatus,
    SerialUnit,
    SerialUnitStatus,
)
from stock.services import (
    LedgerRuleViolation,
    MovementRequest,
    ReelExhausted,
    balance_at,
    post_movement,
)


@pytest.fixture
def yard(tenant):
    return YardFactory(name="Main yard")


@pytest.fixture
def supplier(tenant):
    return external_node(tenant.pk)


def move(**kwargs):
    with transaction.atomic():
        return post_movement(MovementRequest(**kwargs))


class TestSerializedUnits:
    """§3.5: one unit, individually identified."""

    def test_a_serialized_movement_is_exactly_one_unit(self, tenant, yard, supplier):
        unit = SerialUnitFactory(current_node=supplier)

        move(
            item_type=unit.item_type,
            quantity=Decimal("1"),
            from_node=supplier,
            to_node=yard.node,
            movement_type=MovementType.RECEIPT,
            tracking_mode=TrackingMode.SERIALIZED,
            serial_unit=unit,
        )

        assert balance_at(yard.node, unit.item_type) == Decimal("1")

    def test_a_serialized_movement_of_more_than_one_is_refused(self, tenant, yard, supplier):
        """Two units are two movements. Allowing quantity 2 with one serial would
        make the serial history in E2 unreadable."""
        unit = SerialUnitFactory(current_node=supplier)

        with pytest.raises(LedgerRuleViolation, match="exactly one unit"):
            move(
                item_type=unit.item_type,
                quantity=Decimal("2"),
                from_node=supplier,
                to_node=yard.node,
                movement_type=MovementType.RECEIPT,
                tracking_mode=TrackingMode.SERIALIZED,
                serial_unit=unit,
            )

    def test_a_serialized_movement_must_name_its_unit(self, tenant, yard, supplier):
        item = ItemTypeFactory(default_tracking_mode=TrackingMode.SERIALIZED)

        with pytest.raises(LedgerRuleViolation, match="must name the unit"):
            move(
                item_type=item,
                quantity=Decimal("1"),
                from_node=supplier,
                to_node=yard.node,
                movement_type=MovementType.RECEIPT,
                tracking_mode=TrackingMode.SERIALIZED,
            )

    def test_a_duplicate_serial_is_rejected_within_the_tenant(self, tenant, supplier):
        """D3: "duplicate serial within the tenant must be rejected"."""
        SerialUnitFactory(serial_number="RRU-0001", current_node=supplier)

        with pytest.raises(IntegrityError), transaction.atomic():
            SerialUnitFactory(serial_number="RRU-0001", current_node=supplier)

    def test_the_same_serial_may_exist_in_another_tenant(
        self, organization, other_organization
    ):
        """Two contractors may hold units with the same manufacturer serial."""
        from core.rls import rls_bypass
        from core.tenancy import tenant_context

        for org in (organization, other_organization):
            with tenant_context(org):
                SerialUnitFactory(
                    serial_number="RRU-0001", current_node=external_node(org.pk)
                )

        with rls_bypass():
            assert SerialUnit.all_objects.filter(serial_number="RRU-0001").count() == 2

    def test_the_units_position_follows_the_movement(self, tenant, yard, supplier):
        """§3.5: the denormalised position is written in the same transaction."""
        unit = SerialUnitFactory(current_node=supplier)

        move(
            item_type=unit.item_type,
            quantity=Decimal("1"),
            from_node=supplier,
            to_node=yard.node,
            movement_type=MovementType.RECEIPT,
            tracking_mode=TrackingMode.SERIALIZED,
            serial_unit=unit,
        )

        unit.refresh_from_db()
        assert unit.current_node == yard.node
        assert unit.status == SerialUnitStatus.IN_STOCK

    @pytest.mark.parametrize(
        ("destination", "expected_status"),
        [
            ("site", SerialUnitStatus.INSTALLED),
            ("person", SerialUnitStatus.IN_CUSTODY),
            ("quarantine", SerialUnitStatus.QUARANTINED),
            ("scrap", SerialUnitStatus.SCRAPPED),
        ],
    )
    def test_status_is_derived_from_where_the_unit_is(
        self, tenant, yard, supplier, destination, expected_status
    ):
        """Derived, never passed in.

        A unit sitting at a SITE node *is* installed. Letting a caller assert
        otherwise would allow the status and the ledger to disagree, and the
        status is what the stock screens show.
        """
        unit = SerialUnitFactory(current_node=supplier)
        move(
            item_type=unit.item_type,
            quantity=Decimal("1"),
            from_node=supplier,
            to_node=yard.node,
            movement_type=MovementType.RECEIPT,
            tracking_mode=TrackingMode.SERIALIZED,
            serial_unit=unit,
        )

        targets = {
            "site": lambda: node_for_site(SiteFactory()),
            "person": lambda: node_for_user(UserFactory(organization=tenant)),
            "quarantine": lambda: Location.objects.get(
                parent=yard, type=LocationType.QUARANTINE
            ).node,
            "scrap": lambda: scrap_node(tenant.pk),
        }

        move(
            item_type=unit.item_type,
            quantity=Decimal("1"),
            from_node=yard.node,
            to_node=targets[destination](),
            movement_type=MovementType.ISSUE,
            tracking_mode=TrackingMode.SERIALIZED,
            serial_unit=unit,
        )

        unit.refresh_from_db()
        assert unit.status == expected_status

    def test_a_unit_cannot_be_issued_twice(self, tenant, yard, supplier):
        """§13: the second attempt must fail rather than double-count."""
        unit = SerialUnitFactory(current_node=supplier)
        move(
            item_type=unit.item_type,
            quantity=Decimal("1"),
            from_node=supplier,
            to_node=yard.node,
            movement_type=MovementType.RECEIPT,
            tracking_mode=TrackingMode.SERIALIZED,
            serial_unit=unit,
        )
        move(
            item_type=unit.item_type,
            quantity=Decimal("1"),
            from_node=yard.node,
            to_node=consumed_node(tenant.pk),
            movement_type=MovementType.CONSUME,
            tracking_mode=TrackingMode.SERIALIZED,
            serial_unit=unit,
        )

        from stock.services import InsufficientStock

        with pytest.raises(InsufficientStock):
            move(
                item_type=unit.item_type,
                quantity=Decimal("1"),
                from_node=yard.node,
                to_node=scrap_node(tenant.pk),
                movement_type=MovementType.DISPOSE,
                tracking_mode=TrackingMode.SERIALIZED,
                serial_unit=unit,
            )

    def test_a_recovered_unit_keeps_its_origin_site(self, tenant, supplier):
        """D5: the operator is shown what was retrieved from where."""
        site = SiteFactory(internal_ref="SLV-9000")
        unit = SerialUnitFactory(current_node=supplier, origin_site=site)

        unit.refresh_from_db()
        assert unit.origin_site == site


class TestSerialHistory:
    """E2: "the single most likely question from an operator audit"."""

    def test_a_units_whole_life_is_readable_in_order(self, tenant, yard, supplier):
        """T3.14's criterion, proved here at the ledger level.

        Received, issued, installed, recovered — four events, in order.
        """
        unit = SerialUnitFactory(current_node=supplier)
        site = SiteFactory()
        technician = node_for_user(UserFactory(organization=tenant))

        steps = [
            (supplier, yard.node, MovementType.RECEIPT),
            (yard.node, technician, MovementType.ISSUE),
            (technician, node_for_site(site), MovementType.INSTALL),
            (node_for_site(site), yard.node, MovementType.RETURN),
        ]
        for source, destination, movement_type in steps:
            move(
                item_type=unit.item_type,
                quantity=Decimal("1"),
                from_node=source,
                to_node=destination,
                movement_type=movement_type,
                tracking_mode=TrackingMode.SERIALIZED,
                serial_unit=unit,
            )

        history = list(unit.movements.order_by("occurred_at", "id"))

        assert [m.movement_type for m in history] == [
            MovementType.RECEIPT,
            MovementType.ISSUE,
            MovementType.INSTALL,
            MovementType.RETURN,
        ]


class TestReels:
    """D4, D12, E3: a numbered drum with a measured length."""

    def test_receiving_a_drum_records_its_length(self, tenant, yard, supplier):
        reel = ReelFactory(current_node=supplier, initial_length=500, remaining_length=500)

        move(
            item_type=reel.item_type,
            quantity=Decimal("500"),
            from_node=supplier,
            to_node=yard.node,
            movement_type=MovementType.RECEIPT,
            tracking_mode=TrackingMode.REEL,
            reel=reel,
            uom="m",
        )

        assert balance_at(yard.node, reel.item_type) == Decimal("500")

    def test_issuing_decrements_the_remaining_length(self, tenant, yard, supplier):
        """E3: "issuing from a drum decrements its remaining length"."""
        reel = ReelFactory(current_node=yard.node, initial_length=500, remaining_length=500)
        move(
            item_type=reel.item_type,
            quantity=Decimal("500"),
            from_node=supplier,
            to_node=yard.node,
            movement_type=MovementType.RECEIPT,
            tracking_mode=TrackingMode.REEL,
            reel=reel,
            uom="m",
        )

        move(
            item_type=reel.item_type,
            quantity=Decimal("160"),
            from_node=yard.node,
            to_node=consumed_node(tenant.pk),
            movement_type=MovementType.CONSUME,
            tracking_mode=TrackingMode.REEL,
            reel=reel,
            uom="m",
        )

        reel.refresh_from_db()
        assert reel.remaining_length == Decimal("340.000")

    def test_partial_issues_accumulate_correctly(self, tenant, yard, supplier):
        """Q5: partial consumption of a drum on site is normal."""
        reel = ReelFactory(current_node=yard.node, initial_length=500, remaining_length=500)
        move(
            item_type=reel.item_type,
            quantity=Decimal("500"),
            from_node=supplier,
            to_node=yard.node,
            movement_type=MovementType.RECEIPT,
            tracking_mode=TrackingMode.REEL,
            reel=reel,
            uom="m",
        )

        for length in (100, 50, 25.5):
            move(
                item_type=reel.item_type,
                quantity=Decimal(str(length)),
                from_node=yard.node,
                to_node=consumed_node(tenant.pk),
                movement_type=MovementType.CONSUME,
                tracking_mode=TrackingMode.REEL,
                reel=reel,
                uom="m",
            )

        reel.refresh_from_db()
        assert reel.remaining_length == Decimal("324.500")

    def test_over_issue_is_rejected_with_the_remaining_length(self, tenant, yard, supplier):
        """E3, §6.1: "issuing more than remains is rejected", and the message
        must carry the figure so the storekeeper knows what to pick instead."""
        reel = ReelFactory(current_node=yard.node, initial_length=500, remaining_length=500)
        move(
            item_type=reel.item_type,
            quantity=Decimal("500"),
            from_node=supplier,
            to_node=yard.node,
            movement_type=MovementType.RECEIPT,
            tracking_mode=TrackingMode.REEL,
            reel=reel,
            uom="m",
        )
        move(
            item_type=reel.item_type,
            quantity=Decimal("160"),
            from_node=yard.node,
            to_node=consumed_node(tenant.pk),
            movement_type=MovementType.CONSUME,
            tracking_mode=TrackingMode.REEL,
            reel=reel,
            uom="m",
        )

        with pytest.raises(ReelExhausted) as caught:
            move(
                item_type=reel.item_type,
                quantity=Decimal("400"),
                from_node=yard.node,
                to_node=consumed_node(tenant.pk),
                movement_type=MovementType.CONSUME,
                tracking_mode=TrackingMode.REEL,
                reel=reel,
                uom="m",
            )

        assert caught.value.code == "REEL_EXHAUSTED"
        assert caught.value.details["remaining"] == "340.000"
        assert caught.value.details["reel"] == reel.drum_number

    def test_a_drum_reaching_zero_closes_itself(self, tenant, yard, supplier):
        """E3: "a drum reaching zero is closed automatically"."""
        reel = ReelFactory(current_node=yard.node, initial_length=200, remaining_length=200)
        move(
            item_type=reel.item_type,
            quantity=Decimal("200"),
            from_node=supplier,
            to_node=yard.node,
            movement_type=MovementType.RECEIPT,
            tracking_mode=TrackingMode.REEL,
            reel=reel,
            uom="m",
        )

        move(
            item_type=reel.item_type,
            quantity=Decimal("200"),
            from_node=yard.node,
            to_node=consumed_node(tenant.pk),
            movement_type=MovementType.CONSUME,
            tracking_mode=TrackingMode.REEL,
            reel=reel,
            uom="m",
        )

        reel.refresh_from_db()
        assert reel.remaining_length == Decimal("0.000")
        assert reel.status == ReelStatus.CLOSED
        assert reel.is_closed is True

    def test_a_cut_leaves_the_drum_where_it_was(self, tenant, yard, supplier):
        """E3, Q5: 120 m off a 500 m drum is a cut, not a handover.

        The drum stays in the yard holding 380 m, and 120 m of loose cable goes
        to the technician. Sending the drum along would leave the yard's balance
        short of cable the drum still claims to hold, and E3's "how much is left
        on drum D-0007" would stop being answerable.
        """
        from accounts.factories import UserFactory
        from locations.nodes import node_for_user

        holder = UserFactory(organization=tenant, full_name="Cutting Carl")
        reel = ReelFactory(current_node=yard.node, initial_length=500, remaining_length=500)
        move(
            item_type=reel.item_type,
            quantity=Decimal("500"),
            from_node=supplier,
            to_node=yard.node,
            movement_type=MovementType.RECEIPT,
            tracking_mode=TrackingMode.REEL,
            reel=reel,
            uom="m",
        )

        move(
            item_type=reel.item_type,
            quantity=Decimal("120"),
            from_node=yard.node,
            to_node=node_for_user(holder),
            movement_type=MovementType.ISSUE,
            tracking_mode=TrackingMode.REEL,
            reel=reel,
            uom="m",
        )

        reel.refresh_from_db()
        assert reel.remaining_length == Decimal("380.000")
        assert reel.current_node == yard.node
        # And the ledger agrees: the cable is with the technician, the rest is
        # still in the yard.
        assert balance_at(node_for_user(holder), reel.item_type) == Decimal("120")
        assert balance_at(yard.node, reel.item_type) == Decimal("380")

    def test_handing_over_a_whole_drum_moves_it_intact(self, tenant, yard, supplier):
        """E3: nobody cuts 500 m off a 500 m drum and leaves the drum behind.

        Asking for everything on the drum means taking the drum, so it travels
        still holding its length — and the ledger's 500 m at the destination is
        the same 500 m the drum reports.
        """
        from accounts.factories import UserFactory
        from locations.nodes import node_for_user

        holder = UserFactory(organization=tenant, full_name="Whole-drum Wanjiru")
        reel = ReelFactory(current_node=yard.node, initial_length=500, remaining_length=500)
        move(
            item_type=reel.item_type,
            quantity=Decimal("500"),
            from_node=supplier,
            to_node=yard.node,
            movement_type=MovementType.RECEIPT,
            tracking_mode=TrackingMode.REEL,
            reel=reel,
            uom="m",
        )

        move(
            item_type=reel.item_type,
            quantity=Decimal("500"),
            from_node=yard.node,
            to_node=node_for_user(holder),
            movement_type=MovementType.ISSUE,
            tracking_mode=TrackingMode.REEL,
            reel=reel,
            uom="m",
        )

        reel.refresh_from_db()
        assert reel.remaining_length == Decimal("500.000")
        assert reel.current_node == node_for_user(holder)
        assert reel.status != ReelStatus.CLOSED
        assert balance_at(node_for_user(holder), reel.item_type) == Decimal("500")

    def test_receiving_a_drum_does_not_consume_it(self, tenant, yard, supplier):
        """The receipt is the full length, and must not empty the drum.

        A drum arriving is the one case where quantity equals the whole reel and
        nothing has been used — an easy thing to get wrong, and the result would
        be 500 m of cable in the yard on a drum reporting zero.
        """
        reel = ReelFactory(current_node=supplier, initial_length=500, remaining_length=500)
        move(
            item_type=reel.item_type,
            quantity=Decimal("500"),
            from_node=supplier,
            to_node=yard.node,
            movement_type=MovementType.RECEIPT,
            tracking_mode=TrackingMode.REEL,
            reel=reel,
            uom="m",
        )

        reel.refresh_from_db()
        assert reel.remaining_length == Decimal("500.000")
        assert reel.current_node == yard.node
        assert reel.status != ReelStatus.CLOSED

    def test_drum_numbers_are_unique_within_the_tenant(self, tenant, supplier):
        """D4: "drum numbers are unique within the tenant"."""
        ReelFactory(drum_number="D-0007", current_node=supplier)

        with pytest.raises(IntegrityError), transaction.atomic():
            ReelFactory(drum_number="D-0007", current_node=supplier)

    def test_a_reel_movement_must_name_its_drum(self, tenant, yard, supplier):
        item = ItemTypeFactory(default_tracking_mode=TrackingMode.REEL, uom="m")

        with pytest.raises(LedgerRuleViolation, match="must name its drum"):
            move(
                item_type=item,
                quantity=Decimal("10"),
                from_node=supplier,
                to_node=yard.node,
                movement_type=MovementType.RECEIPT,
                tracking_mode=TrackingMode.REEL,
                uom="m",
            )

    def test_the_database_refuses_a_negative_remainder(self, tenant, supplier):
        """A negative length on a drum would be silently wrong for months."""
        reel = ReelFactory(current_node=supplier, initial_length=100, remaining_length=100)

        with pytest.raises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE stock_reel SET remaining_length = -1 WHERE id = %s", [reel.pk]
                )

    def test_the_database_refuses_a_remainder_above_the_start(self, tenant, supplier):
        reel = ReelFactory(current_node=supplier, initial_length=100, remaining_length=100)

        with pytest.raises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE stock_reel SET remaining_length = 101 WHERE id = %s", [reel.pk]
                )


class TestBulkTracking:
    def test_a_bulk_movement_carries_no_serial_or_drum(self, tenant, yard, supplier):
        item = ItemTypeFactory(default_tracking_mode=TrackingMode.BULK)
        unit = SerialUnitFactory(current_node=supplier)

        with pytest.raises(LedgerRuleViolation, match="neither a serial nor a drum"):
            move(
                item_type=item,
                quantity=Decimal("5"),
                from_node=supplier,
                to_node=yard.node,
                movement_type=MovementType.RECEIPT,
                tracking_mode=TrackingMode.BULK,
                serial_unit=unit,
            )


@pytest.mark.django_db(transaction=True)
class TestConcurrentIssueOfTheSameUnit:
    """§13: "concurrent release of the same serial — loser gets 409"."""

    def test_two_simultaneous_issues_of_one_unit_produce_exactly_one_movement(
        self, organization
    ):
        """The race a busy gate actually produces.

        Two storekeepers release the same serialized unit at the same moment.
        Exactly one must succeed; the other must fail rather than both
        succeeding and leaving the unit in two places.
        """
        from core.tenancy import tenant_context
        from stock.models import StockMovement

        with transaction.atomic(), tenant_context(organization):
            yard = YardFactory(name="Main yard")
            supplier_node = external_node(organization.pk)
            unit = SerialUnitFactory(current_node=supplier_node)
            post_movement(
                MovementRequest(
                    item_type=unit.item_type,
                    quantity=Decimal("1"),
                    from_node=supplier_node,
                    to_node=yard.node,
                    movement_type=MovementType.RECEIPT,
                    tracking_mode=TrackingMode.SERIALIZED,
                    serial_unit=unit,
                )
            )
            scrap = scrap_node(organization.pk)
            consumed = consumed_node(organization.pk)

        outcomes: list[str] = []
        lock = threading.Lock()
        start = threading.Barrier(2)

        def issue(destination):
            try:
                start.wait(timeout=10)
                with transaction.atomic(), tenant_context(organization):
                    post_movement(
                        MovementRequest(
                            item_type=unit.item_type,
                            quantity=Decimal("1"),
                            from_node=yard.node,
                            to_node=destination,
                            movement_type=MovementType.ISSUE,
                            tracking_mode=TrackingMode.SERIALIZED,
                            serial_unit=unit,
                        )
                    )
                with lock:
                    outcomes.append("ok")
            except Exception as exc:
                with lock:
                    outcomes.append(type(exc).__name__)
            finally:
                connection.close()

        threads = [
            threading.Thread(target=issue, args=(scrap,)),
            threading.Thread(target=issue, args=(consumed,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert outcomes.count("ok") == 1, (
            f"exactly one issue must succeed, got {outcomes}"
        )

        with transaction.atomic(), tenant_context(organization):
            issues = StockMovement.objects.filter(
                serial_unit=unit, movement_type=MovementType.ISSUE
            ).count()
        assert issues == 1
