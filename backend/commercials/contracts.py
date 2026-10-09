"""Subcontracts and what is paid against them (R8, design §4.19.4).

A subcontract is the written agreement with a subcontractor on one project. Jobs
are delivered under it, Finance records payments against it, and the project's
PM answers each payment. Everything here follows ``commercials.finance`` — the
same ``_route``, decide, audit and notify shape — so a payment recorded online
and one replayed from a phone are judged by one piece of code.

Figures are queries (``position``), never stored: a stored "owed" would drift
the moment a job closed or a payment was reversed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.db import IntegrityError, transaction
from django.db.models import (
    Case,
    DecimalField,
    F,
    OuterRef,
    Subquery,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Coalesce
from django.utils import timezone

from accounts.permissions_registry import PERM
from accounts.services import resolve_permissions
from approvals import engine
from approvals.models import ApprovalDecision
from commercials.finance import (
    FinanceNotDecidable,
    FinanceSelfApproval,
    PaymentReferenceRequired,
    RejectionReasonRequired,
    _invalid,
)
from commercials.models import (
    ExpenseStatus,
    Subcontract,
    SubcontractPayment,
    SubcontractStatus,
)
from core.audit import client_ip, record
from core.exceptions import DomainError, PermissionDeniedError
from core.models import AuditAction
from core.numbering import DocumentType, allocate_number
from notifications.events import emit
from notifications.matrix import Event

ZERO = Decimal("0.00")
_MONEY: DecimalField = DecimalField(max_digits=14, decimal_places=2)


# --------------------------------------------------------------------------
# Errors (§4.19.15). The code is the contract; the message is for a person.
# --------------------------------------------------------------------------


class SubcontractAmbiguous(DomainError):
    code = "SUBCONTRACT_AMBIGUOUS"
    status_code = 400
    default_message = (
        "This project has more than one active contract with that subcontractor. "
        "Choose which one this job is under."
    )


class SubcontractOverValue(DomainError):
    code = "SUBCONTRACT_OVER_VALUE"
    status_code = 400
    default_message = (
        "The jobs awarded under this contract would pass its value. Say why, so "
        "the project manager can see it."
    )


class SubcontractMismatch(DomainError):
    code = "SUBCONTRACT_MISMATCH"
    status_code = 400
    default_message = "That contract is not with this job's subcontractor on this project."


class PaymentNeedsOtherApprover(DomainError):
    code = "PAYMENT_NEEDS_OTHER_APPROVER"
    status_code = 409
    default_message = (
        "You manage this project, so nobody else can give the approval. Ask "
        "another person in Finance to record this payment."
    )


# --------------------------------------------------------------------------
# Who may do what
# --------------------------------------------------------------------------


def _holds_directly(user, codename: str) -> bool:  # type: ignore[no-untyped-def]
    """Held, not lent: a delegation does not lend a Finance signature (D22)."""
    permissions = resolve_permissions(user)
    return bool(permissions.has(codename) and not permissions.is_delegated(codename))


def may_manage(user, project) -> bool:  # type: ignore[no-untyped-def]
    """The project's PM, or Finance (§4.19.4)."""
    if project.manager_id == user.pk:
        return True
    return bool(resolve_permissions(user).has(PERM.FINANCE_APPROVE))


def _require_manage(user, project) -> None:  # type: ignore[no-untyped-def]
    if not may_manage(user, project):
        raise PermissionDeniedError(
            "Only this project's manager, or Finance, can change its subcontracts."
        )


def _audit(action, target, *, actor, note: str, request=None, before=None, after=None) -> None:  # type: ignore[no-untyped-def]
    record(
        action,
        actor=actor,
        organization=target.organization_id,
        target=target,
        target_label=str(target),
        request=request,
        before=before,
        after=after,
        note=note,
    )


# --------------------------------------------------------------------------
# Subcontracts
# --------------------------------------------------------------------------


def _checked_sites(project, sites) -> list:  # type: ignore[no-untyped-def]
    """Every site must already be on the project (§4.19.4)."""
    sites = list(sites)
    on_project = set(
        project.sites.filter(pk__in=[s.pk for s in sites]).values_list("pk", flat=True)
    )
    stray = [site for site in sites if site.pk not in on_project]
    if stray:
        names = ", ".join(site.name for site in stray)
        raise _invalid("sites", f"These sites are not on {project}: {names}.")
    return sites


def _checked_value(value) -> Decimal:  # type: ignore[no-untyped-def]
    value = Decimal(value)
    if value <= 0:
        raise _invalid("contract_value", "The contract value must be more than zero.")
    return value


def create_subcontract(
    *,
    actor,  # type: ignore[no-untyped-def]
    project,  # type: ignore[no-untyped-def]
    subcontractor,  # type: ignore[no-untyped-def]
    contract_value,  # type: ignore[no-untyped-def]
    sites=(),  # type: ignore[no-untyped-def]
    payment_terms: str = "",
    request=None,  # type: ignore[no-untyped-def]
) -> Subcontract:
    """Make a contract. The project's PM or Finance (§4.19.4)."""
    _require_manage(actor, project)
    if not subcontractor.is_active:
        raise _invalid("subcontractor", "That subcontractor is deactivated.")
    value = _checked_value(contract_value)
    site_list = _checked_sites(project, sites)

    with transaction.atomic():
        contract = Subcontract.objects.create(
            organization_id=project.organization_id,
            number=allocate_number(
                DocumentType.SUBCONTRACT, organization_id=project.organization_id
            ),
            project=project,
            subcontractor=subcontractor,
            contract_value=value,
            payment_terms=payment_terms.strip(),
            created_by=actor,
        )
        contract.sites.set(site_list)
        _audit(
            AuditAction.DOCUMENT_POSTED,
            contract,
            actor=actor,
            request=request,
            after={"contract_value": str(value), "project": str(project)},
            note=f"Contract with {subcontractor} on {project}, value {value}.",
        )
    return contract


def update_subcontract(
    contract: Subcontract,
    *,
    actor,  # type: ignore[no-untyped-def]
    changes: dict[str, Any],
    request=None,  # type: ignore[no-untyped-def]
) -> Subcontract:
    """Edit terms, sites, value or status. A value change is audited old to new.

    That audit row is the whole variation history (§4.19.4 assumption): no
    variation table, but nothing about the value is ever silently overwritten.
    """
    project = contract.project
    _require_manage(actor, project)
    unknown = set(changes) - {"sites", "contract_value", "payment_terms", "status"}
    if unknown:
        raise TypeError(f"Unknown subcontract fields: {sorted(unknown)}")

    with transaction.atomic():
        contract = Subcontract.objects.select_for_update().get(pk=contract.pk)
        if "contract_value" in changes:
            new_value = _checked_value(changes["contract_value"])
            if new_value != contract.contract_value:
                old_value = contract.contract_value
                contract.contract_value = new_value
                _audit(
                    AuditAction.DOCUMENT_AMENDED,
                    contract,
                    actor=actor,
                    request=request,
                    before={"contract_value": str(old_value)},
                    after={"contract_value": str(new_value)},
                    note=f"Contract value changed from {old_value} to {new_value}.",
                )
        if "payment_terms" in changes:
            contract.payment_terms = (changes["payment_terms"] or "").strip()
        if "status" in changes and changes["status"] != contract.status:
            if changes["status"] not in SubcontractStatus.values:
                raise _invalid("status", "Unknown status.")
            old_status = contract.status
            contract.status = changes["status"]
            _audit(
                AuditAction.STATUS_CHANGED,
                contract,
                actor=actor,
                request=request,
                before={"status": old_status},
                after={"status": contract.status},
                note=f"Contract {contract.get_status_display().lower()}.",
            )
        contract.save()
        if "sites" in changes:
            contract.sites.set(_checked_sites(project, changes["sites"]))
    return contract


# --------------------------------------------------------------------------
# Figures (§4.19.4): queries, never stored
# --------------------------------------------------------------------------


def _signed_amount() -> Case:
    return Case(
        When(reverses__isnull=False, then=-F("amount")),
        default=F("amount"),
        output_field=_MONEY,
    )


def _total(queryset, expression, field: str = "subcontract"):  # type: ignore[no-untyped-def]
    """A correlated ``SUM`` that cannot multiply by another relation's rows."""
    inner = queryset.order_by().values(field).annotate(total=Sum(expression)).values("total")[:1]
    return Coalesce(Subquery(inner, output_field=_MONEY), Value(ZERO), output_field=_MONEY)


def position_annotations() -> dict[str, Any]:
    """``Subcontract.objects.annotate(**position_annotations())`` — one query for a page."""
    from jobs.models import Job, JobStatus

    jobs = Job.objects.filter(subcontract=OuterRef("pk"))
    payments = SubcontractPayment.objects.filter(subcontract=OuterRef("pk"))
    return {
        "pos_work_done": _total(jobs.filter(status=JobStatus.CLOSED), "agreed_price"),
        "pos_committed": _total(jobs.exclude(status=JobStatus.CANCELLED), "agreed_price"),
        "pos_paid": _total(payments.filter(status=ExpenseStatus.APPROVED), _signed_amount()),
        "pos_awaiting": _total(
            payments.filter(status=ExpenseStatus.PENDING_PM, reverses__isnull=True),
            "amount",
        ),
    }


@dataclass(frozen=True)
class Position:
    contract_value: Decimal
    work_done: Decimal
    paid: Decimal
    awaiting_approval: Decimal
    committed: Decimal

    @property
    def owed(self) -> Decimal:
        """Work done less paid. Negative reads "advance paid"."""
        return self.work_done - self.paid

    @property
    def paid_exceeds_work_done(self) -> bool:
        return self.paid > self.work_done

    @property
    def paid_exceeds_contract_value(self) -> bool:
        return self.paid > self.contract_value

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_value": self.contract_value,
            "work_done": self.work_done,
            "paid": self.paid,
            "awaiting_approval": self.awaiting_approval,
            "owed": self.owed,
            "committed": self.committed,
            "paid_exceeds_work_done": self.paid_exceeds_work_done,
            "paid_exceeds_contract_value": self.paid_exceeds_contract_value,
        }


def position_of(annotated: Subcontract) -> Position:
    """The position of a row fetched with ``position_annotations``."""
    return Position(
        contract_value=annotated.contract_value,
        work_done=annotated.pos_work_done,  # type: ignore[attr-defined]
        paid=annotated.pos_paid,  # type: ignore[attr-defined]
        awaiting_approval=annotated.pos_awaiting,  # type: ignore[attr-defined]
        committed=annotated.pos_committed,  # type: ignore[attr-defined]
    )


def position(contract: Subcontract) -> Position:
    """What is contracted, done, paid and owed on one contract (§4.19.4)."""
    row = Subcontract.objects.annotate(**position_annotations()).get(pk=contract.pk)
    return position_of(row)


# --------------------------------------------------------------------------
# Jobs under a contract (§4.19.4)
# --------------------------------------------------------------------------


def resolve_job_link(
    *,
    project,  # type: ignore[no-untyped-def]
    subcontractor,  # type: ignore[no-untyped-def]
    agreed_price: Decimal,
    subcontract: Subcontract | None,
    explicit: bool,
    reason: str = "",
    job=None,  # type: ignore[no-untyped-def]
) -> tuple[Subcontract | None, str]:
    """Which contract a SUBCONTRACTED job sits under, and its over-value reason.

    ``explicit`` is whether the caller named a contract (or deliberately none).
    Unnamed, a single ACTIVE contract with that subcontractor is chosen; several
    are ``SUBCONTRACT_AMBIGUOUS``; none leaves the job unlinked (jobs from before
    contracts existed, and POs that never sign one, keep working). Over the
    contract's value the job is recorded with a reason, not blocked.
    """
    reason = (reason or "").strip()
    if not explicit:
        if project is None:
            return None, ""
        active = list(
            Subcontract.objects.filter(
                project=project,
                subcontractor=subcontractor,
                status=SubcontractStatus.ACTIVE,
            )[:2]
        )
        if len(active) > 1:
            raise SubcontractAmbiguous()
        subcontract = active[0] if active else None
    if subcontract is None:
        return None, ""

    if project is None or subcontract.project_id != project.pk:
        raise SubcontractMismatch()
    if subcontract.subcontractor_id != subcontractor.pk:
        raise SubcontractMismatch()
    already_on_it = job is not None and job.subcontract_id == subcontract.pk
    if subcontract.status != SubcontractStatus.ACTIVE and not already_on_it:
        raise SubcontractMismatch("That contract is closed, so no new job can go under it.")

    from jobs.models import Job, JobStatus

    awarded = Job.objects.filter(subcontract=subcontract).exclude(status=JobStatus.CANCELLED)
    if job is not None and job.pk:
        awarded = awarded.exclude(pk=job.pk)
    committed = awarded.aggregate(total=Sum("agreed_price"))["total"] or ZERO
    if committed + agreed_price > subcontract.contract_value:
        if not reason:
            raise SubcontractOverValue(
                details={
                    "contract_value": str(subcontract.contract_value),
                    "committed": str(committed),
                    "this_job": str(agreed_price),
                }
            )
        return subcontract, reason
    return subcontract, ""


# --------------------------------------------------------------------------
# Payments (§4.19.4)
# --------------------------------------------------------------------------


def _notify(payment: SubcontractPayment, event_key: str, **extra: Any) -> None:
    """Tell people (R4). Inside the caller's transaction: ``emit`` sends on commit."""
    recorder = payment.recorded_by
    emit(
        event_key,
        payment,
        payload={
            "kind": "payment",
            "label": f"{payment.reference} {payment.subcontract.subcontractor}",
            "amount": f"{payment.amount:.2f}",
            "amount_display": f"KES {payment.amount:,.2f}",
            "category": "subcontract payment",
            "site": "",
            "project": str(payment.subcontract.project),
            "recorded_by": recorder.full_name or recorder.email,
            "status": payment.status,
            **extra,
        },
    )


def _route(payment: SubcontractPayment, actor) -> None:  # type: ignore[no-untyped-def]
    """One level, the project's PM. The engine refuses an inactive or missing one (D28)."""
    engine.create_requests(payment, requested_by=actor)
    _notify(payment, Event.FINANCE_AWAITING_APPROVAL)


def record_subcontract_payment(
    *,
    actor,  # type: ignore[no-untyped-def]
    subcontract: Subcontract,
    amount,  # type: ignore[no-untyped-def]
    paid_on: date,
    reference: str,
    client_uuid: UUID | None = None,
    request=None,  # type: ignore[no-untyped-def]
) -> SubcontractPayment:
    """Finance records money paid to a subcontractor; the project's PM answers it."""
    if client_uuid is not None:
        existing = SubcontractPayment.objects.filter(client_uuid=client_uuid).first()
        if existing is not None:
            return existing
    if not _holds_directly(actor, PERM.FINANCE_APPROVE):
        raise PermissionDeniedError("Only Finance can record a subcontract payment.")
    project = subcontract.project
    if project.manager_id == actor.pk:
        raise PaymentNeedsOtherApprover()
    amount = Decimal(amount)
    if amount <= 0:
        raise _invalid("amount", "The amount must be more than zero.")
    if paid_on > timezone.localdate():
        raise _invalid("paid_on", "The payment date cannot be in the future.")
    reference = (reference or "").strip()
    if not reference:
        raise PaymentReferenceRequired()

    try:
        with transaction.atomic():
            payment = SubcontractPayment.objects.create(
                organization_id=subcontract.organization_id,
                subcontract=subcontract,
                amount=amount,
                paid_on=paid_on,
                reference=reference,
                recorded_by=actor,
                client_uuid=client_uuid,
            )
            _route(payment, actor)
            _audit(
                AuditAction.DOCUMENT_POSTED,
                payment,
                actor=actor,
                request=request,
                note=f"Payment of {amount} to {subcontract.subcontractor} on {project} "
                f"recorded; waiting on the project manager.",
            )
    except IntegrityError:
        existing = (
            SubcontractPayment.objects.filter(client_uuid=client_uuid).first()
            if client_uuid is not None
            else None
        )
        if existing is None:
            raise
        return existing
    return payment


def _stamp(payment: SubcontractPayment, actor, reason: str) -> None:  # type: ignore[no-untyped-def]
    payment.decided_by = actor
    payment.decided_at = timezone.now()
    payment.decision_reason = reason


def decide(
    payment: SubcontractPayment,
    *,
    actor,  # type: ignore[no-untyped-def]
    approved: bool,
    reason: str = "",
    request=None,  # type: ignore[no-untyped-def]
) -> SubcontractPayment:
    """The project's PM approves or rejects. Approval is the end: it counts as paid."""
    if payment.status != ExpenseStatus.PENDING_PM:
        raise FinanceNotDecidable(f"This was already {payment.get_status_display().lower()}.")
    # Before the engine, so the answer is the finance one (403, its own code),
    # even for the PM who is somehow also the recorder.
    if payment.recorded_by_id == actor.pk:
        raise FinanceSelfApproval()
    reason = reason.strip()
    if not approved and not reason:
        raise RejectionReasonRequired()

    with transaction.atomic():
        locked = SubcontractPayment.objects.select_for_update().get(pk=payment.pk)
        if locked.status != ExpenseStatus.PENDING_PM:
            raise FinanceNotDecidable(f"This was already {locked.get_status_display().lower()}.")
        decided, _upcoming = engine.record_decision(
            locked,
            actor=actor,
            decision=ApprovalDecision.APPROVED if approved else ApprovalDecision.REJECTED,
            reason=reason,
            ip=client_ip(request) if request is not None else None,
            user_agent=(request.META.get("HTTP_USER_AGENT") or "")[:400]
            if request is not None
            else "",
        )
        if approved:
            locked.status = ExpenseStatus.APPROVED
            _stamp(locked, actor, "")
            locked.save()
            _audit(
                AuditAction.APPROVED,
                locked,
                actor=actor,
                request=request,
                note="Approved by the project manager.",
            )
            _notify(locked, Event.FINANCE_APPROVED)
        else:
            locked.status = ExpenseStatus.REJECTED
            _stamp(locked, actor, reason)
            locked.save()
            _audit(
                AuditAction.REJECTED,
                locked,
                actor=actor,
                request=request,
                note=f"Rejected at level {decided.level}: {reason}",
            )
            _notify(locked, Event.FINANCE_REJECTED, reason=reason)
    return locked


def resubmit(
    payment: SubcontractPayment,
    *,
    actor,  # type: ignore[no-untyped-def]
    request=None,  # type: ignore[no-untyped-def]
) -> SubcontractPayment:
    """Send a rejected payment round again. Its recorder only; the old requests stay."""
    if payment.recorded_by_id != actor.pk:
        raise PermissionDeniedError("Only the person who recorded this can send it again.")
    if payment.status != ExpenseStatus.REJECTED:
        raise FinanceNotDecidable("Only a rejected payment can be sent again.")
    if payment.subcontract.project.manager_id == actor.pk:
        raise PaymentNeedsOtherApprover()

    with transaction.atomic():
        locked = SubcontractPayment.objects.select_for_update().get(pk=payment.pk)
        if locked.status != ExpenseStatus.REJECTED:
            raise FinanceNotDecidable("Only a rejected payment can be sent again.")
        # Cleared first: the CHECK allows no decision on a pending payment.
        locked.status = ExpenseStatus.PENDING_PM
        locked.decided_by = None
        locked.decided_at = None
        locked.decision_reason = ""
        locked.save()
        _route(locked, actor)
        _audit(
            AuditAction.STATUS_CHANGED,
            locked,
            actor=actor,
            request=request,
            note="Sent again; waiting on the project manager.",
        )
    return locked


def reverse_subcontract_payment(
    payment: SubcontractPayment,
    *,
    actor,  # type: ignore[no-untyped-def]
    reason: str,
    request=None,  # type: ignore[no-untyped-def]
) -> SubcontractPayment:
    """Undo an approved payment by recording its opposite (O16).

    Born approved and counted negatively: the correction is the project
    manager's or Finance's own act, so asking for a signature would be theatre.
    """
    if payment.status != ExpenseStatus.APPROVED:
        raise FinanceNotDecidable("Only an approved payment needs reversing.")
    if payment.reverses_id:
        raise FinanceNotDecidable("A reversal cannot itself be reversed.")
    if not may_manage(actor, payment.subcontract.project):
        raise PermissionDeniedError(
            "Only the manager of this project, or Finance, can reverse its payments."
        )
    reason = (reason or "").strip()
    if not reason:
        raise _invalid("reason", "Reversing a payment needs a reason.")

    with transaction.atomic():
        locked = SubcontractPayment.objects.select_for_update().get(pk=payment.pk)
        if locked.reversals.exists():
            raise FinanceNotDecidable("This payment has already been reversed.")
        now = timezone.now()
        reversal = SubcontractPayment.objects.create(
            organization_id=locked.organization_id,
            subcontract=locked.subcontract,
            amount=locked.amount,
            paid_on=timezone.localdate(),
            reference=f"Reversal of {locked.reference}"[:100],
            recorded_by=actor,
            status=ExpenseStatus.APPROVED,
            decided_by=actor,
            decided_at=now,
            decision_reason=reason[:500],
            reverses=locked,
        )
        _audit(
            AuditAction.STATUS_CHANGED,
            reversal,
            actor=actor,
            request=request,
            note=f"Reversed payment {locked.pk} on {locked.subcontract.project}: {reason}",
        )
    return reversal
