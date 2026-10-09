"""Reversing expenses and seeding categories (§4.14; O16, D29).

Deciding moved to ``commercials.finance.decide`` (§4.17.3): approval is two
levels through the engine now, not one status flip here."""

from __future__ import annotations

from django.utils import timezone

from accounts.permissions_registry import PERM
from accounts.services import resolve_permissions
from approvals.engine import NotAnApprover
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


def reverse_expense(
    expense: ProjectExpense, *, actor, reason: str, request=None
) -> ProjectExpense:
    """Undo an approved expense by recording its opposite (O16).

    Never an edit, for the reason the ledger is never edited (§3.2): the
    correction and the thing it corrects should both stay visible. The reversal
    is approved on creation — it is the manager's (or Finance's) own act, and
    asking them to approve their own correction would be theatre.
    """
    if expense.status not in COSTED_STATUSES:
        raise ExpenseNotDecidable("Only an approved expense needs reversing.")
    if expense.reverses_id:
        raise ExpenseNotDecidable("A reversal cannot itself be reversed.")
    if expense.reversals.exists():
        raise ExpenseNotDecidable("This expense has already been reversed.")
    # §4.17.2: the PM whose budget it lands on, or Finance. Held directly, as
    # at the approval levels — a delegation does not lend a signature (D22).
    permissions = resolve_permissions(actor)
    is_finance = permissions.has(PERM.FINANCE_APPROVE) and not permissions.is_delegated(
        PERM.FINANCE_APPROVE
    )
    if expense.project.manager_id != actor.pk and not is_finance:
        raise NotAnApprover(
            "Only the manager of this project, or Finance, can reverse its expenses."
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
        # Fuel by vehicle (R14) nets the reversal against the same vehicle.
        vehicle=expense.vehicle,
        vehicle_reg=expense.vehicle_reg,
        litres=expense.litres,
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
