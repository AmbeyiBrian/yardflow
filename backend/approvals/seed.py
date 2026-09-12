"""The approval rules a new tenant starts with (F3, §5.2).

Provisioning used to create none, and a tenant with no rules approves every
gate-out itself — correctly and by design, because a yard that has named no
approvers must not have passes stranded waiting on nobody. But the effect on a
new organization was that approval looked like a feature and did nothing, and
nothing on any screen said so. Somebody watched a technician's request for
client-owned material approve itself in the same second and reasonably asked
whether the permissions were broken.

So a new tenant starts with the one rule almost every yard would write first:
**high-criticality material needs the owner's signature.** It is deliberately
narrow — a hi-vis vest still walks out without ceremony — and it is an ordinary
rule, editable and removable from Settings like any other. The point is that
approval arrives switched on and visible rather than silently empty.
"""

from __future__ import annotations

import logging

from catalogue.models import Criticality

logger = logging.getLogger(__name__)


def seed_default_approval_rules(organization) -> dict:
    """One rule: the owner signs for high-criticality material."""
    from accounts.models import Role
    from approvals.models import ApprovalRule

    owner = Role.objects.filter(name="Owner").first()
    if owner is None:
        # Roles are seeded before this runs; if that ever changes, a tenant
        # without approval routing is far better than a failed provisioning.
        logger.warning(
            "No Owner role for %s, so no starter approval rule was created.",
            organization.slug,
        )
        return {"approval_rules": 0}

    rule, created = ApprovalRule.objects.get_or_create(
        organization=organization,
        category=None,
        criticality=Criticality.HIGH,
        required_role=owner,
        defaults={
            "sequence": 1,
            "description": "High-criticality material needs the owner's approval.",
        },
    )
    return {"approval_rules": 1 if created else 0, "rule_id": rule.pk}
