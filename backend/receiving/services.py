"""Posting and voiding a gate-in (design §4.6; D1–D8, J1, M4, M6).

Posting is the moment a piece of paper becomes stock. Everything happens in one
transaction: allocate the number, create the serial units and drums, post the
movements, update the balances. A gate-in that half-posted would leave the yard
with stock nobody can account for, which is the situation the whole system
exists to end.
"""

from __future__ import annotations

import re
from dataclasses import replace
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from catalogue.models import TrackingMode
from core.audit import record
from core.exceptions import AlreadyPosted, DomainError
from core.models import AuditAction
from core.numbering import DocumentType, allocate_number
from locations.models import NodeType, StockNode
from locations.nodes import (
    external_node,
    node_for_location,
    node_for_user,
    quarantine_location,
)
from receiving.models import (
    DocumentStatus,
    GateIn,
    GateInBox,
    GateInLine,
    GateInSerial,
    GateInSource,
)
from stock.box_hooks import EventContext
from stock.boxes import MAX_DEPTH, create_box, empty_box, put_bulk, put_units
from stock.models import (
    Box,
    BoxStatus,
    BulkEarmark,
    Condition,
    EarmarkAction,
    EarmarkEvent,
    MovementType,
    OwnerType,
    Reel,
    SerialUnit,
    UnitCostSource,
)
from stock.services import DuplicateSerial, MovementRequest, post_movement


class GateInNotReady(DomainError):
    """The document cannot be posted as it stands."""

    code = "GATE_IN_NOT_READY"
    status_code = 400
    default_message = "This gate-in is not ready to post."


class AttachmentRequired(DomainError):
    """C8: a tenant may make attachments mandatory at gate-in (D6)."""

    code = "ATTACHMENT_REQUIRED"
    status_code = 400
    default_message = "An attachment is required before this can be posted."


# --------------------------------------------------------------------------
# T3.8 — asset tag generation
# --------------------------------------------------------------------------


def _declared_valuation(line: GateInLine) -> dict:
    """The client's figure for a client-owned line, if the note carried one (O11).

    Only for client-owned material. Our own stock is valued from the catalogue,
    which is what ``post_movement`` falls back to — supplying the same number
    here would just be a second place for it to go stale.
    """
    if line.owner_type != OwnerType.CLIENT or line.declared_unit_value is None:
        return {}
    return {
        "unit_cost": line.declared_unit_value,
        "unit_cost_source": UnitCostSource.CLIENT_DECLARED,
    }


def generate_asset_tag(organization, item_type) -> str:
    """Generate an internal asset tag (D3).

    D3: "if a unit has no readable manufacturer serial and asset tags are
    enabled, the system generates an internal asset tag."

    The format comes from ``OrganizationSettings.asset_tag_prefix_format`` (C8),
    e.g. ``SLV-{category}-{seq:06d}``. Unknown placeholders are left alone rather
    than raising: a tenant mistyping their format should get an odd-looking tag,
    not an unpostable delivery with a driver waiting at the gate.
    """
    settings = organization.settings
    template = settings.asset_tag_prefix_format or "{org}-{seq:06d}"

    # The sequence is per tenant and shares the numbering machinery, so tags are
    # gap-free and two simultaneous receipts cannot collide (M6).
    sequence_number = _next_asset_tag_sequence(organization)

    values = {
        "org": (organization.slug or "").upper()[:6],
        "category": _slugify_for_tag(item_type.category.name),
        "item": _slugify_for_tag(item_type.name),
        "seq": sequence_number,
        "year": timezone.now().year,
    }

    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError):
        # A malformed template must not block receiving.
        return f"{values['org']}-{sequence_number:06d}"


def _slugify_for_tag(value: str) -> str:
    """A short, upper-case, punctuation-free fragment for an asset tag."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "", value or "")
    return cleaned.upper()[:6] or "ITEM"


def _next_asset_tag_sequence(organization) -> int:
    """Allocate the next asset tag number for this tenant.

    Uses the same locked-counter mechanism as document numbers, so concurrent
    receipts cannot produce two units with the same tag (M6).
    """
    from core.numbering import DocumentSequence

    sequence, _created = DocumentSequence.objects.get_or_create(
        organization=organization,
        document_type="ASSET_TAG",
        defaults={"next_number": 1},
    )
    locked = DocumentSequence.objects.select_for_update().get(pk=sequence.pk)
    number = locked.next_number
    locked.next_number = number + 1
    locked.save(update_fields=["next_number"])
    return number


# --------------------------------------------------------------------------
# T3.11 — validation before posting
# --------------------------------------------------------------------------


def validate_for_posting(gate_in: GateIn) -> None:
    """Everything that must be true before a gate-in becomes stock.

    Raises a :class:`~core.exceptions.DomainError` carrying ``field_errors``
    keyed by line, so the message lands on the right input in the mobile form
    (§6.1) rather than as one unhelpful banner.
    """
    if gate_in.status == DocumentStatus.POSTED:
        raise AlreadyPosted()
    if gate_in.status == DocumentStatus.VOID:
        raise GateInNotReady("This gate-in has been voided and cannot be posted.")

    lines = list(
        gate_in.lines.select_related("item_type", "item_type__category", "for_site").all()
    )
    if not lines:
        raise GateInNotReady("Add at least one line before posting.")

    settings = gate_in.organization.settings
    field_errors: dict[str, list[str]] = {}

    from network.models import SiteStatus

    header_site = gate_in.for_site
    if header_site is not None and header_site.status != SiteStatus.ACTIVE:
        field_errors["for_site"] = [
            f"{header_site.internal_ref} is decommissioned; material cannot be "
            f"earmarked for it."
        ]

    for index, line in enumerate(lines):
        prefix = f"lines.{index}"

        line_site = line.for_site
        if line_site is not None and line_site.status != SiteStatus.ACTIVE:
            field_errors[f"{prefix}.for_site"] = [
                f"{line_site.internal_ref} is decommissioned; material cannot be "
                f"earmarked for it."
            ]

        # C2: required custom fields are enforced at posting (T3.11).
        try:
            line.item_type.validate_custom_field_values(line.custom_field_values)
        except ValidationError as exc:
            for key, messages in exc.message_dict.items():
                field_errors[f"{prefix}.custom_field_values.{key}"] = list(messages)

        if line.tracking_mode == TrackingMode.SERIALIZED:
            serials = list(line.serials.all())
            # D3: "serialized lines require one identifier per unit."
            if len(serials) != int(line.quantity):
                field_errors[f"{prefix}.serials"] = [
                    f"{int(line.quantity)} unit(s) on this line but "
                    f"{len(serials)} serial number(s) entered. A serialized line "
                    f"needs one identifier per unit."
                ]
            _check_serials_are_new(gate_in, serials, field_errors, prefix)

        elif line.tracking_mode == TrackingMode.REEL:
            reels = list(line.reels.all())
            if not reels:
                field_errors[f"{prefix}.reels"] = [
                    "A reel line needs at least one drum with its length (D4)."
                ]
            total = sum((reel.length for reel in reels), Decimal("0"))
            if reels and total != line.quantity:
                field_errors[f"{prefix}.quantity"] = [
                    f"The drums total {total} {line.uom} but the line says "
                    f"{line.quantity} {line.uom}."
                ]
            _check_drums_are_new(gate_in, reels, field_errors, prefix)

        else:  # BULK
            # D3: a line forced to bulk because no serial was available must say
            # so, otherwise it is indistinguishable from one that was always bulk.
            if line.item_type.default_tracking_mode == TrackingMode.SERIALIZED and not (
                line.no_serial_reason
            ):
                field_errors[f"{prefix}.no_serial_reason"] = [
                    "This item is normally tracked by serial number. Record why "
                    "it is being received as bulk (D3)."
                ]

    _check_boxes(gate_in, lines, field_errors)

    # D6, C8: attachments may be mandatory.
    if settings.attachments_required_gate_in:
        from core.attachments import attachments_for

        if not attachments_for(gate_in).exists():
            raise AttachmentRequired(
                "This organization requires a photo of the delivery, or a scan "
                "of the delivery note, attached to this gate-in before it can "
                "be posted. Add one under “Photos and the delivery note”, "
                "then post again."
            )

    if field_errors:
        raise GateInNotReady("This gate-in cannot be posted yet.", field_errors=field_errors)


def _box_name(box) -> str:
    return box.code or f"{box.key} (code to be generated)"


def _box_depths(boxes_by_key: dict, field_errors: dict, index_of: dict) -> dict[str, int]:
    """Depth of every box whose chain resolves; reports cycles and over-deep trees (P10)."""
    depths: dict[str, int] = {}
    for key, box in boxes_by_key.items():
        chain: list[str] = []
        current = box
        cyclic = False
        while True:
            if current.key in chain:
                cyclic = True
                break
            chain.append(current.key)
            parent = boxes_by_key.get(current.parent_key) if current.parent_key else None
            if parent is None:
                break
            current = parent
        if cyclic:
            field_errors.setdefault(f"boxes.{index_of[key]}.parent_key", []).append(
                f"Box {_box_name(box)} sits inside itself (BOX_CYCLE). Nothing can be "
                f"inside a box that is inside it."
            )
            continue
        depths[key] = len(chain)
        if len(chain) > MAX_DEPTH:
            field_errors.setdefault(f"boxes.{index_of[key]}.parent_key", []).append(
                f"Box {_box_name(box)} would be {len(chain)} deep (BOX_TOO_DEEP). "
                f"Boxes nest {MAX_DEPTH} deep at most."
            )
    return depths


def _check_boxes(gate_in: GateIn, lines: list[GateInLine], field_errors: dict) -> None:
    """P1, P2, P9, P10: the draft's boxes must be sound before they become ``stock.Box``."""
    boxes = list(gate_in.gate_in_boxes.all())
    boxes_by_key: dict[str, GateInBox] = {}
    index_of: dict[str, int] = {}
    seen_codes: set[str] = set()

    for index, box in enumerate(boxes):
        prefix = f"boxes.{index}"
        if not box.key:
            field_errors[f"{prefix}.key"] = ["A box needs a key."]
            continue
        if box.key in boxes_by_key:
            field_errors[f"{prefix}.key"] = [f"Two boxes share the key {box.key}."]
            continue
        boxes_by_key[box.key] = box
        index_of[box.key] = index

        code = box.code.strip()
        if not code:
            continue
        if code.lower() in seen_codes:
            field_errors[f"{prefix}.code"] = [
                f"Box code {code} appears twice on this gate-in (BOX_CODE_IN_USE)."
            ]
            continue
        seen_codes.add(code.lower())
        existing = Box.objects.filter(code__iexact=code).select_related("current_node").first()
        if existing is not None:
            state = "closed" if existing.status == BoxStatus.CLOSED else "open"
            tail = " A closed box's code is never reused." if state == "closed" else ""
            field_errors[f"{prefix}.code"] = [
                f"Box {existing.code} already exists (BOX_CODE_IN_USE): it is {state}, "
                f"at {existing.current_node.label}.{tail}"
            ]

    for key, box in boxes_by_key.items():
        if box.parent_key and box.parent_key not in boxes_by_key:
            field_errors[f"boxes.{index_of[key]}.parent_key"] = [
                f"Box {_box_name(box)} says it is inside {box.parent_key}, which is "
                f"not a box on this gate-in."
            ]

    # What each box holds directly, and the node that lands on.
    held: dict[str, list[StockNode]] = {key: [] for key in boxes_by_key}

    def refer(key: str, error_key: str, node: StockNode, what: str) -> None:
        if not key:
            return
        if key not in boxes_by_key:
            field_errors[error_key] = [
                f"{what} says it is in box {key}, which is not a box on this gate-in."
            ]
            return
        held[key].append(node)

    for line_index, line in enumerate(lines):
        prefix = f"lines.{line_index}"
        if line.tracking_mode == TrackingMode.REEL:
            if line.box_key:
                field_errors[f"{prefix}.box_key"] = [
                    "A drum is not boxed. Receive it outside any box."
                ]
            continue
        node = destination_node_for(gate_in, line)
        if line.tracking_mode == TrackingMode.SERIALIZED:
            if line.box_key:
                field_errors[f"{prefix}.box_key"] = [
                    "On a serialized line, name the box on each serial, not on the line."
                ]
            for serial_index, serial in enumerate(line.serials.all()):
                refer(
                    serial.box_key,
                    f"{prefix}.serials.{serial_index}.box_key",
                    node,
                    f"Serial {serial.serial_number}",
                )
                # A returning unit still in some other box cannot be received.
                unit = _existing_unit_for_return(gate_in, serial)
                if unit is not None and unit.box is not None:
                    field_errors[f"{prefix}.serials.{serial.serial_number}"] = [
                        f"Serial {serial.serial_number} is still in box {unit.box.code}. "
                        f"Take it out of that box before receiving it."
                    ]
        else:
            refer(line.box_key, f"{prefix}.box_key", node, "This line")

    depths = _box_depths(boxes_by_key, field_errors, index_of)

    # Roll each box's contents up to every ancestor.
    below: dict[str, list[StockNode]] = {key: list(nodes) for key, nodes in held.items()}
    for key in boxes_by_key:
        if key not in depths:
            continue
        current = boxes_by_key[key]
        while current.parent_key in boxes_by_key:
            current = boxes_by_key[current.parent_key]
            below[current.key].extend(held[key])

    for key, box in boxes_by_key.items():
        if key not in depths:
            continue
        if not below[key]:
            field_errors.setdefault(f"boxes.{index_of[key]}.code", []).append(
                f"Box {_box_name(box)} holds nothing (BOX_EMPTY). Put something in it or "
                f"remove it."
            )
        elif len({n.pk for n in below[key]}) > 1:
            labels = ", ".join(sorted({n.label for n in below[key]}))
            field_errors.setdefault(f"boxes.{index_of[key]}.code", []).append(
                f"Box {_box_name(box)} would hold stock for different places ({labels}) "
                f"(BOX_MIXED_DESTINATIONS). Serviceable and quarantined stock cannot share "
                f"a box: receive them in separate boxes."
            )


def _check_serials_are_new(gate_in, serials, field_errors, prefix) -> None:
    """D3: reject a duplicate serial, saying where the existing one sits."""
    for serial in serials:
        existing = (
            SerialUnit.objects.filter(serial_number=serial.serial_number)
            .select_related("current_node")
            .first()
        )
        if existing is not None:
            field_errors[f"{prefix}.serials.{serial.serial_number}"] = [
                f"Serial {serial.serial_number} is already recorded and is "
                f"currently at {existing.current_node.label} "
                f"({existing.get_status_display()})."
            ]


def _check_drums_are_new(gate_in, reels, field_errors, prefix) -> None:
    """D4: drum numbers are unique within the tenant."""
    for reel in reels:
        existing = Reel.objects.filter(drum_number=reel.drum_number).first()
        if existing is not None:
            field_errors[f"{prefix}.reels.{reel.drum_number}"] = [
                f"Drum {reel.drum_number} is already recorded, with "
                f"{existing.remaining_length} {existing.uom} remaining at "
                f"{existing.current_node.label}."
            ]


# --------------------------------------------------------------------------
# T3.9 — posting
# --------------------------------------------------------------------------


def source_node_for(gate_in: GateIn) -> StockNode:
    """Where the material came from, as a node (§3.1).

    Every movement is double-entry, so a receipt needs a source node.

    **A recovery comes from the external node, not from its origin site.** That
    looks wrong at first glance and is deliberate. D18 says the system starts
    clean with no data migration, so equipment recovered from a site that was
    built years ago was never recorded as installed there — the site node holds
    nothing, and sourcing from it would ask for stock that never existed and fail
    every recovery.

    The origin site is not lost: it is recorded on the document and on each
    serial unit (D5), which is what answers "recoveries by originating site"
    (M1). The *ledger* records that the material entered our books; the
    *document* records where it came off.

    A **return from site is the one source that is not external**: that material
    is already on our books, sitting on the returning person's custody node. It
    has to come off there, or the technician's record never clears and H4's
    reconciliation reports everything issued and nothing returned.
    """
    if gate_in.source_type == GateInSource.RETURN_FROM_SITE and gate_in.returned_by_id:
        return node_for_user(gate_in.returned_by)

    if gate_in.source_type == GateInSource.CLIENT_ISSUE and gate_in.client_id:
        return external_node(gate_in.organization_id, client=gate_in.client)

    return external_node(gate_in.organization_id)


def destination_node_for(gate_in: GateIn, line: GateInLine) -> StockNode:
    """Where the material lands (D2, J1).

    Faulty, damaged and scrap lines go to quarantine rather than free stock —
    J1's "quarantined stock never appears as available" starts here, at receipt,
    not later. Sending them to the yard first and moving them afterwards would
    leave a window in which they were issuable.
    """
    if line.is_unserviceable:
        yard = gate_in.to_location.yard or gate_in.to_location
        return node_for_location(quarantine_location(gate_in.organization_id, yard))

    return node_for_location(gate_in.to_location)


@transaction.atomic
def post_gate_in(gate_in: GateIn, *, posted_by=None, request=None) -> GateIn:
    """Turn a draft gate-in into stock (D1–D5, D8, J1, M6).

    In one transaction: validate, allocate the number, create serial units and
    drums, post a movement per unit or line, update balances.
    """
    validate_for_posting(gate_in)

    gate_in.number = allocate_number(DocumentType.GATE_IN, organization_id=gate_in.organization_id)
    gate_in.status = DocumentStatus.POSTED
    gate_in.posted_at = timezone.now()
    gate_in.posted_by = posted_by

    source = source_node_for(gate_in)
    settings = gate_in.organization.settings
    # What the loop made or found, so the boxes can be filled without searching.
    box_units: dict[str, list[SerialUnit]] = {}
    box_bulk: dict[str, list[tuple[GateInLine, Decimal]]] = {}
    box_nodes: dict[str, StockNode] = {}
    # Q1: what each line produced, so earmarking happens once the stock is in.
    earmark_units: list[tuple[GateInLine, SerialUnit]] = []
    earmark_reels: list[tuple[GateInLine, Reel]] = []
    earmark_bulk: list[tuple[GateInLine, StockNode]] = []

    for line in gate_in.lines.select_related(
        "item_type", "item_type__category", "for_site"
    ).all():
        destination = destination_node_for(gate_in, line)
        common = {
            "item_type": line.item_type,
            "owner_type": line.owner_type,
            "owner_client": line.owner_client,
            "condition": line.condition,
            "uom": line.uom,
            "occurred_at": gate_in.received_at,
            "posted_by": posted_by,
            "document_type": "receiving.GateIn",
            "document_id": str(gate_in.pk),
            "document_line_id": str(line.pk),
            "document_number": gate_in.number,
            "movement_type": MovementType.RECEIPT,
            "from_node": source,
            "to_node": destination,
            # O11: the client's own figure, where the issue note carried one.
            # Own material is left to the catalogue price, which is what
            # `post_movement` falls back to.
            **_declared_valuation(line),
        }

        if line.tracking_mode == TrackingMode.SERIALIZED:
            for serial_entry in line.serials.all():
                unit = _existing_unit_for_return(gate_in, serial_entry)
                if unit is None:
                    unit = _create_serial_unit(
                        gate_in, line, serial_entry, source, settings=settings
                    )
                    unit_source = source
                else:
                    # It is already ours; it comes back from wherever it is.
                    unit_source = unit.current_node
                post_movement(
                    MovementRequest(
                        **{**common, "from_node": unit_source},
                        quantity=Decimal("1"),
                        tracking_mode=TrackingMode.SERIALIZED,
                        serial_unit=unit,
                        from_condition=unit.condition,
                    )
                )
                earmark_units.append((line, unit))
                if serial_entry.box_key:
                    box_units.setdefault(serial_entry.box_key, []).append(unit)
                    box_nodes[serial_entry.box_key] = destination

        elif line.tracking_mode == TrackingMode.REEL:
            for reel_entry in line.reels.all():
                reel = _create_reel(gate_in, line, reel_entry, source)
                post_movement(
                    MovementRequest(
                        quantity=reel_entry.length,
                        tracking_mode=TrackingMode.REEL,
                        reel=reel,
                        **common,
                    )
                )
                earmark_reels.append((line, reel))

        else:
            held = held_condition_for(gate_in, line)
            for from_node, quantity in _bulk_sources(gate_in, line, source):
                post_movement(
                    MovementRequest(
                        **{**common, "from_node": from_node},
                        quantity=quantity,
                        tracking_mode=TrackingMode.BULK,
                        # An external top-up is not a reclassification: it never
                        # came off anybody's record in another condition.
                        from_condition=(held if from_node.type != NodeType.EXTERNAL else ""),
                    )
                )
            earmark_bulk.append((line, destination))
            if line.box_key:
                box_bulk.setdefault(line.box_key, []).append((line, line.quantity))
                box_nodes[line.box_key] = destination

    _create_boxes(gate_in, box_nodes, box_units, box_bulk, posted_by=posted_by)
    # After the boxes: an earmark is independent of them, so the order only
    # keeps all the projection writes together at the end.
    _earmark_for_sites(gate_in, earmark_units, earmark_reels, earmark_bulk, posted_by=posted_by)

    gate_in.save(update_fields=["number", "status", "posted_at", "posted_by", "updated_at"])

    record(
        AuditAction.DOCUMENT_POSTED,
        actor=posted_by,
        organization=gate_in.organization_id,
        target=gate_in,
        target_label=gate_in.number,
        request=request,
        after={"status": gate_in.status, "number": gate_in.number},
        note=f"Gate-in {gate_in.number} posted ({gate_in.get_source_type_display()}).",
    )

    return gate_in


def _effective_site(gate_in: GateIn, line: GateInLine):
    """Q1: the line's own site, else the delivery's."""
    return line.for_site if line.for_site_id else gate_in.for_site


def _earmark_for_sites(gate_in, units, reels, bulk, *, posted_by) -> None:
    """Earmark what this gate-in received for a site (§4.16.4, Q1).

    Written directly, in the posting transaction, beside the ledger movements
    (§4.16.1). A line with no effective site is left free.
    """
    for line, unit in units:
        site = _effective_site(gate_in, line)
        if site is None:
            continue
        unit.earmark_site = site
        unit.save(update_fields=["earmark_site", "updated_at"])
        _earmark_event(gate_in, posted_by, site=site, serial_unit=unit)

    for line, reel in reels:
        site = _effective_site(gate_in, line)
        if site is None:
            continue
        reel.earmark_site = site
        reel.save(update_fields=["earmark_site", "updated_at"])
        _earmark_event(gate_in, posted_by, site=site, reel=reel, quantity=reel.initial_length)

    for line, node in bulk:
        site = _effective_site(gate_in, line)
        if site is None:
            continue
        claim, created = BulkEarmark.objects.get_or_create(
            organization_id=gate_in.organization_id,
            site=site,
            node=node,
            item_type=line.item_type,
            owner_client=line.owner_client,
            condition=line.condition,
            defaults={"quantity": line.quantity},
        )
        if not created:
            claim.quantity += line.quantity
            claim.save(update_fields=["quantity", "updated_at"])
        _earmark_event(
            gate_in,
            posted_by,
            site=site,
            quantity=line.quantity,
            node=node,
            item_type=line.item_type,
            owner_client=line.owner_client,
            condition=line.condition,
        )


def _earmark_event(gate_in, actor, *, action=EarmarkAction.EARMARKED, reason="", **fields):
    return EarmarkEvent.objects.create(
        organization_id=gate_in.organization_id,
        action=action,
        document_type="receiving.GateIn",
        document_id=str(gate_in.pk),
        document_number=gate_in.number,
        actor=actor,
        reason=reason,
        **fields,
    )


def _clear_earmarks(gate_in: GateIn, *, actor, reason: str) -> None:
    """Undo what this gate-in earmarked, before its movements are reversed (Q1).

    The events are the record of what posting did, so they say what to undo.
    A unit or drum goes back to free only if it is still earmarked for the site
    this gate-in named (someone may have changed it since, Q4). A bulk claim is
    reduced by what this GRN added, never below zero.
    """
    events = list(
        EarmarkEvent.objects.filter(
            document_type="receiving.GateIn",
            document_id=str(gate_in.pk),
            action=EarmarkAction.EARMARKED,
        ).order_by("pk")
    )
    note = f"Void of {gate_in.number}: {reason}"
    for event in events:
        subject = event.serial_unit or event.reel
        if subject is not None:
            if subject.earmark_site_id != event.site_id:
                continue
            subject.earmark_site = None
            subject.save(update_fields=["earmark_site", "updated_at"])
            _earmark_event(
                gate_in,
                actor,
                action=EarmarkAction.CLEARED,
                reason=note,
                site=event.site,
                serial_unit=event.serial_unit,
                reel=event.reel,
                quantity=event.quantity,
            )
            continue

        claim = BulkEarmark.objects.filter(
            site_id=event.site_id,
            node_id=event.node_id,
            item_type_id=event.item_type_id,
            owner_client_id=event.owner_client_id,
            condition=event.condition,
        ).first()
        if claim is None or event.quantity is None:
            continue
        taken = min(claim.quantity, event.quantity)
        if taken >= claim.quantity:
            claim.delete()
            action = EarmarkAction.CLEARED
        else:
            claim.quantity -= taken
            claim.save(update_fields=["quantity", "updated_at"])
            action = EarmarkAction.REDUCED
        _earmark_event(
            gate_in,
            actor,
            action=action,
            reason=note,
            site=event.site,
            quantity=taken,
            node=event.node,
            item_type=event.item_type,
            owner_client=event.owner_client,
            condition=event.condition,
        )


def _box_context(gate_in: GateIn, actor) -> EventContext:
    return EventContext(
        actor=actor,
        occurred_at=gate_in.received_at,
        document_type="receiving.GateIn",
        document_id=str(gate_in.pk),
        document_number=gate_in.number,
    )


def _create_boxes(gate_in, box_nodes, box_units, box_bulk, *, posted_by) -> None:
    """Create the draft's boxes top-down at the node their contents landed on, then
    fill them (P1, P9, P10). Validation has made each tree land on one node."""
    drafts = {box.key: box for box in gate_in.gate_in_boxes.all()}
    if not drafts:
        return

    def depth(box) -> int:
        count = 1
        while box.parent_key:
            box = drafts[box.parent_key]
            count += 1
        return count

    # A box's node is its own contents' node, or failing that its descendants'.
    node_of: dict[str, StockNode] = {}
    for key, node in box_nodes.items():
        current = drafts[key]
        node_of[current.key] = node
        while current.parent_key:
            current = drafts[current.parent_key]
            node_of[current.key] = node

    ctx = _box_context(gate_in, posted_by)
    made: dict[str, Box] = {}
    for draft in sorted(drafts.values(), key=lambda b: (depth(b), b.pk)):
        box = create_box(
            code=draft.code.strip(),
            node=node_of[draft.key],
            parent=made[draft.parent_key] if draft.parent_key else None,
            gate_in=gate_in,
            label_text=draft.label_text,
            ctx=ctx,
        )
        made[draft.key] = box
        if draft.code != box.code:
            draft.code = box.code
            draft.save(update_fields=["code", "updated_at"])

    for key, units in box_units.items():
        put_units(made[key], units, ctx=ctx)
    for key, entries in box_bulk.items():
        for line, quantity in entries:
            put_bulk(
                made[key],
                item_type=line.item_type,
                owner_client=line.owner_client,
                condition=line.condition,
                quantity=quantity,
                ctx=ctx,
            )


def held_condition_for(gate_in: GateIn, line: GateInLine) -> str:
    """The condition the returning material was held in (H3, §3.3).

    A gate-in line's condition is the gate's *assessment*: a jumper issued as new
    comes back used. The holder's record still says new, so that is the balance
    the movement has to come off. Debiting them at the assessed condition instead
    would drive one custody balance negative and strand the other, and their
    record would never clear.
    """
    if gate_in.source_type != GateInSource.RETURN_FROM_SITE:
        return line.condition

    from stock.services import balance_at

    source = source_node_for(gate_in)
    if (
        balance_at(source, line.item_type, owner_client=line.owner_client, condition=line.condition)
        >= line.quantity
    ):
        # They hold it in exactly the condition assessed; nothing was reclassified.
        return line.condition

    for candidate in (Condition.NEW, Condition.USED_SERVICEABLE):
        if candidate == line.condition:
            continue
        if (
            balance_at(source, line.item_type, owner_client=line.owner_client, condition=candidate)
            > 0
        ):
            return candidate

    return line.condition


def _bulk_sources(gate_in, line, source) -> list[tuple[StockNode, Decimal]]:
    """Split one bulk line across the nodes it can honestly come from (H3).

    Only a return from site can be short: the technician's record says they hold
    four, they hand back six. Two bad options exist and both are rejected here —
    failing the receipt turns away material the yard physically has, and posting
    all six off a record holding four drives the custody balance negative.

    So the four come off custody and the two come from external, and
    ``match_return`` raises the variance that makes somebody explain the extra
    (H3: "a difference between declared and actual creates a variance"). The
    ledger stays true to what was on the books, and the discrepancy is a piece of
    open work rather than a silently absorbed number.
    """
    if gate_in.source_type != GateInSource.RETURN_FROM_SITE:
        return [(source, line.quantity)]

    from stock.services import balance_at

    held = balance_at(
        source,
        line.item_type,
        owner_client=line.owner_client,
        condition=held_condition_for(gate_in, line),
    )
    if held >= line.quantity:
        return [(source, line.quantity)]

    sources = []
    if held > 0:
        sources.append((source, held))
    sources.append((external_node(gate_in.organization_id), line.quantity - held))
    return sources


def _existing_unit_for_return(gate_in, entry: GateInSerial) -> SerialUnit | None:
    """The unit a return refers to, if we already have it (D3, H3).

    A serialized unit coming back from site is not a new arrival — it is the same
    unit we issued. Creating a second record for it would trip D3's duplicate
    serial rule and make every serialized return unpostable, so the return moves
    the existing unit instead.

    A serial we have never seen still receives normally: D18 starts the system
    clean, so a technician can genuinely hand back a unit issued before any of
    this existed.
    """
    if gate_in.source_type not in (
        GateInSource.RETURN_FROM_SITE,
        GateInSource.WARRANTY_RETURN,
    ):
        return None

    return SerialUnit.objects.filter(serial_number=entry.serial_number).first()


def _create_serial_unit(gate_in, line, entry: GateInSerial, source, *, settings) -> SerialUnit:
    """Create the tracked unit a serialized line refers to (D3)."""
    if SerialUnit.objects.filter(serial_number=entry.serial_number).exists():
        raise DuplicateSerial(
            f"Serial {entry.serial_number} is already recorded.",
            details={"serial_number": entry.serial_number},
        )

    asset_tag = entry.asset_tag
    if not asset_tag and settings.asset_tag_enabled:
        asset_tag = generate_asset_tag(gate_in.organization, line.item_type)

    return SerialUnit.objects.create(
        organization_id=gate_in.organization_id,
        item_type=line.item_type,
        serial_number=entry.serial_number,
        asset_tag=asset_tag or "",
        source=entry.source,
        # Starts at the source and is moved by the receipt movement itself, as
        # a drum is: the ledger requires a serialized movement to start where
        # the unit is (§4.15.3), and the unit's position then has a movement
        # behind it.
        current_node=source,
        condition=line.condition,
        owner_type=line.owner_type,
        owner_client=line.owner_client,
        # D5: recovered equipment keeps the site it came off.
        origin_site=gate_in.origin_site if gate_in.source_type == GateInSource.RECOVERY else None,
    )


def _create_reel(gate_in, line, entry, source) -> Reel:
    """Create the drum record a reel line refers to (D4)."""
    return Reel.objects.create(
        organization_id=gate_in.organization_id,
        item_type=line.item_type,
        drum_number=entry.drum_number,
        initial_length=entry.length,
        remaining_length=entry.length,
        uom=line.uom,
        # Starts at the source and is moved by the receipt movement itself, so
        # the drum's position always has a movement behind it.
        current_node=source,
        condition=line.condition,
        owner_type=line.owner_type,
        owner_client=line.owner_client,
    )


# --------------------------------------------------------------------------
# T3.10 — void and reversal
# --------------------------------------------------------------------------


@transaction.atomic
def void_gate_in(gate_in: GateIn, *, reason: str, voided_by=None, request=None) -> GateIn:
    """Void a posted gate-in by reversing its movements (M4, M6).

    The document keeps its number and is marked void; the movements are reversed
    rather than deleted. An auditor sees both the original receipt and its
    reversal, which is the point — a receipt that could vanish would make the
    sequence meaningless.
    """
    from stock.models import StockMovement

    if gate_in.status != DocumentStatus.POSTED:
        raise GateInNotReady("Only a posted gate-in can be voided.")
    if not reason:
        raise GateInNotReady("Voiding a posted document requires a reason (M4).")

    # Q1: clear this gate-in's earmarks first, so the reversals below find the
    # units and the bulk lot with nothing claimed on them by this delivery.
    _clear_earmarks(gate_in, actor=voided_by, reason=reason)

    # P5: a box is not stock, so it is emptied and closed before the units go.
    # Deepest first, so a parent is never asked to close around an open child.
    ctx = replace(_box_context(gate_in, voided_by), note=f"Void of {gate_in.number}: {reason}")
    for box_id in list(
        Box.objects.filter(gate_in=gate_in, status=BoxStatus.OPEN)
        .order_by("-depth", "pk")
        .values_list("pk", flat=True)
    ):
        box = Box.objects.get(pk=box_id)
        if box.status == BoxStatus.OPEN:
            empty_box(box, ctx=ctx)

    movements = StockMovement.objects.filter(
        document_type="receiving.GateIn", document_id=str(gate_in.pk)
    ).exclude(movement_type=MovementType.REVERSAL)

    for movement in movements:
        if movement.reversals.exists():
            continue

        # A reel is handled here rather than in `reverse_movement`: returning
        # length to a drum is the inverse of consuming it, and the generic
        # helper deliberately does not guess.
        post_movement(
            MovementRequest(
                item_type=movement.item_type,
                quantity=movement.quantity,
                from_node=movement.to_node,
                to_node=movement.from_node,
                movement_type=MovementType.REVERSAL,
                owner_type=movement.owner_type,
                owner_client=movement.owner_client,
                condition=movement.condition,
                tracking_mode=movement.tracking_mode,
                uom=movement.uom,
                serial_unit=movement.serial_unit,
                reel=movement.reel,
                posted_by=voided_by,
                document_type=movement.document_type,
                document_id=movement.document_id,
                document_line_id=movement.document_line_id,
                document_number=gate_in.number,
                reversal_of=movement,
                note=f"Void of {gate_in.number}: {reason}",
            )
        )

    gate_in.status = DocumentStatus.VOID
    gate_in.void_reason = reason
    gate_in.voided_at = timezone.now()
    gate_in.voided_by = voided_by
    gate_in.save(update_fields=["status", "void_reason", "voided_at", "voided_by", "updated_at"])

    record(
        AuditAction.DOCUMENT_VOIDED,
        actor=voided_by,
        organization=gate_in.organization_id,
        target=gate_in,
        target_label=gate_in.number,
        request=request,
        before={"status": DocumentStatus.POSTED},
        after={"status": DocumentStatus.VOID},
        note=reason,
    )

    return gate_in


def can_amend(gate_in: GateIn) -> bool:
    """M4: amendment of posted documents is off unless a tenant enables it.

    Default off. Corrections are made by reversal and re-entry — which leaves
    both entries visible, where an amendment would quietly replace history.
    """
    if gate_in.status == DocumentStatus.DRAFT:
        return True
    return bool(gate_in.organization.settings.allow_document_amendment)
