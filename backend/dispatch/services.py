"""Gate-out lifecycle (design §4.7, §5.3; F1–F8, G1–G3, I2).

The order of operations at the gate is the control:

    request -> route -> approve -> **release**

Release is a separate action with its own permission (G1, Q7), and it refuses
anything not approved or expired. That refusal is the product.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.utils import timezone

from approvals.engine import (
    NotAnApprover,
    NothingToApprove,
    SelfApprovalNotAllowed,
    active_delegation_for,
    can_approve,
    create_requests,
    next_pending_request,
    record_auto_approval,
    required_levels,
)
from approvals.models import (
    ApprovalAction,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalRequestStatus,
)
from catalogue.models import TrackingMode
from core.audit import record
from core.exceptions import DomainError
from core.models import AuditAction, AuthMethod
from core.numbering import DocumentType, allocate_number
from dispatch.models import (
    GateOut,
    GateOutStatus,
    ReleaseVariance,
)
from locations.nodes import node_for_client, node_for_location, node_for_user
from stock.models import MovementType
from stock.services import MovementRequest, post_movement

logger = logging.getLogger(__name__)


class GateOutNotReady(DomainError):
    code = "GATE_OUT_NOT_READY"
    status_code = 400
    default_message = "This gate pass is not ready for that."


class ReleaseNotPermitted(DomainError):
    """G1: "the system will not permit release of an unapproved or expired pass".

    The single most important refusal in the system. If this can be bypassed,
    nothing else in the design matters.
    """

    code = "RELEASE_NOT_PERMITTED"
    status_code = 409
    default_message = "This gate pass cannot be released."


# --------------------------------------------------------------------------
# T4.6 — submit for approval
# --------------------------------------------------------------------------


@transaction.atomic
def submit_gate_out(gate_out: GateOut, *, submitted_by=None, request=None) -> GateOut:
    """Route a request to its approvers, or auto-approve it (F1, F2, F3, §5.2).

    The number is allocated here rather than at release: an approver being asked
    to authorise "GP-000042" can refer to it, and M6's sequence stays gap-free
    because a request that is later rejected still exists as a document.
    """
    if gate_out.status not in (
        GateOutStatus.DRAFT,
        GateOutStatus.REJECTED,
        GateOutStatus.EXPIRED,
    ):
        raise GateOutNotReady(
            f"A gate pass in status {gate_out.get_status_display()} cannot be submitted."
        )

    lines = list(gate_out.lines.all())
    if not lines:
        raise GateOutNotReady("Add at least one line before submitting.")

    _validate_stock_is_available(gate_out)

    if not gate_out.number:
        gate_out.number = allocate_number(
            DocumentType.GATE_OUT, organization_id=gate_out.organization_id
        )

    gate_out.submitted_at = timezone.now()

    levels = required_levels(gate_out)

    if not levels:
        # §5.2: no rule applies, so it auto-approves — and is recorded as such,
        # because "why did this leave without approval?" must have an answer.
        gate_out.transition(GateOutStatus.PENDING_APPROVAL, save=False)
        record_auto_approval(gate_out)
        gate_out.transition(GateOutStatus.APPROVED, save=False)
        gate_out.approved_at = timezone.now()
        gate_out.expires_at = _expiry_for(gate_out)
        note = "Auto-approved: no approval rule applies."
    else:
        gate_out.transition(GateOutStatus.PENDING_APPROVAL, save=False)
        create_requests(gate_out, requested_by=submitted_by or gate_out.requested_by)
        note = (
            f"Submitted for approval: {len(levels)} level(s), "
            f"{', '.join(level.label for level in levels)}."
        )

    gate_out.save(
        update_fields=[
            "number",
            "status",
            "submitted_at",
            "approved_at",
            "expires_at",
            "updated_at",
        ]
    )

    record(
        AuditAction.STATUS_CHANGED,
        actor=submitted_by,
        organization=gate_out.organization_id,
        target=gate_out,
        target_label=gate_out.number,
        request=request,
        after={"status": gate_out.status},
        note=note,
    )

    _emit(gate_out, "gate_out.awaiting_approval" if levels else "gate_out.approved")

    return gate_out


def _validate_stock_is_available(gate_out: GateOut) -> None:
    """Check the request against stock before asking anyone to approve it.

    Sending an approver a request that cannot be released wastes their time and
    trains them to approve without looking. The check is repeated at release,
    because stock moves in between (§13, N3).
    """
    from stock.services import balance_at

    source = node_for_location(gate_out.from_location)
    field_errors: dict[str, list[str]] = {}

    messages: list[str] = []

    for index, line in enumerate(gate_out.lines.select_related("item_type", "owner_client").all()):
        available = balance_at(
            source,
            line.item_type,
            owner_client=line.owner_client,
            condition=line.condition,
        )
        if available < line.requested_qty:
            message = (
                f"{line.item_type}: only {_amount(available)} {line.uom} of "
                f"{_whose(line)} at {gate_out.from_location.name}, and this line "
                f"asks for {_amount(line.requested_qty)} {line.uom}."
            )
            nearby = _what_else_is_there(source, line, gate_out.from_location.name)
            if nearby:
                message = f"{message} {nearby}"
            field_errors[f"lines.{index}.requested_qty"] = [message]
            messages.append(message)

    if field_errors:
        raise GateOutNotReady(_summary(messages), field_errors=field_errors)


def _amount(quantity) -> str:
    """A number a person would say out loud: 5, not 5.000."""
    text = f"{quantity:f}".rstrip("0").rstrip(".")
    return text or "0"


def _whose(line) -> str:
    return f"{line.owner_client.name}’s stock" if line.owner_client else "your own stock"


def _what_else_is_there(source, line, location_name: str) -> str:
    """Why the shortfall, when the shelf plainly has some.

    From a real confusion: a technician asked for three vests, the stock screen
    showed five at the yard, and the request was refused. Both were right — the
    five belonged to Safaricom, and the line asked for the company’s own. A
    refusal that does not explain that sends somebody to argue with the stock
    screen, which is not wrong either.
    """
    from stock.models import StockBalance

    rows = (
        StockBalance.objects.filter(node=source, item_type=line.item_type, quantity__gt=0)
        .select_related("owner_client")
        .exclude(owner_client=line.owner_client, condition=line.condition)
    )

    others = list(rows)
    if not others:
        return ""

    parts = []
    for row in others[:3]:
        owner = f"{row.owner_client.name}’s" if row.owner_client else "your own"
        condition = row.condition.replace("_", " ").lower()
        parts.append(f"{_amount(row.quantity)} {row.uom} of {owner} stock ({condition})")

    return (
        f"{location_name} holds {', '.join(parts)}. Change the owner or the "
        "condition on this line to take that instead."
    )


def _summary(messages: list[str]) -> str:
    """The banner a requester actually reads.

    The screen shows the envelope message, so burying the useful sentence in
    the field errors meant the only thing on screen was “some lines ask for more
    than is in stock” — true, unhelpful, and indistinguishable from a bug.
    """
    if len(messages) == 1:
        return messages[0]
    if len(messages) <= 3:
        return " ".join(messages)
    return f"{len(messages)} lines ask for more than is in stock. " + messages[0]


def _notify_if_expensive(gate_out, *, approved_by) -> None:
    """Tell the owner when something expensive leaves on a project (O7).

    After the fact, and blocking nothing: the material moves and the message
    follows. The point is that single-signature approval on project material
    (D22) should not also be unwatched — not to add a second gate, which O6
    deliberately did not ask for.

    Failures here must never reach the approval. `emit` schedules on commit for
    exactly that reason (§9.1, L3), and the value calculation is wrapped because
    a missing price is not grounds for refusing an approval that already
    happened.
    """
    from approvals.engine import project_of

    project = project_of(gate_out)
    if project is None:
        return

    threshold = gate_out.organization.settings.project_release_notify_above
    if threshold is None:
        return

    try:
        value = _requested_value(gate_out)
    except Exception:  # pragma: no cover - defensive; see the docstring
        logger.exception("could not value gate-out %s for O7", gate_out.pk)
        return

    if value is None or value < threshold:
        return

    _emit(
        gate_out,
        "project.high_value_release",
        payload={
            "label": str(gate_out),
            "number": gate_out.number,
            "project": str(project),
            "value": str(value),
            "approved_by": approved_by.full_name or str(approved_by),
            "self_approved": gate_out.requested_by_id == approved_by.pk,
        },
    )


def _requested_value(gate_out) -> Decimal | None:
    """What this pass is worth, at the catalogue prices it would move at (O7).

    Uses the item types' current unit costs rather than the ledger's captured
    ones, because nothing has moved yet — the movements that carry a captured
    valuation (O11) are written at release, not at approval.

    Returns None when no line carries a price at all, so an unvalued pass is
    never reported as a cheap one.
    """
    total = Decimal("0")
    priced = False
    for line in gate_out.lines.select_related("item_type"):
        unit_cost = line.item_type.unit_cost
        if unit_cost is None:
            continue
        priced = True
        total += unit_cost * Decimal(str(line.requested_qty))
    if not priced:
        return None
    # Quantities carry three decimal places (§3.2) and money carries two, so the
    # product needs rounding back or the figure reads as 1800.00000.
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _expiry_for(gate_out: GateOut):
    """Q3: an approved pass expires if not released within the window."""
    hours = gate_out.organization.settings.gate_pass_expiry_hours or 24
    return timezone.now() + timedelta(hours=hours)


# --------------------------------------------------------------------------
# T4.7, T4.8, T4.9 — approve and reject
# --------------------------------------------------------------------------


@transaction.atomic
def approve_gate_out(
    gate_out: GateOut,
    *,
    actor,
    auth_method: str = AuthMethod.PASSWORD,
    webauthn_credential=None,
    request=None,
    reason: str = "",
) -> GateOut:
    """Approve the next outstanding level (F4, F5, §5.3).

    Records who, when, from what device and by what authentication method — the
    non-repudiation evidence in §4.8. When the final level approves, the pass
    becomes releasable and its expiry starts running.
    """
    approval_request = next_pending_request(gate_out)
    if approval_request is None:
        raise NothingToApprove("There is no approval outstanding on this gate pass.")

    allowed, why = can_approve(actor, approval_request, document=gate_out)
    if not allowed:
        if why == "self":
            # F3's edge case, and the refusal most likely to be questioned — so
            # the message says what to do instead.
            raise SelfApprovalNotAllowed()
        role = approval_request.required_role
        role_name = role.name if role is not None else "an approver"
        raise NotAnApprover(f"Approving this needs the {role_name} role.")

    delegation = None
    on_behalf_of = None
    if why == "delegated":
        delegation = active_delegation_for(actor, approval_request.required_role_id)
        if delegation is not None:
            # F5: recorded as "X on behalf of Y", never as Y.
            on_behalf_of = delegation.from_user

    ApprovalAction.objects.create(
        organization_id=gate_out.organization_id,
        approval_request=approval_request,
        actor=actor,
        on_behalf_of=on_behalf_of,
        delegation=delegation,
        decision=ApprovalDecision.APPROVED,
        reason=reason,
        auth_method=auth_method,
        webauthn_credential=webauthn_credential,
        ip=_ip(request),
        user_agent=_user_agent(request),
    )

    approval_request.status = ApprovalRequestStatus.APPROVED
    approval_request.resolved_at = timezone.now()
    approval_request.save(update_fields=["status", "resolved_at", "updated_at"])

    remaining = next_pending_request(gate_out)
    if remaining is None:
        gate_out.transition(GateOutStatus.APPROVED, save=False)
        gate_out.approved_at = timezone.now()
        gate_out.expires_at = _expiry_for(gate_out)
        gate_out.save(update_fields=["status", "approved_at", "expires_at", "updated_at"])
        _emit(gate_out, "gate_out.approved")
        _notify_if_expensive(gate_out, approved_by=actor)
    else:
        _emit(gate_out, "gate_out.awaiting_approval")

    record(
        AuditAction.APPROVED,
        actor=actor,
        organization=gate_out.organization_id,
        target=gate_out,
        target_label=gate_out.number,
        request=request,
        auth_method=auth_method,
        after={"status": gate_out.status},
        note=(
            f"Level {approval_request.level} approved by {actor} on behalf of {on_behalf_of}."
            if on_behalf_of
            else f"Level {approval_request.level} approved by {actor}."
        ),
    )

    return gate_out


@transaction.atomic
def reject_gate_out(
    gate_out: GateOut,
    *,
    actor,
    reason: str,
    auth_method: str = AuthMethod.PASSWORD,
    request=None,
) -> GateOut:
    """Reject a request. A reason is mandatory (F4)."""
    if not reason:
        raise GateOutNotReady("Rejecting a request requires a reason (F4).")

    approval_request = next_pending_request(gate_out)
    if approval_request is None:
        raise NothingToApprove("There is no approval outstanding on this gate pass.")

    allowed, why = can_approve(actor, approval_request, document=gate_out)
    if not allowed:
        if why == "self":
            raise SelfApprovalNotAllowed()
        role = approval_request.required_role
        role_name = role.name if role is not None else "an approver"
        raise NotAnApprover(f"Deciding this needs the {role_name} role.")

    ApprovalAction.objects.create(
        organization_id=gate_out.organization_id,
        approval_request=approval_request,
        actor=actor,
        decision=ApprovalDecision.REJECTED,
        reason=reason,
        auth_method=auth_method,
        ip=_ip(request),
        user_agent=_user_agent(request),
    )

    approval_request.status = ApprovalRequestStatus.REJECTED
    approval_request.resolved_at = timezone.now()
    approval_request.save(update_fields=["status", "resolved_at", "updated_at"])

    # Any later levels are moot.
    ApprovalRequest.objects.filter(
        document_type="dispatch.GateOut",
        document_id=str(gate_out.pk),
        status=ApprovalRequestStatus.PENDING,
    ).update(status=ApprovalRequestStatus.SUPERSEDED, resolved_at=timezone.now())

    gate_out.transition(GateOutStatus.REJECTED, reason=reason)

    record(
        AuditAction.REJECTED,
        actor=actor,
        organization=gate_out.organization_id,
        target=gate_out,
        target_label=gate_out.number,
        request=request,
        auth_method=auth_method,
        after={"status": gate_out.status},
        note=reason,
    )

    _emit(gate_out, "gate_out.rejected")

    return gate_out


# --------------------------------------------------------------------------
# T4.11, T4.12 — amend and cancel
# --------------------------------------------------------------------------


@transaction.atomic
def amend_gate_out(gate_out: GateOut, *, amended_by=None, request=None) -> GateOut:
    """Re-open a request for editing, voiding any approval (F6).

    F6: "amending an approved gate-out **voids the approval and re-triggers
    routing**." Otherwise someone could get a small request approved and then
    enlarge it — which would make approval decorative.
    """
    if gate_out.status not in (
        GateOutStatus.REJECTED,
        GateOutStatus.APPROVED,
        GateOutStatus.PENDING_APPROVAL,
        GateOutStatus.EXPIRED,
    ):
        raise GateOutNotReady(
            f"A gate pass in status {gate_out.get_status_display()} cannot be amended."
        )
    if gate_out.lines.filter(released_qty__gt=0).exists():
        raise GateOutNotReady(
            "Part of this pass has already been released, so it cannot be amended. "
            "Close it and raise a new one."
        )

    voided = ApprovalRequest.objects.filter(
        document_type="dispatch.GateOut",
        document_id=str(gate_out.pk),
        status__in=(
            ApprovalRequestStatus.PENDING,
            ApprovalRequestStatus.APPROVED,
            ApprovalRequestStatus.ESCALATED,
        ),
    )
    # Superseded rather than deleted: F6 keeps the version history visible, and
    # a voided approval is part of that history.
    voided_count = voided.update(
        status=ApprovalRequestStatus.SUPERSEDED, resolved_at=timezone.now()
    )

    gate_out.version += 1
    gate_out.approved_at = None
    gate_out.expires_at = None
    if gate_out.status != GateOutStatus.PENDING_APPROVAL:
        # Amending one that is *already* awaiting approval is ordinary — a
        # requester correcting a line while it sits in somebody's queue — and
        # the state machine rightly refuses a move from a status to itself.
        gate_out.transition(GateOutStatus.PENDING_APPROVAL, save=False)

    # Routing is re-run, which means the same question submission asks: is there
    # anybody to route to?
    #
    # From a stuck pass in a live yard. GP-000001 auto-approved on submission
    # because the organization has no approval rule, was amended half a minute
    # later, and sat in "pending approval" for ever: the approval was voided,
    # re-routing produced nothing to approve, and no screen could move it. The
    # requester had a notification saying it was approved and a list saying it
    # was not, which is the worst of both.
    levels = required_levels(gate_out)
    if levels:
        gate_out.save(
            update_fields=["version", "status", "approved_at", "expires_at", "updated_at"]
        )
        create_requests(gate_out, requested_by=amended_by or gate_out.requested_by)
        routing = f"{voided_count} approval(s) voided and routing re-run"
    else:
        record_auto_approval(gate_out)
        gate_out.transition(GateOutStatus.APPROVED, save=False)
        gate_out.approved_at = timezone.now()
        gate_out.expires_at = _expiry_for(gate_out)
        gate_out.save(
            update_fields=["version", "status", "approved_at", "expires_at", "updated_at"]
        )
        routing = (
            f"{voided_count} approval(s) voided; auto-approved again because no "
            "approval rule applies"
        )

    record(
        AuditAction.DOCUMENT_AMENDED,
        actor=amended_by,
        organization=gate_out.organization_id,
        target=gate_out,
        target_label=gate_out.number,
        request=request,
        after={"status": gate_out.status, "version": gate_out.version},
        note=f"Amended to version {gate_out.version}; {routing}.",
    )

    _emit(gate_out, "gate_out.awaiting_approval" if levels else "gate_out.approved")

    return gate_out


@transaction.atomic
def cancel_gate_out(gate_out: GateOut, *, reason: str, cancelled_by=None, request=None) -> GateOut:
    """Cancel before release (F8).

    F8: "cancellation requires a reason and is not possible after any release."
    Once material has physically left, cancelling the paperwork would leave stock
    unaccounted for — the pass has to be closed with a reason instead.
    """
    if not reason:
        raise GateOutNotReady("Cancelling requires a reason (F8).")

    if gate_out.lines.filter(released_qty__gt=0).exists():
        raise GateOutNotReady(
            "Part of this load has already left the yard, so the pass cannot be "
            "cancelled. Close it with a reason instead (F8).",
        )

    ApprovalRequest.objects.filter(
        document_type="dispatch.GateOut",
        document_id=str(gate_out.pk),
        status__in=(ApprovalRequestStatus.PENDING, ApprovalRequestStatus.ESCALATED),
    ).update(status=ApprovalRequestStatus.SUPERSEDED, resolved_at=timezone.now())

    gate_out.transition(GateOutStatus.CANCELLED, reason=reason)

    record(
        AuditAction.STATUS_CHANGED,
        actor=cancelled_by,
        organization=gate_out.organization_id,
        target=gate_out,
        target_label=gate_out.number,
        request=request,
        after={"status": gate_out.status},
        note=f"Cancelled: {reason}",
    )

    return gate_out


# --------------------------------------------------------------------------
# T4.13, T4.14 — release at the gate
# --------------------------------------------------------------------------


def destination_node_for(gate_out: GateOut):
    """Where released material goes (§3.1, I1).

    Material released to a person becomes their custody (I1) — which is why the
    custody view and the stock ledger can never disagree. Material sent to
    another location, a site or a client goes to that node instead.
    """
    to_location = gate_out.to_location
    if to_location is not None:
        return node_for_location(to_location)
    if gate_out.client_id:
        # K1: in transit to the client, still our exposure until acknowledged.
        return node_for_client(gate_out.client)
    # A site or project destination is carried by a person until they install
    # it (H2), so custody is the correct destination at the gate.
    return node_for_user(gate_out.custody_holder)


@transaction.atomic
def release_gate_out(
    gate_out: GateOut,
    *,
    released_by,
    released_lines: dict | None = None,
    vehicle_reg: str = "",
    driver_name: str = "",
    variance_reasons: dict | None = None,
    request=None,
) -> GateOut:
    """Release material at the gate (G1, G2, F7, I2).

    ``released_lines`` maps line id to the quantity actually loaded. Omitted
    lines release in full. A quantity below the approved figure is recorded as a
    :class:`~dispatch.models.ReleaseVariance` and **does not block the release**
    (G1's edge case): the driver leaves with what was loaded, and the discrepancy
    stays on the exceptions register until an approver acknowledges it.
    """
    if not gate_out.is_releasable:
        if gate_out.is_expired:
            raise ReleaseNotPermitted(
                f"Gate pass {gate_out.number} expired at "
                f"{gate_out.expires_at:%Y-%m-%d %H:%M} and cannot be released. "
                f"Resubmit it for approval.",
                details={"status": gate_out.status, "expired_at": str(gate_out.expires_at)},
            )
        raise ReleaseNotPermitted(
            f"Gate pass {gate_out.number} is {gate_out.get_status_display()}. "
            f"Only an approved pass can be released (G1).",
            details={"status": gate_out.status},
        )

    settings = gate_out.organization.settings
    if settings.signature_required_on_release:
        from core.attachments import attachments_for

        if not attachments_for(gate_out).exists():
            raise GateOutNotReady(
                "This organization requires a signature or photo of the load before release (G3)."
            )

    released_lines = released_lines or {}
    variance_reasons = variance_reasons or {}

    # `from_location` is non-nullable on the model; the local makes that
    # explicit to the type checker.
    from_location = gate_out.from_location
    source = node_for_location(from_location)
    destination = destination_node_for(gate_out)

    # Read the lines fresh rather than through whatever the caller prefetched.
    # A viewset that prefetched `lines` for its response would otherwise hand a
    # stale `released_qty` to `is_fully_released` below, and a pass released in
    # full would stay PARTIALLY_RELEASED — open forever, and unclosable.
    lines = list(
        gate_out.lines.select_related("item_type", "owner_client").order_by("line_number", "id")
    )

    for line in lines:
        outstanding = line.outstanding_qty
        if outstanding <= 0:
            continue

        requested = released_lines.get(line.pk, released_lines.get(str(line.pk), outstanding))
        quantity = Decimal(str(requested))
        if quantity <= 0:
            continue
        if quantity > outstanding:
            raise ReleaseNotPermitted(
                f"{line.item_type} was approved for {line.requested_qty} {line.uom} "
                f"and {line.released_qty} has already gone. Only {outstanding} "
                f"{line.uom} remains on this pass.",
                details={"line_id": line.pk, "outstanding": str(outstanding)},
            )

        _release_line(
            gate_out,
            line,
            quantity,
            source=source,
            destination=destination,
            released_by=released_by,
        )

        # G1's edge case: a short release is a variance, not a refusal.
        if quantity < outstanding:
            ReleaseVariance.objects.create(
                organization_id=gate_out.organization_id,
                gate_out_line=line,
                approved_qty=line.requested_qty,
                released_qty=line.released_qty,
                reason=variance_reasons.get(
                    line.pk, variance_reasons.get(str(line.pk), "Short-loaded at the gate.")
                ),
                recorded_by=released_by,
            )
            _emit(gate_out, "release_variance.raised")

    gate_out.vehicle_reg = vehicle_reg or gate_out.vehicle_reg
    gate_out.driver_name = driver_name or gate_out.driver_name
    gate_out.released_by = released_by
    gate_out.released_at = timezone.now()

    if all(line.is_fully_released for line in lines):
        gate_out.transition(GateOutStatus.RELEASED, save=False)
    else:
        # F7: "a partially released gate-out remains open until fully released,
        # cancelled, or closed with a reason."
        gate_out.transition(GateOutStatus.PARTIALLY_RELEASED, save=False)

    gate_out.save(
        update_fields=[
            "status",
            "vehicle_reg",
            "driver_name",
            "released_by",
            "released_at",
            "updated_at",
        ]
    )

    record(
        AuditAction.STATUS_CHANGED,
        actor=released_by,
        organization=gate_out.organization_id,
        target=gate_out,
        target_label=gate_out.number,
        request=request,
        after={"status": gate_out.status},
        note=(
            f"Released to {gate_out.custody_holder} "
            f"(vehicle {gate_out.vehicle_reg or 'not recorded'}, "
            f"driver {gate_out.driver_name or 'not recorded'})."
        ),
    )

    _emit(gate_out, "gate_out.released")

    return gate_out


def _release_line(gate_out, line, quantity, *, source, destination, released_by):
    """Post the movements for one released line, and start any custody clock."""
    common = {
        "item_type": line.item_type,
        "owner_type": line.owner_type,
        "owner_client": line.owner_client,
        "condition": line.condition,
        "uom": line.uom,
        "posted_by": released_by,
        "document_type": "dispatch.GateOut",
        "document_id": str(gate_out.pk),
        "document_line_id": str(line.pk),
        "document_number": gate_out.number,
        "movement_type": MovementType.ISSUE,
        "from_node": source,
        "to_node": destination,
    }

    if line.tracking_mode == TrackingMode.SERIALIZED:
        remaining = int(quantity)
        for entry in line.serials.filter(released=False).select_related("serial_unit")[:remaining]:
            post_movement(
                MovementRequest(
                    quantity=Decimal("1"),
                    tracking_mode=TrackingMode.SERIALIZED,
                    serial_unit=entry.serial_unit,
                    **common,
                )
            )
            entry.released = True
            entry.save(update_fields=["released"])

    elif line.tracking_mode == TrackingMode.REEL:
        outstanding = quantity
        for entry in line.reels.select_related("reel").all():
            if outstanding <= 0:
                break
            take = min(outstanding, entry.length_requested - entry.length_released)
            if take <= 0:
                continue
            post_movement(
                MovementRequest(
                    quantity=take, tracking_mode=TrackingMode.REEL, reel=entry.reel, **common
                )
            )
            entry.length_released += take
            entry.save(update_fields=["length_released"])
            outstanding -= take

    else:
        post_movement(MovementRequest(quantity=quantity, tracking_mode=TrackingMode.BULK, **common))

    line.released_qty += quantity
    line.save(update_fields=["released_qty", "updated_at"])

    # I2: a returnable line starts an expectation the moment it leaves, so
    # overdue chasing has something to chase (I3). Created here rather than at
    # request time, because nothing is owed until it physically goes.
    if line.is_returnable:
        _create_custody_expectation(gate_out, line, quantity)


def _create_custody_expectation(gate_out, line, quantity):
    """I1, I2: what this person now owes back, and by when.

    The custody app lands in Phase 5 (T5.8). Until then the expectation is
    implicit in the PERSON-node balance, which is already correct — this hook is
    where the explicit record attaches.
    """
    try:
        from custody.models import CustodyExpectation
    except ImportError:  # pragma: no cover - Phase 5
        return

    due = line.expected_return_date
    if due is None and line.item_type.default_return_days:
        due = (timezone.now() + timedelta(days=line.item_type.default_return_days)).date()

    CustodyExpectation.objects.create(
        organization_id=gate_out.organization_id,
        holder=gate_out.custody_holder,
        gate_out_line=line,
        item_type=line.item_type,
        quantity=quantity,
        expected_return_date=due,
    )


@transaction.atomic
def acknowledge_variance(variance: ReleaseVariance, *, actor, request=None) -> ReleaseVariance:
    """An approver accepts a release discrepancy (G1)."""
    if not variance.is_open:
        raise GateOutNotReady("That variance has already been acknowledged.")

    variance.acknowledged_by = actor
    variance.acknowledged_at = timezone.now()
    variance.save(update_fields=["acknowledged_by", "acknowledged_at", "updated_at"])

    record(
        AuditAction.APPROVED,
        actor=actor,
        organization=variance.organization_id,
        target=variance,
        target_label=str(variance),
        request=request,
        note=f"Release variance acknowledged: {variance.reason}",
    )

    return variance


@transaction.atomic
def close_gate_out(gate_out: GateOut, *, reason: str, closed_by=None, request=None) -> GateOut:
    """Close a pass, writing off any outstanding balance (F7)."""
    if gate_out.status == GateOutStatus.PARTIALLY_RELEASED and not reason:
        raise GateOutNotReady(
            "Closing a partially released pass requires a reason, because the "
            "outstanding balance is being written off (F7)."
        )

    gate_out.transition(GateOutStatus.CLOSED, reason=reason)

    record(
        AuditAction.STATUS_CHANGED,
        actor=closed_by,
        organization=gate_out.organization_id,
        target=gate_out,
        target_label=gate_out.number,
        request=request,
        after={"status": gate_out.status},
        note=f"Closed: {reason}" if reason else "Closed.",
    )

    return gate_out


# --------------------------------------------------------------------------
# T4.10 — escalation and expiry
# --------------------------------------------------------------------------


def expire_stale_passes(organization_id) -> int:
    """Expire approved passes never released within the window (Q3, §5.3).

    A stale approval is dangerous: the stock it was checked against has moved on,
    so releasing against it would be authorising something nobody actually
    reviewed.
    """
    now = timezone.now()
    stale = GateOut.objects.filter(
        organization_id=organization_id,
        status=GateOutStatus.APPROVED,
        expires_at__lte=now,
    )

    expired = 0
    for gate_out in stale:
        gate_out.transition(GateOutStatus.EXPIRED)
        _emit(gate_out, "gate_out.expired")
        expired += 1
    return expired


def escalate_overdue_approvals(organization_id) -> int:
    """Escalate approvals nobody answered in time (F5, §5.3)."""
    now = timezone.now()
    overdue = ApprovalRequest.objects.filter(
        organization_id=organization_id,
        status=ApprovalRequestStatus.PENDING,
        due_at__lte=now,
    )

    escalated = 0
    for approval_request in overdue:
        approval_request.status = ApprovalRequestStatus.ESCALATED
        approval_request.escalated_at = now
        approval_request.save(update_fields=["status", "escalated_at", "updated_at"])
        escalated += 1

    return escalated


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _emit(gate_out, event: str, *, payload: dict | None = None) -> None:
    """Emit a notification event (L1–L3, §9.1).

    Dispatched on commit by the notification framework (T4.16), so **a failed
    notification can never roll back the approval it was announcing** (L3).
    """
    try:
        from notifications.events import emit
    except ImportError:  # pragma: no cover - before T4.16
        return
    emit(event, gate_out, payload=payload)


def _ip(request):
    if request is None:
        return None
    from core.audit import client_ip

    return client_ip(request)


def _user_agent(request) -> str:
    if request is None:
        return ""
    return (request.META.get("HTTP_USER_AGENT") or "")[:400]
