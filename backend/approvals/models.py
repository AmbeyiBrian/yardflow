"""Approval rules, requests and actions (design §4.8, §5; F3, F4, F5, M3).

F3's requirement shapes this whole app:

> The rule engine is written so further dimensions (client-owned, quantity
> threshold, monetary value, destination) can be enabled later **without schema
> change** — but only category criticality is active in v1.

So :class:`ApprovalRule` carries a ``conditions`` JSONB field that is **empty in
v1**, and the fact-collection in ``approvals.engine`` gathers every fact a future
predicate might need. Switching on a new dimension is then a settings change plus
a predicate function, not a migration.
"""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from catalogue.models import Criticality
from core.models import AuthMethod, TimeStampedModel
from core.tenancy import TenantModel


class ApprovalRule(TenantModel, TimeStampedModel):
    """A rule mapping risk to the role that must authorise it (F3, §4.8).

    v1 routes on **item category criticality** only. ``conditions`` exists and is
    read by the predicate evaluator, so a tenant can later route on ownership,
    quantity or value without a migration — which is exactly what F3 asks for.
    """

    # Null means "any category at this criticality", which is the normal case: a
    # tenant sets one rule per criticality level, not one per category.
    category = models.ForeignKey(
        "catalogue.ItemCategory",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="approval_rules",
    )
    criticality = models.CharField(max_length=10, choices=Criticality.choices)

    required_role = models.ForeignKey(
        "accounts.Role", on_delete=models.PROTECT, related_name="approval_rules"
    )

    # Higher sequence means more senior. A document matching several rules takes
    # the highest (F3), and this is what "highest" is measured on.
    sequence = models.PositiveIntegerField(default=1)

    # F3: empty in v1. The evaluator reads it, so enabling a dimension later
    # needs no schema change.
    conditions = models.JSONField(default=dict, blank=True)

    is_active = models.BooleanField(default=True)
    description = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "category", "criticality", "required_role"],
                name="uniq_approval_rule_per_scope",
                nulls_distinct=False,
            )
        ]
        ordering = ("-sequence", "criticality")

    def __str__(self) -> str:
        category = self.category
        scope = category.name if category is not None else "any category"
        return f"{self.get_criticality_display()} ({scope}) -> {self.required_role.name}"


class ApprovalRequestStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    # F5: unanswered after N hours, escalate to a named fallback.
    ESCALATED = "ESCALATED", "Escalated"
    # The document was cancelled or amended out from under it.
    SUPERSEDED = "SUPERSEDED", "Superseded"


class ApprovalRequest(TenantModel, TimeStampedModel):
    """One level of approval required on one document (§4.8, F3).

    Generic ``document_type``/``document_id`` rather than a foreign key per
    document: gate-outs, disposals, dispositions and client-owned stock
    adjustments all need approval, and adding a fifth must not require a
    migration here.
    """

    document_type = models.CharField(max_length=50, db_index=True)
    document_id = models.CharField(max_length=64, db_index=True)
    document_number = models.CharField(max_length=50, blank=True)

    level = models.PositiveIntegerField(
        default=1, help_text="Higher levels are more senior. Levels approve in order."
    )
    # Null means no role was required — the auto-approval case (§5.2). A request
    # row still exists so the AUTO action has something to attach to and the
    # trail has no gap; borrowing an arbitrary role instead would misrepresent
    # who was asked.
    required_role = models.ForeignKey(
        "accounts.Role",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approval_requests",
    )

    # O6: project material routes to a *named person* — the project's manager —
    # where every other level routes to a role. A rule table keyed on category
    # and criticality cannot express "the manager of whichever project this
    # happens to be for" without inventing a placeholder role that nobody holds
    # and anybody could be granted, so the person is carried here instead.
    required_user = models.ForeignKey(
        "accounts.User",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approval_requests_addressed",
    )

    status = models.CharField(
        max_length=20,
        choices=ApprovalRequestStatus.choices,
        default=ApprovalRequestStatus.PENDING,
        db_index=True,
    )

    requested_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    # F5: "optional escalation timeout: unanswered after N hours, escalate to a
    # named fallback."
    due_at = models.DateTimeField(null=True, blank=True, db_index=True)
    escalated_at = models.DateTimeField(null=True, blank=True)
    escalated_to = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("level", "id")
        indexes = [
            models.Index(fields=["organization", "document_type", "document_id"]),
            models.Index(fields=["organization", "status", "due_at"]),
            models.Index(fields=["organization", "required_user", "status"]),
        ]
        constraints = [
            # At most one, never both. *Neither* stays legal, because §5.2's
            # auto-approval row is exactly that: a request nobody was asked to
            # answer, kept so the trail has no gap.
            models.CheckConstraint(
                condition=Q(required_role__isnull=True)
                | Q(required_user__isnull=True),
                name="a_request_is_addressed_to_a_role_or_a_person_not_both",
            ),
        ]

    def __str__(self) -> str:
        required_role = self.required_role
        role = (
            required_role.name if required_role is not None else "no approval required"
        )
        return f"Level {self.level} ({role}) — {self.get_status_display()}"

    @property
    def is_pending(self) -> bool:
        return self.status in (
            ApprovalRequestStatus.PENDING,
            ApprovalRequestStatus.ESCALATED,
        )


class ApprovalDecision(models.TextChoices):
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    # §5.2: "zero matched rules means auto-approval, recorded as an
    # ApprovalAction with decision = AUTO so the audit trail never has a gap."
    AUTO = "AUTO", "Automatically approved — no approval required"


class ApprovalAction(TenantModel):
    """Who decided what, when, how they proved it was them (§4.8, F4, M3).

    §4.8: "this row is the non-repudiation evidence an ISO auditor asks for."

    Append-only, for the same reason the audit trail is: an approval record that
    could be edited afterwards is not evidence of anything.
    """

    approval_request = models.ForeignKey(
        ApprovalRequest, on_delete=models.PROTECT, related_name="actions"
    )

    # F5: recorded as "X on behalf of Y", never as Y. Attributing a delegated
    # approval to the principal would forge their signature on a decision they
    # never made.
    actor = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    on_behalf_of = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    delegation = models.ForeignKey(
        "accounts.Delegation",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approval_actions",
    )

    decision = models.CharField(max_length=20, choices=ApprovalDecision.choices)
    reason = models.CharField(max_length=500, blank=True)

    # F4: "approval records who, when, from what device, and by what
    # authentication method."
    auth_method = models.CharField(
        max_length=20, choices=AuthMethod.choices, default=AuthMethod.PASSWORD
    )
    webauthn_credential = models.ForeignKey(
        "accounts.WebAuthnCredential",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approval_actions",
    )
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=400, blank=True)

    decided_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-decided_at",)
        constraints = [
            # A rejection must say why (F4). Enforced in the database because
            # this row is evidence, and evidence with a blank reason is useless.
            models.CheckConstraint(
                condition=~Q(decision=ApprovalDecision.REJECTED) | ~Q(reason=""),
                name="rejection_records_a_reason",
            ),
            # An auto-approval has no human actor; a human decision must have one.
            models.CheckConstraint(
                condition=(
                    Q(decision=ApprovalDecision.AUTO, actor__isnull=True)
                    | (~Q(decision=ApprovalDecision.AUTO) & Q(actor__isnull=False))
                ),
                name="human_decisions_name_their_actor",
            ),
        ]
        indexes = [models.Index(fields=["organization", "-decided_at"])]

    def __str__(self) -> str:
        if self.decision == ApprovalDecision.AUTO:
            return "Automatically approved"
        if self.on_behalf_of_id:
            return f"{self.actor} on behalf of {self.on_behalf_of} — {self.decision}"
        return f"{self.actor} — {self.decision}"

    @property
    def attribution(self) -> str:
        """How this decision reads in the audit trail (F5)."""
        if self.decision == ApprovalDecision.AUTO:
            return "Automatically approved — no approval required"
        if self.on_behalf_of_id:
            return f"{self.actor} on behalf of {self.on_behalf_of}"
        return str(self.actor)

    def save(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self.pk is not None and not self._state.adding:
            raise ValueError(
                "An approval action is evidence and cannot be changed once "
                "recorded (M3, §4.8)."
            )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise ValueError("An approval action cannot be deleted.")
