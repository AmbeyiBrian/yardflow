"""The PO's milestones, invoices and receipts (R11; design §4.19.7).

``milestone_state`` is pure: it takes the milestone, today and when each site was
accepted, and answers. The API, the PO payments report and (later) the sweep all
call it, so the screen, the report and the notification can never disagree about
whether something is due or overdue.

Nothing here stores a verdict. Due and overdue are questions asked of dates and
sums (D23); the only stored facts are what was invoiced and what was received.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Max

from commercials.models import (
    MilestoneCondition,
    MilestoneInvoice,
    MilestoneReceipt,
    MilestoneShare,
    ProjectMilestone,
)
from core.audit import record
from core.exceptions import DomainError
from core.models import AuditAction

ZERO = Decimal("0")
CENT = Decimal("0.01")

#: The milestones a PO starts with: name and condition, shares left for Finance.
DEFAULT_MILESTONES = (
    ("Deposit", MilestoneCondition.NONE),
    ("Conditional acceptance", MilestoneCondition.ALL_SITES_ACCEPTED),
    ("Final acceptance", MilestoneCondition.ALL_SITES_ACCEPTED),
)


class State:
    NOT_DUE = "NOT_DUE"
    DUE = "DUE"
    OVERDUE = "OVERDUE"
    INVOICED = "INVOICED"
    PART_PAID = "PART_PAID"
    PAID = "PAID"


# --------------------------------------------------------------------------
# Errors (§4.19.15)
# --------------------------------------------------------------------------


class ReceiptExceedsInvoiced(DomainError):
    code = "RECEIPT_EXCEEDS_INVOICED"
    status_code = 400
    default_message = "That is more than has been invoiced for this milestone."


class MilestoneLocked(DomainError):
    code = "MILESTONE_LOCKED"
    status_code = 409
    default_message = (
        "This milestone has an invoice against it, so only its condition date can "
        "change."
    )


class ProjectHasNoPo(DomainError):
    code = "PROJECT_HAS_NO_PO"
    status_code = 409
    default_message = "This project has no PO yet. Attach the PO first."


class MilestonesExist(DomainError):
    code = "MILESTONES_EXIST"
    status_code = 409
    default_message = "This project already has milestones."


# --------------------------------------------------------------------------
# The pure part
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MilestoneState:
    state: str
    condition_met: bool
    met_on: date | None
    amount: Decimal | None
    invoiced: Decimal
    received: Decimal
    latest_invoice_date: date | None
    due_by: date | None

    @property
    def outstanding(self) -> Decimal:
        """Invoiced and not yet received."""
        return self.invoiced - self.received


def milestone_amount(milestone: ProjectMilestone, contract_value: Decimal | None) -> Decimal | None:
    """Percent of the current contract value, or the fixed amount. ``None``
    while the share is not set (or there is no value to take a percent of)."""
    if milestone.share_value is None:
        return None
    if milestone.share_type == MilestoneShare.AMOUNT:
        return milestone.share_value
    if contract_value is None:
        return None
    return (contract_value * milestone.share_value / Decimal(100)).quantize(
        CENT, rounding=ROUND_HALF_UP
    )


def milestone_state(
    milestone: ProjectMilestone,
    today: date,
    accepted_dates: Sequence[date | None],
    *,
    contract_value: Decimal | None = None,
) -> MilestoneState:
    """Where this milestone stands today.

    ``accepted_dates`` has one entry per site of the project: the date it was
    accepted, or ``None`` while it is not (§4.19.6 — a date alone is not
    acceptance). No sites means nothing is accepted.
    """
    project = milestone.project

    if milestone.condition == MilestoneCondition.NONE:
        met = bool(project.po_number)
        met_on = project.po_issue_date if met else None
    elif milestone.condition == MilestoneCondition.ALL_SITES_ACCEPTED:
        met = bool(accepted_dates) and all(d is not None for d in accepted_dates)
        met_on = max(d for d in accepted_dates if d is not None) if met else None
    else:  # DATE
        met = milestone.condition_date is not None and today >= milestone.condition_date
        met_on = milestone.condition_date if met else None

    invoices = [i for i in milestone.invoices.all() if i.voided_at is None]
    receipts = [r for r in milestone.receipts.all() if r.voided_at is None]
    invoiced = sum((i.amount for i in invoices), ZERO)
    received = sum((r.amount for r in receipts), ZERO)
    latest = max((i.invoice_date for i in invoices), default=None)

    terms = project.payment_terms_days
    due_by = latest + timedelta(days=terms) if latest is not None and terms else None

    if invoiced > 0:
        if invoiced - received <= 0:
            state = State.PAID
        elif due_by is not None and today > due_by:
            # The latest invoice, not the first: a second invoice restarts the
            # clock (§4.19.7). No terms days, never overdue.
            state = State.OVERDUE
        elif received > 0:
            state = State.PART_PAID
        else:
            state = State.INVOICED
    else:
        state = State.DUE if met else State.NOT_DUE

    return MilestoneState(
        state=state,
        condition_met=met,
        met_on=met_on,
        amount=milestone_amount(milestone, contract_value),
        invoiced=invoiced,
        received=received,
        latest_invoice_date=latest,
        due_by=due_by,
    )


# --------------------------------------------------------------------------
# Reading a project's milestones
# --------------------------------------------------------------------------


def project_milestones(project):  # type: ignore[no-untyped-def]
    """The project's milestones with what they need to be judged, in two queries."""
    return list(
        ProjectMilestone.objects.filter(project=project)
        .select_related("project")
        .prefetch_related("invoices", "receipts")
        .order_by("sequence")
    )


def states_for_project(
    project, today: date, milestones: Iterable[ProjectMilestone] | None = None
) -> list[tuple[ProjectMilestone, MilestoneState]]:
    """Every milestone of ``project`` with its state; the sites are read once."""
    from network.project_sites import accepted_dates_of

    rows = list(project_milestones(project) if milestones is None else milestones)
    if not rows:
        return []
    accepted = accepted_dates_of(project)
    value = project.current_contract_value
    return [(m, milestone_state(m, today, accepted, contract_value=value)) for m in rows]


# --------------------------------------------------------------------------
# Changing milestones (Finance)
# --------------------------------------------------------------------------

_EDITABLE_AFTER_INVOICE = frozenset({"condition_date"})


def _invoiced_count(milestone: ProjectMilestone, *, include_void: bool) -> int:
    rows = MilestoneInvoice.objects.filter(milestone=milestone)
    if not include_void:
        rows = rows.filter(voided_at__isnull=True)
    return rows.count()


def _next_sequence(project) -> int:  # type: ignore[no-untyped-def]
    top = ProjectMilestone.objects.filter(project=project).aggregate(m=Max("sequence"))["m"]
    return (top or 0) + 1


def create_milestone(project, data: dict, *, actor, request=None) -> ProjectMilestone:  # type: ignore[no-untyped-def]
    if not project.po_number:
        raise ProjectHasNoPo()
    with transaction.atomic():
        milestone = ProjectMilestone.objects.create(
            organization_id=project.organization_id,
            project=project,
            sequence=_next_sequence(project),
            **data,
        )
        record(
            AuditAction.DOCUMENT_AMENDED,
            actor=actor,
            target=milestone,
            request=request,
            after=_snapshot(milestone),
            note=f"Milestone M{milestone.sequence} added to {project}.",
        )
    return milestone


def update_milestone(milestone: ProjectMilestone, data: dict, *, actor, request=None):  # type: ignore[no-untyped-def]
    """Re-share, rename or re-condition while nothing is invoiced; after that,
    only the condition date (§4.19.7)."""
    changed = {k for k, v in data.items() if getattr(milestone, k) != v}
    if changed - _EDITABLE_AFTER_INVOICE and _invoiced_count(milestone, include_void=False):
        raise MilestoneLocked()
    if not changed:
        return milestone
    before = _snapshot(milestone)
    with transaction.atomic():
        for key, value in data.items():
            setattr(milestone, key, value)
        milestone.save()
        record(
            AuditAction.DOCUMENT_AMENDED,
            actor=actor,
            target=milestone,
            request=request,
            before=before,
            after=_snapshot(milestone),
            note=f"Milestone M{milestone.sequence} changed on {milestone.project}.",
        )
    return milestone


def delete_milestone(milestone: ProjectMilestone, *, actor, request=None) -> None:  # type: ignore[no-untyped-def]
    """Only while no invoice (even a voided one) is on it: invoices are never
    deleted, so the milestone they name has to stay."""
    if _invoiced_count(milestone, include_void=True) or milestone.receipts.exists():
        raise MilestoneLocked(
            "This milestone has invoices or receipts on record, so it cannot be removed."
        )
    project, sequence = milestone.project, milestone.sequence
    before = _snapshot(milestone)
    with transaction.atomic():
        milestone.delete()
        # Close the gap, lowest first, so M-numbers stay M1..Mn.
        for later in ProjectMilestone.objects.filter(
            project=project, sequence__gt=sequence
        ).order_by("sequence"):
            later.sequence -= 1
            later.save(update_fields=["sequence", "updated_at"])
        record(
            AuditAction.DOCUMENT_AMENDED,
            actor=actor,
            target_label=f"Milestone M{sequence}",
            request=request,
            before=before,
            note=f"Milestone M{sequence} removed from {project}.",
        )


def seed_default_milestones(project, *, actor=None, request=None) -> list[ProjectMilestone]:  # type: ignore[no-untyped-def]
    """M1 Deposit, M2 Conditional acceptance, M3 Final acceptance, shares empty
    (§4.19.7). Used when a PO is attached and by Finance's "Add default
    milestones"."""
    created = []
    for sequence, (name, condition) in enumerate(DEFAULT_MILESTONES, start=1):
        created.append(
            ProjectMilestone.objects.create(
                organization_id=project.organization_id,
                project=project,
                sequence=sequence,
                name=name,
                share_type=MilestoneShare.PERCENT,
                share_value=None,
                condition=condition,
            )
        )
    if actor is not None:
        record(
            AuditAction.DOCUMENT_AMENDED,
            actor=actor,
            target_label=str(project),
            request=request,
            after={"milestones": [m.name for m in created]},
            note=f"Default milestones added to {project}.",
        )
    return created


def add_default_milestones(project, *, actor, request=None) -> list[ProjectMilestone]:  # type: ignore[no-untyped-def]
    if not project.po_number:
        raise ProjectHasNoPo()
    with transaction.atomic():
        if ProjectMilestone.objects.filter(project=project).exists():
            raise MilestonesExist()
        return seed_default_milestones(project, actor=actor, request=request)


def _snapshot(milestone: ProjectMilestone) -> dict:
    return {
        "sequence": milestone.sequence,
        "name": milestone.name,
        "share_type": milestone.share_type,
        "share_value": str(milestone.share_value) if milestone.share_value is not None else None,
        "condition": milestone.condition,
        "condition_date": str(milestone.condition_date) if milestone.condition_date else None,
    }


# --------------------------------------------------------------------------
# Invoices and receipts (Finance, finance.approve)
# --------------------------------------------------------------------------


def _locked(milestone: ProjectMilestone) -> ProjectMilestone:
    """Serialise two people recording against one milestone: the receipt cap is a
    sum, and a sum read twice can be exceeded twice."""
    return ProjectMilestone.objects.select_for_update(of=("self",)).select_related("project").get(
        pk=milestone.pk
    )


def _live_sum(model, milestone: ProjectMilestone) -> Decimal:  # type: ignore[no-untyped-def]
    from django.db.models import Sum

    return model.objects.filter(milestone=milestone, voided_at__isnull=True).aggregate(
        s=Sum("amount")
    )["s"] or ZERO


def record_invoice(  # type: ignore[no-untyped-def]
    milestone: ProjectMilestone,
    *,
    invoice_number: str,
    invoice_date: date,
    amount: Decimal,
    actor,
    request=None,
) -> tuple[MilestoneInvoice, str]:
    """Record an invoice. Returns it and a warning (``""`` when there is none):
    invoicing past the milestone's amount is allowed but pointed out."""
    with transaction.atomic():
        milestone = _locked(milestone)
        invoice = MilestoneInvoice.objects.create(
            organization_id=milestone.organization_id,
            milestone=milestone,
            invoice_number=invoice_number,
            invoice_date=invoice_date,
            amount=amount,
            recorded_by=actor,
        )
        record(
            AuditAction.DOCUMENT_POSTED,
            actor=actor,
            target=invoice,
            request=request,
            after={"amount": str(amount), "invoice_number": invoice_number},
            note=f"Invoice {invoice_number} recorded for {milestone.project}.",
        )
    warning = ""
    cap = milestone_amount(milestone, milestone.project.current_contract_value)
    if cap is not None and _live_sum(MilestoneInvoice, milestone) > cap:
        warning = f"Invoiced is now more than this milestone's amount ({cap})."
    return invoice, warning


def record_receipt(  # type: ignore[no-untyped-def]
    milestone: ProjectMilestone,
    *,
    received_on: date,
    amount: Decimal,
    reference: str = "",
    actor,
    request=None,
) -> MilestoneReceipt:
    with transaction.atomic():
        milestone = _locked(milestone)
        room = _live_sum(MilestoneInvoice, milestone) - _live_sum(MilestoneReceipt, milestone)
        if amount > room:
            raise ReceiptExceedsInvoiced(
                f"Only {room} is invoiced and not yet received on this milestone.",
                field_errors={"amount": [f"Only {room} is left to receive."]},
                details={"room": str(room)},
            )
        receipt = MilestoneReceipt.objects.create(
            organization_id=milestone.organization_id,
            milestone=milestone,
            received_on=received_on,
            amount=amount,
            reference=reference,
            recorded_by=actor,
        )
        record(
            AuditAction.DOCUMENT_POSTED,
            actor=actor,
            target=receipt,
            request=request,
            after={"amount": str(amount), "reference": reference},
            note=f"Receipt recorded for {milestone.project} M{milestone.sequence}.",
        )
    return receipt


def _require_reason(reason: str) -> str:
    reason = (reason or "").strip()
    if not reason:
        from rest_framework.exceptions import ValidationError

        raise ValidationError({"reason": ["Say why it is being voided."]})
    return reason


def void_invoice(  # type: ignore[no-untyped-def]
    invoice: MilestoneInvoice, *, reason: str, actor, request=None
) -> MilestoneInvoice:
    reason = _require_reason(reason)
    from django.utils import timezone

    with transaction.atomic():
        milestone = _locked(invoice.milestone)
        invoice = MilestoneInvoice.objects.select_for_update().get(pk=invoice.pk)
        if invoice.voided_at is not None:
            return invoice
        remaining = _live_sum(MilestoneInvoice, milestone) - invoice.amount
        if remaining < _live_sum(MilestoneReceipt, milestone):
            raise ReceiptExceedsInvoiced(
                "Money has been received against this invoice. Void the receipt first.",
            )
        invoice.voided_at = timezone.now()
        invoice.voided_by = actor
        invoice.void_reason = reason
        invoice.save()
        record(
            AuditAction.DOCUMENT_VOIDED,
            actor=actor,
            target=invoice,
            request=request,
            note=f"Invoice {invoice.invoice_number} voided: {reason}",
        )
    return invoice


def void_receipt(  # type: ignore[no-untyped-def]
    receipt: MilestoneReceipt, *, reason: str, actor, request=None
) -> MilestoneReceipt:
    reason = _require_reason(reason)
    from django.utils import timezone

    with transaction.atomic():
        _locked(receipt.milestone)
        receipt = MilestoneReceipt.objects.select_for_update().get(pk=receipt.pk)
        if receipt.voided_at is not None:
            return receipt
        receipt.voided_at = timezone.now()
        receipt.voided_by = actor
        receipt.void_reason = reason
        receipt.save()
        record(
            AuditAction.DOCUMENT_VOIDED,
            actor=actor,
            target=receipt,
            request=request,
            note=f"Receipt {receipt.reference or receipt.pk} voided: {reason}",
        )
    return receipt
