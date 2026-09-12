"""T3.7–T3.11 — gate-in (§4.6; D1–D8, J1, M4, M6)."""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from accounts.factories import UserFactory
from catalogue.factories import CategoryCustomFieldFactory, ItemCategoryFactory, ItemTypeFactory
from catalogue.models import CategoryCustomField, TrackingMode
from core.exceptions import AlreadyPosted
from locations.factories import YardFactory
from locations.models import Location, LocationType
from network.factories import ClientFactory, SiteFactory
from receiving.models import (
    DocumentStatus,
    GateIn,
    GateInLine,
    GateInReel,
    GateInSerial,
    GateInSource,
)
from receiving.services import (
    AttachmentRequired,
    GateInNotReady,
    generate_asset_tag,
    post_gate_in,
    void_gate_in,
)
from stock.models import (
    Condition,
    MovementType,
    OwnerType,
    Reel,
    SerialUnit,
    SerialUnitStatus,
    StockMovement,
)
from stock.services import balance_at


@pytest.fixture
def yard(tenant):
    return YardFactory(name="Main yard")


@pytest.fixture
def storekeeper(tenant):
    return UserFactory(organization=tenant, full_name="Sara Storekeeper")


def draft(tenant, yard, **kwargs):
    kwargs.setdefault("source_type", GateInSource.PURCHASE)
    kwargs.setdefault("supplier_name", "Cable Supplies Ltd")
    return GateIn.objects.create(
        organization=tenant, to_location=yard, received_at=timezone.now(), **kwargs
    )


def add_bulk_line(gate_in, *, quantity=10, condition=Condition.NEW, item=None, **kwargs):
    item = item or ItemTypeFactory(default_tracking_mode=TrackingMode.BULK)
    return GateInLine.objects.create(
        organization=gate_in.organization,
        gate_in=gate_in,
        item_type=item,
        tracking_mode=TrackingMode.BULK,
        quantity=Decimal(str(quantity)),
        uom=item.uom,
        condition=condition,
        **kwargs,
    )


class TestDraftsDoNotAffectStock:
    """D8: "only posting affects stock. Drafts are freely editable"."""

    def test_a_draft_has_no_number(self, tenant, yard):
        """M6: allocating at draft creation would leave a gap per abandonment."""
        gate_in = draft(tenant, yard)

        assert gate_in.number == ""
        assert gate_in.status == DocumentStatus.DRAFT

    def test_a_draft_posts_no_movements(self, tenant, yard):
        gate_in = draft(tenant, yard)
        line = add_bulk_line(gate_in, quantity=10)

        assert StockMovement.objects.count() == 0
        assert balance_at(yard.node, line.item_type) == Decimal("0")

    def test_a_draft_is_editable(self, tenant, yard):
        gate_in = draft(tenant, yard)

        assert gate_in.is_editable is True


class TestSourceTypesCarryTheirInformation:
    """D1: origin and ownership must be unambiguous."""

    def test_consignment_stock_must_name_its_client(self, tenant, yard):
        """Otherwise it cannot be audited back to the operator (D1)."""
        with pytest.raises(ValidationError, match="must name the client"):
            draft(tenant, yard, source_type=GateInSource.CLIENT_ISSUE, client=None)

    def test_a_recovery_must_name_its_origin_site(self, tenant, yard):
        """D5: the operator is shown what was retrieved from where."""
        with pytest.raises(ValidationError, match="site it came from"):
            draft(tenant, yard, source_type=GateInSource.RECOVERY, origin_site=None)

    def test_a_recovery_with_a_site_is_accepted(self, tenant, yard):
        site = SiteFactory(internal_ref="SLV-9000")

        gate_in = draft(tenant, yard, source_type=GateInSource.RECOVERY, origin_site=site)

        assert gate_in.origin_site == site


class TestPostingBulkLines:
    def test_posting_allocates_a_number_and_creates_stock(self, tenant, yard, storekeeper):
        gate_in = draft(tenant, yard)
        line = add_bulk_line(gate_in, quantity=40)

        post_gate_in(gate_in, posted_by=storekeeper)

        gate_in.refresh_from_db()
        assert gate_in.number.startswith("GRN-")
        assert gate_in.status == DocumentStatus.POSTED
        assert balance_at(yard.node, line.item_type) == Decimal("40")

    def test_a_mixed_delivery_posts_in_one_document(self, tenant, yard, storekeeper):
        """D2: "mixed deliveries are recorded in one document"."""
        gate_in = draft(tenant, yard)
        bulk = add_bulk_line(gate_in, quantity=40)

        serialized_item = ItemTypeFactory(default_tracking_mode=TrackingMode.SERIALIZED)
        serial_line = GateInLine.objects.create(
            organization=tenant,
            gate_in=gate_in,
            item_type=serialized_item,
            tracking_mode=TrackingMode.SERIALIZED,
            quantity=Decimal("2"),
            uom="ea",
            line_number=2,
        )
        for serial in ("RRU-001", "RRU-002"):
            GateInSerial.objects.create(
                organization=tenant, line=serial_line, serial_number=serial
            )

        reel_item = ItemTypeFactory(default_tracking_mode=TrackingMode.REEL, uom="m")
        reel_line = GateInLine.objects.create(
            organization=tenant,
            gate_in=gate_in,
            item_type=reel_item,
            tracking_mode=TrackingMode.REEL,
            quantity=Decimal("500"),
            uom="m",
            line_number=3,
        )
        GateInReel.objects.create(
            organization=tenant, line=reel_line, drum_number="D-0007", length=Decimal("500")
        )

        post_gate_in(gate_in, posted_by=storekeeper)

        assert balance_at(yard.node, bulk.item_type) == Decimal("40")
        assert balance_at(yard.node, serialized_item) == Decimal("2")
        assert balance_at(yard.node, reel_item) == Decimal("500")
        assert SerialUnit.objects.count() == 2
        assert Reel.objects.get(drum_number="D-0007").remaining_length == Decimal("500.000")

    def test_posting_twice_is_refused(self, tenant, yard, storekeeper):
        """§13: ALREADY_POSTED rather than a second set of movements."""
        gate_in = draft(tenant, yard)
        add_bulk_line(gate_in)
        post_gate_in(gate_in, posted_by=storekeeper)

        with pytest.raises(AlreadyPosted):
            post_gate_in(gate_in, posted_by=storekeeper)

    def test_an_empty_gate_in_cannot_be_posted(self, tenant, yard, storekeeper):
        gate_in = draft(tenant, yard)

        with pytest.raises(GateInNotReady, match="at least one line"):
            post_gate_in(gate_in, posted_by=storekeeper)

    def test_the_movement_records_the_document_it_came_from(self, tenant, yard, storekeeper):
        """So a stock figure can always be traced back to its paperwork (M1)."""
        gate_in = draft(tenant, yard)
        add_bulk_line(gate_in, quantity=5)
        post_gate_in(gate_in, posted_by=storekeeper)

        movement = StockMovement.objects.get()
        assert movement.document_type == "receiving.GateIn"
        assert movement.document_id == str(gate_in.pk)
        assert movement.document_number == gate_in.number
        assert movement.posted_by == storekeeper


class TestOwnership:
    """D1, D3: ownership is set at gate-in and carried permanently."""

    def test_consignment_stock_is_recorded_against_its_client(self, tenant, yard, storekeeper):
        safaricom = ClientFactory(name="Safaricom")
        gate_in = draft(
            tenant, yard, source_type=GateInSource.CLIENT_ISSUE, client=safaricom
        )
        line = add_bulk_line(
            gate_in, quantity=25, owner_type=OwnerType.CLIENT, owner_client=safaricom
        )

        post_gate_in(gate_in, posted_by=storekeeper)

        assert balance_at(yard.node, line.item_type, owner_client=safaricom) == Decimal("25")
        # And not as own stock.
        assert balance_at(yard.node, line.item_type) == Decimal("0")

    def test_consignment_stock_comes_from_the_clients_issuing_store(
        self, tenant, yard, storekeeper
    ):
        """§3.1: so the client-owned position report can be answered."""
        safaricom = ClientFactory(name="Safaricom")
        gate_in = draft(
            tenant, yard, source_type=GateInSource.CLIENT_ISSUE, client=safaricom
        )
        add_bulk_line(gate_in, quantity=5, owner_type=OwnerType.CLIENT, owner_client=safaricom)

        post_gate_in(gate_in, posted_by=storekeeper)

        movement = StockMovement.objects.get()
        assert movement.from_node.client == safaricom


class TestUnserviceableLinesGoToQuarantine:
    """D2, J1: faulty, damaged and scrap land in quarantine, not free stock."""

    @pytest.mark.parametrize(
        "condition", [Condition.FAULTY, Condition.DAMAGED, Condition.SCRAP]
    )
    def test_an_unserviceable_line_lands_in_quarantine(
        self, tenant, yard, storekeeper, condition
    ):
        """Straight to quarantine at receipt, not moved there afterwards.

        Receiving into the yard first would leave a window in which faulty
        material was issuable — which is exactly what J1 forbids.
        """
        gate_in = draft(tenant, yard, source_type=GateInSource.WARRANTY_RETURN)
        line = add_bulk_line(gate_in, quantity=3, condition=condition)

        post_gate_in(gate_in, posted_by=storekeeper)

        quarantine = Location.objects.get(parent=yard, type=LocationType.QUARANTINE)
        assert balance_at(
            quarantine.node, line.item_type, condition=condition
        ) == Decimal("3")
        assert balance_at(yard.node, line.item_type, condition=condition) == Decimal("0")

    def test_quarantined_stock_is_not_available(self, tenant, yard, storekeeper):
        from stock.models import StockBalance

        gate_in = draft(tenant, yard, source_type=GateInSource.WARRANTY_RETURN)
        add_bulk_line(gate_in, quantity=3, condition=Condition.FAULTY)
        post_gate_in(gate_in, posted_by=storekeeper)

        quarantine = Location.objects.get(parent=yard, type=LocationType.QUARANTINE)
        available_nodes = {balance.node_id for balance in StockBalance.objects.available()}

        assert quarantine.node.pk not in available_nodes

    def test_a_serviceable_line_lands_in_the_yard(self, tenant, yard, storekeeper):
        gate_in = draft(tenant, yard)
        line = add_bulk_line(gate_in, quantity=3, condition=Condition.USED_SERVICEABLE)

        post_gate_in(gate_in, posted_by=storekeeper)

        assert balance_at(
            yard.node, line.item_type, condition=Condition.USED_SERVICEABLE
        ) == Decimal("3")


class TestSerializedLines:
    """D3: one identifier per unit, and duplicates rejected clearly."""

    def _serialized_gate_in(self, tenant, yard, serials, quantity=None):
        gate_in = draft(tenant, yard)
        item = ItemTypeFactory(default_tracking_mode=TrackingMode.SERIALIZED)
        line = GateInLine.objects.create(
            organization=tenant,
            gate_in=gate_in,
            item_type=item,
            tracking_mode=TrackingMode.SERIALIZED,
            quantity=Decimal(str(quantity if quantity is not None else len(serials))),
            uom="ea",
        )
        for serial in serials:
            GateInSerial.objects.create(
                organization=tenant, line=line, serial_number=serial
            )
        return gate_in, line

    def test_one_serial_per_unit_is_required(self, tenant, yard, storekeeper):
        gate_in, _ = self._serialized_gate_in(tenant, yard, ["RRU-001"], quantity=3)

        with pytest.raises(GateInNotReady) as caught:
            post_gate_in(gate_in, posted_by=storekeeper)

        assert "lines.0.serials" in caught.value.field_errors

    def test_each_serial_becomes_a_tracked_unit(self, tenant, yard, storekeeper):
        gate_in, _line = self._serialized_gate_in(tenant, yard, ["RRU-001", "RRU-002"])

        post_gate_in(gate_in, posted_by=storekeeper)

        units = SerialUnit.objects.order_by("serial_number")
        assert [unit.serial_number for unit in units] == ["RRU-001", "RRU-002"]
        assert all(unit.current_node == yard.node for unit in units)
        assert all(unit.status == SerialUnitStatus.IN_STOCK for unit in units)

    def test_a_duplicate_serial_says_where_the_existing_one_sits(
        self, tenant, yard, storekeeper
    ):
        """D3's stated criterion, exactly.

        "Already exists" is useless to a storekeeper holding the unit. Where it
        is lets them work out whether this is the same unit coming back.
        """
        first, _ = self._serialized_gate_in(tenant, yard, ["RRU-001"])
        post_gate_in(first, posted_by=storekeeper)

        second, _ = self._serialized_gate_in(tenant, yard, ["RRU-001"])

        with pytest.raises(GateInNotReady) as caught:
            post_gate_in(second, posted_by=storekeeper)

        message = caught.value.field_errors["lines.0.serials.RRU-001"][0]
        assert "already recorded" in message
        assert "Main yard" in message

    def test_a_recovered_unit_keeps_its_origin_site(self, tenant, yard, storekeeper):
        """D5: recovered client equipment stays attributable to its site."""
        site = SiteFactory(internal_ref="SLV-9000")
        gate_in = draft(
            tenant, yard, source_type=GateInSource.RECOVERY, origin_site=site
        )
        item = ItemTypeFactory(default_tracking_mode=TrackingMode.SERIALIZED)
        recovery_line = GateInLine.objects.create(
            organization=tenant,
            gate_in=gate_in,
            item_type=item,
            tracking_mode=TrackingMode.SERIALIZED,
            quantity=Decimal("1"),
            uom="ea",
            condition=Condition.USED_SERVICEABLE,
        )
        GateInSerial.objects.create(
            organization=tenant, line=recovery_line, serial_number="OLD-1"
        )

        post_gate_in(gate_in, posted_by=storekeeper)

        assert SerialUnit.objects.get(serial_number="OLD-1").origin_site == site


class TestTrackingModeOverride:
    """D3, D11: "the tracking mode defaults from the item type but is overridable"."""

    def test_a_serialized_item_may_be_received_as_bulk_with_a_reason(
        self, tenant, yard, storekeeper
    ):
        """D3's exact scenario: recovered units with unreadable serials."""
        gate_in = draft(tenant, yard, source_type=GateInSource.RECOVERY,
                        origin_site=SiteFactory())
        item = ItemTypeFactory(default_tracking_mode=TrackingMode.SERIALIZED)
        add_bulk_line(
            gate_in,
            quantity=7,
            item=item,
            condition=Condition.USED_SERVICEABLE,
            no_serial_reason="Serial plates unreadable after demolition.",
        )

        post_gate_in(gate_in, posted_by=storekeeper)

        assert balance_at(
            yard.node, item, condition=Condition.USED_SERVICEABLE
        ) == Decimal("7")
        assert SerialUnit.objects.count() == 0

    def test_forcing_bulk_without_a_reason_is_refused(self, tenant, yard, storekeeper):
        """Otherwise it is indistinguishable from an item that was always bulk,
        and the audit trail loses why the serials are missing (D3)."""
        gate_in = draft(tenant, yard)
        item = ItemTypeFactory(default_tracking_mode=TrackingMode.SERIALIZED)
        add_bulk_line(gate_in, quantity=7, item=item, no_serial_reason="")

        with pytest.raises(GateInNotReady) as caught:
            post_gate_in(gate_in, posted_by=storekeeper)

        assert "lines.0.no_serial_reason" in caught.value.field_errors


class TestAssetTags:
    """D3, C8: internal asset tags, when the tenant has them enabled."""

    def test_no_tag_is_generated_when_the_setting_is_off(self, tenant, yard, storekeeper):
        gate_in = draft(tenant, yard)
        item = ItemTypeFactory(default_tracking_mode=TrackingMode.SERIALIZED)
        line = GateInLine.objects.create(
            organization=tenant,
            gate_in=gate_in,
            item_type=item,
            tracking_mode=TrackingMode.SERIALIZED,
            quantity=Decimal("1"),
            uom="ea",
        )
        GateInSerial.objects.create(organization=tenant, line=line, serial_number="SN-1")

        post_gate_in(gate_in, posted_by=storekeeper)

        assert SerialUnit.objects.get(serial_number="SN-1").asset_tag == ""

    def test_a_tag_is_generated_when_the_setting_is_on(self, tenant, yard, storekeeper):
        settings = tenant.settings
        settings.asset_tag_enabled = True
        settings.save()

        gate_in = draft(tenant, yard)
        item = ItemTypeFactory(default_tracking_mode=TrackingMode.SERIALIZED)
        line = GateInLine.objects.create(
            organization=tenant,
            gate_in=gate_in,
            item_type=item,
            tracking_mode=TrackingMode.SERIALIZED,
            quantity=Decimal("1"),
            uom="ea",
        )
        GateInSerial.objects.create(organization=tenant, line=line, serial_number="SN-1")

        post_gate_in(gate_in, posted_by=storekeeper)

        assert SerialUnit.objects.get(serial_number="SN-1").asset_tag != ""

    def test_the_tag_follows_the_configured_format(self, tenant):
        settings = tenant.settings
        settings.asset_tag_enabled = True
        settings.asset_tag_prefix_format = "{org}-{category}-{seq:06d}"
        settings.save()
        category = ItemCategoryFactory(name="Active equipment")
        item = ItemTypeFactory(category=category, name="RRU 2x40W")

        with transaction.atomic():
            tag = generate_asset_tag(tenant, item)

        assert tag.startswith("SILVER-ACTIVE-")
        assert tag.endswith("000001")

    def test_tags_are_sequential_and_do_not_repeat(self, tenant):
        settings = tenant.settings
        settings.asset_tag_enabled = True
        settings.save()
        item = ItemTypeFactory()

        with transaction.atomic():
            tags = [generate_asset_tag(tenant, item) for _ in range(3)]

        assert len(set(tags)) == 3

    def test_a_malformed_format_does_not_block_receiving(self, tenant):
        """A tenant mistyping their format should get an odd-looking tag, not an
        unpostable delivery with a driver waiting at the gate."""
        settings = tenant.settings
        settings.asset_tag_enabled = True
        settings.asset_tag_prefix_format = "{nonsense}-{seq:06d}"
        settings.save()

        with transaction.atomic():
            tag = generate_asset_tag(tenant, ItemTypeFactory())

        assert tag


class TestReelLines:
    """D4: cable onto a numbered drum with a starting length."""

    def _reel_gate_in(self, tenant, yard, drums, quantity=None):
        gate_in = draft(tenant, yard)
        item = ItemTypeFactory(default_tracking_mode=TrackingMode.REEL, uom="m")
        total = quantity if quantity is not None else sum(length for _, length in drums)
        line = GateInLine.objects.create(
            organization=tenant,
            gate_in=gate_in,
            item_type=item,
            tracking_mode=TrackingMode.REEL,
            quantity=Decimal(str(total)),
            uom="m",
        )
        for drum_number, length in drums:
            GateInReel.objects.create(
                organization=tenant,
                line=line,
                drum_number=drum_number,
                length=Decimal(str(length)),
            )
        return gate_in, line, item

    def test_a_drum_is_created_with_its_starting_length(self, tenant, yard, storekeeper):
        gate_in, _line, _item = self._reel_gate_in(tenant, yard, [("D-0007", 500)])

        post_gate_in(gate_in, posted_by=storekeeper)

        reel = Reel.objects.get(drum_number="D-0007")
        assert reel.initial_length == Decimal("500.000")
        assert reel.remaining_length == Decimal("500.000")
        assert reel.current_node == yard.node

    def test_several_drums_on_one_line(self, tenant, yard, storekeeper):
        gate_in, _line, item = self._reel_gate_in(
            tenant, yard, [("D-1", 500), ("D-2", 250)]
        )

        post_gate_in(gate_in, posted_by=storekeeper)

        assert Reel.objects.count() == 2
        assert balance_at(yard.node, item) == Decimal("750")

    def test_the_drums_must_total_the_line_quantity(self, tenant, yard, storekeeper):
        """A mismatch means someone mistyped, and posting it would put the
        ledger and the paperwork permanently out of step."""
        gate_in, _line, _item = self._reel_gate_in(
            tenant, yard, [("D-1", 400)], quantity=500
        )

        with pytest.raises(GateInNotReady) as caught:
            post_gate_in(gate_in, posted_by=storekeeper)

        assert "lines.0.quantity" in caught.value.field_errors

    def test_a_reel_line_needs_at_least_one_drum(self, tenant, yard, storekeeper):
        gate_in, _line, _item = self._reel_gate_in(tenant, yard, [], quantity=500)

        with pytest.raises(GateInNotReady) as caught:
            post_gate_in(gate_in, posted_by=storekeeper)

        assert "lines.0.reels" in caught.value.field_errors

    def test_a_duplicate_drum_number_is_refused(self, tenant, yard, storekeeper):
        """D4: drum numbers are unique within the tenant."""
        first, _line, _item = self._reel_gate_in(tenant, yard, [("D-0007", 500)])
        post_gate_in(first, posted_by=storekeeper)

        second, _line, _item = self._reel_gate_in(tenant, yard, [("D-0007", 300)])

        with pytest.raises(GateInNotReady) as caught:
            post_gate_in(second, posted_by=storekeeper)

        assert "lines.0.reels.D-0007" in caught.value.field_errors


class TestCustomFieldValidation:
    """C2, T3.11: required custom fields are enforced at posting."""

    def test_a_missing_required_field_blocks_posting(self, tenant, yard, storekeeper):
        category = ItemCategoryFactory(name="Active equipment")
        CategoryCustomFieldFactory(
            category=category, key="vendor", label="Vendor", required_at_gate_in=True
        )
        item = ItemTypeFactory(category=category, default_tracking_mode=TrackingMode.BULK)

        gate_in = draft(tenant, yard)
        add_bulk_line(gate_in, item=item, custom_field_values={})

        with pytest.raises(GateInNotReady) as caught:
            post_gate_in(gate_in, posted_by=storekeeper)

        assert "lines.0.custom_field_values.vendor" in caught.value.field_errors

    def test_a_provided_required_field_allows_posting(self, tenant, yard, storekeeper):
        category = ItemCategoryFactory(name="Active equipment")
        CategoryCustomFieldFactory(
            category=category, key="vendor", label="Vendor", required_at_gate_in=True
        )
        item = ItemTypeFactory(category=category, default_tracking_mode=TrackingMode.BULK)

        gate_in = draft(tenant, yard)
        add_bulk_line(gate_in, item=item, custom_field_values={"vendor": "Huawei"})

        post_gate_in(gate_in, posted_by=storekeeper)

        assert gate_in.status == DocumentStatus.POSTED

    def test_a_wrongly_typed_value_blocks_posting(self, tenant, yard, storekeeper):
        category = ItemCategoryFactory()
        CategoryCustomFieldFactory(
            category=category,
            key="year",
            label="Year",
            field_type=CategoryCustomField.FieldType.NUMBER,
        )
        item = ItemTypeFactory(category=category, default_tracking_mode=TrackingMode.BULK)

        gate_in = draft(tenant, yard)
        add_bulk_line(gate_in, item=item, custom_field_values={"year": "not a number"})

        with pytest.raises(GateInNotReady) as caught:
            post_gate_in(gate_in, posted_by=storekeeper)

        assert "lines.0.custom_field_values.year" in caught.value.field_errors


class TestAttachmentRequirement:
    """D6, C8: a tenant may make attachments mandatory at gate-in."""

    def test_posting_without_a_required_attachment_is_refused(
        self, tenant, yard, storekeeper
    ):
        settings = tenant.settings
        settings.attachments_required_gate_in = True
        settings.save()

        gate_in = draft(tenant, yard)
        add_bulk_line(gate_in)

        with pytest.raises(AttachmentRequired):
            post_gate_in(gate_in, posted_by=storekeeper)

    def test_posting_with_an_attachment_succeeds(self, tenant, yard, storekeeper):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from core.attachments import store_attachment
        from core.models import AttachmentKind

        settings = tenant.settings
        settings.attachments_required_gate_in = True
        settings.save()

        gate_in = draft(tenant, yard)
        add_bulk_line(gate_in)
        store_attachment(
            target=gate_in,
            uploaded_file=SimpleUploadedFile("note.pdf", b"delivery note"),
            kind=AttachmentKind.DOCUMENT,
        )

        post_gate_in(gate_in, posted_by=storekeeper)

        assert gate_in.status == DocumentStatus.POSTED

    def test_attachments_are_optional_by_default(self, tenant, yard, storekeeper):
        """D6: "optional by default"."""
        gate_in = draft(tenant, yard)
        add_bulk_line(gate_in)

        post_gate_in(gate_in, posted_by=storekeeper)

        assert gate_in.status == DocumentStatus.POSTED


class TestVoidAndReversal:
    """T3.10, M4, M6: void by reversal, keeping the number."""

    def test_voiding_returns_the_balance_to_its_prior_value(self, tenant, yard, storekeeper):
        gate_in = draft(tenant, yard)
        line = add_bulk_line(gate_in, quantity=40)
        post_gate_in(gate_in, posted_by=storekeeper)

        void_gate_in(gate_in, reason="Delivery rejected at the gate", voided_by=storekeeper)

        assert balance_at(yard.node, line.item_type) == Decimal("0")

    def test_the_number_is_kept_and_never_reused(self, tenant, yard, storekeeper):
        """M6: "a voided document keeps its number and is marked void"."""
        first = draft(tenant, yard)
        add_bulk_line(first)
        post_gate_in(first, posted_by=storekeeper)
        original_number = first.number

        void_gate_in(first, reason="Wrong supplier", voided_by=storekeeper)

        first.refresh_from_db()
        assert first.number == original_number
        assert first.status == DocumentStatus.VOID

        # The next document takes the following number, not the voided one.
        second = draft(tenant, yard)
        add_bulk_line(second)
        post_gate_in(second, posted_by=storekeeper)
        assert second.number != original_number

    def test_the_original_movements_survive(self, tenant, yard, storekeeper):
        """An auditor sees the receipt and its reversal, not a gap."""
        gate_in = draft(tenant, yard)
        add_bulk_line(gate_in, quantity=40)
        post_gate_in(gate_in, posted_by=storekeeper)

        void_gate_in(gate_in, reason="Rejected", voided_by=storekeeper)

        movements = StockMovement.objects.filter(document_id=str(gate_in.pk))
        assert movements.filter(movement_type=MovementType.RECEIPT).count() == 1
        assert movements.filter(movement_type=MovementType.REVERSAL).count() == 1

    def test_voiding_requires_a_reason(self, tenant, yard, storekeeper):
        gate_in = draft(tenant, yard)
        add_bulk_line(gate_in)
        post_gate_in(gate_in, posted_by=storekeeper)

        with pytest.raises(GateInNotReady, match="requires a reason"):
            void_gate_in(gate_in, reason="", voided_by=storekeeper)

    def test_a_draft_cannot_be_voided(self, tenant, yard, storekeeper):
        gate_in = draft(tenant, yard)

        with pytest.raises(GateInNotReady, match="posted gate-in"):
            void_gate_in(gate_in, reason="Never mind", voided_by=storekeeper)

    def test_voiding_a_serialized_receipt_returns_the_unit(self, tenant, yard, storekeeper):
        gate_in = draft(tenant, yard)
        item = ItemTypeFactory(default_tracking_mode=TrackingMode.SERIALIZED)
        line = GateInLine.objects.create(
            organization=tenant,
            gate_in=gate_in,
            item_type=item,
            tracking_mode=TrackingMode.SERIALIZED,
            quantity=Decimal("1"),
            uom="ea",
        )
        GateInSerial.objects.create(organization=tenant, line=line, serial_number="SN-1")
        post_gate_in(gate_in, posted_by=storekeeper)

        void_gate_in(gate_in, reason="Wrong unit", voided_by=storekeeper)

        assert balance_at(yard.node, item) == Decimal("0")
        # The unit record itself survives — its history is what proves the void.
        assert SerialUnit.objects.filter(serial_number="SN-1").exists()


class TestAmendmentIsOffByDefault:
    """M4: "default: posted documents are immutable"."""

    def test_a_posted_document_may_not_be_amended_by_default(self, tenant, yard, storekeeper):
        from receiving.services import can_amend

        gate_in = draft(tenant, yard)
        add_bulk_line(gate_in)
        post_gate_in(gate_in, posted_by=storekeeper)

        assert can_amend(gate_in) is False

    def test_a_tenant_may_enable_amendment(self, tenant, yard, storekeeper):
        from receiving.services import can_amend

        settings = tenant.settings
        settings.allow_document_amendment = True
        settings.save()

        gate_in = draft(tenant, yard)
        add_bulk_line(gate_in)
        post_gate_in(gate_in, posted_by=storekeeper)

        assert can_amend(gate_in) is True

    def test_a_draft_is_always_amendable(self, tenant, yard):
        from receiving.services import can_amend

        assert can_amend(draft(tenant, yard)) is True
