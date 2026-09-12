"""Disposition and disposal (design §4.11, §3.2; J1, J2, J3, K3).

Each of the four dispositions is a movement out of quarantine, and *which*
movement is the whole of J2:

* ``RESTORE_TO_SERVICEABLE`` — quarantine → a yard location, **and the condition
  changes**. That condition change is what puts it back in free stock, because
  ``stock_on_hand`` excludes quarantine locations rather than faulty conditions
  (§3.3). Restoring without it would move a faulty radio into the yard and offer
  it to the next technician.
* ``REPAIR`` — quarantine → the repair vendor's EXTERNAL node. It leaves the
  yard but stays ours, so it is still on the books and still chaseable (I3).
* ``RETURN_TO_CLIENT`` — nothing moves here. The material goes back on a gate-out
  (K1), which is the document the client signs for; this disposition records the
  decision that it should.
* ``SCRAP`` — nothing moves here either. Scrap is destroyed by a **Disposal**,
  which is approved separately (J3). A disposition that scrapped material
  outright would write it off on the authority of a triage note.

T6.1's criterion — "restoring to serviceable returns stock to availability and
scrapping does not" — is exactly the difference between the first and the last.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from approvals.engine import (
    create_requests,
    record_auto_approval,
    record_decision,
    required_levels,
    requires_approval_regardless,
)
from approvals.models import ApprovalDecision
from core.audit import record
from core.exceptions import DomainError
from core.models import AuditAction, AuthMethod
from core.numbering import DocumentType, allocate_number
from disposition.models import (
    Disposal,
    DisposalStatus,
    Disposition,
    DispositionDecision,
    DispositionStatus,
)
from locations.models import LocationType
from locations.nodes import (
    external_node,
    node_for_client,
    node_for_location,
    scrap_node,
)
from stock.models import Condition, MovementType, OwnerType
from stock.services import MovementRequest, post_movement


class DispositionNotReady(DomainError):
    code = "DISPOSITION_NOT_READY"
    status_code = 409
    default_message = "This disposition cannot be actioned yet."


class DisposalNotReady(DomainError):
    code = "DISPOSAL_NOT_READY"
    status_code = 409
    default_message = "This disposal cannot be actioned yet."


class NotFromQuarantine(DomainError):
    code = "NOT_FROM_QUARANTINE"
    default_message = "A disposition takes material out of quarantine."


#: What a restored unit becomes when the line does not say (J2).
#:
#: Serviceable, not NEW: a radio that came back faulty and was repaired is not
#: new again, and recording it as new would quietly launder condition history.
DEFAULT_RESTORED_CONDITION = Condition.USED_SERVICEABLE


# --------------------------------------------------------------------------
# T6.1 — dispositions
# --------------------------------------------------------------------------


def _assert_from_quarantine(document) -> None:
    if document.from_location.type != LocationType.QUARANTINE:
        raise NotFromQuarantine(
            f"{document.from_location.name} is not a quarantine location. A "
            f"disposition decides what happens to quarantined material (J1, J2).",
            details={"from_location": document.from_location.name},
        )


@transaction.atomic
def submit_disposition(disposition: Disposition, *, submitted_by=None, request=None) -> Disposition:
    """Route the decision for approval, or auto-approve it (J2, §5.2).

    J2 says each outcome is an "explicit, **approved** decision". Which approvals
    apply comes from the same engine that routes a gate-out, so a tenant's rule
    about high-criticality material covers scrapping it as well as issuing it —
    and client-owned material escalates whatever the rules say (§5.2).
    """
    if disposition.status not in (DispositionStatus.DRAFT, DispositionStatus.REJECTED):
        raise DispositionNotReady(
            f"A disposition in status {disposition.get_status_display()} cannot "
            f"be submitted."
        )
    if not disposition.lines.exists():
        raise DispositionNotReady("Add at least one line before submitting.")

    _assert_from_quarantine(disposition)

    if not disposition.number:
        disposition.number = allocate_number(
            DocumentType.DISPOSITION, organization_id=disposition.organization_id
        )

    forced, why = requires_approval_regardless(disposition)
    levels = required_levels(disposition)

    if levels or forced:
        disposition.status = DispositionStatus.PENDING_APPROVAL
        if levels:
            create_requests(disposition, requested_by=submitted_by)
            note = f"Submitted for approval: {len(levels)} level(s)."
        else:
            # §5.2 forces a sign-off that no rule asked for. Without a level to
            # route to there would be nobody to ask, so the fallback is the
            # highest-criticality rule set — see `_forced_requests`.
            _forced_requests(disposition, requested_by=submitted_by)
            note = why
    else:
        disposition.status = DispositionStatus.APPROVED
        record_auto_approval(disposition)
        note = "Auto-approved: no approval rule applies."

    disposition.save(update_fields=["number", "status", "updated_at"])

    record(
        AuditAction.STATUS_CHANGED,
        actor=submitted_by,
        organization=disposition.organization_id,
        target=disposition,
        target_label=disposition.number,
        request=request,
        after={"status": disposition.status},
        note=f"{disposition.get_decision_display()}: {note}",
    )
    return disposition


def _forced_requests(document, *, requested_by=None) -> None:
    """Create a request for §5.2's hardcoded escalation when no rule matched.

    The escalation says approval is required; it does not say by whom. Asking the
    role that approves the tenant's most critical material is the closest thing
    to the intent — and if a tenant has configured no rules at all, the request
    is created with no required role, which `can_approve` answers by letting
    anyone holding the approval permission decide. That is a deliberate choice:
    an unapprovable document would strand the material instead of controlling it.
    """
    from accounts.models import Role
    from approvals.models import ApprovalRequest, ApprovalRule
    from catalogue.models import Criticality

    rule = (
        ApprovalRule.objects.filter(is_active=True, criticality=Criticality.HIGH)
        .select_related("required_role")
        .order_by("sequence")
        .first()
    )
    role: Role | None = rule.required_role if rule is not None else None

    settings = document.organization.settings
    from datetime import timedelta

    ApprovalRequest.objects.create(
        organization_id=document.organization_id,
        document_type=document._meta.label,
        document_id=str(document.pk),
        document_number=getattr(document, "number", "") or "",
        level=1,
        required_role=role,
        requested_by=requested_by,
        due_at=timezone.now()
        + timedelta(hours=settings.approval_escalation_hours or 24),
    )


@transaction.atomic
def approve_disposition(
    disposition: Disposition,
    *,
    actor,
    reason: str = "",
    auth_method: str = AuthMethod.PASSWORD,
    request=None,
) -> Disposition:
    """Approve the next outstanding level (J2, §5.3)."""
    if disposition.status != DispositionStatus.PENDING_APPROVAL:
        raise DispositionNotReady("This disposition is not awaiting approval.")

    _decided, remaining = record_decision(
        disposition,
        actor=actor,
        decision=ApprovalDecision.APPROVED,
        reason=reason,
        auth_method=auth_method,
    )

    if remaining is None:
        disposition.status = DispositionStatus.APPROVED
        disposition.decided_by = actor
        disposition.save(update_fields=["status", "decided_by", "updated_at"])

    record(
        AuditAction.APPROVED,
        actor=actor,
        organization=disposition.organization_id,
        target=disposition,
        target_label=disposition.number,
        request=request,
        auth_method=auth_method,
        after={"status": disposition.status},
    )
    return disposition


@transaction.atomic
def reject_disposition(
    disposition: Disposition,
    *,
    actor,
    reason: str,
    auth_method: str = AuthMethod.PASSWORD,
    request=None,
) -> Disposition:
    """Refuse the decision. The material stays in quarantine (J2)."""
    if not reason:
        raise DispositionNotReady("Rejecting a disposition requires a reason.")
    if disposition.status != DispositionStatus.PENDING_APPROVAL:
        raise DispositionNotReady("This disposition is not awaiting approval.")

    record_decision(
        disposition,
        actor=actor,
        decision=ApprovalDecision.REJECTED,
        reason=reason,
        auth_method=auth_method,
    )

    disposition.status = DispositionStatus.REJECTED
    disposition.reject_reason = reason
    disposition.save(update_fields=["status", "reject_reason", "updated_at"])

    record(
        AuditAction.REJECTED,
        actor=actor,
        organization=disposition.organization_id,
        target=disposition,
        target_label=disposition.number,
        request=request,
        auth_method=auth_method,
        note=reason,
    )
    return disposition


@transaction.atomic
def post_disposition(disposition: Disposition, *, posted_by=None, request=None) -> Disposition:
    """Move the material where the decision says it goes (J2, §3.2).

    Only after approval, and only once — the movements are irreversible by
    design (§3.2 refuses UPDATE and DELETE on the ledger), so a second posting
    would double every one of them.
    """
    if disposition.status != DispositionStatus.APPROVED:
        raise DispositionNotReady(
            f"A disposition in status {disposition.get_status_display()} cannot "
            f"be posted. It has to be approved first (J2)."
        )

    lines = list(
        disposition.lines.select_related(
            "item_type", "serial_unit", "reel", "owner_client"
        )
    )
    if not lines:
        raise DispositionNotReady("This disposition has no lines.")

    source = node_for_location(disposition.from_location)

    for line in lines:
        destination = _destination_node(disposition, line)
        if destination is None:
            # RETURN_TO_CLIENT and SCRAP move nothing here. The material stays
            # in quarantine until its own document takes it out, which is what
            # keeps "decided" and "gone" distinguishable.
            continue

        to_condition = line.to_condition or line.condition
        post_movement(
            MovementRequest(
                item_type=line.item_type,
                quantity=line.quantity,
                from_node=source,
                to_node=destination,
                movement_type=_movement_type(disposition.decision),
                owner_type=line.owner_type,
                owner_client=line.owner_client,
                condition=to_condition,
                # The condition it is coming off, so the faulty balance clears
                # rather than going negative while a serviceable one appears.
                from_condition=line.condition if to_condition != line.condition else "",
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
                posted_by=posted_by,
                document_type=disposition._meta.label,
                document_id=str(disposition.pk),
                document_line_id=str(line.pk),
                document_number=disposition.number,
                note=f"{disposition.get_decision_display()}: {disposition.reason}",
            )
        )

    disposition.status = DispositionStatus.POSTED
    disposition.posted_at = timezone.now()
    disposition.posted_by = posted_by
    disposition.save(update_fields=["status", "posted_at", "posted_by", "updated_at"])

    record(
        AuditAction.DOCUMENT_POSTED,
        actor=posted_by,
        organization=disposition.organization_id,
        target=disposition,
        target_label=disposition.number,
        request=request,
        after={"status": disposition.status},
        note=(
            f"{disposition.get_decision_display()} posted for "
            f"{len(lines)} line(s): {disposition.reason}"
        ),
    )

    _emit(disposition, "disposition.posted")
    return disposition


def _destination_node(disposition: Disposition, line):
    """Where this line goes, or ``None`` when nothing moves yet (J2)."""
    if disposition.decision == DispositionDecision.RESTORE_TO_SERVICEABLE:
        # `clean()` guarantees a destination for this decision, so the material
        # can never leave quarantine for nowhere.
        assert disposition.to_location is not None
        return node_for_location(disposition.to_location)
    if disposition.decision == DispositionDecision.REPAIR:
        # Out of the yard but still ours: an EXTERNAL node, which is where
        # anything outside the perimeter sits (§3.1).
        return external_node(disposition.organization_id)
    return None


def _movement_type(decision: str) -> str:
    if decision == DispositionDecision.RESTORE_TO_SERVICEABLE:
        return MovementType.RESTORE
    return MovementType.TRANSFER


def restored_condition_for(line) -> str:
    """What a restored line becomes, if the caller did not say (J2)."""
    return line.to_condition or DEFAULT_RESTORED_CONDITION


# --------------------------------------------------------------------------
# T6.2 — disposals
# --------------------------------------------------------------------------


@transaction.atomic
def submit_disposal(disposal: Disposal, *, submitted_by=None, request=None) -> Disposal:
    """Route a write-off for approval (J3, §5.2).

    There is no auto-approval path for client-owned material, and that is not a
    configuration: `requires_approval_regardless` is consulted *before* the
    rules, so a tenant with no approval rules at all still cannot write off an
    operator's property unilaterally.
    """
    if disposal.status not in (DisposalStatus.DRAFT, DisposalStatus.REJECTED):
        raise DisposalNotReady(
            f"A disposal in status {disposal.get_status_display()} cannot be submitted."
        )
    if not disposal.lines.exists():
        raise DisposalNotReady("Add at least one line before submitting.")

    if not disposal.number:
        disposal.number = allocate_number(
            DocumentType.DISPOSAL, organization_id=disposal.organization_id
        )

    disposal.submitted_at = timezone.now()
    disposal.requested_by = disposal.requested_by or submitted_by

    forced, why = requires_approval_regardless(disposal)
    levels = required_levels(disposal)

    if levels or forced:
        disposal.status = DisposalStatus.PENDING_APPROVAL
        if levels:
            create_requests(disposal, requested_by=submitted_by)
            note = f"Submitted for approval: {len(levels)} level(s)."
        else:
            _forced_requests(disposal, requested_by=submitted_by)
            note = why
    else:
        disposal.status = DisposalStatus.APPROVED
        disposal.approved_at = timezone.now()
        record_auto_approval(disposal)
        note = "Auto-approved: no approval rule applies."

    disposal.save(
        update_fields=[
            "number",
            "status",
            "submitted_at",
            "approved_at",
            "requested_by",
            "updated_at",
        ]
    )

    record(
        AuditAction.STATUS_CHANGED,
        actor=submitted_by,
        organization=disposal.organization_id,
        target=disposal,
        target_label=disposal.number,
        request=request,
        after={"status": disposal.status},
        note=note,
    )
    _emit(disposal, "disposal.awaiting_approval")
    return disposal


@transaction.atomic
def approve_disposal(
    disposal: Disposal,
    *,
    actor,
    reason: str = "",
    auth_method: str = AuthMethod.PASSWORD,
    request=None,
) -> Disposal:
    """Authorise the write-off (J3, §5.3)."""
    if disposal.status != DisposalStatus.PENDING_APPROVAL:
        raise DisposalNotReady("This disposal is not awaiting approval.")

    _decided, remaining = record_decision(
        disposal,
        actor=actor,
        decision=ApprovalDecision.APPROVED,
        reason=reason,
        auth_method=auth_method,
    )

    if remaining is None:
        disposal.status = DisposalStatus.APPROVED
        disposal.approved_at = timezone.now()
        disposal.save(update_fields=["status", "approved_at", "updated_at"])
        _emit(disposal, "disposal.approved")

    record(
        AuditAction.APPROVED,
        actor=actor,
        organization=disposal.organization_id,
        target=disposal,
        target_label=disposal.number,
        request=request,
        auth_method=auth_method,
        after={"status": disposal.status},
    )
    return disposal


@transaction.atomic
def reject_disposal(
    disposal: Disposal,
    *,
    actor,
    reason: str,
    auth_method: str = AuthMethod.PASSWORD,
    request=None,
) -> Disposal:
    if not reason:
        raise DisposalNotReady("Rejecting a disposal requires a reason.")
    if disposal.status != DisposalStatus.PENDING_APPROVAL:
        raise DisposalNotReady("This disposal is not awaiting approval.")

    record_decision(
        disposal,
        actor=actor,
        decision=ApprovalDecision.REJECTED,
        reason=reason,
        auth_method=auth_method,
    )

    disposal.status = DisposalStatus.REJECTED
    disposal.reject_reason = reason
    disposal.save(update_fields=["status", "reject_reason", "updated_at"])

    record(
        AuditAction.REJECTED,
        actor=actor,
        organization=disposal.organization_id,
        target=disposal,
        target_label=disposal.number,
        request=request,
        auth_method=auth_method,
        note=reason,
    )
    return disposal


@transaction.atomic
def post_disposal(disposal: Disposal, *, posted_by=None, request=None) -> Disposal:
    """Write the material off: quarantine → SCRAP (J3, §3.1).

    The SCRAP node is the one place material goes and does not come back. That
    is why the approval is not optional and why the movement carries the disposal
    number: a write-off has to be explicable years later, from the ledger alone.
    """
    if disposal.status != DisposalStatus.APPROVED:
        raise DisposalNotReady(
            f"A disposal in status {disposal.get_status_display()} cannot be "
            f"posted. Disposal requires approval (J3)."
        )

    lines = list(
        disposal.lines.select_related("item_type", "serial_unit", "reel", "owner_client")
    )
    if not lines:
        raise DisposalNotReady("This disposal has no lines.")

    source = node_for_location(disposal.from_location)
    destination = scrap_node(disposal.organization_id)

    for line in lines:
        post_movement(
            MovementRequest(
                item_type=line.item_type,
                quantity=line.quantity,
                from_node=source,
                to_node=destination,
                movement_type=MovementType.DISPOSE,
                owner_type=line.owner_type,
                owner_client=line.owner_client,
                condition=line.condition,
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
                posted_by=posted_by,
                document_type=disposal._meta.label,
                document_id=str(disposal.pk),
                document_line_id=str(line.pk),
                document_number=disposal.number,
                note=f"Disposed of: {disposal.get_method_display()}. {disposal.notes}".strip(),
            )
        )

    disposal.status = DisposalStatus.DISPOSED
    disposal.disposed_at = timezone.now()
    disposal.disposed_by = posted_by
    disposal.save(update_fields=["status", "disposed_at", "disposed_by", "updated_at"])

    record(
        AuditAction.DOCUMENT_POSTED,
        actor=posted_by,
        organization=disposal.organization_id,
        target=disposal,
        target_label=disposal.number,
        request=request,
        after={"status": disposal.status},
        note=(
            f"{len(lines)} line(s) disposed of by "
            f"{disposal.get_method_display()}"
            + (f", reference {disposal.handler_reference}" if disposal.handler_reference else "")
        ),
    )
    _emit(disposal, "disposal.completed")
    return disposal


def written_off_value(disposal: Disposal) -> Decimal | None:
    """What the write-off cost, when the tenant tracks money (C8, §4.14).

    ``None`` rather than zero when money tracking is off: a certificate that
    printed "KES 0.00" would be read as free, and the honest answer is that the
    system was never told.
    """
    if not disposal.organization.settings.money_tracking_enabled:
        return None

    total = Decimal("0")
    for line in disposal.lines.select_related("item_type"):
        if line.written_off_value is not None:
            total += line.written_off_value
        elif line.item_type.unit_cost is not None:
            total += line.item_type.unit_cost * line.quantity
    return total


# --------------------------------------------------------------------------
# T6.4 — client return acknowledgement
# --------------------------------------------------------------------------


class ReturnNotAcknowledgeable(DomainError):
    code = "RETURN_NOT_ACKNOWLEDGEABLE"
    status_code = 409
    default_message = "This gate pass is not a client return that can be acknowledged."


@transaction.atomic
def acknowledge_client_return(
    gate_out,
    *,
    acknowledged_ref: str,
    acknowledged_at=None,
    acknowledged_by_name: str = "",
    notes: str = "",
    recorded_by=None,
    request=None,
):
    """Record that the client has the material (K3, §4.12).

    "Our liability for that material ends on the record" — so this is a document,
    not a flag. Nothing moves in the ledger: the material is already at the
    CLIENT node from the release (K1), and what changes is that it stops counting
    as in-transit exposure in the client position report.

    Only a released return can be acknowledged. Accepting one earlier would let
    a signature exist for material still sitting in the yard.
    """
    from dispatch.models import GateOutPurpose, GateOutStatus
    from disposition.models import ClientReturnAck

    if gate_out.purpose_type != GateOutPurpose.RETURN_TO_CLIENT:
        raise ReturnNotAcknowledgeable(
            "Only a return to client carries a client acknowledgement (K3)."
        )
    if gate_out.status not in (
        GateOutStatus.PARTIALLY_RELEASED,
        GateOutStatus.RELEASED,
        GateOutStatus.CLOSED,
    ):
        raise ReturnNotAcknowledgeable(
            f"{gate_out.number} has not left the yard yet, so the client cannot "
            f"have acknowledged it."
        )
    if hasattr(gate_out, "client_return_ack"):
        raise ReturnNotAcknowledgeable(
            f"{gate_out.number} was already acknowledged as "
            f"{gate_out.client_return_ack.acknowledged_ref}."
        )

    ack = ClientReturnAck.objects.create(
        organization_id=gate_out.organization_id,
        gate_out=gate_out,
        acknowledged_ref=acknowledged_ref,
        acknowledged_at=acknowledged_at or timezone.now(),
        acknowledged_by_name=acknowledged_by_name,
        notes=notes,
        recorded_by=recorded_by,
    )

    record(
        AuditAction.STATUS_CHANGED,
        actor=recorded_by,
        organization=gate_out.organization_id,
        target=gate_out,
        target_label=gate_out.number,
        request=request,
        after={"acknowledged_ref": acknowledged_ref},
        note=(
            f"Client acknowledged {gate_out.number} as {acknowledged_ref}. "
            f"Liability for that material ends here (K3)."
        ),
    )
    return ack


def unacknowledged_returns(organization_id, *, older_than_days: int = 7):
    """Returns the client has not signed for (K3, L2).

    The beat task notifies on these. Ordered oldest first, because the one that
    has been outstanding longest is the one somebody has to ring about.
    """
    from datetime import timedelta

    from dispatch.models import GateOut, GateOutPurpose, GateOutStatus

    cutoff = timezone.now() - timedelta(days=older_than_days)
    return (
        GateOut.objects.filter(
            organization_id=organization_id,
            purpose_type=GateOutPurpose.RETURN_TO_CLIENT,
            status__in=(
                GateOutStatus.PARTIALLY_RELEASED,
                GateOutStatus.RELEASED,
                GateOutStatus.CLOSED,
            ),
            released_at__lt=cutoff,
            client_return_ack__isnull=True,
        )
        .select_related("client")
        .order_by("released_at")
    )


def client_node_for(gate_out):
    """K1: a return to client moves stock to the CLIENT node."""
    return node_for_client(gate_out.client)


def _emit(document, event_key: str) -> None:
    """Notify, without letting a channel failure roll back the movement (L3)."""
    from notifications.events import emit

    emit(event_key, document)


__all__ = [
    "DEFAULT_RESTORED_CONDITION",
    "DisposalNotReady",
    "DispositionNotReady",
    "NotFromQuarantine",
    "OwnerType",
    "ReturnNotAcknowledgeable",
    "acknowledge_client_return",
    "approve_disposal",
    "approve_disposition",
    "client_node_for",
    "post_disposal",
    "post_disposition",
    "reject_disposal",
    "reject_disposition",
    "restored_condition_for",
    "submit_disposal",
    "submit_disposition",
    "unacknowledged_returns",
    "written_off_value",
]
