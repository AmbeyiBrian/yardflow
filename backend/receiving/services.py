"""Posting and voiding a gate-in (design §4.6; D1–D8, J1, M4, M6).

Posting is the moment a piece of paper becomes stock. Everything happens in one
transaction: allocate the number, create the serial units and drums, post the
movements, update the balances. A gate-in that half-posted would leave the yard
with stock nobody can account for, which is the situation the whole system
exists to end.
"""

from __future__ import annotations

import re
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
from receiving.models import DocumentStatus, GateIn, GateInLine, GateInSerial, GateInSource
from stock.models import Condition, MovementType, OwnerType, Reel, SerialUnit, UnitCostSource
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

    lines = list(gate_in.lines.select_related("item_type", "item_type__category").all())
    if not lines:
        raise GateInNotReady("Add at least one line before posting.")

    settings = gate_in.organization.settings
    field_errors: dict[str, list[str]] = {}

    for index, line in enumerate(lines):
        prefix = f"lines.{index}"

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

    for line in gate_in.lines.select_related("item_type", "item_type__category").all():
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
                        gate_in, line, serial_entry, destination, settings=settings
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


def _create_serial_unit(gate_in, line, entry: GateInSerial, destination, *, settings) -> SerialUnit:
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
        current_node=destination,
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
