"""The earmark rules inside ``post_movement`` (design §4.16.3; Q2, Q3).

Private to the ``stock`` app, beside :mod:`stock.box_hooks` and in the same
spirit: an earmark is a projection kept next to the ledger, so the rules that
keep it right live in the one path every movement takes.

* **A unit or a drum** (:func:`apply_unit_rule`, :func:`apply_reel_rule`) that
  moves inside the perimeter keeps its earmark (a MOVED event, recording the
  node it went to). One that leaves the perimeter uses the earmark up:
  DELIVERED when its site is one the movement delivers to, otherwise DIVERTED,
  which needs a reason. A *cut* off a drum leaves the earmark on the drum and
  records the length drawn; the earmark is cleared only when the drum itself
  goes (whole, or emptied and closed). A cut that stays inside the perimeter
  is loose cable changing store and records nothing.
* **Bulk** (:func:`apply_bulk_rule`) drawn from an inside node is taken in
  order: the earmarks of the sites in ``for_sites``, then free stock, then other
  sites' earmarks by site name. Inside the perimeter the drawn earmarks move to
  the destination node; leaving it they are DELIVERED or DIVERTED; a correction
  takes free first and then reduces earmarks (REDUCED), never needing a reason.

**Lock order**, extending :mod:`stock.box_hooks`: balances (by node, condition),
then the serial unit or drum, then boxes (and their claims), then earmark claims
(``BulkEarmark`` rows, in site-name order). Earmarks come after boxes because
the box rule runs first on a bulk draw and a box row is always locked before
anything of an earmark's; the unit's own box rule runs after the move and locks
no earmark row, so the two never cross. The balance lock already serialises
every movement of one lot, so the claim locks are a second guard, not the first.

The common case, nothing earmarked at the node for the lot, costs one
existence query for bulk and nothing for a unit that carries no earmark.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from django.db.models.functions import Lower
from django.utils import timezone

from locations.models import NodeType, StockNode
from network.models import Site
from stock.box_hooks import CORRECTION_TYPES, EventContext
from stock.models import (
    BulkEarmark,
    EarmarkAction,
    EarmarkEvent,
    Reel,
    SerialUnit,
)

if TYPE_CHECKING:
    from stock.services import MovementRequest

ZERO = Decimal("0")



def _reason(request) -> str:  # type: ignore[no-untyped-def]
    """The diversion reason, given or implied (§4.16.3).

    Scrapping or sending out for repair is decided on its own document
    (Epic J), which already says why; asking the gate for a second reason would
    leave an earmarked item impossible to scrap. Such a movement carries its
    document as the reason. Everything else must say why in words.
    """
    given = (request.divert_reason or "").strip()
    if given:
        return given
    if (request.document_type or "").startswith("disposition."):
        number = request.document_number or request.document_id or ""
        return f"Decided on {request.document_type.split('.')[-1]} {number}".strip()
    return ""

def is_inside(node: StockNode) -> bool:
    """Is ``node`` a place inside the yard perimeter (E4)? Reuses the counting
    constant, so a vehicle is outside here as it is there."""
    from stock.counting import is_inside_perimeter

    if node.type != NodeType.LOCATION or node.location is None:
        return False
    return is_inside_perimeter(node.location)


def record_event(
    ctx: EventContext, organization_id, action: str, reason: str = "", **fields
) -> EarmarkEvent:
    """Append one EarmarkEvent carrying the movement's document refs (P8)."""
    return EarmarkEvent.objects.create(
        organization_id=organization_id,
        action=action,
        actor=ctx.actor,
        occurred_at=ctx.occurred_at or timezone.now(),
        document_type=ctx.document_type,
        document_id=ctx.document_id,
        document_number=ctx.document_number,
        reason=reason,
        **fields,
    )


def _diverted_to(for_sites) -> Site | None:
    """The one site a diversion went to, when the movement names exactly one."""
    if for_sites and len(for_sites) == 1:
        return next(iter(for_sites))
    return None


def _settle(
    request: MovementRequest,
    ctx: EventContext,
    site: Site,
    *,
    subject: dict,
    quantity: Decimal | None,
    description: str,
) -> None:
    """Write DELIVERED or DIVERTED for one earmark leaving the perimeter."""
    from stock.services import EarmarkDiversionNeedsReason

    for_sites = request.for_sites or frozenset()
    organization_id = request.from_node.organization_id
    common = {"site": site, "quantity": quantity, "node": request.from_node, **subject}
    if site in for_sites:
        record_event(ctx, organization_id, EarmarkAction.DELIVERED, **common)
        return
    if not _reason(request):
        raise EarmarkDiversionNeedsReason(
            f"{description} is earmarked for {site.name}, not for where it is going. "
            f"Give a reason to divert it, or take other stock.",
            details={"sites": [site.name], "subject": description},
        )
    record_event(
        ctx,
        organization_id,
        EarmarkAction.DIVERTED,
        reason=_reason(request),
        to_site=_diverted_to(for_sites),
        **common,
    )


def apply_unit_rule(request: MovementRequest, unit: SerialUnit, ctx: EventContext) -> None:
    """A serialized unit's earmark: kept inside the perimeter, used up leaving it.

    ``unit`` is the locked, fresh row read before the move. Units get a MOVED
    event too: it is one row, and "when did this unit change store" is a fair
    question about earmarked stock.
    """
    if unit.earmark_site_id is None or not is_inside(request.from_node):
        return
    site = Site.objects.get(pk=unit.earmark_site_id)
    subject: dict[str, Any] = {
        "serial_unit": unit,
        "item_type": unit.item_type,
        "owner_client": unit.owner_client,
        "condition": unit.condition,
    }
    if is_inside(request.to_node):
        record_event(
            ctx,
            request.from_node.organization_id,
            EarmarkAction.MOVED,
            site=site,
            node=request.to_node,
            **subject,
        )
        return
    _settle(
        request,
        ctx,
        site,
        subject=subject,
        quantity=None,
        description=f"Unit {unit.serial_number}",
    )
    SerialUnit.objects.filter(pk=unit.pk).update(earmark_site=None, updated_at=timezone.now())
    if request.serial_unit is not None:
        request.serial_unit.earmark_site = None


def apply_reel_rule(request: MovementRequest, length: Decimal, ctx: EventContext) -> None:
    """A drum's earmark. The drum is locked fresh here, since the caller's copy
    can be stale and the earmark must be decided on the real row."""
    assert request.reel is not None
    reel = Reel.objects.select_for_update().get(pk=request.reel.pk)
    if reel.earmark_site_id is None or not is_inside(request.from_node):
        return
    site = Site.objects.get(pk=reel.earmark_site_id)
    # Same test as _move_or_consume_reel: less than what is on the drum is a cut.
    whole_drum = length >= reel.remaining_length
    subject: dict[str, Any] = {
        "reel": reel,
        "item_type": reel.item_type,
        "owner_client": reel.owner_client,
        "condition": reel.condition,
    }
    if is_inside(request.to_node):
        if whole_drum:
            record_event(
                ctx,
                request.from_node.organization_id,
                EarmarkAction.MOVED,
                site=site,
                node=request.to_node,
                quantity=length,
                **subject,
            )
        return
    _settle(
        request,
        ctx,
        site,
        subject=subject,
        quantity=length,
        description=f"Drum {reel.drum_number}",
    )
    if whole_drum:
        Reel.objects.filter(pk=reel.pk).update(earmark_site=None, updated_at=timezone.now())
        request.reel.earmark_site = None


@dataclass
class _Draw:
    claim: BulkEarmark
    amount: Decimal
    own: bool


def apply_bulk_rule(
    request: MovementRequest,
    *,
    quantity: Decimal,
    held_condition: str,
    balance_before: Decimal,
    ctx: EventContext,
) -> None:
    """Bulk out of a node against the earmarks there (§4.16.3).

    ``balance_before`` is the locked balance at ``from_node`` for the lot before
    this movement debits it; ``held_condition`` the condition it is debited in,
    which is the one earmark claims are held in.
    """
    lot: dict[str, Any] = {
        "item_type": request.item_type,
        "owner_client": request.owner_client,
        "condition": held_condition,
    }
    if not BulkEarmark.objects.filter(node=request.from_node, **lot).exists():
        return
    if not is_inside(request.from_node):
        return

    claims = list(
        BulkEarmark.objects.select_for_update(of=("self",))
        .select_related("site")
        .filter(node=request.from_node, **lot)
        .order_by(Lower("site__name"), "pk")
    )
    claimed = sum((c.quantity for c in claims), ZERO)
    free = max(balance_before - claimed, ZERO)
    for_sites = request.for_sites or frozenset()
    organization_id = request.from_node.organization_id

    correction = request.movement_type in CORRECTION_TYPES
    draws: list[_Draw] = []
    remaining = quantity

    def take_claims(selected: list[BulkEarmark], own: bool) -> None:
        nonlocal remaining
        for claim in selected:
            if remaining <= 0:
                return
            amount = min(claim.quantity, remaining)
            draws.append(_Draw(claim, amount, own))
            remaining -= amount

    own_claims = [c for c in claims if c.site in for_sites]
    other_claims = [c for c in claims if c.site not in for_sites]
    if correction:
        remaining -= min(remaining, free)
        take_claims(claims, own=False)
    else:
        take_claims(own_claims, own=True)
        remaining -= min(remaining, free)
        take_claims(other_claims, own=False)

    if not draws:
        return

    if correction:
        for draw in draws:
            _lower(draw.claim, draw.amount)
            record_event(
                ctx,
                organization_id,
                EarmarkAction.REDUCED,
                reason="Reduced by a correction",
                site=draw.claim.site,
                node=request.from_node,
                quantity=draw.amount,
                **lot,
            )
        return

    if is_inside(request.to_node):
        for draw in draws:
            _lower(draw.claim, draw.amount)
            _raise(draw, request.to_node, request.condition)
            record_event(
                ctx,
                organization_id,
                EarmarkAction.MOVED,
                site=draw.claim.site,
                node=request.to_node,
                quantity=draw.amount,
                **lot,
            )
        return

    # Leaving the perimeter: own-site portions are delivered, others are diverted.
    diverted = [d for d in draws if not d.own]
    if diverted and not _reason(request):
        from stock.services import EarmarkDiversionNeedsReason

        listed = ", ".join(f"{d.claim.site.name} ({d.amount.normalize():f})" for d in diverted)
        raise EarmarkDiversionNeedsReason(
            f"{request.item_type} is earmarked for {listed}, not for where it is going. "
            f"Give a reason to divert it, or take free stock.",
            details={
                "sites": [d.claim.site.name for d in diverted],
                "quantities": [str(d.amount) for d in diverted],
            },
        )
    for draw in draws:
        _lower(draw.claim, draw.amount)
        if draw.own:
            record_event(
                ctx,
                organization_id,
                EarmarkAction.DELIVERED,
                site=draw.claim.site,
                node=request.from_node,
                quantity=draw.amount,
                **lot,
            )
        else:
            record_event(
                ctx,
                organization_id,
                EarmarkAction.DIVERTED,
                reason=_reason(request),
                site=draw.claim.site,
                to_site=_diverted_to(for_sites),
                node=request.from_node,
                quantity=draw.amount,
                **lot,
            )


def _lower(claim: BulkEarmark, amount: Decimal) -> None:
    """Reduce a claim, deleting it at zero."""
    remaining = claim.quantity - amount
    if remaining <= 0:
        claim.delete()
    else:
        claim.quantity = remaining
        claim.save(update_fields=["quantity", "updated_at"])


def _raise(draw: _Draw, to_node: StockNode, condition: str) -> None:
    """Add ``draw.amount`` to the site's claim on the lot at ``to_node``."""
    claim = draw.claim
    existing = (
        BulkEarmark.objects.select_for_update()
        .filter(
            site=claim.site,
            node=to_node,
            item_type=claim.item_type,
            owner_client=claim.owner_client,
            condition=condition,
        )
        .first()
    )
    if existing is not None:
        existing.quantity += draw.amount
        existing.save(update_fields=["quantity", "updated_at"])
        return
    BulkEarmark.objects.create(
        organization_id=claim.organization_id,
        site=claim.site,
        node=to_node,
        item_type=claim.item_type,
        owner_client=claim.owner_client,
        condition=condition,
        quantity=draw.amount,
    )
