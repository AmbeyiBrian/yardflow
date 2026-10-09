"""The supplier register: add, edit, approve, link history (§4.20.3; R15).

Approval goes through ``approvals.engine`` (one Finance level, no self-approval);
``Supplier.status`` is a projection of that request. Editing the PIN or payment
details of an APPROVED supplier keeps it APPROVED but is audited with before and
after and Finance is told (decision 2026-10-09, §4.20.2).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from django.db import IntegrityError, transaction
from django.utils import timezone

from approvals import engine
from approvals.models import ApprovalDecision
from commercials.finance import (
    FinanceNotDecidable,
    FinanceSelfApproval,
    RejectionReasonRequired,
)
from core.audit import changed_fields, client_ip, record, snapshot
from core.exceptions import DomainError, PermissionDeniedError
from core.models import AuditAction
from network.models import Supplier, SupplierStatus
from notifications.events import emit
from notifications.matrix import Event

#: Changing any of these on an APPROVED supplier is recorded and told to Finance.
SENSITIVE_FIELDS = (
    "kra_pin",
    "bank_name",
    "account_number",
    "mpesa_type",
    "mpesa_number",
    "mpesa_account",
)
EDITABLE_FIELDS = (
    "name",
    "contact_name",
    "phone",
    "email",
    "address",
    *SENSITIVE_FIELDS,
)


class SupplierNameRequired(DomainError):
    code = "SUPPLIER_NAME_REQUIRED"
    status_code = 400
    default_message = "A supplier needs a name."


class SupplierPinDuplicate(DomainError):
    code = "SUPPLIER_PIN_DUPLICATE"
    status_code = 409
    default_message = "That KRA PIN is already on the supplier register."


class SupplierNameDuplicate(DomainError):
    code = "SUPPLIER_NAME_DUPLICATE"
    status_code = 409
    default_message = "A supplier with that name is already on the register."


class SupplierPinRequired(DomainError):
    code = "SUPPLIER_PIN_REQUIRED"
    status_code = 400
    default_message = "Approving needs the supplier's KRA PIN and a payment route."


class SupplierNotApproved(DomainError):
    code = "SUPPLIER_NOT_APPROVED"
    status_code = 409
    default_message = "This supplier is not approved and active, so it cannot be paid."


def _existing_details(existing: Supplier) -> dict[str, Any]:
    return {"existing": {"id": existing.pk, "name": existing.name, "status": existing.status}}


def _check_duplicates(
    organization_id, name: str, kra_pin: str, *, exclude_pk: int | None = None
) -> None:
    others = Supplier.objects.filter(organization_id=organization_id)
    if exclude_pk is not None:
        others = others.exclude(pk=exclude_pk)
    pin_key = Supplier.normalise_pin(kra_pin)
    if pin_key:
        found = others.filter(kra_pin_key=pin_key).first()
        if found is not None:
            raise SupplierPinDuplicate(details=_existing_details(found))
    found = others.filter(name_key=Supplier.normalise_name(name)).first()
    if found is not None:
        raise SupplierNameDuplicate(details=_existing_details(found))


def _audit(action, supplier, *, actor, note, request=None, before=None, after=None) -> None:  # type: ignore[no-untyped-def]
    record(
        action,
        actor=actor,
        organization=supplier.organization_id,
        target=supplier,
        target_label=str(supplier),
        request=request,
        before=before,
        after=after,
        note=note,
    )


def _clean(values: dict[str, Any]) -> dict[str, Any]:
    return {k: (v.strip() if isinstance(v, str) else v) for k, v in values.items()}


def add_supplier(
    *,
    actor,  # type: ignore[no-untyped-def]
    name: str,
    client_uuid: UUID | None = None,
    request=None,  # type: ignore[no-untyped-def]
    **details: Any,
) -> Supplier:
    """Register a supplier as PENDING and route it to Finance (R15).

    Only ``name`` is required. A replay of the same ``client_uuid`` returns the
    row it made (R6).
    """
    name = (name or "").strip()
    if not name:
        raise SupplierNameRequired()
    unknown = set(details) - set(EDITABLE_FIELDS)
    if unknown:
        raise TypeError(f"Unknown supplier fields: {sorted(unknown)}")
    values = _clean(details)

    organization_id = actor.organization_id
    if client_uuid is not None:
        replay = Supplier.objects.filter(client_uuid=client_uuid).first()
        if replay is not None:
            return replay
    _check_duplicates(organization_id, name, values.get("kra_pin", ""))

    try:
        with transaction.atomic():
            supplier = Supplier.objects.create(
                organization_id=organization_id,
                name=name,
                registered_by=actor,
                client_uuid=client_uuid,
                **values,
            )
            engine.create_requests(supplier, requested_by=actor)
            emit(Event.FINANCE_AWAITING_APPROVAL, supplier, payload=_payload(supplier))
            _audit(
                AuditAction.DOCUMENT_POSTED,
                supplier,
                actor=actor,
                request=request,
                note=f"Supplier {supplier.name} added; waiting for Finance.",
            )
    except IntegrityError:
        # Lost a race on a unique key: say which, naming the winner.
        if client_uuid is not None:
            replay = Supplier.objects.filter(client_uuid=client_uuid).first()
            if replay is not None:
                return replay
        _check_duplicates(organization_id, name, values.get("kra_pin", ""))
        raise
    return supplier


def _payload(supplier: Supplier, **extra: Any) -> dict[str, Any]:
    return {
        "kind": "supplier",
        "label": supplier.name,
        "status": supplier.status,
        "recorded_by": supplier.registered_by.full_name or supplier.registered_by.email,
        **extra,
    }


def update_supplier(
    supplier: Supplier,
    *,
    actor,  # type: ignore[no-untyped-def]
    changes: dict[str, Any],
    request=None,  # type: ignore[no-untyped-def]
) -> Supplier:
    """Edit a supplier. A sensitive edit of an APPROVED one is recorded, not re-approved."""
    unknown = set(changes) - set(EDITABLE_FIELDS)
    if unknown:
        raise TypeError(f"Unknown supplier fields: {sorted(unknown)}")
    changes = _clean(changes)

    with transaction.atomic():
        # Lock, then refresh the caller's own instance so it stays current.
        Supplier.objects.select_for_update().get(pk=supplier.pk)
        supplier.refresh_from_db()
        before = snapshot(supplier, list(EDITABLE_FIELDS))
        name = changes.get("name", supplier.name)
        if not name:
            raise SupplierNameRequired()
        pin = changes.get("kra_pin", supplier.kra_pin)
        _check_duplicates(supplier.organization_id, name, pin, exclude_pk=supplier.pk)

        for field, value in changes.items():
            setattr(supplier, field, value)
        supplier.save()
        after = snapshot(supplier, list(EDITABLE_FIELDS))
        diff = changed_fields(before, after)
        if not diff:
            return supplier

        sensitive = sorted(set(diff) & set(SENSITIVE_FIELDS))
        if sensitive and supplier.status == SupplierStatus.APPROVED:
            _audit(
                AuditAction.DOCUMENT_AMENDED,
                supplier,
                actor=actor,
                request=request,
                before={k: before[k] for k in diff},
                after={k: after[k] for k in diff},
                note=f"Payment details changed on an approved supplier: {', '.join(sensitive)}.",
            )
            emit(
                Event.SUPPLIER_DETAILS_CHANGED,
                supplier,
                payload=_payload(
                    supplier,
                    changed=sensitive,
                    changed_by=actor.full_name or actor.email,
                ),
            )
        else:
            _audit(
                AuditAction.DOCUMENT_AMENDED,
                supplier,
                actor=actor,
                request=request,
                before={k: before[k] for k in diff},
                after={k: after[k] for k in diff},
                note=f"Supplier edited: {', '.join(sorted(diff))}.",
            )
    return supplier


def _has_payment_route(supplier: Supplier) -> bool:
    bank = bool(supplier.bank_name and supplier.account_number)
    mpesa = bool(supplier.mpesa_type and supplier.mpesa_number)
    return bank or mpesa


def decide_supplier(
    supplier: Supplier,
    *,
    actor,  # type: ignore[no-untyped-def]
    approved: bool,
    reason: str = "",
    request=None,  # type: ignore[no-untyped-def]
) -> Supplier:
    """Finance approves or rejects a PENDING supplier (R15)."""
    if supplier.status != SupplierStatus.PENDING:
        raise FinanceNotDecidable(f"This was already {supplier.get_status_display().lower()}.")
    if supplier.registered_by_id == actor.pk:
        raise FinanceSelfApproval()
    reason = reason.strip()
    if not approved and not reason:
        raise RejectionReasonRequired()
    if approved and (not supplier.kra_pin_key or not _has_payment_route(supplier)):
        raise SupplierPinRequired()

    with transaction.atomic():
        locked = Supplier.objects.select_for_update().get(pk=supplier.pk)
        if locked.status != SupplierStatus.PENDING:
            raise FinanceNotDecidable(f"This was already {locked.get_status_display().lower()}.")
        engine.record_decision(
            locked,
            actor=actor,
            decision=ApprovalDecision.APPROVED if approved else ApprovalDecision.REJECTED,
            reason=reason,
            ip=client_ip(request) if request is not None else None,
            user_agent=(request.META.get("HTTP_USER_AGENT") or "")[:400]
            if request is not None
            else "",
        )
        locked.status = SupplierStatus.APPROVED if approved else SupplierStatus.REJECTED
        locked.decided_by = actor
        locked.decided_at = timezone.now()
        locked.decision_reason = "" if approved else reason
        locked.save()
        emit(
            Event.FINANCE_APPROVED if approved else Event.FINANCE_REJECTED,
            locked,
            payload=_payload(locked, reason=reason),
        )
        _audit(
            AuditAction.APPROVED if approved else AuditAction.REJECTED,
            locked,
            actor=actor,
            request=request,
            note="Approved by Finance." if approved else f"Rejected: {reason}",
        )
    return locked


def resubmit(supplier: Supplier, *, actor, request=None) -> Supplier:  # type: ignore[no-untyped-def]
    """Send a rejected supplier round again; registrar only."""
    if supplier.registered_by_id != actor.pk:
        raise PermissionDeniedError("Only the person who added this can send it again.")
    with transaction.atomic():
        locked = Supplier.objects.select_for_update().get(pk=supplier.pk)
        if locked.status != SupplierStatus.REJECTED:
            raise FinanceNotDecidable("Only a rejected supplier can be sent again.")
        locked.status = SupplierStatus.PENDING
        locked.decided_by = None
        locked.decided_at = None
        locked.decision_reason = ""
        locked.save()
        engine.create_requests(locked, requested_by=actor)
        emit(Event.FINANCE_AWAITING_APPROVAL, locked, payload=_payload(locked))
        _audit(
            AuditAction.STATUS_CHANGED,
            locked,
            actor=actor,
            request=request,
            note="Sent again; waiting for Finance.",
        )
    return locked


def set_active(supplier: Supplier, *, actor, active: bool, request=None) -> Supplier:  # type: ignore[no-untyped-def]
    """Deactivate or reactivate; never deletes (R15)."""
    if supplier.is_active == active:
        return supplier
    supplier.is_active = active
    supplier.save(update_fields=["is_active", "updated_at"])
    _audit(
        AuditAction.STATUS_CHANGED,
        supplier,
        actor=actor,
        request=request,
        note="Reactivated." if active else "Deactivated.",
    )
    return supplier


def link_history(supplier: Supplier, *, actor=None, request=None) -> int:  # type: ignore[no-untyped-def]
    """Link past gate-ins whose typed supplier name matches this one (§4.20.5).

    Same rule as the receiving migration: ``name_key`` equality. Never
    overwrites a supplier already set, so running it twice links nothing new.
    Tenant-bounded by the manager. Returns how many were linked.
    """
    from receiving.models import GateIn

    candidates = GateIn.objects.filter(
        organization_id=supplier.organization_id, supplier__isnull=True
    ).exclude(supplier_name="")
    ids = [
        pk
        for pk, name in candidates.values_list("pk", "supplier_name")
        if Supplier.normalise_name(name) == supplier.name_key
    ]
    if not ids:
        return 0
    linked = GateIn.objects.filter(pk__in=ids, supplier__isnull=True).update(supplier=supplier)
    if linked:
        _audit(
            AuditAction.STATUS_CHANGED,
            supplier,
            actor=actor,
            request=request,
            note=f"Linked {linked} past gate-in(s) to this supplier.",
        )
    return linked


def assert_payable(supplier: Supplier) -> None:
    """The one definition of "may be paid" (§4.20.3): APPROVED and active."""
    if not supplier.is_usable:
        raise SupplierNotApproved()
