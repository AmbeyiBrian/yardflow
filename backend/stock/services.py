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
from django.db.models import Sum
from django.db.models.functions import Lower
from django.utils import timezone

from accounts.models import User
from catalogue.models import ItemType, TrackingMode
from core.exceptions import DomainError
from locations.models import NodeType, StockNode
from network.models import Client
from stock.models import (
    Box,
    Condition,
    MovementType,
    OwnerType,
    Reel,
    ReelStatus,
    SerialUnit,
    SerialUnitStatus,
    StockBalance,
    StockMovement,
    UnitCostSource,
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


class UnitNotAtOrigin(DomainError):
    """§4.15.3: a serialized movement was asked to start somewhere the unit is not.

    A unit is in exactly one place (§3.5). A movement saying it leaves node X
    when the unit is at Y would debit X's balance for stock that was never
    there and leave the unit's own position unchanged in meaning.
    """

    code = "UNIT_NOT_AT_ORIGIN"
    status_code = 409
    default_message = "That unit is not where the movement says it is."


class BoxedStockOnly(DomainError):
    """§4.15.3, P9: bulk drawn without naming a box, loose stock short.

    The rest of the quantity is claimed by boxes. Taking it from one of them
    silently would empty a carton nobody opened, so the caller is told which
    boxes hold it and asked to name one.
    """

    code = "BOXED_STOCK_ONLY"
    status_code = 409
    default_message = "The rest of that stock is in boxes."


class OnDrumsOnly(DomainError):
    """D10, §7.3c: loose cable asked for beyond the loose length.

    The rest of the stock is on drums. Taking it as loose length would empty a
    drum without anyone naming it, so the drums are named and the caller asks
    for one of them.
    """

    code = "ON_DRUMS_ONLY"
    status_code = 409
    default_message = "The rest of that cable is on drums."


class BoxClaimShort(DomainError):
    """§4.15.3, P9: a named box does not hold what was asked of it (or is closed
    or at another node)."""

    code = "BOX_CLAIM_SHORT"
    status_code = 409
    default_message = "That box does not hold enough of the item."


class EarmarkDiversionNeedsReason(DomainError):
    """§4.16.3, Q3: stock earmarked for another site is leaving without a reason.

    Nothing is blocked beyond the reason: give one, or take free stock.
    """

    code = "EARMARK_DIVERSION_NEEDS_REASON"
    status_code = 409
    default_message = "That stock is earmarked for another site; give a reason to divert it."


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
    #: O11: an explicit valuation, where the caller knows one the catalogue does
    #: not — a client-owned receipt carrying the operator's declared value
    #: (T10.7). Left unset, ``post_movement`` resolves it.
    unit_cost: Decimal | None = None
    unit_cost_source: str | None = None
    #: §4.15.3: the box a bulk quantity is drawn from (P9).
    from_box: Box | None = None
    #: §4.15.3: set only by ``move_box``, meaning "this movement carries the box
    #: intact", so units and claims inside it stay put.
    moving_box: Box | None = None
    #: §4.16.3: the sites this movement delivers to. ``None`` or empty means none,
    #: so earmarked stock leaving the perimeter is a diversion.
    for_sites: frozenset | None = None
    #: §4.16.3: why earmarked stock is leaving for somewhere else; required then.
    divert_reason: str = ""


def _resolve_valuation(
    request: MovementRequest, item_type: ItemType
) -> tuple[Decimal | None, str]:
    """What this quantity is worth, decided once and captured (O11, D27).

    Three rules, in order:

    1. **A reversal inherits the original's valuation.** If it did not, a
       correction would not cancel the cost it corrects, and a project's margin
       would drift every time somebody fixed a mistake.
    2. **An explicit valuation wins**, because the caller knows something the
       catalogue does not — a client's declared value on a receipt (T10.7).
    3. Otherwise **our own material takes the catalogue price**, and anything
       else is left unvalued rather than guessed. An item type with no unit
       cost is unvalued too: a zero there would be a silent understatement.
    """
    if request.reversal_of is not None:
        original = request.reversal_of
        return original.unit_cost, original.unit_cost_source

    if request.unit_cost is not None:
        source = request.unit_cost_source or UnitCostSource.CATALOGUE
        return Decimal(str(request.unit_cost)), source

    if request.owner_type == OwnerType.OWN and item_type.unit_cost is not None:
        return item_type.unit_cost, UnitCostSource.CATALOGUE

    return None, UnitCostSource.NONE


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

    # 2c. A serialized movement must start where the unit is (§4.15.3). Read
    #     under lock, after the balances
    #     and after the stock check (a unit already issued on still reads as
    #     "not enough stock" at the node it left, which is what callers and
    #     §13's double-issue guard expect): the unit's position is what a
    #     concurrent movement of the same unit would change, so a second one
    #     waits here and then sees the new position and is refused. Lock order
    #     is always balances then unit; no other code path locks a SerialUnit
    #     row before calling this, so the order cannot invert and deadlock.
    locked_unit: SerialUnit | None = None
    if request.serial_unit is not None:
        locked_unit = _assert_unit_at_origin(request.serial_unit, request.from_node)

    # 2c'. Box rule 2 (§4.15.3): bulk out of a node draws on a box's claim or on
    #      loose stock. After the stock check, so an over-issue is still "not
    #      enough stock" and not "boxed stock". Lock order, in full: balances,
    #      then the unit, then boxes (parent before child, claims after their
    #      box); see stock/box_hooks.py. Imported here because box_hooks
    #      raises errors defined in this module.
    from stock import box_hooks

    if (
        tracking_mode == TrackingMode.BULK
        and request.from_node.type != NodeType.EXTERNAL
    ):
        box_hooks.apply_bulk_rule(
            request,
            quantity=quantity,
            held_condition=held_condition,
            balance_before=outbound.quantity,
            ctx=box_hooks.EventContext(
                actor=request.posted_by,
                occurred_at=request.occurred_at,
                document_type=request.document_type,
                document_id=str(request.document_id or ""),
                document_number=request.document_number,
            ),
        )

    # 2c'''. Earmark rules (§4.16.3), after the box rule and before anything is
    #        written, so a diversion without a reason leaves nothing behind.
    #        Lock order, in full: balances, the unit or drum, boxes (claims
    #        after their box), then earmark claims; see stock/earmark_hooks.py.
    from stock import earmark_hooks

    earmark_ctx = box_hooks.EventContext(
        actor=request.posted_by,
        occurred_at=request.occurred_at,
        document_type=request.document_type,
        document_id=str(request.document_id or ""),
        document_number=request.document_number,
    )
    if locked_unit is not None:
        earmark_hooks.apply_unit_rule(request, locked_unit, earmark_ctx)
    elif request.reel is not None:
        earmark_hooks.apply_reel_rule(request, quantity, earmark_ctx)
    elif tracking_mode == TrackingMode.BULK and request.from_node.type != NodeType.EXTERNAL:
        earmark_hooks.apply_bulk_rule(
            request,
            quantity=quantity,
            held_condition=held_condition,
            balance_before=outbound.quantity,
            ctx=earmark_ctx,
        )

    # 2c''. D10 (§7.3c): loose cable length out of a node can never draw on
    #       metres that are on a drum. Reel items only; no query for any other.
    if (
        tracking_mode == TrackingMode.BULK
        and item_type.default_tracking_mode == TrackingMode.REEL
        and request.from_node.type != NodeType.EXTERNAL
        and request.movement_type not in (MovementType.ADJUST, MovementType.REVERSAL)
    ):
        _assert_loose_length(
            request,
            quantity=quantity,
            held_condition=held_condition,
            balance_before=outbound.quantity,
            uom=uom,
        )

    # 2d. Value it, once, now (O11, D27).
    unit_cost, unit_cost_source = _resolve_valuation(request, item_type)

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
        unit_cost=unit_cost,
        unit_cost_source=unit_cost_source,
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
        # Box rule 1 (§4.15.3), on the locked row read before the move.
        assert locked_unit is not None
        left_box = box_hooks.apply_unit_rule(
            locked_unit, request.moving_box, box_hooks.EventContext.for_movement(movement)
        )
        # The caller's instance must not keep claiming a box the unit left.
        if left_box:
            request.serial_unit.box = None

    if request.reel is not None:
        _move_or_consume_reel(request.reel, quantity, request.to_node, request.condition)

    return movement


def _assert_loose_length(
    request: MovementRequest,
    *,
    quantity: Decimal,
    held_condition: str,
    balance_before: Decimal,
    uom: str,
) -> None:
    """D10: a BULK movement of a reel item may take at most the loose length."""
    drums = Reel.objects.filter(
        current_node=request.from_node,
        item_type=request.item_type,
        owner_client=request.owner_client,
        condition=held_condition,
        status=ReelStatus.OPEN,
    )
    on_drums = drums.aggregate(total=Sum("remaining_length"))["total"] or Decimal("0")
    if on_drums == 0:
        return
    loose = balance_before - on_drums
    if quantity <= loose:
        return

    held = list(
        drums.filter(remaining_length__gt=0)
        .order_by(Lower("drum_number"))
        .values_list("drum_number", "remaining_length")
    )
    names = [f"{number} ({length.normalize():f} {uom})" for number, length in held]
    listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    shown = max(loose, Decimal("0"))
    raise OnDrumsOnly(
        f"Only {shown.normalize():f} {uom} is loose here; the rest is on "
        f"{'drum' if len(names) == 1 else 'drums'} {listed}. Take it from a drum.",
        details={
            "drums": [number for number, _ in held],
            "loose": str(shown),
        },
    )


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


def _assert_unit_at_origin(unit: SerialUnit, from_node: StockNode) -> SerialUnit:
    """Lock the unit and refuse a movement that does not start at its node (§4.15.3).

    Returns the locked row, which box rule 1 reads the unit's box from.
    """
    locked = SerialUnit.objects.select_for_update().select_related("current_node").get(pk=unit.pk)
    if locked.current_node_id != from_node.pk:
        raise UnitNotAtOrigin(
            f"Serial {locked.serial_number} is at {locked.current_node.label}, "
            f"not at {from_node.label}.",
            details={
                "serial_number": locked.serial_number,
                "current_node": locked.current_node.label,
                "from_node": from_node.label,
            },
        )
    return locked


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
