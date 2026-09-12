"""Isolation fixtures for the administration endpoints (T1.20, A3).

The user endpoint matters most here. ``User`` is not a ``TenantModel`` —
a platform admin belongs to no tenant — so its viewset scopes the queryset by
hand, and hand-written scoping is exactly what this suite exists to check.
"""

from datetime import timedelta

from django.utils import timezone

from core.isolation import register_isolation_fixture


def register() -> None:
    from accounts.models import Delegation, Role, User

    def make_user(organization):
        return User.objects.create_user(
            email="iso-subject@example.com",
            organization=organization,
            full_name="Isolation subject",
        )

    def make_role(organization):
        return Role.objects.create(
            organization=organization, name="Isolation role", description="Fixture"
        )

    def make_delegation(organization):
        principal = make_user(organization)
        delegate = User.objects.create_user(
            email="iso-delegate@example.com",
            organization=organization,
            full_name="Isolation delegate",
        )
        now = timezone.now()
        return Delegation.objects.create(
            organization=organization,
            from_user=principal,
            to_user=delegate,
            role=make_role(organization),
            starts_at=now,
            ends_at=now + timedelta(days=7),
            reason="Isolation fixture",
        )

    register_isolation_fixture("user", make_user, payload={"full_name": "renamed"})
    register_isolation_fixture("role", make_role, payload={"description": "renamed"})
    register_isolation_fixture(
        "delegation", make_delegation, payload={"reason": "renamed"}
    )
