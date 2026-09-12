"""Stock counts and internal transfers (design §4.5, §4.13; E4, E5).

Two operations that both write to the ledger through ``post_movement``, and both
have a rule worth stating plainly:

* **A transfer out of the yard perimeter is a gate-out, not a transfer** (E4).
  Letting material leave through the transfer screen would route around the
  approval control the whole system exists to provide.
* **An adjustment to client-owned stock always needs approval** (E5), whatever
  the category criticality says. Writing off an operator's property is not
  something a storekeeper does alone.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from core.audit import record
from core.exceptions import AlreadyPosted, DomainError
from core.models import AuditAction
from core.numbering import DocumentType, allocate_number
from locations.models import Location, LocationType
from locations.nodes import node_for_location
from stock.models import (
    Condition,
    MovementType,
    OwnerType,
    StockCount,
    StockCountLine,
    StockCountStatus,
)
from stock.services import MovementRequest, post_movement


class TransferNotAllowed(DomainError):
    """E4: material leaving the yard must go through a gate-out."""

    code = "TRANSFER_REQUIRES_GATE_OUT"
    status_code = 400
    default_message = (
        "Material leaving the yard must be raised as a gate-out so it can be "
        "approved before it goes."
    )


class CountNotReady(DomainError):
    code = "COUNT_NOT_READY"
    status_code = 400
    default_message = "This stock count cannot be posted yet."


class ApprovalRequired(DomainError):
    """E5, J3: escalations that no configuration can switch off."""

    code = "APPROVAL_REQUIRED"
    status_code = 400
    default_message = "This needs approval before it can be posted."


# --------------------------------------------------------------------------
# T3.15 — internal transfers
# --------------------------------------------------------------------------

#: Location types that are inside the yard perimeter (E4).
#:
#: A vehicle is deliberately *not* one of them: material on a vehicle has left
#: the yard, which is the moment a gate pass is required. Treating a vehicle as
#: internal would turn "load the pickup" into an unapproved way out of the yard.
INSIDE_PERIMETER = (LocationType.YARD, LocationType.STORE, LocationType.QUARANTINE)


def is_inside_perimeter(location: Location) -> bool:
    return location.type in INSIDE_PERIMETER


@transaction.atomic
def transfer_stock(
    *,
    organization,
    item_type,
    quantity: Decimal,
    from_location: Location,
    to_location: Location,
    owner_type: str = OwnerType.OWN,
    owner_client=None,
    condition: str = Condition.NEW,
    serial_unit=None,
    reel=None,
    tracking_mode: str | None = None,
    uom: str | None = None,
    performed_by=None,
    note: str = "",
    request=None,
):
    """Move stock between locations inside the yard (E4).

    Refuses any destination outside the perimeter, naming the alternative — a
    message that says "not allowed" without saying what to do instead just gets
    worked around.
    """
    if not is_inside_perimeter(to_location):
        raise TransferNotAllowed(
            f"{to_location.name} is outside the yard, so material cannot be "
            f"transferred there. Raise a gate-out instead, so it can be approved "
            f"and released at the gate (E4).",
            details={"to_location": to_location.name, "type": to_location.type},
        )
    if not is_inside_perimeter(from_location):
        raise TransferNotAllowed(
            f"{from_location.name} is outside the yard. Material coming back in "
            f"is recorded as a gate-in (D1).",
            details={"from_location": from_location.name, "type": from_location.type},
        )

    movement = post_movement(
        MovementRequest(
            item_type=item_type,
            quantity=Decimal(str(quantity)),
            from_node=node_for_location(from_location),
            to_node=node_for_location(to_location),
            movement_type=MovementType.TRANSFER,
            owner_type=owner_type,
            owner_client=owner_client,
            condition=condition,
            tracking_mode=tracking_mode,
            uom=uom,
            serial_unit=serial_unit,
            reel=reel,
            posted_by=performed_by,
            document_type="stock.Transfer",
            note=note,
        )
    )

    record(
        AuditAction.STATUS_CHANGED,
        actor=performed_by,
        organization=organization,
        target=movement.item_type,
        target_label=str(item_type),
        request=request,
        note=(
            f"Transferred {quantity} {movement.uom} from {from_location.name} "
            f"to {to_location.name}. {note}".strip()
        ),
    )

    return movement


# --------------------------------------------------------------------------
# T3.16 — stock counts
# --------------------------------------------------------------------------


def expected_quantity_for(
    location: Location, item_type, *, owner_client=None, condition: str = Condition.NEW
) -> Decimal:
    """What the system believes is there, for a count line to be measured against."""
    from stock.services import balance_at

    return balance_at(
        node_for_location(location),
        item_type,
        owner_client=owner_client,
        condition=condition,
    )


def add_count_line(
    stock_count: StockCount,
    item_type,
    counted_quantity: Decimal,
    *,
    owner_client=None,
    condition: str = Condition.NEW,
    reason: str = "",
) -> StockCountLine:
    """Add a line, capturing what the system expected at that moment (E5)."""
    expected = expected_quantity_for(
        stock_count.location, item_type, owner_client=owner_client, condition=condition
    )

    return StockCountLine.objects.create(
        organization_id=stock_count.organization_id,
        stock_count=stock_count,
        item_type=item_type,
        owner_client=owner_client,
        condition=condition,
        expected_quantity=expected,
        counted_quantity=Decimal(str(counted_quantity)),
        uom=item_type.uom,
        reason=reason,
    )


def validate_count_for_posting(stock_count: StockCount) -> None:
    """Everything that must hold before a count adjusts the ledger (E5)."""
    if stock_count.status == StockCountStatus.POSTED:
        raise AlreadyPosted()
    if stock_count.status == StockCountStatus.CANCELLED:
        raise CountNotReady("This count was cancelled.")

    lines = list(stock_count.lines.select_related("item_type", "owner_client").all())
    if not lines:
        raise CountNotReady("Add at least one counted line before posting.")

    field_errors: dict[str, list[str]] = {}
    for index, line in enumerate(lines):
        # E5: "adjustment movements with a mandatory reason". Only variances need
        # one — demanding a reason for a line that matched would train people to
        # type "ok" and stop reading the prompt.
        if line.has_variance and not line.reason:
            field_errors[f"lines.{index}.reason"] = [
                f"{line.item_type} counted {line.counted_quantity} but "
                f"{line.expected_quantity} was expected. Record why."
            ]

    if field_errors:
        raise CountNotReady("This count cannot be posted yet.", field_errors=field_errors)

    # E5: client-owned adjustments always require approval. Hardcoded here and
    # in the approval engine's guard rails (§5.2), deliberately not configurable —
    # writing off an operator's property is not a storekeeper's decision.
    if stock_count.touches_client_owned_stock and stock_count.status != (
        StockCountStatus.PENDING_APPROVAL
    ):
        raise ApprovalRequired(
            "This count adjusts client-owned stock, which always requires "
            "approval before it can be posted (E5).",
            details={"requires_approval": True},
        )


@transaction.atomic
def post_stock_count(
    stock_count: StockCount, *, posted_by=None, request=None, approved: bool = False
) -> StockCount:
    """Post a count, writing an ADJUST movement per variance (E5).

    Adjustments move material between the counted location and the tenant's
    adjustment node, so a count is expressed in the same double-entry shape as
    everything else and the invariant in §3.2 keeps holding.
    """
    if approved:
        # An approver has signed off, so the client-owned gate is satisfied.
        stock_count.status = StockCountStatus.PENDING_APPROVAL

    validate_count_for_posting(stock_count)

    stock_count.number = allocate_number(
        DocumentType.STOCK_COUNT, organization_id=stock_count.organization_id
    )

    counted_node = node_for_location(stock_count.location)
    adjustment_node = _adjustment_node(stock_count.organization_id)

    for line in stock_count.lines.select_related("item_type", "owner_client").all():
        if not line.has_variance:
            continue

        variance = line.variance
        if variance > 0:
            # More found than expected: it comes *from* the adjustment node.
            source, destination = adjustment_node, counted_node
        else:
            source, destination = counted_node, adjustment_node

        post_movement(
            MovementRequest(
                item_type=line.item_type,
                quantity=abs(variance),
                from_node=source,
                to_node=destination,
                movement_type=MovementType.ADJUST,
                owner_type=OwnerType.CLIENT if line.owner_client_id else OwnerType.OWN,
                owner_client=line.owner_client,
                condition=line.condition,
                uom=line.uom,
                occurred_at=stock_count.counted_at,
                posted_by=posted_by,
                document_type="stock.StockCount",
                document_id=str(stock_count.pk),
                document_line_id=str(line.pk),
                document_number=stock_count.number,
                note=line.reason,
            )
        )

    stock_count.status = StockCountStatus.POSTED
    stock_count.posted_at = timezone.now()
    stock_count.posted_by = posted_by
    stock_count.save(
        update_fields=["number", "status", "posted_at", "posted_by", "updated_at"]
    )

    record(
        AuditAction.DOCUMENT_POSTED,
        actor=posted_by,
        organization=stock_count.organization_id,
        target=stock_count,
        target_label=stock_count.number,
        request=request,
        after={"status": stock_count.status, "number": stock_count.number},
        note=f"Stock count {stock_count.number} posted at {stock_count.location.name}.",
    )

    return stock_count


def _adjustment_node(organization_id):
    """The counterparty for a count adjustment.

    Adjustments are not receipts or disposals, so they use the generic external
    node: material found is recognised as arriving from outside our records, and
    material missing as having left them. Both are visible in the ledger as
    ADJUST movements with a reason, which is what E5 asks for.
    """
    from locations.nodes import external_node

    return external_node(organization_id)
