"""Posting to the ledger (design §3.2, §3.3, §13; E1, E3, M4).

**One function writes stock: :func:`post_movement`.** Every document in the
system — gate-in, gate-out release, transfer, count adjustment, job closeout,
disposition, disposal, client return — goes through it. That is not tidiness for
its own sake: it is the only way the ledger and the balance cache can be
guaranteed to agree, because there is exactly one place where both are written,
in one transaction, under one lock.

Nothing else may write :class:`~stock.models.StockBalance`. T3.5's invariant
suite fails if anything does.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from accounts.models import User
from catalogue.models import ItemType, TrackingMode
from core.exceptions import DomainError
from locations.models import NodeType, StockNode
from network.models import Client
from stock.models import (
    Condition,
    MovementType,
    OwnerType,
    Reel,
    ReelStatus,
    SerialUnit,
    SerialUnitStatus,
    StockBalance,
    StockMovement,
)

#: Quantities and lengths are stored to three decimal places (§3.2), so figures
#: reported to a user are formatted the same way.
LENGTH_PRECISION = Decimal("0.001")


class InsufficientStock(DomainError):
    """§13: not enough on hand to move what was asked for."""

    code = "INSUFFICIENT_STOCK"
    status_code = 400
    default_message = "There is not enough stock at that location."


class ReelExhausted(DomainError):
    """E3: "issuing more than remains is rejected"."""

    code = "REEL_EXHAUSTED"
    status_code = 400
    default_message = "That drum does not have enough cable left."


class SerialAlreadyIssued(DomainError):
    """§13: two people released the same serialized unit at once."""

    code = "SERIAL_ALREADY_ISSUED"
    status_code = 409
    default_message = "That unit has already been issued."


class DuplicateSerial(DomainError):
    """D3: "rejected with a clear message identifying where the existing one sits"."""

    code = "DUPLICATE_SERIAL"
    status_code = 400
    default_message = "That serial number is already recorded."


class LedgerRuleViolation(DomainError):
    """A caller asked for something the ledger's shape forbids."""

    code = "INVALID_MOVEMENT"
    status_code = 400
    default_message = "That movement is not valid."


@dataclass(frozen=True)
class MovementRequest:
    """One movement to post.

    A value object rather than a long argument list, because callers build these
    in loops over document lines and a positional mistake would be silent.
    """

    item_type: ItemType
    quantity: Decimal
    from_node: StockNode
    to_node: StockNode
    movement_type: str
    owner_type: str = OwnerType.OWN
    owner_client: Client | None = None
    condition: str = Condition.NEW
    # Set only when the movement changes condition — a return assessed at the
    # gate. Blank means it comes off the same condition it lands in.
    from_condition: str = ""
    tracking_mode: str | None = None
    uom: str | None = None
    serial_unit: SerialUnit | None = None
    reel: Reel | None = None
    occurred_at: datetime | None = None
    posted_by: User | None = None
    document_type: str = ""
    document_id: str = ""
    document_line_id: str = ""
    document_number: str = ""
    reversal_of: StockMovement | None = None
    note: str = ""


def post_movement(request: MovementRequest) -> StockMovement:
    """Post one movement and update both balances, atomically (§3.2, §3.3).

    Ordering inside the transaction matters:

    1. lock both balance rows, always in a stable order
    2. check the outbound side has the stock
    3. write the movement
    4. update both balances and any serial or reel denormalisation

    The locks are taken in a deterministic order (by node id) because two
    simultaneous transfers in opposite directions between the same pair of nodes
    would otherwise deadlock — which in a yard looks like the app hanging while a
    driver waits.
    """
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError(
            "post_movement() must run inside a transaction: the movement and the "
            "balances it changes have to commit or roll back together (§3.3)."
        )

    quantity = Decimal(str(request.quantity))
    if quantity <= 0:
        raise LedgerRuleViolation(
            "A movement quantity must be positive. Direction is expressed by the "
            "nodes, never by a negative quantity (§3.2)."
        )

    if request.from_node.pk == request.to_node.pk:
        raise LedgerRuleViolation("A movement must go from one node to a different node.")

    item_type = request.item_type
    tracking_mode = request.tracking_mode or item_type.default_tracking_mode
    uom = request.uom or item_type.uom

    _validate_tracking(tracking_mode, quantity, request)

    owner_type = request.owner_type
    owner_client = request.owner_client
    if owner_type == OwnerType.CLIENT and owner_client is None:
        raise LedgerRuleViolation(
            "Client-owned stock must name its client, so it stays attributable (D3)."
        )
    if owner_type == OwnerType.OWN and owner_client is not None:
        raise LedgerRuleViolation("Own stock must not name a client (D3).")

    organization_id = request.from_node.organization_id

    # A return can be assessed at the gate, so the two sides may sit in
    # different conditions (§3.3: condition is part of a balance's identity).
    held_condition = request.from_condition or request.condition

    # 1. Lock both balance rows in a stable order, to rule out deadlock.
    outbound, inbound = _lock_balances(
        organization_id=organization_id,
        sides=(
            (request.from_node, held_condition),
            (request.to_node, request.condition),
        ),
        item_type=item_type,
        owner_client=owner_client,
        uom=uom,
    )

    # 2a. For a drum, check the drum first. Both errors would be true when
    #     over-issuing the last of a reel, and "340 m left on drum D-0007" tells
    #     the storekeeper which drum to pick instead; "insufficient stock" does
    #     not (E3, §6.1).
    if request.reel is not None and request.to_node.type in REEL_CONSUMING_NODE_TYPES:
        _assert_reel_has_length(request.reel, quantity)

    # 2b. The outbound side must actually have it. Receipts come from an EXTERNAL
    #     node, which is where material legitimately appears from — that node is
    #     allowed to go negative because it represents the outside world.
    if request.from_node.type != NodeType.EXTERNAL and outbound.quantity < quantity:
        raise InsufficientStock(
            f"Only {outbound.quantity} {uom} of {item_type} available at "
            f"{request.from_node.label}.",
            details={
                "available": str(outbound.quantity),
                "requested": str(quantity),
                "uom": uom,
                "node": request.from_node.label,
            },
        )

    # 3. Write the movement.
    movement = StockMovement.objects.create(
        organization_id=organization_id,
        occurred_at=request.occurred_at or timezone.now(),
        posted_by=request.posted_by,
        movement_type=request.movement_type,
        item_type=item_type,
        tracking_mode=tracking_mode,
        uom=uom,
        quantity=quantity,
        from_node=request.from_node,
        to_node=request.to_node,
        serial_unit=request.serial_unit,
        reel=request.reel,
        owner_type=owner_type,
        owner_client=owner_client,
        condition=request.condition,
        from_condition="" if held_condition == request.condition else held_condition,
        document_type=request.document_type,
        document_id=str(request.document_id or ""),
        document_line_id=str(request.document_line_id or ""),
        document_number=request.document_number,
        reversal_of=request.reversal_of,
        note=request.note,
    )

    # 4. Update both cached balances.
    outbound.quantity = outbound.quantity - quantity
    outbound.uom = uom
    outbound.save(update_fields=["quantity", "uom", "updated_at"])

    inbound.quantity = inbound.quantity + quantity
    inbound.uom = uom
    inbound.save(update_fields=["quantity", "uom", "updated_at"])

    if request.serial_unit is not None:
        _move_serial_unit(request.serial_unit, request.to_node, request.condition, owner_client)

    if request.reel is not None:
        _move_or_consume_reel(request.reel, quantity, request.to_node, request.condition)

    return movement


def _validate_tracking(tracking_mode: str, quantity: Decimal, request: MovementRequest) -> None:
    """Enforce the shape each tracking mode requires (§3.5)."""
    if tracking_mode == TrackingMode.SERIALIZED:
        if request.serial_unit is None:
            raise LedgerRuleViolation(
                "A serialized movement must name the unit that moved (§3.5)."
            )
        if quantity != 1:
            raise LedgerRuleViolation(
                "A serialized movement is exactly one unit. Post one movement per "
                "unit (§3.5)."
            )
    elif tracking_mode == TrackingMode.REEL:
        if request.reel is None:
            raise LedgerRuleViolation("A reel movement must name its drum (D4, §3.5).")
    elif tracking_mode == TrackingMode.BULK:
        if request.serial_unit is not None or request.reel is not None:
            raise LedgerRuleViolation(
                "A bulk movement carries neither a serial nor a drum (§3.5)."
            )


def _lock_balances(
    *,
    organization_id,
    sides: tuple[tuple[StockNode, str], tuple[StockNode, str]],
    item_type,
    owner_client,
    uom: str,
) -> tuple[StockBalance, StockBalance]:
    """Get or create both balance rows, locked, in a deadlock-free order.

    Two simultaneous transfers in opposite directions between the same pair of
    nodes would deadlock if each locked its own side first. Ordering the locks by
    (node, condition) — the balance's own identity — makes that impossible, and
    holds even when the two sides sit in different conditions.
    """
    keys = sorted({(node.pk, condition) for node, condition in sides})

    locked: dict[tuple, StockBalance] = {}
    for node_id, condition in keys:
        balance, _created = StockBalance.objects.get_or_create(
            organization_id=organization_id,
            node_id=node_id,
            item_type=item_type,
            owner_client=owner_client,
            condition=condition,
            defaults={"quantity": Decimal("0"), "uom": uom},
        )
        # Re-read under lock: get_or_create does not lock, and another
        # transaction may have changed the row between create and here.
        locked[(node_id, condition)] = StockBalance.objects.select_for_update().get(
            pk=balance.pk
        )

    return tuple(locked[(node.pk, condition)] for node, condition in sides)  # type: ignore[return-value]


def _move_serial_unit(unit: SerialUnit, to_node: StockNode, condition: str, owner_client) -> None:
    """Update the denormalised position of a serialized unit (§3.5)."""
    unit.current_node = to_node
    unit.condition = condition
    if owner_client is not None:
        unit.owner_client = owner_client
    unit.status = _status_for_node(to_node, condition)
    unit.save(
        update_fields=["current_node", "condition", "owner_client", "status", "updated_at"]
    )


def _status_for_node(node: StockNode, condition: str) -> str:
    """Derive a unit's status from where it now is.

    Derived rather than passed in, so the status can never contradict the node —
    a unit at a SITE node is installed, whatever a caller might claim.
    """
    from locations.models import LocationType

    if node.type == NodeType.SITE:
        return SerialUnitStatus.INSTALLED
    if node.type == NodeType.PERSON:
        return SerialUnitStatus.IN_CUSTODY
    if node.type == NodeType.CLIENT:
        return SerialUnitStatus.RETURNED_TO_CLIENT
    if node.type == NodeType.SCRAP:
        return SerialUnitStatus.SCRAPPED
    if node.type == NodeType.LOCATION and node.location is not None:
        if node.location.type == LocationType.QUARANTINE:
            return SerialUnitStatus.QUARANTINED
    return SerialUnitStatus.IN_STOCK


#: Destinations where cable genuinely leaves the drum (E3).
#:
#: A drum is a container that sits somewhere. Moving it to a store, a vehicle or
#: a technician moves the container — the cable is still on it, and the remaining
#: length is unchanged. Cable only leaves the drum when it is installed at a site
#: or used up. Decrementing on every movement would empty a drum simply by
#: driving it to the site and back.
REEL_CONSUMING_NODE_TYPES = (NodeType.SITE, NodeType.CONSUMED)


def _move_or_consume_reel(
    reel: Reel, length: Decimal, to_node: StockNode, condition: str
) -> None:
    """Decide whether cable was cut off a drum, or the drum itself moved (E3, Q5).

    Two different operations wear the same shape in the ledger, and the quantity
    is what tells them apart:

    * **less than what is on the drum** — a cut. 120 m off a 500 m drum leaves
      the drum where it is, holding 380 m, and 120 m of loose cable travels. This
      is the ordinary issue from a yard, and the ordinary consumption on site
      (Q5: "partial consumption of a drum on site is normal").

    * **all of what is on the drum** — the drum goes. Nobody cuts 500 m off a
      500 m drum and leaves an empty drum behind; they hand over the drum. So it
      moves, still holding its length, and the ledger's 500 m at the destination
      is the same 500 m the drum reports.

    The exception is a destination that *uses material up* — a site or the
    consumed node. Cable that reaches either is gone, so the drum empties and
    closes (E3: "a drum reaching zero is closed automatically").

    Getting this wrong is not cosmetic. Moving a drum on a partial issue leaves
    the yard's balance short while the drum claims to be full; decrementing it on
    a full handover leaves an empty drum record travelling with 500 m of cable.
    Either way the ledger and the drum disagree, and E3's "how much is left on
    drum D-0007" stops being answerable.
    """
    consuming = to_node.type in REEL_CONSUMING_NODE_TYPES

    if length < reel.remaining_length:
        _consume_reel(reel, length, to_node, condition, moves=False)
        return

    if consuming:
        _consume_reel(reel, length, to_node, condition, moves=True)
        return

    # The whole drum changed hands. Its remaining length is unaffected.
    reel.current_node = to_node
    reel.condition = condition
    reel.save(update_fields=["current_node", "condition", "updated_at"])


def _assert_reel_has_length(reel: Reel, length: Decimal) -> None:
    """E3: "issuing more than remains is rejected".

    Called under the balance lock taken by :func:`post_movement`, so two
    simultaneous issues from one drum cannot both pass it.
    """
    if length > reel.remaining_length:
        # Quantized so the figure reads the same whether the drum was just
        # loaded from the database or decremented in memory a moment ago.
        remaining = reel.remaining_length.quantize(LENGTH_PRECISION)
        raise ReelExhausted(
            f"Only {remaining} {reel.uom} remaining on drum {reel.drum_number}.",
            details={
                "reel": reel.drum_number,
                "remaining": str(remaining),
                "requested": str(length.quantize(LENGTH_PRECISION)),
                "uom": reel.uom,
            },
        )


def _consume_reel(
    reel: Reel, length: Decimal, to_node: StockNode, condition: str, *, moves: bool
) -> None:
    """Decrement a drum's remaining length, closing it at zero (E3).

    ``moves`` says whether the drum itself travelled. It does not on a cut: 120 m
    off a 500 m drum leaves the drum where it was, holding 380 m. Sending it
    along with the cable would put the rest of the reel out of reach of the next
    job, and nobody would be accountable for 380 m of feeder.
    """
    _assert_reel_has_length(reel, length)

    reel.remaining_length = reel.remaining_length - length
    reel.condition = condition
    if moves:
        reel.current_node = to_node
    # E3: "a drum reaching zero is closed automatically".
    if reel.remaining_length == 0:
        reel.status = ReelStatus.CLOSED
        reel.current_node = to_node
    reel.save(
        update_fields=["remaining_length", "current_node", "condition", "status", "updated_at"]
    )


def reverse_movement(
    movement: StockMovement, *, posted_by=None, note: str = ""
) -> StockMovement:
    """Post the inverse of a movement (M4, §3.2).

    The correction mechanism. The original stays exactly as posted and a new
    movement moves the material back, pointing at what it reverses — so the
    history shows both what was recorded and that it was corrected, which is what
    an auditor needs to see.
    """
    if movement.reversals.exists():
        raise LedgerRuleViolation(
            "That movement has already been reversed.",
            details={"movement_id": movement.pk},
        )

    return post_movement(
        MovementRequest(
            item_type=movement.item_type,
            quantity=movement.quantity,
            # Swapped: the material goes back where it came from.
            from_node=movement.to_node,
            to_node=movement.from_node,
            movement_type=MovementType.REVERSAL,
            owner_type=movement.owner_type,
            owner_client=movement.owner_client,
            condition=movement.condition,
            tracking_mode=movement.tracking_mode,
            uom=movement.uom,
            serial_unit=movement.serial_unit,
            # A reel reversal returns length to the drum, which _consume_reel
            # cannot express, so it is handled by the caller in T3.10.
            reel=None,
            posted_by=posted_by,
            document_type=movement.document_type,
            document_id=movement.document_id,
            document_line_id=movement.document_line_id,
            document_number=movement.document_number,
            reversal_of=movement,
            note=note or f"Reversal of movement {movement.pk}",
        )
    )


def balance_at(node: StockNode, item_type, *, owner_client=None, condition=Condition.NEW):
    """The cached quantity at one node, or zero."""
    balance = StockBalance.objects.filter(
        node=node, item_type=item_type, owner_client=owner_client, condition=condition
    ).first()
    return balance.quantity if balance else Decimal("0")


def recompute_balance(organization_id, node, item_type, owner_client, condition) -> Decimal:
    """Recompute one balance from the ledger (§3.3, T3.6).

    The ledger is the source of truth; this is what proves the cache still
    agrees with it.
    """
    from django.db.models import Q, Sum

    inbound = StockMovement.objects.filter(
        organization_id=organization_id,
        to_node=node,
        item_type=item_type,
        owner_client=owner_client,
        condition=condition,
    ).aggregate(total=Sum("quantity"))["total"] or Decimal("0")

    # The outbound side matches the condition the material was *held* in, which
    # differs from ``condition`` on a movement that reclassified it. Filtering
    # both sides on one column would leave the old condition's balance
    # permanently overstated and the new one understated — and this function is
    # what proves the cache agrees with the ledger, so it has to get this right.
    outbound = StockMovement.objects.filter(
        Q(from_condition=condition) | Q(from_condition="", condition=condition),
        organization_id=organization_id,
        from_node=node,
        item_type=item_type,
        owner_client=owner_client,
    ).aggregate(total=Sum("quantity"))["total"] or Decimal("0")

    return inbound - outbound
