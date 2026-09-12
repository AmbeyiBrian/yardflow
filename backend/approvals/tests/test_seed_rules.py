"""What a new tenant can and cannot get out of the gate on day one (F3, §5.2).

Provisioning created no approval rules, so every gate-out in a fresh
organization approved itself. That is the documented behaviour for an empty rule
set — but arriving in it by default meant approval was decorative in every
tenant, and no screen said so.
"""

from __future__ import annotations

import pytest

from approvals.models import ApprovalRule
from catalogue.models import Criticality
from core.provisioning import provision_tenant, registered_seeders
from core.tenancy import tenant_context

pytestmark = pytest.mark.django_db


def test_a_new_tenant_has_approval_routing(db, settings):
    settings.TENANT_BASE_DOMAIN = "localhost"
    result = provision_tenant(
        name="Silvertech", slug="silvertech-rules", owner_email="owner@silvertech.co.ke"
    )

    # Inside the context throughout: following the role is another tenant-scoped
    # read, and outside a context there is nothing to follow it to.
    with tenant_context(result["organization"]):
        rules = list(ApprovalRule.objects.select_related("required_role"))

        assert rules, "a tenant with no rules approves everything, silently"
        assert rules[0].criticality == Criticality.HIGH
        assert rules[0].required_role.name == "Owner"


def test_the_rule_is_narrow_on_purpose(db, settings):
    """Only high-criticality material. A hi-vis vest walking out of the yard
    does not need the owner woken up, and a rule that stops everything is a rule
    somebody switches off in week one."""
    settings.TENANT_BASE_DOMAIN = "localhost"
    result = provision_tenant(
        name="Silvertech", slug="silvertech-narrow", owner_email="owner@silvertech.co.ke"
    )

    with tenant_context(result["organization"]):
        assert not ApprovalRule.objects.exclude(criticality=Criticality.HIGH).exists()
        assert ApprovalRule.objects.filter(category__isnull=True).exists(), (
            "any category at that criticality, which is how a tenant writes it"
        )


def test_the_seeder_is_registered():
    assert "approval rules" in registered_seeders()
