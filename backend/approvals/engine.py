"""The approval engine (design §5; F3, F4, F5, E5, J3).

§5: "lives in ``approvals/engine.py`` and is the only place routing is decided."

That single-place rule matters more here than anywhere else in the system. If two
code paths could decide whether something needs approving, one of them would
eventually decide "no" for a case the other would have caught — and the control
the whole product exists to provide would have a hole in it.

**The fact set is complete even though only criticality is consulted.** That is
F3's explicit requirement and the reason `collect_facts` gathers ownership,
quantities, monetary value and destination that nothing currently reads: enabling
a dimension later is then a predicate function, not a migration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from accounts.models import Role
from approvals.models import (
    ApprovalAction,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalRequestStatus,
    ApprovalRule,
)
from catalogue.models import CRITICALITY_ORDER, Criticality
from core.exceptions import DomainError


class SelfApprovalNotAllowed(DomainError):
    """F3: a requester must not approve their own request.

    Default off, and turning it on is a deliberate act by an admin — which is why
    this is a domain error naming the setting rather than a silent allowance.
    """

    code = "SELF_APPROVAL_NOT_ALLOWED"
    status_code = 403
    default_message = (
        "You raised this request, so you cannot approve it. Ask another approver."
    )


class NotAnApprover(DomainError):
    """The caller does not hold the role this level requires."""

    code = "NOT_AN_APPROVER"
    status_code = 403
    default_message = "You do not hold the role required to approve this."


class NothingToApprove(DomainError):
    code = "NOTHING_TO_APPROVE"
    status_code = 409
    default_message = "There is no approval outstanding on this document."


@dataclass
class ApprovalFacts:
    """Everything any routing predicate might need (§5.1, F3).

    Deliberately larger than v1 uses. F3 requires that switching on a new
    dimension needs no schema change; that is only true if the facts are already
    being gathered when the rule is written.
    """

    #: Categories represented on the document.
    category_ids: set[int] = field(default_factory=set)
    #: The criticality of each line's category, after inheritance (C1).
    criticalities: set[str] = field(default_factory=set)
    #: The highest criticality present — what v1 actually routes on.
    highest_criticality: str = Criticality.NONE

    # --- gathered but unused in v1 (F3) ---------------------------------
    involves_client_owned: bool = False
    client_ids: set[int] = field(default_factory=set)
    total_quantity: Decimal = Decimal("0")
    quantity_by_item: dict[int, Decimal] = field(default_factory=dict)
    monetary_value: Decimal | None = None
    destination_type: str = ""
    purpose_type: str = ""
    line_count: int = 0

    def as_dict(self) -> dict:
        """For predicate evaluation and for recording why a decision was made."""
        return {
            "category_ids": sorted(self.category_ids),
            "criticalities": sorted(self.criticalities),
            "highest_criticality": self.highest_criticality,
            "involves_client_owned": self.involves_client_owned,
            "client_ids": sorted(self.client_ids),
            "total_quantity": str(self.total_quantity),
            "monetary_value": str(self.monetary_value) if self.monetary_value is not None else None,
            "destination_type": self.destination_type,
            "purpose_type": self.purpose_type,
            "line_count": self.line_count,
        }


def line_quantity(line) -> Decimal:
    """How much one line is asking for, whatever kind of document it is on.

    A gate-out line calls it ``requested_qty`` because some of it may not be
    released; a disposition or disposal line calls it ``quantity`` because all of
    it is going. Routing does not care about that distinction, and the engine
    stays document-agnostic by asking here rather than everywhere.
    """
    for name in ("requested_qty", "quantity"):
        value = getattr(line, name, None)
        if value is not None:
            return value
    return Decimal("0")


def collect_facts(document) -> ApprovalFacts:
    """Gather every fact a routing rule might consult (§5.1).

    Takes any document with ``lines`` — a gate-out, a disposition, a disposal.
    One evaluator for all of them, because a tenant's rule about high-criticality
    material should not need restating per document type, and two evaluators
    would eventually disagree.
    """
    facts = ApprovalFacts()

    settings = document.organization.settings
    money_enabled = settings.money_tracking_enabled
    value = Decimal("0")

    lines = document.lines.select_related(
        "item_type", "item_type__category", "item_type__category__parent", "owner_client"
    ).all()

    for line in lines:
        facts.line_count += 1
        category = line.item_type.category
        facts.category_ids.add(category.pk)

        criticality = category.effective_criticality()
        facts.criticalities.add(criticality)

        quantity = line_quantity(line)
        facts.total_quantity += quantity
        facts.quantity_by_item[line.item_type_id] = (
            facts.quantity_by_item.get(line.item_type_id, Decimal("0")) + quantity
        )

        if line.owner_client_id:
            facts.involves_client_owned = True
            facts.client_ids.add(line.owner_client_id)

        if money_enabled and line.item_type.unit_cost is not None:
            value += line.item_type.unit_cost * quantity

    facts.highest_criticality = highest_of(facts.criticalities)
    facts.monetary_value = value if money_enabled else None
    facts.destination_type = _destination_type(document)
    # A disposition routes on its decision and a disposal on its method; both
    # read as the document's purpose, which is what a rule would name.
    facts.purpose_type = (
        getattr(document, "purpose_type", "")
        or getattr(document, "decision", "")
        or getattr(document, "method", "")
    )

    return facts


def highest_of(criticalities) -> str:
    """F3: "a gate-out containing lines from several categories takes the
    **highest** applicable approval level"."""
    if not criticalities:
        return Criticality.NONE
    return max(criticalities, key=lambda value: CRITICALITY_ORDER.get(value, 0))


def _destination_type(document) -> str:
    """Where it is going, for documents that have a destination at all.

    ``getattr`` rather than attribute access: a disposal has no destination —
    the material is gone — and that reads as an empty string rather than an
    error, so a rule keyed on destination simply does not match it.
    """
    if getattr(document, "site_id", None):
        return "SITE"
    if getattr(document, "project_id", None):
        return "PROJECT"
    if getattr(document, "client_id", None):
        return "CLIENT"
    if getattr(document, "to_location_id", None):
        return "LOCATION"
    return ""


#: The dimensions the evaluator understands (F3). Empty in v1's rules, but a
#: tenant enabling one later needs no migration — and the API refuses anything
#: outside this set rather than storing a rule nothing has tested.
KNOWN_CONDITION_KEYS = frozenset(
    {
        "involves_client_owned",
        "min_total_quantity",
        "min_monetary_value",
        "destination_type",
        "purpose_type",
    }
)


def predicate_matches(conditions: dict, facts: ApprovalFacts) -> bool:
    """Evaluate a rule's extra conditions against the facts (§5.1, F3).

    **Empty conditions match everything**, which is every rule in v1. The
    evaluator exists so that a tenant enabling "client-owned material always
    needs the owner" later is a data change, not a release.

    Unknown keys deliberately do **not** match. A condition nothing understands
    must not silently reduce the approval required — failing closed is the only
    safe direction for a control.
    """
    if not conditions:
        return True

    for key, expected in conditions.items():
        if key == "involves_client_owned":
            if facts.involves_client_owned is not bool(expected):
                return False
        elif key == "min_total_quantity":
            if facts.total_quantity < Decimal(str(expected)):
                return False
        elif key == "min_monetary_value":
            if facts.monetary_value is None or facts.monetary_value < Decimal(str(expected)):
                return False
        elif key == "destination_type":
            if facts.destination_type != expected:
                return False
        elif key == "purpose_type":
            if facts.purpose_type != expected:
                return False
        else:
            # Fail closed: an unrecognised condition means this rule cannot be
            # confirmed as satisfied, so it does not match and cannot be used to
            # justify a *lower* level of approval.
            return False

    return True


@dataclass(frozen=True)
class RequiredLevel:
    """One level of approval a document needs."""

    level: int
    role: Role
    rule: ApprovalRule


def required_levels(document, *, facts: ApprovalFacts | None = None) -> list[RequiredLevel]:
    """Which approvals this document needs (§5.1, F3).

    Returns an empty list when nothing is required — §5.2's auto-approval case,
    which still writes an ``ApprovalAction`` so the trail has no gap.
    """
    facts = facts or collect_facts(document)

    rules = (
        ApprovalRule.objects.filter(is_active=True)
        .select_related("required_role", "category")
        .order_by("-sequence")
    )

    matched: list[ApprovalRule] = []
    for rule in rules:
        # v1 routes on criticality only, and on the *highest* present (F3).
        if rule.criticality != facts.highest_criticality:
            continue
        # A rule scoped to one category applies only if that category is present.
        if rule.category_id and rule.category_id not in facts.category_ids:
            continue
        if not predicate_matches(rule.conditions, facts):
            continue
        matched.append(rule)

    return _dedupe_by_sequence(matched)


def _dedupe_by_sequence(rules: list[ApprovalRule]) -> list[RequiredLevel]:
    """One level per sequence, lowest first.

    Two rules at the same sequence would mean two approvers at the same level,
    which Q4 settles as out of scope for v1 ("assumed one"). Keeping the first
    means a tenant who configures both gets one approval, not a silent
    requirement for two that nobody is told about.
    """
    by_sequence: dict[int, ApprovalRule] = {}
    for rule in rules:
        by_sequence.setdefault(rule.sequence, rule)

    return [
        RequiredLevel(level=index + 1, role=rule.required_role, rule=rule)
        for index, (_sequence, rule) in enumerate(sorted(by_sequence.items()))
    ]


# --------------------------------------------------------------------------
# Hardcoded escalations (§5.2) — T4.15
# --------------------------------------------------------------------------


def requires_approval_regardless(document) -> tuple[bool, str]:
    """The two escalations no configuration can switch off (§5.2).

    §5.2: "hardcoded, non-configurable escalations: any disposal of client-owned
    material (J3), and any stock adjustment touching client-owned stock (E5)."

    Deliberately not rules in the database. A tenant editing their approval rules
    must not be able to remove these — writing off an operator's property is not
    a decision a contractor gets to make unilaterally, whatever their settings
    say.
    """
    from stock.models import StockCount

    if isinstance(document, StockCount):
        if document.touches_client_owned_stock:
            return True, (
                "This count adjusts client-owned stock, which always requires "
                "approval (E5)."
            )

    # Disposals arrive in Phase 6 (T6.2); the same check covers them there.
    if getattr(document, "involves_client_owned_material", False):
        return True, (
            "Disposing of client-owned material always requires approval (J3)."
        )

    return False, ""


# --------------------------------------------------------------------------
# Creating and resolving requests — T4.5, T4.6, T4.8
# --------------------------------------------------------------------------


def document_type_of(document) -> str:
    """How a document is addressed in the approval tables.

    ``ApprovalRequest`` stores its subject as ``(document_type, document_id)``
    text rather than a foreign key, so one table serves gate-outs, dispositions,
    disposals and whatever Phase 7 adds. The label is the model's own, so a
    caller cannot invent a type the resolver then fails to load.
    """
    return document._meta.label


def create_requests(document, *, requested_by=None) -> list[ApprovalRequest]:
    """Create one pending request per required level (§5.1, F3)."""
    settings = document.organization.settings
    escalation_hours = settings.approval_escalation_hours or 24
    due_at = timezone.now() + timedelta(hours=escalation_hours)

    requests: list[ApprovalRequest] = []
    for required in required_levels(document):
        requests.append(
            ApprovalRequest.objects.create(
                organization_id=document.organization_id,
                document_type=document_type_of(document),
                document_id=str(document.pk),
                document_number=getattr(document, "number", "") or "",
                level=required.level,
                required_role=required.role,
                requested_by=requested_by,
                due_at=due_at,
            )
        )
    return requests


def record_auto_approval(document, *, note: str = "") -> ApprovalAction:
    """§5.2: "zero matched rules means auto-approval, recorded as an
    ApprovalAction with decision = AUTO so the audit trail never has a gap."

    A request row is created too, resolved immediately. Without it, an
    auto-approved document would have no approval record at all, and "why did
    this leave without approval?" would have no answer.
    """
    request = ApprovalRequest.objects.create(
        organization_id=document.organization_id,
        document_type=document_type_of(document),
        document_id=str(document.pk),
        document_number=getattr(document, "number", "") or "",
        # Level 0 and no required role: nobody was asked, because nothing
        # required asking.
        level=0,
        required_role=None,
        status=ApprovalRequestStatus.APPROVED,
        resolved_at=timezone.now(),
    )

    from core.models import AuthMethod

    return ApprovalAction.objects.create(
        organization_id=document.organization_id,
        approval_request=request,
        actor=None,
        decision=ApprovalDecision.AUTO,
        auth_method=AuthMethod.SYSTEM,
        reason=note or "No approval rule applies to this request.",
    )


def next_pending_request(document) -> ApprovalRequest | None:
    """The level currently waiting on a decision.

    Levels are answered in order, so an owner is not asked before the supervisor
    has looked at it.
    """
    return (
        ApprovalRequest.objects.filter(
            document_type=document_type_of(document),
            document_id=str(document.pk),
            status__in=(ApprovalRequestStatus.PENDING, ApprovalRequestStatus.ESCALATED),
        )
        .order_by("level")
        .first()
    )


def can_approve(user, approval_request: ApprovalRequest, *, document=None) -> tuple[bool, str]:
    """Whether ``user`` may decide this request, and why not if they may not.

    Two checks, in this order:

    1. **Self-approval** (F3). Blocked unless the tenant has explicitly enabled
       it. Checked first because it is the more surprising refusal, and the
       clearer message.
    2. **Role**, including any active delegation (F5).
    """
    from accounts.services import resolve_permissions

    if document is not None:
        requester_id = getattr(document, "requested_by_id", None)
        if requester_id and requester_id == user.pk:
            settings = approval_request.organization.settings
            if not settings.allow_self_approval:
                return False, "self"

    holds_role = user.user_roles.filter(role_id=approval_request.required_role_id).exists()
    if holds_role:
        return True, ""

    # F5: a delegation may confer the role for a period.
    delegated = user.delegations_received.filter(
        is_revoked=False,
        starts_at__lte=timezone.now(),
        ends_at__gte=timezone.now(),
        role_id=approval_request.required_role_id,
    ).exists()
    if delegated:
        return True, "delegated"

    # A user with blanket approval permission may also act, so a tenant that
    # prefers permissions to roles is not locked out of its own approvals (B4).
    from accounts.permissions_registry import PERM

    if resolve_permissions(user).has(PERM.GATE_OUT_APPROVE):
        return True, ""

    return False, "role"


def active_delegation_for(user, role_id):
    """The delegation letting ``user`` act in ``role_id``, if any (F5)."""
    return user.delegations_received.filter(
        is_revoked=False,
        starts_at__lte=timezone.now(),
        ends_at__gte=timezone.now(),
        role_id=role_id,
    ).select_related("from_user").first()


# --------------------------------------------------------------------------
# Recording one decision — shared by every approvable document
# --------------------------------------------------------------------------


def record_decision(
    document,
    *,
    actor,
    decision: str,
    reason: str = "",
    auth_method: str = "",
    webauthn_credential=None,
    ip=None,
    user_agent: str = "",
) -> tuple[ApprovalRequest, ApprovalRequest | None]:
    """Record one approval or rejection and resolve that level.

    Returns ``(the request just decided, the next one still pending)`` — so a
    caller knows whether the document is now fully approved without repeating
    the query.

    This exists because approval is a *control*, and a control implemented twice
    is a control with two behaviours. Everything that makes it trustworthy is
    here once: that only the required role may decide, that self-approval is
    refused unless the tenant allowed it (§5.3), that a delegated decision reads
    as "X on behalf of Y" and never as Y (§4.2), and that rejecting supersedes
    every later level rather than leaving them pending for an approver who will
    never be asked.

    The caller keeps what is genuinely its own: which status the document moves
    to, and what it emits.
    """
    from core.models import AuthMethod

    approval_request = next_pending_request(document)
    if approval_request is None:
        raise NothingToApprove()

    allowed, why = can_approve(actor, approval_request, document=document)
    if not allowed:
        if why == "self":
            raise SelfApprovalNotAllowed()
        role = approval_request.required_role
        role_name = role.name if role is not None else "an approver"
        raise NotAnApprover(f"Deciding this needs the {role_name} role.")

    delegation = None
    on_behalf_of = None
    if why == "delegated":
        delegation = active_delegation_for(actor, approval_request.required_role_id)
        if delegation is not None:
            on_behalf_of = delegation.from_user

    ApprovalAction.objects.create(
        organization_id=document.organization_id,
        approval_request=approval_request,
        actor=actor,
        on_behalf_of=on_behalf_of,
        delegation=delegation,
        decision=decision,
        reason=reason,
        auth_method=auth_method or AuthMethod.PASSWORD,
        webauthn_credential=webauthn_credential,
        ip=ip,
        user_agent=user_agent,
    )

    approval_request.status = (
        ApprovalRequestStatus.APPROVED
        if decision == ApprovalDecision.APPROVED
        else ApprovalRequestStatus.REJECTED
    )
    approval_request.resolved_at = timezone.now()
    approval_request.save(update_fields=["status", "resolved_at", "updated_at"])

    if decision != ApprovalDecision.APPROVED:
        # Later levels are moot. Leaving them pending would put a request in
        # front of an approver that has already been refused.
        ApprovalRequest.objects.filter(
            document_type=document_type_of(document),
            document_id=str(document.pk),
            status__in=(ApprovalRequestStatus.PENDING, ApprovalRequestStatus.ESCALATED),
        ).update(status=ApprovalRequestStatus.SUPERSEDED, resolved_at=timezone.now())
        return approval_request, None

    return approval_request, next_pending_request(document)
