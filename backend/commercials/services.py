"""Deciding and reversing expenses (§4.14; O16, D29)."""

from __future__ import annotations

from django.utils import timezone

from commercials.models import (
    COSTED_STATUSES,
    DEFAULT_EXPENSE_CATEGORIES,
    ExpenseCategory,
    ExpenseStatus,
    ProjectExpense,
)
from core.audit import record
from core.exceptions import DomainError
from core.models import AuditAction


class ExpenseNotDecidable(DomainError):
    """This expense is not in a state where a decision makes sense."""


class NotTheProjectManager(DomainError):
    """Only the manager whose budget it lands on decides (D29)."""


def seed_expense_categories(organization) -> list[ExpenseCategory]:
    """Give a new tenant something to file against (O16).

    Seeded rather than left empty for the reason approval rules are (§5.2): an
    empty list looks like a feature that does nothing, and the first person to
    record an expense should not have to invent a taxonomy first.
    """
    created = []
    for name, code, kind in DEFAULT_EXPENSE_CATEGORIES:
        category, was_created = ExpenseCategory.objects.get_or_create(
            organization=organization, name=name, defaults={"code": code, "kind": kind}
        )
        if was_created:
            created.append(category)
    return created


def decide_expense(
    expense: ProjectExpense,
    *,
    actor,
    approved: bool,
    reason: str = "",
    request=None,
) -> ProjectExpense:
    """Approve or reject an expense (O16, D29).

    It reaches project cost **only** on approval. This is the one cost line
    with no ledger movement and no contract behind it, so it is also the only
    one where a second person looks at the figure before it counts.
    """
    # T15.1 keeps the single-level behaviour; T15.3 replaces this function with
    # the two-level flow through the approval engine (§4.17.3).
    if expense.status != ExpenseStatus.PENDING_PM:
        raise ExpenseNotDecidable(
            f"This expense was already {expense.get_status_display().lower()}."
        )

    project = expense.project
    if project.manager_id != actor.pk:
        raise NotTheProjectManager(
            "Only the manager of this project can decide its expenses (D29)."
        )

    if not approved and not reason:
        raise ExpenseNotDecidable("Rejecting an expense needs a reason.")

    if approved:
        # The guard allows only the §4.17.3 moves, so this goes via the Finance
        # level in two saves rather than jumping PENDING_PM to APPROVED.
        expense.status = ExpenseStatus.PENDING_FINANCE
        expense.save()
    expense.status = ExpenseStatus.APPROVED if approved else ExpenseStatus.REJECTED
    expense.decided_by = actor
    expense.decided_at = timezone.now()
    expense.decision_reason = "" if approved else reason
    expense.save()

    # T15.2: the status is a projection of the approval requests (§4.17.1), so a
    # decision made here must not leave them open. They would otherwise show
    # Finance an expense that is already APPROVED. Superseded rather than
    # approved: this legacy path is one signature, and recording a Finance
    # approval nobody gave would put a false signature on the trail.
    from approvals.models import ApprovalRequest, ApprovalRequestStatus

    ApprovalRequest.objects.filter(
        document_type=expense._meta.label,
        document_id=str(expense.pk),
        status__in=(ApprovalRequestStatus.PENDING, ApprovalRequestStatus.ESCALATED),
    ).update(status=ApprovalRequestStatus.SUPERSEDED, resolved_at=timezone.now())

    record(
        AuditAction.STATUS_CHANGED,
        actor=actor,
        organization=expense.organization_id,
        target=expense,
        target_label=str(expense),
        request=request,
        note=f"Expense {'approved' if approved else 'rejected'} on {project}.",
    )
    return expense


def reverse_expense(
    expense: ProjectExpense, *, actor, reason: str, request=None
) -> ProjectExpense:
    """Undo an approved expense by recording its opposite (O16).

    Never an edit, for the reason the ledger is never edited (§3.2): the
    correction and the thing it corrects should both stay visible. The reversal
    is approved on creation — it is the manager's own act, and asking them to
    approve their own correction would be theatre.
    """
    if expense.status not in COSTED_STATUSES:
        raise ExpenseNotDecidable("Only an approved expense needs reversing.")
    if expense.reverses_id:
        raise ExpenseNotDecidable("A reversal cannot itself be reversed.")
    if expense.reversals.exists():
        raise ExpenseNotDecidable("This expense has already been reversed.")
    if expense.project.manager_id != actor.pk:
        raise NotTheProjectManager(
            "Only the manager of this project can reverse its expenses (D29)."
        )
    if not reason:
        raise ExpenseNotDecidable("Reversing an expense needs a reason.")

    reversal = ProjectExpense.objects.create(
        organization_id=expense.organization_id,
        project=expense.project,
        job=expense.job,
        category=expense.category,
        amount=expense.amount,
        incurred_on=expense.incurred_on,
        description=f"Reversal of expense {expense.pk}: {reason}",
        recorded_by=actor,
        status=ExpenseStatus.APPROVED,
        decided_by=actor,
        decided_at=timezone.now(),
        reverses=expense,
    )

    record(
        AuditAction.STATUS_CHANGED,
        actor=actor,
        organization=expense.organization_id,
        target=reversal,
        target_label=str(reversal),
        request=request,
        note=f"Reversed expense {expense.pk} on {expense.project}: {reason}",
    )
    return reversal
