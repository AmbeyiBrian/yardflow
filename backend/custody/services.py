"""Custody, overdue chasing and handovers (design §4.10; I1–I5).

The overdue sweep is the part that actually stops tools disappearing (I3). Two
details make it work rather than become noise:

* **each escalation stage fires once**, not once per nightly run — otherwise
  people learn to ignore it
* **the chain is holder, then storekeeper, then owner** (Q6's answer, since there
  is no supervisor role in the model)
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from core.audit import record
from core.exceptions import DomainError
from core.models import AuditAction
from core.numbering import DocumentType, allocate_number
from custody.models import (
    CustodyExpectation,
    CustodyTransfer,
    ExpectationStatus,
    TransferStatus,
)
from locations.nodes import node_for_user
from stock.models import Condition, MovementType, OwnerType
from stock.services import MovementRequest, post_movement


class TransferNotReady(DomainError):
    code = "TRANSFER_NOT_READY"
    status_code = 400
    default_message = "This handover cannot be completed."


class HolderStillHasMaterial(DomainError):
    """B3's edge case: a user holding custody cannot simply be deactivated.

    "Deactivating a user holding items in custody must warn and require the
    custody to be reassigned or returned first." Deactivating them anyway would
    orphan the material — it would still be somewhere, but nobody would be
    accountable for it.
    """

    code = "HOLDER_STILL_HAS_MATERIAL"
    status_code = 400
    default_message = (
        "This person is still holding material. Have it returned or handed over "
        "before deactivating them."
    )


# --------------------------------------------------------------------------
# T5.8 — the overdue sweep
# --------------------------------------------------------------------------


def mark_overdue(organization_id, *, today=None) -> int:
    """Flag expectations past their return date (I3)."""
    today = today or timezone.now().date()

    overdue = CustodyExpectation.objects.filter(
        organization_id=organization_id,
        status=ExpectationStatus.OPEN,
        expected_return_date__lt=today,
    )
    return overdue.update(status=ExpectationStatus.OVERDUE)


def escalate_overdue(organization_id, *, today=None, escalate_after_days: int = 3) -> dict:
    """Chase overdue items, one stage at a time (I3).

    I3: "escalating reminders: to the holder, then to their supervisor, then to
    the owner."

    Each stage is recorded on the expectation, so a stage fires **once** rather
    than every night. A nightly duplicate reminder is how a control becomes
    background noise, and then nobody reads any of them.
    """
    today = today or timezone.now().date()
    now = timezone.now()

    counts = {"holder": 0, "supervisor": 0, "owner": 0}

    overdue = CustodyExpectation.objects.filter(
        organization_id=organization_id, status=ExpectationStatus.OVERDUE
    ).select_related("holder", "item_type")

    for expectation in overdue:
        days_late = (today - expectation.expected_return_date).days

        if expectation.reminded_holder_at is None:
            _notify_overdue(expectation, stage="holder")
            expectation.reminded_holder_at = now
            expectation.save(update_fields=["reminded_holder_at", "updated_at"])
            counts["holder"] += 1
            continue

        if (
            expectation.reminded_supervisor_at is None
            and days_late >= escalate_after_days
        ):
            _notify_overdue(expectation, stage="supervisor")
            expectation.reminded_supervisor_at = now
            expectation.save(update_fields=["reminded_supervisor_at", "updated_at"])
            counts["supervisor"] += 1
            continue

        if (
            expectation.reminded_owner_at is None
            and days_late >= escalate_after_days * 2
        ):
            _notify_overdue(expectation, stage="owner")
            expectation.reminded_owner_at = now
            expectation.save(update_fields=["reminded_owner_at", "updated_at"])
            counts["owner"] += 1

    return counts


def _notify_overdue(expectation: CustodyExpectation, *, stage: str) -> None:
    try:
        from notifications.events import emit
        from notifications.matrix import Event

        emit(
            Event.ITEM_OVERDUE,
            expectation,
            payload={
                "label": str(expectation.item_type),
                "holder": str(expectation.holder),
                "quantity": str(expectation.outstanding_quantity),
                "due": str(expectation.expected_return_date),
                "stage": stage,
            },
        )
    except ImportError:  # pragma: no cover
        pass


def overdue_report(organization_id, *, today=None) -> dict:
    """I4: overdue items by person and by item, so an owner can act.

    Both groupings, because they answer different questions: "who keeps doing
    this?" and "what do we keep losing?".
    """
    today = today or timezone.now().date()

    overdue = CustodyExpectation.objects.filter(
        organization_id=organization_id,
        status__in=(ExpectationStatus.OPEN, ExpectationStatus.OVERDUE),
        expected_return_date__lt=today,
    ).select_related("holder", "item_type")

    by_person: dict[int, dict] = {}
    by_item: dict[int, dict] = {}

    for expectation in overdue:
        person = by_person.setdefault(
            expectation.holder_id,
            {
                # The id travels with the row so the overdue screen can open
                # that person's holdings — "who is overdue" and "what are they
                # holding" are one question asked twice (I4, T5.11).
                "holder_id": expectation.holder_id,
                "holder": str(expectation.holder),
                "items": 0,
                "quantity": Decimal("0"),
                "days_overdue": 0,
            },
        )
        person["items"] += 1
        person["quantity"] += expectation.outstanding_quantity
        # The worst line, not the average: an owner chasing a person wants to
        # know how bad it has got, and a mean would hide a six-week item behind
        # five recent ones.
        person["days_overdue"] = max(
            person["days_overdue"], (today - expectation.expected_return_date).days
        )

        item = by_item.setdefault(
            expectation.item_type_id,
            {
                "item_type_id": expectation.item_type_id,
                "item": str(expectation.item_type),
                "holders": 0,
                "quantity": Decimal("0"),
                "uom": expectation.item_type.uom,
            },
        )
        item["holders"] += 1
        item["quantity"] += expectation.outstanding_quantity

    return {
        "as_at": today,
        "by_person": sorted(by_person.values(), key=lambda row: -row["quantity"]),
        "by_item": sorted(by_item.values(), key=lambda row: -row["quantity"]),
        "total": len(overdue),
    }


# --------------------------------------------------------------------------
# T5.9 — handovers
# --------------------------------------------------------------------------


@transaction.atomic
def acknowledge_transfer(
    transfer: CustodyTransfer, *, actor, request=None
) -> CustodyTransfer:
    """Accept a handover, and only then move the material (I5).

    I5: "requires acknowledgement by the receiving person." Posting on request
    instead would let one technician push material onto another's record without
    their knowledge — which is exactly how a tool goes missing while each of them
    believes the other has it.
    """
    if transfer.status != TransferStatus.PENDING:
        raise TransferNotReady(
            f"This handover is already {transfer.get_status_display().lower()}."
        )
    if actor.pk != transfer.to_holder_id:
        raise TransferNotReady(
            "Only the person receiving the material can acknowledge a handover (I5)."
        )

    source = node_for_user(transfer.from_holder)
    destination = node_for_user(transfer.to_holder)

    if not transfer.number:
        transfer.number = allocate_number(
            DocumentType.CUSTODY_TRANSFER, organization_id=transfer.organization_id
        )

    for line in transfer.lines.select_related("item_type", "serial_unit", "reel").all():
        post_movement(
            MovementRequest(
                item_type=line.item_type,
                quantity=line.quantity,
                from_node=source,
                to_node=destination,
                movement_type=MovementType.TRANSFER,
                owner_type=(
                    OwnerType.CLIENT
                    if getattr(line.serial_unit, "owner_client_id", None)
                    else OwnerType.OWN
                ),
                owner_client=getattr(line.serial_unit, "owner_client", None),
                condition=line.condition or Condition.NEW,
                tracking_mode=(
                    "SERIALIZED"
                    if line.serial_unit_id
                    else "REEL"
                    if line.reel_id
                    else line.item_type.default_tracking_mode
                ),
                uom=line.uom,
                serial_unit=line.serial_unit,
                reel=line.reel,
                posted_by=actor,
                document_type="custody.CustodyTransfer",
                document_id=str(transfer.pk),
                document_line_id=str(line.pk),
                document_number=transfer.number,
                note=f"Handover from {transfer.from_holder} to {transfer.to_holder}",
            )
        )

        # Any expectation for this material follows the person who now holds it,
        # so the overdue chase goes to the right phone (I2, I3).
        CustodyExpectation.objects.filter(
            holder=transfer.from_holder,
            item_type=line.item_type,
            status__in=(ExpectationStatus.OPEN, ExpectationStatus.OVERDUE),
        ).update(holder=transfer.to_holder)

    transfer.status = TransferStatus.ACKNOWLEDGED
    transfer.acknowledged_at = timezone.now()
    transfer.save(update_fields=["number", "status", "acknowledged_at", "updated_at"])

    record(
        AuditAction.CUSTODY_TRANSFERRED,
        actor=actor,
        organization=transfer.organization_id,
        target=transfer,
        target_label=transfer.number,
        request=request,
        note=(
            f"Custody handover {transfer.number} acknowledged: "
            f"{transfer.from_holder} -> {transfer.to_holder}."
        ),
    )

    return transfer


@transaction.atomic
def decline_transfer(
    transfer: CustodyTransfer, *, actor, reason: str, request=None
) -> CustodyTransfer:
    """Refuse a handover (I5).

    Nothing moves. The material stays with the original holder, which is the
    truthful outcome — and the refusal is recorded, because "I never took it" is
    exactly the dispute this exists to settle.
    """
    if transfer.status != TransferStatus.PENDING:
        raise TransferNotReady("That handover is no longer pending.")
    if not reason:
        raise TransferNotReady("Declining a handover requires a reason.")

    transfer.status = TransferStatus.DECLINED
    transfer.declined_reason = reason
    transfer.save(update_fields=["status", "declined_reason", "updated_at"])

    record(
        AuditAction.STATUS_CHANGED,
        actor=actor,
        organization=transfer.organization_id,
        target=transfer,
        target_label=str(transfer),
        request=request,
        note=f"Handover declined by {actor}: {reason}",
    )

    return transfer


# --------------------------------------------------------------------------
# B3's edge case — deactivating a holder
# --------------------------------------------------------------------------


def holdings_of(user) -> list[dict]:
    """What one person currently holds (I1).

    Read from the ledger's PERSON node, not from a separate custody table — which
    is why this and the stock screens can never disagree (§4.10).
    """
    from stock.queries import custody_holdings

    return [
        {
            "item": str(balance.item_type),
            "quantity": balance.quantity,
            "uom": balance.uom,
            "owner": str(balance.owner_client) if balance.owner_client_id else "Own stock",
        }
        for balance in custody_holdings(user=user)
    ]


def assert_can_deactivate(user) -> None:
    """B3's edge case: refuse to deactivate someone still holding material."""
    holdings = holdings_of(user)
    if holdings:
        raise HolderStillHasMaterial(
            f"{user} is still holding {len(holdings)} item(s). Have the material "
            f"returned or handed over to someone else first (B3, I5).",
            details={"holdings": [
                {**holding, "quantity": str(holding["quantity"])} for holding in holdings
            ]},
        )
