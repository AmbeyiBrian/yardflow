"""Box services (design §4.15.4; stories P1, P4, P7, P9, P10, E4).

The only writers of ``Box``, ``BoxBulkContent`` and ``SerialUnit.box`` besides
the ledger hooks in :mod:`stock.box_hooks`. A box groups stock; it is not stock
(§4.15.1), so nothing here posts a movement except :func:`move_box`, which
carries a box to another place inside the yard.

**Lock order**, shared with ``post_movement`` (see ``box_hooks``): balances,
then serial units, then boxes (a parent before its child, a box before its
claims). Functions that lock several rows say how.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from catalogue.models import TrackingMode
from core.audit import record
from core.exceptions import DomainError
from core.models import AuditAction
from core.numbering import DocumentType, allocate_number
from locations.models import NodeType, StockNode
from locations.nodes import node_for_location
from stock import box_hooks
from stock.box_hooks import EventContext, close_if_empty, lock_chain, record_event
from stock.counting import INSIDE_PERIMETER, TransferNotAllowed
from stock.models import (
    Box,
    BoxAction,
    BoxBulkContent,
    BoxEvent,
    BoxSource,
    BoxStatus,
    MovementType,
    OwnerType,
    SerialUnit,
    SerialUnitStatus,
    StockBalance,
)
from stock.services import (
    BoxClaimShort,
    LedgerRuleViolation,
    MovementRequest,
    _lock_balances,
    post_movement,
)

MAX_DEPTH = 3


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class BoxCodeInUse(DomainError):
    """P1, P5: a code is used in the tenant, open or closed, in any case."""

    code = "BOX_CODE_IN_USE"
    status_code = 409
    default_message = "That box code is already in use."


class BoxTooDeep(DomainError):
    """P10: boxes nest three deep at most."""

    code = "BOX_TOO_DEEP"
    status_code = 409
    default_message = "Boxes can be nested three deep at most."


class BoxCycle(DomainError):
    """§4.15.4: a box cannot go inside itself or something inside it."""

    code = "BOX_CYCLE"
    status_code = 409
    default_message = "A box cannot be put inside itself."


class NotEnoughLooseStock(DomainError):
    """P9: a claim must fit in stock that no other open box has claimed."""

    code = "NOT_ENOUGH_LOOSE_STOCK"
    status_code = 409
    default_message = "There is not enough loose stock to put in the box."


class BoxClosed(DomainError):
    """P5: a closed box takes no contents and gives none."""

    code = "BOX_CLOSED"
    status_code = 409
    default_message = "That box is closed."


class BoxElsewhere(DomainError):
    """§4.15.4: contents and children must be at the box's own node."""

    code = "BOX_ELSEWHERE"
    status_code = 409
    default_message = "That is not at the same place as the box."


class BoxOutsidePerimeter(DomainError):
    """§4.15.1: boxes live inside the yard perimeter."""

    code = "BOX_OUTSIDE_PERIMETER"
    status_code = 409
    default_message = "Boxes can only be kept inside the yard."


class AlreadyInBox(DomainError):
    """P1: a unit or box sits in one box at a time."""

    code = "ALREADY_IN_BOX"
    status_code = 409
    default_message = "That is already in a box."


class NotInThisBox(DomainError):
    """P7: only what is in the box can be taken out of it."""

    code = "NOT_IN_THIS_BOX"
    status_code = 409
    default_message = "That is not in this box."


class BoxInsideAnotherBox(DomainError):
    """P7: a box inside another is moved with its parent, or taken out first."""

    code = "BOX_INSIDE_ANOTHER_BOX"
    status_code = 409
    default_message = "That box is inside another box."


class BoxChanged(DomainError):
    """The box's contents changed while a move was being prepared."""

    code = "BOX_CHANGED"
    status_code = 409
    default_message = "The box changed while it was being moved. Try again."


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _ctx(ctx: EventContext | None, actor=None) -> EventContext:
    return ctx if ctx is not None else EventContext(actor=actor)


def _inside_perimeter(node: StockNode) -> bool:
    return (
        node.type == NodeType.LOCATION
        and node.location is not None
        and node.location.type in INSIDE_PERIMETER
    )


def _lock_open(box: Box) -> Box:
    """Lock ``box`` (and its ancestors, parent first) and require it open."""
    locked = lock_chain(box)[box.pk]
    if locked.status != BoxStatus.OPEN:
        raise BoxClosed(f"Box {locked.code} is closed.", details={"box": locked.code})
    return locked


def _subtree(root: Box, *, open_only: bool = False, lock: bool = False) -> list[Box]:
    """``root`` and every descendant, top-down. At most three queries (P10)."""
    result = [root]
    level = [root.pk]
    while level:
        qs = Box.objects.filter(parent_id__in=level)
        if open_only:
            qs = qs.filter(status=BoxStatus.OPEN)
        if lock:
            qs = qs.select_for_update(of=("self",))
        children = list(qs.select_related("current_node").order_by("depth", "pk"))
        result.extend(children)
        level = [c.pk for c in children]
    return result


def _set_depths(root: Box, new_root_depth: int, subtree: list[Box]) -> None:
    shift = new_root_depth - root.depth
    now = timezone.now()
    for b in subtree:
        Box.objects.filter(pk=b.pk).update(depth=b.depth + shift, updated_at=now)
        b.depth += shift


def _audit(box: Box, ctx: EventContext, note: str, request=None) -> None:
    record(
        AuditAction.STATUS_CHANGED,
        actor=ctx.actor,
        organization=box.organization_id,
        target=box,
        target_label=box.code,
        request=request,
        note=note,
    )


def _box_path(box: Box, by_id: dict[int, Box]) -> list[str]:
    """Codes outermost to innermost, walking ancestors outside ``by_id`` too."""
    codes: list[str] = []
    current: Box = box
    while True:
        codes.append(current.code)
        if current.parent_id is None:
            break
        parent = by_id.get(current.parent_id)
        if parent is None:
            parent = Box.objects.get(pk=current.parent_id)
            by_id[parent.pk] = parent
        current = parent
    return list(reversed(codes))


def _refuse_used_code(code: str, organization_id) -> None:
    existing = (
        Box.objects.filter(organization_id=organization_id, code__iexact=code)
        .select_related("current_node")
        .first()
    )
    if existing is not None:
        state = "closed" if existing.status == BoxStatus.CLOSED else "open"
        tail = " A closed box's code is never reused." if state == "closed" else ""
        raise BoxCodeInUse(
            f"Box {existing.code} already exists: it is {state}, at "
            f"{existing.current_node.label}.{tail}",
            details={
                "box": existing.code,
                "status": existing.status,
                "node": existing.current_node.label,
            },
        )


# --------------------------------------------------------------------------
# create_box
# --------------------------------------------------------------------------


def create_box(
    *,
    code: str,
    node: StockNode,
    parent: Box | None = None,
    gate_in=None,
    label_text: str = "",
    actor=None,
    ctx: EventContext | None = None,
) -> Box:
    """Create an open box at ``node`` (P1, P10; §4.15.4).

    A blank ``code`` is generated from the BOX series (``source=INTERNAL``),
    otherwise the printed code is kept (``source=LABEL``). A code used anywhere
    in the tenant, open or closed, in any case, is refused (P5). The parent
    must be open and at the same node, and nesting stops at three (P10). Boxes
    live inside the perimeter only (§4.15.1).
    """
    ctx = _ctx(ctx, actor)
    code = (code or "").strip()

    with transaction.atomic():
        if not _inside_perimeter(node):
            raise BoxOutsidePerimeter(
                f"{node.label} is outside the yard, so a box cannot be kept there.",
                details={"node": node.label},
            )
        depth = 1
        locked_parent = None
        if parent is not None:
            locked_parent = _lock_open(parent)
            if locked_parent.current_node_id != node.pk:
                raise BoxElsewhere(
                    f"Box {locked_parent.code} is at {locked_parent.current_node.label}, "
                    f"not at {node.label}.",
                    details={"box": locked_parent.code},
                )
            depth = locked_parent.depth + 1
            if depth > MAX_DEPTH:
                raise BoxTooDeep(
                    f"Box {locked_parent.code} is already {locked_parent.depth} deep; "
                    f"boxes nest {MAX_DEPTH} deep at most.",
                    details={"box": locked_parent.code, "depth": depth},
                )

        if code:
            source = BoxSource.LABEL
            _refuse_used_code(code, node.organization_id)
        else:
            source = BoxSource.INTERNAL
            code = allocate_number(DocumentType.BOX, organization_id=node.organization_id)

        try:
            with transaction.atomic():
                box = Box.objects.create(
                    organization_id=node.organization_id,
                    code=code,
                    source=source,
                    parent=locked_parent,
                    depth=depth,
                    current_node=node,
                    gate_in=gate_in,
                    label_text=label_text,
                )
        except IntegrityError:
            _refuse_used_code(code, node.organization_id)
            raise

        record_event(box, BoxAction.CREATED, ctx)
        if locked_parent is not None:
            record_event(locked_parent, BoxAction.BOX_IN, ctx, child_box=box)
    return box


# --------------------------------------------------------------------------
# Putting things in
# --------------------------------------------------------------------------


def put_units(box: Box, units: Iterable[SerialUnit], *, ctx: EventContext) -> None:
    """Put units in ``box`` (P1). Each must be at the box's node and in no box."""
    ids = sorted({u.pk for u in units})
    with transaction.atomic():
        # Units before boxes (lock order).
        locked_units = list(
            SerialUnit.objects.select_for_update(of=("self",))
            .select_related("current_node", "box")
            .filter(pk__in=ids)
            .order_by("pk")
        )
        locked = _lock_open(box)
        for unit in locked_units:
            if unit.box_id is not None:
                raise AlreadyInBox(
                    f"Unit {unit.serial_number} is already in box {unit.box.code}.",
                    details={"serial_number": unit.serial_number, "box": unit.box.code},
                )
            if unit.current_node_id != locked.current_node_id:
                raise BoxElsewhere(
                    f"Unit {unit.serial_number} is at {unit.current_node.label}, not at "
                    f"{locked.current_node.label} where box {locked.code} is.",
                    details={"serial_number": unit.serial_number, "box": locked.code},
                )
        for unit in locked_units:
            SerialUnit.objects.filter(pk=unit.pk).update(box=locked, updated_at=timezone.now())
            record_event(locked, BoxAction.UNIT_IN, ctx, serial_unit=unit)


def put_bulk(
    box: Box,
    *,
    item_type,
    owner_client,
    condition: str,
    quantity: Decimal,
    ctx: EventContext,
) -> BoxBulkContent:
    """Claim ``quantity`` of a bulk lot for ``box`` (P9).

    The claim must fit in loose stock: the lot's balance at the box's node less
    the claims of every open box there. Locks the balance row first, then the
    box (§4.15.3 lock order); a missing balance row counts as zero.
    """
    quantity = Decimal(str(quantity))
    if quantity <= 0:
        raise LedgerRuleViolation("A quantity put in a box must be positive.")
    with transaction.atomic():
        balance = (
            StockBalance.objects.select_for_update()
            .filter(
                organization_id=box.organization_id,
                node_id=box.current_node_id,
                item_type=item_type,
                owner_client=owner_client,
                condition=condition,
            )
            .first()
        )
        locked = _lock_open(box)
        held = balance.quantity if balance else Decimal("0")
        claimed = BoxBulkContent.objects.filter(
            box__current_node_id=locked.current_node_id,
            box__status=BoxStatus.OPEN,
            item_type=item_type,
            owner_client=owner_client,
            condition=condition,
        ).aggregate(total=Sum("quantity"))["total"] or Decimal("0")
        loose = max(held - claimed, Decimal("0"))
        if quantity > loose:
            raise NotEnoughLooseStock(
                f"Only {loose} of {item_type} is loose at {locked.current_node.label}; "
                f"{quantity} was asked for.",
                details={"loose": str(loose), "asked": str(quantity)},
            )
        claim = (
            BoxBulkContent.objects.select_for_update()
            .filter(
                box=locked, item_type=item_type, owner_client=owner_client, condition=condition
            )
            .first()
        )
        if claim is None:
            claim = BoxBulkContent.objects.create(
                organization_id=locked.organization_id,
                box=locked,
                item_type=item_type,
                owner_client=owner_client,
                condition=condition,
                quantity=quantity,
            )
        else:
            claim.quantity += quantity
            claim.save(update_fields=["quantity", "updated_at"])
        record_event(
            locked,
            BoxAction.BULK_IN,
            ctx,
            item_type=item_type,
            owner_client=owner_client,
            condition=condition,
            quantity=quantity,
        )
    return claim


def put_box(parent: Box, child: Box, *, ctx: EventContext) -> Box:
    """Put ``child`` (with everything in it) inside ``parent`` (P10).

    Same node, both open, child not already in a box, no cycle, and the child's
    whole subtree must still be at most three deep under the parent.
    """
    with transaction.atomic():
        chain = lock_chain(parent)
        locked_parent = chain[parent.pk]
        locked_child = Box.objects.select_for_update().select_related("current_node").get(
            pk=child.pk
        )
        subtree = _subtree(locked_child, lock=True)
        for b in (locked_parent, locked_child):
            if b.status != BoxStatus.OPEN:
                raise BoxClosed(f"Box {b.code} is closed.", details={"box": b.code})
        if locked_child.pk in chain:
            raise BoxCycle(
                f"Box {locked_child.code} cannot go inside {locked_parent.code}: "
                f"{locked_parent.code} is {locked_child.code} or is inside it.",
                details={"parent": locked_parent.code, "child": locked_child.code},
            )
        if locked_child.parent_id is not None:
            raise AlreadyInBox(
                f"Box {locked_child.code} is already inside another box.",
                details={"box": locked_child.code},
            )
        if locked_child.current_node_id != locked_parent.current_node_id:
            raise BoxElsewhere(
                f"Box {locked_child.code} is at {locked_child.current_node.label}, not at "
                f"{locked_parent.current_node.label} where {locked_parent.code} is.",
                details={"parent": locked_parent.code, "child": locked_child.code},
            )
        height = max(b.depth for b in subtree) - locked_child.depth
        if locked_parent.depth + 1 + height > MAX_DEPTH:
            raise BoxTooDeep(
                f"Box {locked_child.code} with what is inside it would be "
                f"{locked_parent.depth + 1 + height} deep under {locked_parent.code}; "
                f"boxes nest {MAX_DEPTH} deep at most.",
                details={"parent": locked_parent.code, "child": locked_child.code},
            )
        Box.objects.filter(pk=locked_child.pk).update(
            parent=locked_parent, updated_at=timezone.now()
        )
        _set_depths(locked_child, locked_parent.depth + 1, subtree)
        locked_child.parent = locked_parent
        record_event(locked_parent, BoxAction.BOX_IN, ctx, child_box=locked_child)
    return locked_child


# --------------------------------------------------------------------------
# Taking things out
# --------------------------------------------------------------------------


def _take_out(
    box: Box,
    *,
    units: list[SerialUnit],
    bulk: list[dict[str, Any]],
    boxes: list[Box],
    ctx: EventContext,
) -> tuple[Box, list[str]]:
    """The shared body of ``take_out`` and ``empty_box``; the caller closes and audits."""
    unit_ids = sorted({u.pk for u in units})
    child_ids = sorted({b.pk for b in boxes})
    # Units before boxes (lock order).
    locked_units = list(
        SerialUnit.objects.select_for_update().filter(pk__in=unit_ids).order_by("pk")
    )
    locked = _lock_open(box)
    notes: list[str] = []

    if len(locked_units) != len(unit_ids):
        raise NotInThisBox(f"A unit named is not in box {locked.code}.")
    for unit in locked_units:
        if unit.box_id != locked.pk:
            raise NotInThisBox(
                f"Unit {unit.serial_number} is not in box {locked.code}.",
                details={"serial_number": unit.serial_number, "box": locked.code},
            )
    for unit in locked_units:
        SerialUnit.objects.filter(pk=unit.pk).update(box=None, updated_at=timezone.now())
        record_event(locked, BoxAction.UNIT_OUT, ctx, serial_unit=unit)
        notes.append(unit.serial_number)

    for entry in bulk:
        lot = {
            "item_type": entry["item_type"],
            "owner_client": entry.get("owner_client"),
            "condition": entry["condition"],
        }
        amount = Decimal(str(entry["quantity"]))
        claim = BoxBulkContent.objects.select_for_update().filter(box=locked, **lot).first()
        held = claim.quantity if claim else Decimal("0")
        if claim is None or amount <= 0 or amount > held:
            raise BoxClaimShort(
                f"Box {locked.code} holds {held} of {lot['item_type']}; {amount} was asked for.",
                details={"box": locked.code, "held": str(held), "requested": str(amount)},
            )
        box_hooks._reduce_claim(locked, claim, amount, ctx, lot["item_type"], lot["owner_client"])
        notes.append(f"{amount} {lot['item_type']}")

    for child_id in child_ids:
        child = Box.objects.select_for_update().filter(pk=child_id).first()
        if child is None or child.parent_id != locked.pk or child.status != BoxStatus.OPEN:
            raise NotInThisBox(
                f"Box {child.code if child else child_id} is not an open box inside "
                f"{locked.code}.",
                details={"box": locked.code},
            )
        subtree = _subtree(child, lock=True)
        Box.objects.filter(pk=child.pk).update(parent=None, updated_at=timezone.now())
        _set_depths(child, 1, subtree)
        record_event(locked, BoxAction.BOX_OUT, ctx, child_box=child)
        notes.append(f"box {child.code}")

    return locked, notes


def take_out(
    box: Box,
    *,
    units: Iterable[SerialUnit] = (),
    bulk: Iterable[dict[str, Any]] = (),
    boxes: Iterable[Box] = (),
    ctx: EventContext,
) -> Box:
    """Take contents out of ``box`` without moving them (P7).

    ``bulk`` is a list of ``{item_type, owner_client, condition, quantity}``
    reducing the box's claim; child ``boxes`` become top-level boxes. Nothing
    moves in the ledger. The box closes if that leaves it empty (P5). One audit
    record per call.
    """
    units, bulk, boxes = list(units), list(bulk), list(boxes)
    if not (units or bulk or boxes):
        raise LedgerRuleViolation("Name something to take out of the box.")
    with transaction.atomic():
        locked, notes = _take_out(box, units=units, bulk=bulk, boxes=boxes, ctx=ctx)
        close_if_empty(locked, ctx)
        _audit(locked, ctx, f"Took out of box {locked.code}: {', '.join(notes)}.")
    locked.refresh_from_db()
    return locked


def empty_box(box: Box, *, ctx: EventContext) -> Box:
    """Take everything out of ``box``, which closes it (P7)."""
    with transaction.atomic():
        units = list(SerialUnit.objects.filter(box=box))
        bulk = [
            {
                "item_type": c.item_type,
                "owner_client": c.owner_client,
                "condition": c.condition,
                "quantity": c.quantity,
            }
            for c in BoxBulkContent.objects.filter(box=box).select_related(
                "item_type", "owner_client"
            )
        ]
        children = list(Box.objects.filter(parent=box, status=BoxStatus.OPEN))
        locked, notes = _take_out(box, units=units, bulk=bulk, boxes=children, ctx=ctx)
        close_if_empty(locked, ctx)
        _audit(
            locked,
            ctx,
            f"Emptied box {locked.code}" + (f": {', '.join(notes)}." if notes else "."),
        )
    locked.refresh_from_db()
    return locked


# --------------------------------------------------------------------------
# move_box
# --------------------------------------------------------------------------


def move_box(box: Box, to_location, *, actor, request=None) -> Box:
    """Carry ``box`` and everything in it to another place in the yard (P7, E4).

    Both ends must be inside the perimeter. Nothing leaves its box: each unit
    and each claim is posted as a TRANSFER with ``moving_box`` set, under one
    document number (``stock.BoxMove``), so the ledger moves and the box rules
    leave the contents where they are. Only a top-level box moves; a box inside
    another travels with its parent.

    **Lock order**: the balance rows of every lot involved at both nodes (lot by
    lot, each pair as ``_lock_balances`` orders it; missing rows are created),
    then the serial units by id, then the subtree's boxes top-down, then the
    claims. The ``post_movement`` calls re-lock what they need, which is free in
    Postgres.
    """
    with transaction.atomic():
        # The caller's instance may be stale; where the box is now decides.
        box = Box.objects.select_related("current_node").get(pk=box.pk)
        from_node = box.current_node
        to_node = node_for_location(to_location)
        if not _inside_perimeter(to_node):
            raise TransferNotAllowed(
                f"{to_location.name} is outside the yard, so a box cannot be moved there. "
                f"Raise a gate-out instead, so it can be approved and released at the "
                f"gate (E4).",
                details={"to_location": to_location.name, "type": to_location.type},
            )
        if not _inside_perimeter(from_node):
            raise TransferNotAllowed(
                f"Box {box.code} is at {from_node.label}, outside the yard.",
                details={"box": box.code},
            )
        if from_node.pk == to_node.pk:
            raise LedgerRuleViolation(f"Box {box.code} is already at {to_node.label}.")

        # 1. What is involved, read before any lock.
        tree_ids = [b.pk for b in _subtree(box, open_only=True)]
        units = list(
            SerialUnit.objects.filter(box_id__in=tree_ids).select_related(
                "item_type", "owner_client"
            )
        )
        claims = list(
            BoxBulkContent.objects.filter(box_id__in=tree_ids).select_related(
                "item_type", "owner_client"
            )
        )
        lots: dict[tuple, tuple] = {}
        for row in [*units, *claims]:
            lots[(row.item_type_id, row.owner_client_id or 0, row.condition)] = (
                row.item_type,
                row.owner_client,
                row.condition,
            )

        # 2. Balances, lot by lot in a fixed order.
        for key in sorted(lots):
            item_type, owner_client, condition = lots[key]
            _lock_balances(
                organization_id=box.organization_id,
                sides=((from_node, condition), (to_node, condition)),
                item_type=item_type,
                owner_client=owner_client,
                uom=item_type.uom,
            )

        # 3. Units by id, then boxes top-down, then claims.
        locked_units = list(
            SerialUnit.objects.select_for_update(of=("self",))
            .filter(box_id__in=tree_ids)
            .select_related("item_type", "owner_client")
            .order_by("pk")
        )
        root = Box.objects.select_for_update().select_related("current_node").get(pk=box.pk)
        if root.status != BoxStatus.OPEN:
            raise BoxClosed(f"Box {box.code} is closed.", details={"box": box.code})
        if root.parent_id is not None:
            raise BoxInsideAnotherBox(
                f"Box {box.code} is inside another box. Move that box, or take this one "
                f"out first.",
                details={"box": box.code},
            )
        if root.current_node_id != from_node.pk:
            raise BoxChanged(f"Box {box.code} was moved by someone else. Try again.")
        locked_tree = _subtree(root, open_only=True, lock=True)
        locked_ids = [b.pk for b in locked_tree]
        fresh_claims = list(
            BoxBulkContent.objects.select_for_update(of=("self",))
            .filter(box_id__in=locked_ids)
            .select_related("item_type", "owner_client", "box")
            .order_by("box_id", "pk")
        )
        known = set(lots)
        seen_units = {u.pk for u in locked_units}
        if (
            {u.pk for u in SerialUnit.objects.filter(box_id__in=locked_ids)} - seen_units
            or {(c.item_type_id, c.owner_client_id or 0, c.condition) for c in fresh_claims}
            - known
            or {(u.item_type_id, u.owner_client_id or 0, u.condition) for u in locked_units}
            - known
        ):
            raise BoxChanged(f"Box {box.code} changed while it was being moved. Try again.")

        # 4. Post it, under one document.
        number = allocate_number(DocumentType.TRANSFER, organization_id=box.organization_id)
        base: dict[str, Any] = {
            "from_node": from_node,
            "to_node": to_node,
            "movement_type": MovementType.TRANSFER,
            "posted_by": actor,
            "document_type": "stock.BoxMove",
            "document_id": str(box.pk),
            "document_number": number,
            "moving_box": root,
            "note": f"Box {box.code} moved from {from_node.label} to {to_node.label}",
        }
        for unit in locked_units:
            post_movement(
                MovementRequest(
                    item_type=unit.item_type,
                    quantity=Decimal("1"),
                    owner_type=unit.owner_type,
                    owner_client=unit.owner_client,
                    condition=unit.condition,
                    tracking_mode=TrackingMode.SERIALIZED,
                    serial_unit=unit,
                    **base,
                )
            )
        for claim in fresh_claims:
            post_movement(
                MovementRequest(
                    item_type=claim.item_type,
                    quantity=claim.quantity,
                    owner_type=OwnerType.OWN if claim.owner_client is None else OwnerType.CLIENT,
                    owner_client=claim.owner_client,
                    condition=claim.condition,
                    tracking_mode=TrackingMode.BULK,
                    from_box=claim.box,
                    **base,
                )
            )

        # 5. The boxes follow their contents.
        Box.objects.filter(pk__in=locked_ids).update(
            current_node=to_node, updated_at=timezone.now()
        )
        ctx = EventContext(
            actor=actor,
            document_type="stock.BoxMove",
            document_id=str(box.pk),
            document_number=number,
            note=f"From {from_node.label} to {to_node.label}",
        )
        record_event(root, BoxAction.MOVED, ctx)
        _audit(
            root,
            ctx,
            f"Moved box {box.code} ({len(locked_units)} units, {len(fresh_claims)} bulk "
            f"claims, {len(locked_ids) - 1} boxes inside) from {from_node.label} to "
            f"{to_node.label}, {number}.",
            request,
        )
    return Box.objects.get(pk=box.pk)


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def box_tree(box: Box) -> dict[str, Any]:
    """The box with its contents at every level (P4).

    Each node carries ``counts``: what was ever put in (``received``, from the
    UNIT_IN / BULK_IN events of that box and everything beneath it) against
    what is there ``now``. Closed child boxes stay in the tree as history.
    A handful of queries however many boxes there are.
    """
    boxes = _subtree(box)
    ids = [b.pk for b in boxes]
    units_by_box: dict[int, list[SerialUnit]] = defaultdict(list)
    for u in SerialUnit.objects.filter(box_id__in=ids).select_related("item_type").order_by("pk"):
        units_by_box[u.box_id].append(u)  # type: ignore[index]
    claims_by_box: dict[int, list[BoxBulkContent]] = defaultdict(list)
    for c in (
        BoxBulkContent.objects.filter(box_id__in=ids)
        .select_related("item_type", "owner_client")
        .order_by("pk")
    ):
        claims_by_box[c.box_id].append(c)

    units_in: dict[int, set[int]] = defaultdict(set)
    for box_id, unit_id in BoxEvent.objects.filter(
        box_id__in=ids, action=BoxAction.UNIT_IN
    ).values_list("box_id", "serial_unit_id"):
        units_in[box_id].add(unit_id)
    bulk_in: dict[int, Decimal] = {
        row["box_id"]: row["total"]
        for row in BoxEvent.objects.filter(box_id__in=ids, action=BoxAction.BULK_IN)
        .values("box_id")
        .annotate(total=Sum("quantity"))
    }

    children_of: dict[int, list[Box]] = defaultdict(list)
    for b in boxes:
        if b.parent_id is not None:
            children_of[b.parent_id].append(b)

    def build(b: Box) -> tuple[dict[str, Any], set[int]]:
        built = [build(c) for c in children_of.get(b.pk, [])]
        kids = [node for node, _ in built]
        own_units = units_by_box.get(b.pk, [])
        own_claims = claims_by_box.get(b.pk, [])
        received_units = set(units_in.get(b.pk, set()))
        received_bulk = bulk_in.get(b.pk, Decimal("0"))
        now_units = len(own_units)
        now_bulk = sum((c.quantity for c in own_claims), Decimal("0"))
        for node, kid_units in built:
            received_units |= kid_units
            received_bulk += node["counts"]["received"]["bulk"]
            now_units += node["counts"]["now"]["units"]
            now_bulk += node["counts"]["now"]["bulk"]
        node = {
            "id": b.pk,
            "code": b.code,
            "status": b.status,
            "node": b.current_node.label,
            "depth": b.depth,
            "units": [
                {
                    "id": u.pk,
                    "serial_number": u.serial_number,
                    "asset_tag": u.asset_tag,
                    "item_type": u.item_type_id,
                    "item_name": u.item_type.name,
                    "status": u.status,
                    "condition": u.condition,
                }
                for u in own_units
            ],
            "bulk": [
                {
                    "item_type": c.item_type_id,
                    "item_name": c.item_type.name,
                    "owner_client": c.owner_client_id,
                    "owner_name": c.owner_client.name if c.owner_client else "",
                    "condition": c.condition,
                    "quantity": c.quantity,
                    "uom": c.item_type.uom,
                }
                for c in own_claims
            ],
            "children": kids,
            "counts": {
                "received": {"units": len(received_units), "bulk": received_bulk},
                "now": {"units": now_units, "bulk": now_bulk},
            },
        }
        return node, received_units

    return build(boxes[0])[0]


def _terminal_statuses() -> tuple[str, ...]:
    from dispatch.models import GateOutStatus

    return (
        GateOutStatus.RELEASED,
        GateOutStatus.CANCELLED,
        GateOutStatus.REJECTED,
        GateOutStatus.CLOSED,
        GateOutStatus.EXPIRED,
    )


def issuable_contents(box: Box, *, from_node: StockNode) -> dict[str, list[dict[str, Any]]]:
    """Expand ``box`` into proposed gate-out lines, and what cannot go (P6, P9, P10).

    Returns ``{"lines": [...], "excluded": [...]}``. A unit is excluded, with a
    ``reason`` and a plain ``message``, when it is not at ``from_node``
    (``NOT_HERE``), quarantined (``QUARANTINED``), held by a person
    (``HELD_BY_PERSON``), not in stock for any other status (``NOT_IN_STOCK``)
    or named on another open gate pass (``ON_ANOTHER_PASS``). Serialized units
    make one line per (innermost box, item, owner, condition); each bulk claim
    makes one line. Bulk committed on other passes is T11.9's check at submit.
    """
    from dispatch.models import GateOutLineSerial

    tree = _subtree(box, open_only=True)
    by_id: dict[int, Box] = {b.pk: b for b in tree}
    ids = list(by_id)
    units = list(
        SerialUnit.objects.filter(box_id__in=ids)
        .select_related("item_type", "current_node")
        .order_by("serial_number", "pk")
    )
    claims = list(
        BoxBulkContent.objects.filter(box_id__in=ids)
        .select_related("item_type", "owner_client")
        .order_by("pk")
    )
    passes: dict[int, str] = {}
    for serial in (
        GateOutLineSerial.objects.filter(serial_unit_id__in=[u.pk for u in units], released=False)
        .exclude(line__gate_out__status__in=_terminal_statuses())
        .select_related("line__gate_out")
        .order_by("pk")
    ):
        passes.setdefault(serial.serial_unit_id, serial.line.gate_out.number)  # type: ignore[attr-defined]

    paths = {b.pk: _box_path(b, by_id) for b in tree}
    lines: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    groups: dict[tuple, dict[str, Any]] = {}

    def exclude(b: Box, reason: str, message: str, *, unit=None, claim=None) -> None:
        item = unit.item_type if unit else claim.item_type
        excluded.append(
            {
                "kind": "unit" if unit else "bulk",
                "serial_unit": unit.pk if unit else None,
                "serial_number": unit.serial_number if unit else "",
                "item_type": item.pk,
                "item_name": item.name,
                "box": b.pk,
                "box_code": b.code,
                "reason": reason,
                "message": message,
            }
        )

    for unit in units:
        b = by_id[unit.box_id]  # type: ignore[index]
        label = f"Unit {unit.serial_number} (box {b.code})"
        if unit.current_node_id != from_node.pk:
            exclude(
                b,
                "NOT_HERE",
                f"{label} is at {unit.current_node.label}, not at {from_node.label}.",
                unit=unit,
            )
        elif unit.status == SerialUnitStatus.QUARANTINED:
            exclude(b, "QUARANTINED", f"{label} is quarantined.", unit=unit)
        elif unit.status == SerialUnitStatus.IN_CUSTODY:
            exclude(b, "HELD_BY_PERSON", f"{label} is held by a person.", unit=unit)
        elif unit.status != SerialUnitStatus.IN_STOCK:
            exclude(
                b,
                "NOT_IN_STOCK",
                f"{label} is not in stock ({unit.get_status_display().lower()}).",
                unit=unit,
            )
        elif unit.pk in passes:
            exclude(
                b,
                "ON_ANOTHER_PASS",
                f"{label} is already on gate pass {passes[unit.pk]}.",
                unit=unit,
            )
        else:
            key = (b.pk, unit.item_type_id, unit.owner_client_id, unit.condition)
            line = groups.get(key)
            if line is None:
                line = groups[key] = {
                    "item_type": unit.item_type_id,
                    "item_name": unit.item_type.name,
                    "tracking_mode": TrackingMode.SERIALIZED,
                    "uom": unit.item_type.uom,
                    "owner_type": unit.owner_type,
                    "owner_client": unit.owner_client_id,
                    "condition": unit.condition,
                    "requested_qty": Decimal("0"),
                    "units": [],
                    "box": b.pk,
                    "box_code": b.code,
                    "box_path": paths[b.pk],
                }
                lines.append(line)
            line["units"].append({"serial_unit": unit.pk, "serial_number": unit.serial_number})
            line["requested_qty"] += 1

    for claim in claims:
        b = by_id[claim.box_id]  # type: ignore[index]
        if b.current_node_id != from_node.pk:
            exclude(
                b,
                "NOT_HERE",
                f"{claim.quantity} of {claim.item_type.name} in box {b.code} is at "
                f"{b.current_node.label}, not at {from_node.label}.",
                claim=claim,
            )
            continue
        lines.append(
            {
                "item_type": claim.item_type_id,
                "item_name": claim.item_type.name,
                "tracking_mode": TrackingMode.BULK,
                "uom": claim.item_type.uom,
                "owner_type": OwnerType.OWN if claim.owner_client_id is None else OwnerType.CLIENT,
                "owner_client": claim.owner_client_id,
                "condition": claim.condition,
                "requested_qty": claim.quantity,
                "units": [],
                "box": b.pk,
                "box_code": b.code,
                "box_path": paths[b.pk],
            }
        )

    return {"lines": lines, "excluded": excluded}
