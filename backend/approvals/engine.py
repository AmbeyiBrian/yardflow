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
    """One level of approval a document needs.

    Addressed to a **role** by the criticality rules, to a **person** by the
    project branch (`O6`), or to whoever holds a **permission** by the finance
    branch (R4). At most one of the three is set, which is the same invariant
    ``ApprovalRequest`` carries in the database.
    """

    level: int
    role: Role | None = None
    rule: ApprovalRule | None = None
    user: object | None = None
    permission: str = ""

    @property
    def label(self) -> str:
        """Who this level is waiting on, for a log line or a notification."""
        if self.permission:
            return f"holders of {self.permission}"
        if self.user is not None:
            return getattr(self.user, "full_name", "") or str(self.user)
        return self.role.name if self.role is not None else "nobody"


class ProjectHasNoActiveManager(DomainError):
    """A project's material cannot move because its manager cannot act (D28).

    Deliberately an error rather than a fallback. Falling through to the
    criticality rules would quietly restore a weaker control at the one moment
    nobody is watching for it, and the storekeeper would never learn why the
    approval they were waiting for could not arrive.
    """


def _project_level(document) -> RequiredLevel | None:
    """The manager's level for this document, if it belongs to a project.

    ``None`` when there is no project, or the project has no manager — an
    unpriced project is the old work order (D20) and nobody is budgeting it.
    """
    project = project_of(document)
    if project is None or project.manager_id is None:
        return None

    manager = project.manager
    if not manager.is_active:
        raise ProjectHasNoActiveManager(
            f"{manager.full_name or manager} manages {project}, and their "
            f"account is not active. Material for this project cannot move "
            f"until an owner assigns a new manager (D28).",
            details={"project": str(project), "manager": str(manager)},
        )
    return RequiredLevel(level=1, user=manager)


def project_of(document):  # type: ignore[no-untyped-def]
    """The project a document costs to, if any (`O5`, `O6`).

    One place, so routing and costing can never disagree about which project a
    movement belongs to.
    """
    attribution = getattr(document, "project_attribution", None)
    if attribution is not None:
        return attribution
    return getattr(document, "project", None)


#: Money-out entries (R4, §4.17.3). Matched on the label the approval tables
#: already store, so ``can_approve`` can tell them apart from a request alone,
#: without importing ``commercials`` into the engine.
FINANCE_DOCUMENT_TYPES = frozenset(
    {"commercials.ProjectExpense", "commercials.AllowanceRequest"}
)

#: The permission the second finance level is addressed to (§4.17.7).
FINANCE_APPROVE_PERMISSION = "finance.approve"

#: Level numbers on a finance entry. Finance stays level 2 when the PM level is
#: skipped, so "level 1" always means the PM and re-addressing level-1 requests
#: (``readdress_project_requests``) never touches a Finance request.
FINANCE_PM_LEVEL = 1
FINANCE_LEVEL = 2

#: The supplier register (R15, §4.20.3): one level, addressed to Finance, and
#: the registrar never decides their own entry.
SUPPLIER_DOCUMENT_TYPES = frozenset({"network.Supplier"})


def is_finance_document(document) -> bool:  # type: ignore[no-untyped-def]
    return document_type_of(document) in FINANCE_DOCUMENT_TYPES


def _finance_levels(document) -> list[RequiredLevel]:  # type: ignore[no-untyped-def]
    """The PM, then Finance (R4, §4.17.3).

    The PM level is **skipped**, not auto-approved, when the recorder is that
    project's PM or holds the tenant's Finance Director role: asking someone to
    approve their own entry would be theatre, and "approving" it on their behalf
    would put a signature on the trail nobody gave. Finance is never skipped, so
    a second person still sees every entry before it counts.

    No ``due_at`` and no delegation on either level (D22): a signature on a
    budget is not lendable.
    """
    project = document.project
    recorder_id = document.recorded_by_id

    director_role_id = document.organization.settings.finance_director_role_id
    skip_pm = project.manager_id == recorder_id or (
        director_role_id is not None
        and document.recorded_by.user_roles.filter(role_id=director_role_id).exists()
    )

    levels: list[RequiredLevel] = []
    if not skip_pm:
        pm_level = _project_level(document)
        if pm_level is None:
            # D28: no fallback approver. A finance entry with nobody to give the
            # first signature is refused, not routed round the PM.
            raise ProjectHasNoActiveManager(
                f"{project} has no project manager, so nobody can give the "
                f"first approval. An owner must assign one (D28).",
                details={"project": str(project)},
            )
        levels.append(pm_level)

    levels.append(
        RequiredLevel(level=FINANCE_LEVEL, permission=FINANCE_APPROVE_PERMISSION)
    )
    return levels


def required_levels(document, *, facts: ApprovalFacts | None = None) -> list[RequiredLevel]:
    """Which approvals this document needs (§5.1, F3).

    Returns an empty list when nothing is required — §5.2's auto-approval case,
    which still writes an ``ApprovalAction`` so the trail has no gap.
    """
    # R4: money out routes PM then Finance, and never reaches the criticality
    # rules — an expense has no category criticality to match.
    if is_finance_document(document):
        return _finance_levels(document)

    # R15: a supplier has no project and no category, so one Finance level and
    # nothing else (§4.20.3).
    if document_type_of(document) in SUPPLIER_DOCUMENT_TYPES:
        return [RequiredLevel(level=1, permission=FINANCE_APPROVE_PERMISSION)]

    # O6, D22: project material routes to that project's manager, as the only
    # level, and never reaches the criticality rules below.
    #
    # A branch rather than a rule row, deliberately. "The manager of whichever
    # project this happens to be for" cannot be expressed in a table keyed on
    # category and criticality without inventing a placeholder role that nobody
    # holds — and that placeholder would then be grantable to anyone, undoing
    # the very control it stood in for.
    project_level = _project_level(document)
    # O6/D22 on a gate pass: the manager is the only level, and the criticality
    # rules are not consulted at all.
    if project_level is not None and getattr(
        document, "project_approval_replaces_rules", False
    ):
        return [project_level]

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

    levels = _dedupe_by_sequence(matched)

    # O10 on a disposal: the manager is added **above** the document's own
    # rules rather than replacing them. A write-off is permanent, so nothing
    # already in place is given up for it — the PM answers first, then whoever
    # the criticality rules already required.
    if project_level is not None:
        levels = [project_level] + [
            RequiredLevel(level=level.level + 1, role=level.role, rule=level.rule)
            for level in levels
        ]
    return levels


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
                required_user=required.user,
                required_permission=required.permission,
                requested_by=requested_by,
                # D22: no escalation on a PM level, and none on a Finance level
                # (R4). `due_at` left null is what the sweep skips on, so this
                # needs no special case there — and an unanswered request waits,
                # which is the accepted cost of single-signature control.
                due_at=None if required.user is not None or required.permission else due_at,
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


def _is_requester(document, user) -> bool:
    """Whether ``user`` raised ``document``."""
    if document is None:
        return False
    requester_id = getattr(document, "requested_by_id", None)
    return bool(requester_id) and requester_id == user.pk


def can_approve(user, approval_request: ApprovalRequest, *, document=None) -> tuple[bool, str]:
    """Whether ``user`` may decide this request, and why not if they may not.

    Two checks, in this order:

    1. **Self-approval** (F3). Blocked unless the tenant has explicitly enabled
       it. Checked first because it is the more surprising refusal, and the
       clearer message.
    2. **Role**, including any active delegation (F5).
    """
    from accounts.services import resolve_permissions

    # R4: finance entries. The recorder never approves their own entry at either
    # level — not by `allow_self_approval`, which is a gate-out setting, and not
    # by the O6 exception, which exists because a PM is the *only* level on
    # project material. Here Finance is a second signature, so the exception has
    # nothing to stand on.
    if approval_request.document_type in FINANCE_DOCUMENT_TYPES | SUPPLIER_DOCUMENT_TYPES:
        requester_id = (
            getattr(document, "requested_by_id", None) or approval_request.requested_by_id
        )
        if requester_id is not None and requester_id == user.pk:
            return False, "self"

    # O6: a level addressed to a person is that person's to answer, and nobody
    # else's. No delegation — a delegation lends a *role*, and lending someone's
    # signature on a budget they are accountable for is not the same thing
    # (D22). No blanket-permission override either, for the same reason.
    if approval_request.required_user_id is not None:
        if user.pk != approval_request.required_user_id:
            return False, "not_the_manager"
        # Self-approval is permitted here and recorded rather than blocked
        # (O6). It is a deliberate exception, and R2 in the requirements is
        # where the cost of it is written down.
        return True, "self" if _is_requester(document, user) else ""

    # R4: a level addressed to whoever holds a permission. Held *directly*: like
    # a PM level it is not lendable (D22), so a delegation that carries the
    # permission does not count. `resolve_permissions` gives an inactive user
    # nothing, so a deactivated holder cannot act.
    if approval_request.required_permission:
        permissions = resolve_permissions(user)
        code = approval_request.required_permission
        if permissions.has(code) and not permissions.is_delegated(code):
            return True, ""
        return False, "permission"

    if document is not None and _is_requester(document, user):
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
        if why == "not_the_manager":
            manager = approval_request.required_user
            who = (manager.full_name or str(manager)) if manager else "the manager"
            raise NotAnApprover(
                f"This is for {who} to decide — they manage the project it is "
                f"costed to (O6)."
            )
        if why == "permission":
            raise NotAnApprover(
                f"Deciding this needs the {approval_request.required_permission} "
                f"permission (R4)."
            )
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
        self_approved=why == "self",
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


# --------------------------------------------------------------------------
# Reassigning a project's manager (R4, D28, §4.17.3)
# --------------------------------------------------------------------------


def readdress_project_requests(project, *, old_manager_id, actor=None, request=None) -> int:  # type: ignore[no-untyped-def]
    """Hand a project's open PM-level requests to its new manager.

    D28 has no fallback approver, so the only way out when a PM leaves or
    changes is to point the waiting requests at their replacement; otherwise
    they would sit addressed to someone who can no longer answer. A request
    addressed to a person is only ever a PM level (O6, R4), so "open and
    addressed to the old manager, on a document costed to this project" is
    exactly the set: gate-out, disposal and finance level 1 alike, which are
    the same concept, the project manager's signature.

    Finance levels are addressed to a permission, not a person, and are not
    touched. Returns how many were re-addressed.
    """
    from django.apps import apps

    from core.audit import record
    from core.models import AuditAction

    new_manager_id = project.manager_id
    if old_manager_id is None or new_manager_id is None or old_manager_id == new_manager_id:
        return 0

    open_requests = ApprovalRequest.objects.filter(
        required_user_id=old_manager_id,
        status__in=(ApprovalRequestStatus.PENDING, ApprovalRequestStatus.ESCALATED),
    )

    moved: list[ApprovalRequest] = []
    documents: dict[tuple[str, str], object | None] = {}
    for pending in open_requests:
        key = (pending.document_type, pending.document_id)
        if key not in documents:
            try:
                model = apps.get_model(pending.document_type)
            except LookupError:
                documents[key] = None
            else:
                documents[key] = model.objects.filter(pk=pending.document_id).first()
        document = documents[key]
        owner = project_of(document) if document is not None else None
        if owner is not None and owner.pk == project.pk:
            moved.append(pending)

    if not moved:
        return 0

    ApprovalRequest.objects.filter(pk__in=[item.pk for item in moved]).update(
        required_user_id=new_manager_id, updated_at=timezone.now()
    )

    record(
        AuditAction.STATUS_CHANGED,
        actor=actor,
        organization=project.organization_id,
        target=project,
        target_label=str(project),
        request=request,
        note=(
            f"Project manager changed: {len(moved)} open approval(s) "
            f"re-addressed to the new manager ("
            f"{', '.join(item.document_number or item.document_id for item in moved)})."
        ),
    )
    return len(moved)
