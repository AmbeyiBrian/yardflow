"""The box rules inside ``post_movement`` (design §4.15.3; P5, P7, P9, P10).

Private to the ``stock`` app. ``post_movement`` is the one path every stock
movement takes, so the three rules live there and nowhere else: custody,
closeouts, disposition, counts and gate-out all obey the box rules without
knowing boxes exist.

1. a unit that moves leaves its box (:func:`apply_unit_rule`)
2. bulk drawn from a node comes out of a box's claim, or out of loose stock
   (:func:`apply_bulk_rule`)
3. a box left with nothing in it closes, and so does every parent that this
   empties (:func:`close_if_empty`)

:func:`record_event` and :func:`close_if_empty` are the pieces ``stock/boxes.py``
(T11.5) reuses, so a box service and a ledger hook write the same trail.

**Lock order**, extending ``post_movement``'s own: balances (by node, condition),
then the serial unit, then boxes. Where several box rows are locked, a parent is
locked before its child (:func:`lock_chain`); where a correction draws on several
boxes at one node, they are locked in ``Lower(code)`` order, which is also the
order the claims are reduced in. A box row is always locked before its claim
rows.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from django.db.models import Sum
from django.db.models.functions import Lower
from django.utils import timezone

from stock.models import (
    Box,
    BoxAction,
    BoxBulkContent,
    BoxEvent,
    BoxStatus,
    MovementType,
    SerialUnit,
    StockMovement,
)

if TYPE_CHECKING:
    from stock.services import MovementRequest

#: Corrections put the ledger right rather than ask for something, so when a
#: count says boxed stock is gone, the boxes are reduced to match (P9).
CORRECTION_TYPES = (MovementType.ADJUST, MovementType.REVERSAL)


@dataclass(frozen=True)
class EventContext:
    """Who did it, when, and under which document: the part of a BoxEvent that
    is the same for every event one movement causes (P8)."""

    actor: object | None = None
    occurred_at: datetime | None = None
    document_type: str = ""
    document_id: str = ""
    document_number: str = ""
    note: str = ""

    @classmethod
    def for_movement(cls, movement: StockMovement, *, note: str = "") -> EventContext:
        return cls(
            actor=movement.posted_by,
            occurred_at=movement.occurred_at,
            document_type=movement.document_type,
            document_id=movement.document_id,
            document_number=movement.document_number,
            note=note,
        )


def record_event(box: Box, action: str, ctx: EventContext, **fields) -> BoxEvent:
    """Append one BoxEvent for ``box`` (P8).

    ``fields`` are the event's own columns: ``serial_unit``, ``child_box``,
    ``item_type``, ``owner_client``, ``condition``, ``quantity``. The context
    supplies actor, time, document references and the note.
    """
    return BoxEvent.objects.create(
        organization_id=box.organization_id,
        box=box,
        action=action,
        actor=ctx.actor,
        occurred_at=ctx.occurred_at or timezone.now(),
        document_type=ctx.document_type,
        document_id=ctx.document_id,
        document_number=ctx.document_number,
        note=ctx.note,
        **fields,
    )


def lock_chain(box: Box) -> dict[int, Box]:
    """Lock ``box`` and every ancestor, parent before child; return them by id.

    The rows are fresh reads under lock, so a caller never decides on a stale
    status. The ancestor ids come from an unlocked walk, which is safe: a box
    only changes parent through a box service holding these same locks, and the
    chain is re-read here.
    """
    ids = [box.pk]
    parent_id = box.parent_id
    while parent_id is not None:
        ids.append(parent_id)
        parent_id = Box.objects.filter(pk=parent_id).values_list("parent_id", flat=True).first()
    locked = Box.objects.select_for_update().filter(pk__in=ids).order_by("depth", "pk")
    return {b.pk: b for b in locked}


def covers(moving_box: Box | None, box: Box) -> bool:
    """Is ``box`` the box being moved, or inside it at any depth? (§4.15.3 rule 1)"""
    if moving_box is None:
        return False
    current: Box | None = box
    while current is not None:
        if current.pk == moving_box.pk:
            return True
        parent_id = current.parent_id
        current = Box.objects.filter(pk=parent_id).first() if parent_id else None
    return False


def close_if_empty(box: Box, ctx: EventContext) -> list[Box]:
    """Close ``box`` if nothing is in it, then do the same for its parent, and so on.

    P5: a box left with nothing in it closes, and its code is never reused. P10:
    a pallet emptied by one release closes at every level. "Nothing" is no
    units, no bulk claims and no *open* child boxes (a closed child is history,
    not contents). Returns the boxes closed, innermost first.
    """
    chain = lock_chain(box)
    closed: list[Box] = []
    current = chain[box.pk]
    while current.status == BoxStatus.OPEN:
        if (
            SerialUnit.objects.filter(box=current).exists()
            or BoxBulkContent.objects.filter(box=current).exists()
            or Box.objects.filter(parent=current, status=BoxStatus.OPEN).exists()
        ):
            break
        current.status = BoxStatus.CLOSED
        current.closed_at = ctx.occurred_at or timezone.now()
        current.save(update_fields=["status", "closed_at", "updated_at"])
        record_event(current, BoxAction.CLOSED, dataclasses.replace(ctx, note=""))
        closed.append(current)
        if current.parent_id is None:
            break
        current = chain[current.parent_id]
    return closed


def apply_unit_rule(unit: SerialUnit, moving_box: Box | None, ctx: EventContext) -> bool:
    """Rule 1: a unit that moves leaves its box, unless the box moves with it.

    ``unit`` is the locked, fresh row. When ``moving_box`` is the unit's box or
    one of its ancestors, the whole box travels and the unit stays in it.
    Otherwise the unit is clearing out of the box (P5), and the box may now be
    empty (rule 3). Returns whether the unit left a box.
    """
    if unit.box_id is None:
        return False
    box = Box.objects.get(pk=unit.box_id)
    if covers(moving_box, box):
        return False
    SerialUnit.objects.filter(pk=unit.pk).update(box=None, updated_at=timezone.now())
    record_event(box, BoxAction.UNIT_OUT, ctx, serial_unit=unit)
    close_if_empty(box, ctx)
    return True


def apply_bulk_rule(
    request: MovementRequest,
    *,
    quantity: Decimal,
    held_condition: str,
    balance_before: Decimal,
    ctx: EventContext,
) -> None:
    """Rule 2: bulk out of a node draws on a box's claim or on loose stock (P9).

    ``balance_before`` is the locked balance at ``from_node`` for the lot,
    *before* this movement debits it. ``held_condition`` is the condition the
    from side is debited in, which is the one the claims are held in.

    * ``moving_box`` set: the claim travels with the box, so nothing changes.
    * ``from_box`` set: that box's claim must cover the quantity.
    * otherwise: loose stock (balance less open boxes' claims) must cover it. If
      not, a correction takes the rest from the boxes in code order; anything
      else is refused with the boxes named.

    The common case, a node with no open claims for the lot, costs one
    aggregate query.
    """
    from stock.services import BoxClaimShort, BoxedStockOnly

    if request.moving_box is not None:
        return

    lot = {
        "item_type": request.item_type,
        "owner_client": request.owner_client,
        "condition": held_condition,
    }

    if request.from_box is not None:
        box = lock_chain(request.from_box)[request.from_box.pk]
        claim = BoxBulkContent.objects.select_for_update().filter(box=box, **lot).first()
        held = claim.quantity if claim else Decimal("0")
        if (
            box.status != BoxStatus.OPEN
            or box.current_node_id != request.from_node.pk
            or claim is None
            or held < quantity
        ):
            raise BoxClaimShort(
                f"Box {box.code} ({box.status.lower()}, at {box.current_node.label}) holds "
                f"{held} of {request.item_type}; {quantity} was asked for from "
                f"{request.from_node.label}.",
                details={
                    "box": box.code,
                    "held": str(held),
                    "requested": str(quantity),
                },
            )
        _reduce_claim(box, claim, quantity, ctx, request.item_type, request.owner_client)
        close_if_empty(box, ctx)
        return

    claimed = BoxBulkContent.objects.filter(
        box__current_node=request.from_node, box__status=BoxStatus.OPEN, **lot
    ).aggregate(total=Sum("quantity"))["total"] or Decimal("0")
    if claimed == 0:
        return

    loose = balance_before - claimed
    if quantity <= loose:
        return

    if request.movement_type not in CORRECTION_TYPES:
        holders = list(
            BoxBulkContent.objects.filter(
                box__current_node=request.from_node, box__status=BoxStatus.OPEN, **lot
            )
            .order_by(Lower("box__code"))
            .values_list("box__code", flat=True)
        )
        raise BoxedStockOnly(
            f"Only {max(loose, Decimal('0'))} of {request.item_type} is loose at "
            f"{request.from_node.label}; the rest is in box {', '.join(holders)}. "
            f"Scan a box to take it from there.",
            details={"boxes": holders, "loose": str(loose)},
        )

    # A correction: the count says the stock is gone, so the boxes that claim it
    # are reduced to match, in code order.
    shortfall = quantity - max(loose, Decimal("0"))
    boxes = list(
        Box.objects.select_for_update(of=("self",))
        .filter(
            current_node=request.from_node,
            status=BoxStatus.OPEN,
            bulk_contents__item_type=request.item_type,
            bulk_contents__owner_client=request.owner_client,
            bulk_contents__condition=held_condition,
        )
        .order_by(Lower("code"))
    )
    reduced = dataclasses.replace(ctx, note="Reduced by a correction")
    for box in boxes:
        if shortfall <= 0:
            break
        claim = BoxBulkContent.objects.select_for_update().get(box=box, **lot)
        take = min(claim.quantity, shortfall)
        _reduce_claim(box, claim, take, reduced, request.item_type, request.owner_client)
        shortfall -= take
        close_if_empty(box, ctx)


def _reduce_claim(
    box: Box,
    claim: BoxBulkContent,
    amount: Decimal,
    ctx: EventContext,
    item_type,
    owner_client,
) -> None:
    """Lower a claim, deleting it at zero, and write BULK_OUT (P9)."""
    remaining = claim.quantity - amount
    condition = claim.condition
    if remaining <= 0:
        claim.delete()
    else:
        claim.quantity = remaining
        claim.save(update_fields=["quantity", "updated_at"])
    record_event(
        box,
        BoxAction.BULK_OUT,
        ctx,
        item_type=item_type,
        owner_client=owner_client,
        condition=condition,
        quantity=amount,
    )
