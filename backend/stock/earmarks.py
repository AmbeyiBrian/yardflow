"""Changing an earmark, and reading what is waiting for a site (design §4.16.6, §4.16.8a).

An earmark is a projection beside the ledger: changing one moves no stock and
writes no movement. It is written with its ``EarmarkEvent`` in one transaction.
Lock order is balances before claims, as the ledger hook takes them.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.db import transaction

from catalogue.models import ItemType, TrackingMode
from core.audit import record
from core.exceptions import DomainError
from core.models import AuditAction
from locations.models import NodeType, StockNode
from stock.counting import INSIDE_PERIMETER
from stock.models import (
    BulkEarmark,
    EarmarkAction,
    EarmarkEvent,
    OwnerType,
    Reel,
    ReelStatus,
    SerialUnit,
    SerialUnitStatus,
    StockBalance,
)


class EarmarkChangeInvalid(DomainError):
    """§4.16.9: the subject is not earmarked as stated, or the quantity is too big."""

    code = "EARMARK_CHANGE_INVALID"
    status_code = 400
    default_message = "That earmark cannot be changed that way."


def _inside_perimeter(node: StockNode) -> bool:
    return (
        node.type == NodeType.LOCATION
        and node.location is not None
        and node.location.type in INSIDE_PERIMETER
    )


def _lot_claims(node, item_type, owner_client, condition):  # type: ignore[no-untyped-def]
    return BulkEarmark.objects.filter(
        node=node, item_type=item_type, owner_client=owner_client, condition=condition
    )


def _lot_state(node, item_type, owner_client, condition) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    balance = (
        StockBalance.objects.filter(
            node=node, item_type=item_type, owner_client=owner_client, condition=condition
        )
        .values_list("quantity", flat=True)
        .first()
        or Decimal("0")
    )
    claims = list(
        _lot_claims(node, item_type, owner_client, condition)
        .select_related("site")
        .order_by("site__name", "pk")
    )
    return {
        "node": node.pk,
        "item_type": item_type.pk,
        "owner_client": owner_client.pk if owner_client else None,
        "condition": condition,
        "balance": balance,
        "earmarked": [
            {"site": c.site_id, "name": c.site.name, "quantity": c.quantity} for c in claims
        ],
        "free": balance - sum((c.quantity for c in claims), Decimal("0")),
    }


def change_earmark(
    *,
    subject: dict[str, Any],
    to_site,  # type: ignore[no-untyped-def]
    reason: str,
    actor,  # type: ignore[no-untyped-def]
    request=None,
) -> dict[str, Any]:
    """Move or clear one earmark. Returns ``{"kind": ..., "subject": <the row or lot state>}``.

    ``subject`` is ``{"serial_unit": unit}``, ``{"reel": drum}`` or a bulk lot
    ``{"node", "item_type", "owner_client", "condition", "from_site" (None = free),
    "quantity"}``.
    """
    reason = (reason or "").strip()
    if not reason:
        raise EarmarkChangeInvalid(
            "Say why the earmark is changing.",
            field_errors={"reason": ["This field is required."]},
        )
    kinds = [k for k in ("serial_unit", "reel") if subject.get(k) is not None]
    bulk = subject.get("node") is not None
    if len(kinds) + (1 if bulk else 0) != 1:
        raise EarmarkChangeInvalid("Name one thing: a unit, a drum, or a bulk lot at a place.")

    with transaction.atomic():
        if kinds:
            result = _change_tracked(kinds[0], subject[kinds[0]], to_site, reason, actor)
        else:
            result = _change_bulk(subject, to_site, reason, actor)

        record(
            AuditAction.STATUS_CHANGED,
            actor=actor,
            organization=actor.organization_id,
            target=result["target"],
            target_label=result["label"],
            request=request,
            note=result["note"] + f" Reason: {reason}",
        )
    return {"kind": result["kind"], "subject": result["subject"]}


def _site_label(site) -> str:  # type: ignore[no-untyped-def]
    return site.name if site is not None else "free"


def _change_tracked(kind, obj, to_site, reason, actor):  # type: ignore[no-untyped-def]
    model = SerialUnit if kind == "serial_unit" else Reel
    model.objects.select_for_update().filter(pk=obj.pk).first()  # lock the row, no joins
    locked = model.objects.select_related(
        "current_node__location", "item_type", "earmark_site"
    ).get(pk=obj.pk)
    ident = locked.serial_number if kind == "serial_unit" else locked.drum_number
    noun = "Unit" if kind == "serial_unit" else "Drum"
    if not _inside_perimeter(locked.current_node):
        raise EarmarkChangeInvalid(
            f"{noun} {ident} is not inside the yard, so its earmark cannot be changed."
        )
    old = locked.earmark_site
    if old is None and to_site is None:
        raise EarmarkChangeInvalid(
            f"{noun} {ident} is not earmarked, so there is nothing to clear."
        )
    if old is not None and to_site is not None and old.pk == to_site.pk:
        raise EarmarkChangeInvalid(f"{noun} {ident} is already earmarked for {old.name}.")

    locked.earmark_site = to_site
    locked.save(update_fields=["earmark_site", "updated_at"])
    EarmarkEvent.objects.create(
        organization_id=locked.organization_id,
        action=EarmarkAction.CHANGED if to_site is not None else EarmarkAction.CLEARED,
        site=old,
        to_site=to_site,
        serial_unit=locked if kind == "serial_unit" else None,
        reel=locked if kind == "reel" else None,
        node=locked.current_node,
        item_type=locked.item_type,
        owner_client_id=locked.owner_client_id,
        condition=locked.condition,
        quantity=Decimal("1") if kind == "serial_unit" else locked.remaining_length,
        actor=actor,
        reason=reason,
    )
    return {
        "kind": "unit" if kind == "serial_unit" else "drum",
        "subject": locked,
        "target": locked,
        "label": ident,
        "note": (
            f"{noun} {ident} earmark changed from {_site_label(old)} to {_site_label(to_site)}."
        ),
    }


def _change_bulk(subject, to_site, reason, actor):  # type: ignore[no-untyped-def]
    node: StockNode = subject["node"]
    item: ItemType = subject["item_type"]
    owner = subject.get("owner_client")
    condition = subject.get("condition")
    from_site = subject.get("from_site")
    quantity = subject.get("quantity")
    if item is None or not condition:
        raise EarmarkChangeInvalid("A bulk lot needs its item and condition.")
    if quantity is None or Decimal(str(quantity)) <= 0:
        raise EarmarkChangeInvalid("Give a quantity greater than zero.")
    quantity = Decimal(str(quantity))
    if item.default_tracking_mode != TrackingMode.BULK:
        raise EarmarkChangeInvalid(
            f"{item.name} is not a bulk item; earmark its units or drums one by one."
        )
    node = StockNode.objects.select_related("location").get(pk=node.pk)
    if not _inside_perimeter(node):
        raise EarmarkChangeInvalid(f"{node.label} is not inside the yard.")
    if from_site is None and to_site is None:
        raise EarmarkChangeInvalid("The quantity is already free; name a site to earmark it for.")
    if from_site is not None and to_site is not None and from_site.pk == to_site.pk:
        raise EarmarkChangeInvalid(f"That quantity is already earmarked for {to_site.name}.")

    # Balances before claims.
    balance = (
        StockBalance.objects.select_for_update()
        .filter(node=node, item_type=item, owner_client=owner, condition=condition)
        .first()
    )
    on_hand = balance.quantity if balance else Decimal("0")

    if from_site is not None:
        claim = (
            _lot_claims(node, item, owner, condition)
            .select_for_update()
            .filter(site=from_site)
            .first()
        )
        if claim is None or quantity > claim.quantity:
            have = claim.quantity if claim else Decimal("0")
            raise EarmarkChangeInvalid(
                f"Only {have.normalize():f} of {item.name} at {node.label} is earmarked for "
                f"{from_site.name}; {quantity.normalize():f} was asked."
            )
        left = claim.quantity - quantity
        if left > 0:
            claim.quantity = left
            claim.save(update_fields=["quantity", "updated_at"])
        else:
            claim.delete()
    else:
        claimed = list(_lot_claims(node, item, owner, condition).select_for_update())
        free = on_hand - sum((c.quantity for c in claimed), Decimal("0"))
        if quantity > free:
            raise EarmarkChangeInvalid(
                f"Only {max(free, Decimal('0')).normalize():f} of {item.name} at {node.label} "
                f"is free; {quantity.normalize():f} was asked."
            )

    if to_site is not None:
        target = (
            _lot_claims(node, item, owner, condition)
            .select_for_update()
            .filter(site=to_site)
            .first()
        )
        if target is None:
            BulkEarmark.objects.create(
                organization_id=node.organization_id,
                site=to_site,
                node=node,
                item_type=item,
                owner_client=owner,
                condition=condition,
                quantity=quantity,
            )
        else:
            target.quantity += quantity
            target.save(update_fields=["quantity", "updated_at"])

    EarmarkEvent.objects.create(
        organization_id=node.organization_id,
        action=EarmarkAction.CHANGED if to_site is not None else EarmarkAction.CLEARED,
        site=from_site,
        to_site=to_site,
        node=node,
        item_type=item,
        owner_client=owner,
        condition=condition,
        quantity=quantity,
        actor=actor,
        reason=reason,
    )
    return {
        "kind": "bulk",
        "subject": _lot_state(node, item, owner, condition),
        "target": item,
        "label": str(item.name),
        "note": (
            f"{quantity.normalize():f} of {item.name} at {node.label} earmark changed from "
            f"{_site_label(from_site)} to {_site_label(to_site)}."
        ),
    }


# --------------------------------------------------------------------------
# T13.5a — what is waiting for a site (Q6)
# --------------------------------------------------------------------------


def earmarked_for_site(site, from_node: StockNode) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """Everything earmarked for ``site`` at ``from_node``, as proposed gate-out lines.

    Same line shape as ``issuable_contents``. A drum is one entry in its line's
    ``reels`` (``{reel, drum_number, length}``); the line's ``requested_qty`` is
    the drums' remaining length. A unit or drum that cannot go is in ``excluded``
    with a ``reason`` and a plain ``message``.
    """
    from dispatch.models import GateOutLineReel, GateOutLineSerial
    from jobs.models import Job, JobStatus
    from stock.boxes import _terminal_statuses

    done = _terminal_statuses()
    units = list(
        SerialUnit.objects.filter(earmark_site=site, current_node=from_node)
        .select_related("item_type")
        .order_by("serial_number", "pk")
    )
    on_pass: dict[int, str] = {}
    for s in (
        GateOutLineSerial.objects.filter(serial_unit_id__in=[u.pk for u in units], released=False)
        .exclude(line__gate_out__status__in=done)
        .select_related("line__gate_out")
        .order_by("pk")
    ):
        on_pass.setdefault(s.serial_unit_id, s.line.gate_out.number)  # type: ignore[attr-defined]

    drums = list(
        Reel.objects.filter(earmark_site=site, current_node=from_node, status=ReelStatus.OPEN)
        .select_related("item_type")
        .order_by("drum_number", "pk")
    )
    drum_pass: dict[int, str] = {}
    for r in (
        GateOutLineReel.objects.filter(reel_id__in=[d.pk for d in drums])
        .exclude(line__gate_out__status__in=done)
        .select_related("line__gate_out")
        .order_by("pk")
    ):
        drum_pass.setdefault(r.reel_id, r.line.gate_out.number)  # type: ignore[attr-defined]

    lines: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    groups: dict[tuple, dict[str, Any]] = {}

    def exclude(kind, obj, ident, reason, message):  # type: ignore[no-untyped-def]
        excluded.append(
            {
                "kind": kind,
                "serial_unit": obj.pk if kind == "unit" else None,
                "reel": obj.pk if kind == "drum" else None,
                "serial_number": ident if kind == "unit" else "",
                "drum_number": ident if kind == "drum" else "",
                "item_type": obj.item_type_id,
                "item_name": obj.item_type.name,
                "reason": reason,
                "message": message,
            }
        )

    def line_for(kind, obj):  # type: ignore[no-untyped-def]
        key = (kind, obj.item_type_id, obj.owner_client_id, obj.condition)
        line = groups.get(key)
        if line is None:
            line = groups[key] = {
                "item_type": obj.item_type_id,
                "item_name": obj.item_type.name,
                "tracking_mode": TrackingMode.SERIALIZED if kind == "unit" else TrackingMode.REEL,
                "uom": obj.item_type.uom,
                "owner_type": obj.owner_type,
                "owner_client": obj.owner_client_id,
                "condition": obj.condition,
                "requested_qty": Decimal("0"),
                "units": [],
                "reels": [],
            }
            lines.append(line)
        return line

    for unit in units:
        sn = unit.serial_number
        label = f"Unit {sn}"
        if unit.status == SerialUnitStatus.QUARANTINED:
            exclude("unit", unit, sn, "QUARANTINED", f"{label} is quarantined.")
        elif unit.status == SerialUnitStatus.IN_CUSTODY:
            exclude("unit", unit, sn, "HELD_BY_PERSON", f"{label} is held by a person.")
        elif unit.status != SerialUnitStatus.IN_STOCK:
            exclude(
                "unit",
                unit,
                sn,
                "NOT_IN_STOCK",
                f"{label} is not in stock ({unit.get_status_display().lower()}).",
            )
        elif unit.pk in on_pass:
            exclude(
                "unit",
                unit,
                sn,
                "ON_ANOTHER_PASS",
                f"{label} is already on gate pass {on_pass[unit.pk]}.",
            )
        else:
            line = line_for("unit", unit)
            line["units"].append({"serial_unit": unit.pk, "serial_number": sn})
            line["requested_qty"] += 1

    for drum in drums:
        if drum.pk in drum_pass:
            exclude(
                "drum",
                drum,
                drum.drum_number,
                "ON_ANOTHER_PASS",
                f"Drum {drum.drum_number} is already on gate pass {drum_pass[drum.pk]}.",
            )
            continue
        line = line_for("drum", drum)
        line["reels"].append(
            {"reel": drum.pk, "drum_number": drum.drum_number, "length": drum.remaining_length}
        )
        line["requested_qty"] += drum.remaining_length

    for claim in (
        BulkEarmark.objects.filter(site=site, node=from_node)
        .select_related("item_type", "owner_client")
        .order_by("item_type__name", "pk")
    ):
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
                "reels": [],
            }
        )

    jobs = [
        {
            "id": j.pk,
            "reference": j.reference,
            "description": j.description,
            "project": j.project_id,
            "project_name": (j.project.title or j.project.reference) if j.project else "",
        }
        for j in Job.objects.filter(site=site, status__in=[JobStatus.OPEN, JobStatus.IN_PROGRESS])
        .select_related("project")
        .order_by("project__reference", "pk")
    ]
    return {"lines": lines, "excluded": excluded, "jobs": jobs}
