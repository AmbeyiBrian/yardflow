"""Shared setup for tests that need an expense to have been through approval.

An expense made with ``ProjectExpense.objects.create`` has no approval requests,
so it cannot be decided. These put one through the real two levels (§4.17.3)
rather than stamping a status, so the tests keep exercising the engine.
"""

from __future__ import annotations

from approvals.engine import document_type_of
from approvals.models import ApprovalRequest
from commercials import finance
from commercials.models import ExpenseStatus


def submit(entry):  # type: ignore[no-untyped-def]
    """Route an entry created directly, as ``record_expense`` would."""
    finance._route(entry, entry.recorded_by)
    return entry


def _ensure_routed(entry):  # type: ignore[no-untyped-def]
    exists = ApprovalRequest.objects.filter(
        document_type=document_type_of(entry), document_id=str(entry.pk)
    ).exists()
    if not exists:
        submit(entry)


def approve_through(entry, *, pm, finance_user):  # type: ignore[no-untyped-def]
    """The PM approves (unless that level was skipped), then Finance."""
    _ensure_routed(entry)
    if entry.status == ExpenseStatus.PENDING_PM:
        finance.decide(entry, actor=pm, approved=True)
    finance.decide(entry, actor=finance_user, approved=True)
    return entry


def reject_at_pm(entry, *, pm, reason="No receipt."):  # type: ignore[no-untyped-def]
    _ensure_routed(entry)
    finance.decide(entry, actor=pm, approved=False, reason=reason)
    return entry
