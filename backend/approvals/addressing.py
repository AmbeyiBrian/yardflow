"""Which approval requests are the caller's to answer (F4, F5, R4).

One definition shared by ``/approvals/pending`` and the finance entries'
``/pending`` (§4.17.6), so "waiting on me" cannot mean one thing on the approvals
screen and another on the money screen.
"""

from __future__ import annotations

from django.db.models import Exists, OuterRef, Q, QuerySet
from django.utils import timezone

from accounts.permissions_registry import PERM
from accounts.services import resolve_permissions
from approvals.engine import WORK_DAY_DOCUMENT_TYPES
from approvals.models import ApprovalRequest, ApprovalRequestStatus

OPEN_STATUSES = (ApprovalRequestStatus.PENDING, ApprovalRequestStatus.ESCALATED)


def open_requests_addressed_to(user, queryset: QuerySet) -> QuerySet:  # type: ignore[no-untyped-def]
    """``queryset`` narrowed to open requests whose next level is addressed to ``user``.

    By role (or a delegation of it, F5), by name (O6, R4) or by a permission
    held directly (R4). Narrowed in the database so the result stays a
    queryset: cursor pagination orders by a column (§6, N-2).
    """
    now = timezone.now()
    permissions = resolve_permissions(user)
    role_ids = set(user.user_roles.values_list("role_id", flat=True))
    # F5: a delegation confers the role for a period.
    role_ids.update(
        user.delegations_received.filter(
            is_revoked=False, starts_at__lte=now, ends_at__gte=now
        ).values_list("role_id", flat=True)
    )
    # A delegation lends a role, never a named signature or a permission level
    # (D22), so only permissions held directly are matched.
    held = permissions.codenames - set(permissions.delegated_from)

    addressed = Q(required_role_id__in=role_ids) | Q(required_user=user)
    if held:
        addressed |= Q(required_permission__in=held)
    # B4: a blanket approval permission may act on any *role* level
    # (`can_approve`), so those stay visible to its holder. A person- or
    # permission-addressed level is never theirs by that route.
    # Not a work day (R13): a Director slice is the Director role's alone.
    if permissions.has(PERM.GATE_OUT_APPROVE):
        addressed |= Q(required_role__isnull=False) & ~Q(
            document_type__in=WORK_DAY_DOCUMENT_TYPES
        )

    # Levels answer in order, so a Finance request is not the caller's business
    # while the PM is still to answer: it could not be acted on. Work-day slices
    # are all level 1, so one slice never waits on another (R13).
    earlier_open = ApprovalRequest.objects.filter(
        document_type=OuterRef("document_type"),
        document_id=OuterRef("document_id"),
        level__lt=OuterRef("level"),
        status__in=OPEN_STATUSES,
    )
    return queryset.filter(status__in=OPEN_STATUSES).filter(addressed).filter(~Exists(earlier_open))
