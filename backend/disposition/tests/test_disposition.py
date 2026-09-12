"""T6.1–T6.4 — dispositions, disposals and client returns (§4.11, §4.12).

Three criteria are stated outright and each has a test named after it:

* T6.1 — "restoring to serviceable returns stock to availability and scrapping
  does not"
* T6.2 — "disposing of client-owned material without approval is **impossible
  regardless of configured rules**"
* T6.4 — "the client-owned position report distinguishes in-transit from
  acknowledged"

The second is the one worth being careful about. It is not enough that the
default rules require approval — a tenant with *no* rules at all, or rules
deliberately edited to require nothing, must still be unable to write off an
operator's property. So that test starts by deleting every approval rule.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from accounts.factories import UserFactory
from catalogue.factories import ItemTypeFactory
from disposition.models import (
    Disposal,
    DisposalLine,
    DisposalMethod,
    DisposalStatus,
    Disposition,
    DispositionDecision,
    DispositionLine,
    DispositionStatus,
)
from disposition.services import (
    DisposalNotReady,
    DispositionNotReady,
    NotFromQuarantine,
    ReturnNotAcknowledgeable,
    acknowledge_client_return,
    post_disposal,
    post_disposition,
    reject_disposition,
    submit_disposal,
    submit_disposition,
    unacknowledged_returns,
)
from locations.factories import YardFactory
from locations.nodes import node_for_location, quarantine_location, scrap_node
from network.factories import ClientFactory
from stock.models import Condition, MovementType, OwnerType
from stock.queries import stock_on_hand
from stock.services import MovementRequest, balance_at, post_movement


@pytest.fixture
def yard(tenant):
    return YardFactory(name="Main yard")


@pytest.fixture
def quarantine(tenant, yard):
    return quarantine_location(tenant.pk, yard)


@pytest.fixture
def storekeeper(tenant):
    return UserFactory(organization=tenant, full_name="Sara Storekeeper")


@pytest.fixture
def owner(tenant):
    return UserFactory(organization=tenant, full_name="Sam Owner")


def quarantined(tenant, quarantine, item, quantity, *, condition=Condition.FAULTY, client=None):
    """Put something in quarantine the way a gate-in would (D2, J1)."""
    from locations.nodes import external_node

    with transaction.atomic():
        post_movement(
            MovementRequest(
                item_type=item,
                quantity=Decimal(str(quantity)),
                from_node=external_node(tenant.pk, client=client),
                to_node=node_for_location(quarantine),
                movement_type=MovementType.RECEIPT,
                condition=condition,
                owner_type=OwnerType.CLIENT if client else OwnerType.OWN,
                owner_client=client,
            )
        )


def a_disposition(tenant, quarantine, item, quantity, *, decision, **kwargs):
    """A disposition and its one line. Line fields are named separately, because
    ``condition`` means different things on the two."""
    condition = kwargs.pop("condition", Condition.FAULTY)
    to_condition = kwargs.pop("to_condition", "")
    owner_client = kwargs.pop("owner_client", None)

    disposition = Disposition.objects.create(
        organization=tenant,
        from_location=quarantine,
        decision=decision,
        reason=kwargs.pop("reason", "Assessed at the bench."),
        **kwargs,
    )
    DispositionLine.objects.create(
        organization=tenant,
        disposition=disposition,
        item_type=item,
        quantity=Decimal(str(quantity)),
        uom=item.uom,
        condition=condition,
        to_condition=to_condition,
        owner_type=OwnerType.CLIENT if owner_client else OwnerType.OWN,
        owner_client=owner_client,
    )
    return disposition


class TestTheDecisionIsRecorded:
    def test_a_disposition_needs_a_reason(self, tenant, quarantine):
        """J2: "each outcome is an explicit, approved decision with a recorded
        reason." A blank reason is the thing the document exists to prevent."""
        with pytest.raises(ValidationError):
            Disposition.objects.create(
                organization=tenant,
                from_location=quarantine,
                decision=DispositionDecision.SCRAP,
                reason="",
            )

    def test_restoring_has_to_say_where_it_goes_back_to(self, tenant, quarantine):
        with pytest.raises(ValidationError) as failure:
            Disposition.objects.create(
                organization=tenant,
                from_location=quarantine,
                decision=DispositionDecision.RESTORE_TO_SERVICEABLE,
                reason="Tested fine.",
            )
        assert "to_location" in failure.value.message_dict

    def test_a_repair_has_to_name_who_has_it(self, tenant, quarantine):
        """I3: material nobody can chase is material nobody chases."""
        with pytest.raises(ValidationError) as failure:
            Disposition.objects.create(
                organization=tenant,
                from_location=quarantine,
                decision=DispositionDecision.REPAIR,
                reason="Send to Huawei.",
            )
        assert "vendor_name" in failure.value.message_dict

    def test_material_has_to_come_from_quarantine(self, tenant, yard, item_in_yard=None):
        """J1, J2: a disposition is about quarantined stock, not free stock."""
        item = ItemTypeFactory(name="Not quarantined")
        disposition = a_disposition(
            tenant, yard, item, 1, decision=DispositionDecision.SCRAP
        )

        with pytest.raises(NotFromQuarantine):
            submit_disposition(disposition, submitted_by=None)


class TestRestoreAndScrap:
    """T6.1's criterion, both halves."""

    def test_restoring_to_serviceable_returns_stock_to_availability(
        self, tenant, yard, quarantine, storekeeper
    ):
        item = ItemTypeFactory(name="Repaired RRU", uom="ea")
        quarantined(tenant, quarantine, item, 2)

        # J1: quarantined stock is not available, which is where this starts.
        assert _available(item) == Decimal("0")

        disposition = a_disposition(
            tenant,
            quarantine,
            item,
            2,
            decision=DispositionDecision.RESTORE_TO_SERVICEABLE,
            to_location=yard,
            to_condition=Condition.USED_SERVICEABLE,
            reason="Bench-tested and passed.",
        )
        submit_disposition(disposition, submitted_by=storekeeper)
        post_disposition(disposition, posted_by=storekeeper)

        # Out of quarantine, into the yard, and issuable again.
        assert balance_at(node_for_location(quarantine), item, condition=Condition.FAULTY) == (
            Decimal("0")
        )
        assert _available(item) == Decimal("2")

    def test_a_restored_unit_is_serviceable_rather_than_new(
        self, tenant, yard, quarantine, storekeeper
    ):
        """A repaired radio is not new again — recording it as new would launder
        its condition history, and the next person to read the ledger would be
        misled about what they are holding."""
        item = ItemTypeFactory(name="Restored antenna", uom="ea")
        quarantined(tenant, quarantine, item, 1)

        disposition = a_disposition(
            tenant,
            quarantine,
            item,
            1,
            decision=DispositionDecision.RESTORE_TO_SERVICEABLE,
            to_location=yard,
            to_condition=Condition.USED_SERVICEABLE,
        )
        submit_disposition(disposition, submitted_by=storekeeper)
        post_disposition(disposition, posted_by=storekeeper)

        assert balance_at(
            node_for_location(yard), item, condition=Condition.USED_SERVICEABLE
        ) == Decimal("1")
        assert balance_at(node_for_location(yard), item, condition=Condition.NEW) == (
            Decimal("0")
        )

    def test_scrapping_does_not_return_stock_to_availability(
        self, tenant, quarantine, storekeeper
    ):
        """The other half of T6.1, and the reason SCRAP moves nothing here.

        Deciding something is scrap is not destroying it. The material stays in
        quarantine until an approved **Disposal** takes it out (J3), so it is
        neither issuable nor quietly gone.
        """
        item = ItemTypeFactory(name="Dead RRU", uom="ea")
        quarantined(tenant, quarantine, item, 1)

        disposition = a_disposition(
            tenant, quarantine, item, 1, decision=DispositionDecision.SCRAP
        )
        submit_disposition(disposition, submitted_by=storekeeper)
        post_disposition(disposition, posted_by=storekeeper)

        assert _available(item) == Decimal("0")
        # Still in quarantine, still on the books, awaiting its disposal.
        assert balance_at(
            node_for_location(quarantine), item, condition=Condition.FAULTY
        ) == Decimal("1")

    def test_a_repair_leaves_the_yard_but_stays_ours(
        self, tenant, quarantine, storekeeper
    ):
        """I3: at the vendor, off the shelf, still on the books."""
        item = ItemTypeFactory(name="Vendor repair", uom="ea")
        quarantined(tenant, quarantine, item, 1)

        disposition = a_disposition(
            tenant,
            quarantine,
            item,
            1,
            decision=DispositionDecision.REPAIR,
            vendor_name="Huawei Kenya service centre",
            expected_return_date=timezone.now().date() + timedelta(days=21),
        )
        submit_disposition(disposition, submitted_by=storekeeper)
        post_disposition(disposition, posted_by=storekeeper)

        assert _available(item) == Decimal("0")
        assert balance_at(
            node_for_location(quarantine), item, condition=Condition.FAULTY
        ) == Decimal("0")

    def test_nothing_posts_before_approval(self, tenant, yard, quarantine, storekeeper):
        item = ItemTypeFactory(name="Unapproved restore", uom="ea")
        quarantined(tenant, quarantine, item, 1)

        disposition = a_disposition(
            tenant,
            quarantine,
            item,
            1,
            decision=DispositionDecision.RESTORE_TO_SERVICEABLE,
            to_location=yard,
        )

        with pytest.raises(DispositionNotReady):
            post_disposition(disposition, posted_by=storekeeper)

    def test_a_rejected_disposition_leaves_the_material_where_it_is(
        self, tenant, yard, quarantine, storekeeper, owner
    ):
        item = ItemTypeFactory(name="Refused restore", uom="ea")
        quarantined(tenant, quarantine, item, 1)

        disposition = a_disposition(
            tenant,
            quarantine,
            item,
            1,
            decision=DispositionDecision.RESTORE_TO_SERVICEABLE,
            to_location=yard,
        )
        submit_disposition(disposition, submitted_by=storekeeper)

        if disposition.status == DispositionStatus.PENDING_APPROVAL:
            reject_disposition(
                disposition, actor=owner, reason="Not convinced it was tested."
            )
            assert disposition.status == DispositionStatus.REJECTED

        assert _available(item) == Decimal("0")
        assert balance_at(
            node_for_location(quarantine), item, condition=Condition.FAULTY
        ) == Decimal("1")


class TestDisposalAlwaysNeedsApprovalForClientMaterial:
    """T6.2's criterion: "**impossible regardless of configured rules**"."""

    def test_client_owned_material_cannot_be_written_off_without_approval(
        self, tenant, quarantine, storekeeper
    ):
        from approvals.models import ApprovalRule

        # The hostile case: a tenant with no approval rules whatsoever.
        ApprovalRule.objects.all().delete()

        client = ClientFactory(name="Safaricom")
        item = ItemTypeFactory(name="Client RRU", uom="ea")
        quarantined(tenant, quarantine, item, 1, client=client)

        disposal = Disposal.objects.create(
            organization=tenant,
            from_location=quarantine,
            method=DisposalMethod.LICENSED_HANDLER,
        )
        DisposalLine.objects.create(
            organization=tenant,
            disposal=disposal,
            item_type=item,
            quantity=Decimal("1"),
            uom=item.uom,
            condition=Condition.FAULTY,
            owner_type=OwnerType.CLIENT,
            owner_client=client,
        )

        submit_disposal(disposal, submitted_by=storekeeper)

        # §5.2's hardcoded escalation: no rule asked for this, and it is required
        # anyway. Writing off an operator's property is not a decision a
        # contractor takes unilaterally.
        assert disposal.status == DisposalStatus.PENDING_APPROVAL

        with pytest.raises(DisposalNotReady):
            post_disposal(disposal, posted_by=storekeeper)

        assert balance_at(
            node_for_location(quarantine),
            item,
            condition=Condition.FAULTY,
            # A client-owned balance is a different balance (D1, §3.3).
            owner_client=client,
        ) == Decimal("1")

    def test_own_material_with_no_rules_auto_approves_and_is_recorded(
        self, tenant, quarantine, storekeeper
    ):
        """§5.2: auto-approval is fine for our own scrap, but never silent.

        An auto-approved write-off still writes an ApprovalAction, so "why did
        this leave without approval?" has an answer.
        """
        from approvals.models import ApprovalAction, ApprovalRule

        ApprovalRule.objects.all().delete()

        item = ItemTypeFactory(name="Our own scrap", uom="ea")
        quarantined(tenant, quarantine, item, 3, condition=Condition.SCRAP)

        disposal = Disposal.objects.create(
            organization=tenant,
            from_location=quarantine,
            method=DisposalMethod.SCRAP_DEALER,
            handler_name="Ngong Road scrap dealers",
            handler_reference="SD-4471",
        )
        DisposalLine.objects.create(
            organization=tenant,
            disposal=disposal,
            item_type=item,
            quantity=Decimal("3"),
            uom=item.uom,
            condition=Condition.SCRAP,
        )

        submit_disposal(disposal, submitted_by=storekeeper)

        assert disposal.status == DisposalStatus.APPROVED
        assert ApprovalAction.objects.filter(
            approval_request__document_type="disposition.Disposal",
            approval_request__document_id=str(disposal.pk),
            decision="AUTO",
        ).exists()

    def test_an_approved_disposal_moves_the_material_to_scrap(
        self, tenant, quarantine, storekeeper, owner
    ):
        """§3.1: SCRAP is the node material does not come back from."""
        from approvals.models import ApprovalRule

        ApprovalRule.objects.all().delete()

        item = ItemTypeFactory(name="Disposed cable", uom="m")
        quarantined(tenant, quarantine, item, 40, condition=Condition.SCRAP)

        disposal = Disposal.objects.create(
            organization=tenant,
            from_location=quarantine,
            method=DisposalMethod.DESTROYED_ON_SITE,
        )
        DisposalLine.objects.create(
            organization=tenant,
            disposal=disposal,
            item_type=item,
            quantity=Decimal("40"),
            uom=item.uom,
            condition=Condition.SCRAP,
        )
        submit_disposal(disposal, submitted_by=storekeeper)
        post_disposal(disposal, posted_by=owner)

        assert disposal.status == DisposalStatus.DISPOSED
        assert disposal.disposed_at is not None
        assert balance_at(scrap_node(tenant.pk), item, condition=Condition.SCRAP) == (
            Decimal("40")
        )
        assert balance_at(
            node_for_location(quarantine), item, condition=Condition.SCRAP
        ) == Decimal("0")
        assert _available(item) == Decimal("0")

    def test_a_disposal_cannot_be_posted_twice(self, tenant, quarantine, storekeeper):
        """The ledger refuses UPDATE and DELETE (§3.2), so a second posting would
        double the write-off with nothing to undo it."""
        from approvals.models import ApprovalRule

        ApprovalRule.objects.all().delete()

        item = ItemTypeFactory(name="Twice-disposed", uom="ea")
        quarantined(tenant, quarantine, item, 2, condition=Condition.SCRAP)

        disposal = Disposal.objects.create(
            organization=tenant,
            from_location=quarantine,
            method=DisposalMethod.OTHER,
            notes="Skip at the back.",
        )
        DisposalLine.objects.create(
            organization=tenant,
            disposal=disposal,
            item_type=item,
            quantity=Decimal("2"),
            uom=item.uom,
            condition=Condition.SCRAP,
        )
        submit_disposal(disposal, submitted_by=storekeeper)
        post_disposal(disposal, posted_by=storekeeper)

        with pytest.raises(DisposalNotReady):
            post_disposal(disposal, posted_by=storekeeper)

        assert balance_at(scrap_node(tenant.pk), item, condition=Condition.SCRAP) == (
            Decimal("2")
        )


def _available(item) -> Decimal:
    """What the yard could issue right now (§3.3, J1)."""
    from django.db.models import Sum

    return stock_on_hand(item_type=item).aggregate(total=Sum("quantity"))[
        "total"
    ] or Decimal("0")


class TestClientReturns:
    """T6.3 and T6.4 (§4.12; K1, K3)."""

    @pytest.fixture
    def returned(self, tenant, yard, storekeeper):
        """Consignment material received, then sent back to the client."""
        from dispatch.models import GateOut, GateOutLine, GateOutPurpose
        from dispatch.services import release_gate_out, submit_gate_out
        from locations.nodes import external_node

        client = ClientFactory(name="Safaricom")
        item = ItemTypeFactory(name="Consignment RRU", uom="ea")

        with transaction.atomic():
            post_movement(
                MovementRequest(
                    item_type=item,
                    quantity=Decimal("5"),
                    from_node=external_node(tenant.pk, client=client),
                    to_node=node_for_location(yard),
                    movement_type=MovementType.RECEIPT,
                    owner_type=OwnerType.CLIENT,
                    owner_client=client,
                )
            )

        gate_out = GateOut.objects.create(
            organization=tenant,
            from_location=yard,
            client=client,
            custody_holder=storekeeper,
            requested_by=storekeeper,
            purpose_type=GateOutPurpose.RETURN_TO_CLIENT,
        )
        GateOutLine.objects.create(
            organization=tenant,
            gate_out=gate_out,
            item_type=item,
            tracking_mode="BULK",
            requested_qty=Decimal("2"),
            uom=item.uom,
            owner_type=OwnerType.CLIENT,
            owner_client=client,
        )
        submit_gate_out(gate_out, submitted_by=storekeeper)
        release_gate_out(gate_out, released_by=storekeeper)
        return gate_out, client, item

    def test_returned_material_leaves_available_stock(self, tenant, returned):
        """T6.3: it is gone from the yard the moment it is released."""
        _gate_out, _client, item = returned

        assert _available(item) == Decimal("3")

    def test_returned_material_still_reports_as_in_transit(self, tenant, returned):
        """T6.3: "still reports under the client's position as in transit".

        This is the requirement's point: K1 says returned material "remains our
        exposure" until the client acknowledges it, so it must not vanish from
        the report the moment it leaves the gate.
        """
        from disposition.queries import ClientMaterialState, client_position, exposure_for

        gate_out, client, _item = returned
        rows = client_position(client)
        by_state = {row["state"]: row for row in rows}

        assert by_state[ClientMaterialState.HELD]["quantity"] == "3.000"
        assert by_state[ClientMaterialState.IN_TRANSIT]["quantity"] == "2.000"
        # Actionable: which return, and when it went.
        assert by_state[ClientMaterialState.IN_TRANSIT]["documents"][0]["number"] == (
            gate_out.number
        )
        # All five are still ours to answer for.
        assert exposure_for(client) == Decimal("5")

    def test_acknowledgement_distinguishes_it_from_in_transit(self, tenant, returned):
        """T6.4's criterion, and K3's "liability ends on the record"."""
        from disposition.queries import ClientMaterialState, client_position, exposure_for

        gate_out, client, _item = returned

        acknowledge_client_return(
            gate_out,
            acknowledged_ref="SAF-GRN-77120",
            acknowledged_by_name="J. Mwangi, Safaricom store",
        )

        rows = {row["state"]: row for row in client_position(client)}
        assert ClientMaterialState.IN_TRANSIT not in rows
        assert rows[ClientMaterialState.ACKNOWLEDGED]["quantity"] == "2.000"
        assert rows[ClientMaterialState.ACKNOWLEDGED]["documents"][0][
            "acknowledged_ref"
        ] == "SAF-GRN-77120"
        # Exposure drops by exactly what they signed for.
        assert exposure_for(client) == Decimal("3")

    def test_a_return_cannot_be_acknowledged_before_it_leaves(
        self, tenant, yard, storekeeper
    ):
        """Otherwise a signature exists for material still on the shelf."""
        from dispatch.models import GateOut, GateOutLine, GateOutPurpose

        client = ClientFactory(name="Airtel")
        item = ItemTypeFactory(name="Unreleased return", uom="ea")

        gate_out = GateOut.objects.create(
            organization=tenant,
            from_location=yard,
            client=client,
            custody_holder=storekeeper,
            requested_by=storekeeper,
            purpose_type=GateOutPurpose.RETURN_TO_CLIENT,
        )
        GateOutLine.objects.create(
            organization=tenant,
            gate_out=gate_out,
            item_type=item,
            tracking_mode="BULK",
            requested_qty=Decimal("1"),
            uom=item.uom,
            owner_type=OwnerType.CLIENT,
            owner_client=client,
        )

        with pytest.raises(ReturnNotAcknowledgeable):
            acknowledge_client_return(gate_out, acknowledged_ref="TOO-EARLY-1")

    def test_only_a_return_carries_an_acknowledgement(self, tenant, returned):
        """An installation gate pass is not something a client signs for."""
        from dispatch.models import GateOutPurpose

        gate_out, _client, _item = returned
        gate_out.purpose_type = GateOutPurpose.INSTALLATION
        gate_out.save(update_fields=["purpose_type"])

        with pytest.raises(ReturnNotAcknowledgeable):
            acknowledge_client_return(gate_out, acknowledged_ref="WRONG-KIND-1")

    def test_it_cannot_be_acknowledged_twice(self, tenant, returned):
        gate_out, _client, _item = returned
        acknowledge_client_return(gate_out, acknowledged_ref="FIRST-1")

        gate_out.refresh_from_db()
        with pytest.raises(ReturnNotAcknowledgeable):
            acknowledge_client_return(gate_out, acknowledged_ref="SECOND-1")

    def test_unacknowledged_returns_are_findable_for_chasing(self, tenant, returned):
        """L2: the beat task notifies on these, so the query is what it reads."""
        gate_out, _client, _item = returned

        # Released today, so a seven-day sweep finds nothing yet.
        assert list(unacknowledged_returns(tenant.pk, older_than_days=7)) == []

        gate_out.released_at = timezone.now() - timedelta(days=9)
        gate_out.save(update_fields=["released_at"])

        assert list(unacknowledged_returns(tenant.pk, older_than_days=7)) == [gate_out]

        acknowledge_client_return(gate_out, acknowledged_ref="LATE-BUT-SIGNED-1")
        assert list(unacknowledged_returns(tenant.pk, older_than_days=7)) == []
