"""T1.17 — delegation (§4.2, requirement F5)."""

from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.factories import (
    DelegationFactory,
    RoleFactory,
    UserFactory,
    UserRoleFactory,
)
from accounts.permissions_registry import PERM
from accounts.services import resolve_permissions


class TestDelegationGrantsPermissions:
    """F5: approvals continue when the owner is away."""

    def test_an_active_delegation_grants_the_permission(self, tenant):
        owner = UserFactory(organization=tenant)
        deputy = UserFactory(organization=tenant)
        approver_role = RoleFactory(name="Approver", codenames=[PERM.GATE_OUT_APPROVE])
        UserRoleFactory(user=owner, role=approver_role)

        assert not resolve_permissions(deputy).has(PERM.GATE_OUT_APPROVE)

        DelegationFactory(from_user=owner, to_user=deputy, role=approver_role)

        assert resolve_permissions(deputy).has(PERM.GATE_OUT_APPROVE)

    def test_specific_codenames_can_be_delegated_without_a_whole_role(self, tenant):
        """Lend only the authority actually needed."""
        owner = UserFactory(organization=tenant)
        deputy = UserFactory(organization=tenant)

        DelegationFactory(
            from_user=owner, to_user=deputy, role=None, codenames=[PERM.GATE_OUT_APPROVE]
        )

        resolved = resolve_permissions(deputy)
        assert resolved.has(PERM.GATE_OUT_APPROVE)
        assert not resolved.has(PERM.USERS_MANAGE)


class TestDelegationWindow:
    """F5: "a delegate is named for a period"."""

    def test_a_delegation_that_has_not_started_grants_nothing(self, tenant):
        deputy = UserFactory(organization=tenant)
        DelegationFactory(
            to_user=deputy,
            role=None,
            codenames=[PERM.GATE_OUT_APPROVE],
            starts_at=timezone.now() + timedelta(days=1),
            ends_at=timezone.now() + timedelta(days=5),
        )

        assert not resolve_permissions(deputy).has(PERM.GATE_OUT_APPROVE)

    def test_an_expired_delegation_grants_nothing(self, tenant):
        """The point of a window is that it closes by itself."""
        deputy = UserFactory(organization=tenant)
        DelegationFactory(
            to_user=deputy,
            role=None,
            codenames=[PERM.GATE_OUT_APPROVE],
            starts_at=timezone.now() - timedelta(days=10),
            ends_at=timezone.now() - timedelta(days=1),
        )

        assert not resolve_permissions(deputy).has(PERM.GATE_OUT_APPROVE)

    def test_a_revoked_delegation_grants_nothing(self, tenant):
        """Someone may come back from leave early."""
        deputy = UserFactory(organization=tenant)
        delegation = DelegationFactory(to_user=deputy, role=None, codenames=[PERM.GATE_OUT_APPROVE])

        assert resolve_permissions(deputy).has(PERM.GATE_OUT_APPROVE)

        delegation.is_revoked = True
        delegation.save()

        assert not resolve_permissions(deputy).has(PERM.GATE_OUT_APPROVE)


class TestAttribution:
    """F5: recorded as "X on behalf of Y", never as Y.

    Recording it as Y would forge the principal's signature on an approval they
    never gave, which is precisely what M3's trail exists to prevent.
    """

    def test_a_delegated_permission_reports_its_source(self, tenant):
        owner = UserFactory(organization=tenant)
        deputy = UserFactory(organization=tenant)
        delegation = DelegationFactory(
            from_user=owner, to_user=deputy, role=None, codenames=[PERM.GATE_OUT_APPROVE]
        )

        resolved = resolve_permissions(deputy)

        assert resolved.is_delegated(PERM.GATE_OUT_APPROVE) is True
        assert resolved.delegated_from[PERM.GATE_OUT_APPROVE] == delegation.pk

    def test_a_directly_held_permission_is_not_reported_as_delegated(self, tenant):
        """The subtle case worth pinning down.

        Someone who already holds a permission and *also* receives it by
        delegation is still acting on their own authority. Attributing their
        approval to someone else would be wrong.
        """
        owner = UserFactory(organization=tenant)
        deputy = UserFactory(organization=tenant)
        UserRoleFactory(user=deputy, role=RoleFactory(codenames=[PERM.GATE_OUT_APPROVE]))
        DelegationFactory(
            from_user=owner, to_user=deputy, role=None, codenames=[PERM.GATE_OUT_APPROVE]
        )

        resolved = resolve_permissions(deputy)

        assert resolved.has(PERM.GATE_OUT_APPROVE)
        assert resolved.is_delegated(PERM.GATE_OUT_APPROVE) is False


class TestDelegationConstraints:
    def test_a_delegation_cannot_end_before_it_starts(self, tenant):
        with pytest.raises(IntegrityError), transaction.atomic():
            DelegationFactory(
                starts_at=timezone.now(),
                ends_at=timezone.now() - timedelta(days=1),
            )

    def test_a_user_cannot_delegate_to_themselves(self, tenant):
        """A no-op that would look like a control."""
        user = UserFactory(organization=tenant)

        with pytest.raises(IntegrityError), transaction.atomic():
            DelegationFactory(from_user=user, to_user=user)


class TestDelegationIsolation:
    def test_a_delegation_in_another_tenant_grants_nothing_here(
        self, organization, other_organization
    ):
        from core.tenancy import tenant_context

        with tenant_context(other_organization):
            their_owner = UserFactory(organization=other_organization)
            their_deputy = UserFactory(organization=other_organization)
            DelegationFactory(
                from_user=their_owner,
                to_user=their_deputy,
                role=None,
                codenames=[PERM.GATE_OUT_APPROVE],
            )

        with tenant_context(organization):
            ours = UserFactory(organization=organization)
            assert not resolve_permissions(ours).has(PERM.GATE_OUT_APPROVE)


class TestADelegationMustDelegateSomething:
    """From a row found in a live tenant.

    It named a principal, a delegate and a fortnight, and lent neither a role
    nor a single permission. On the screen it read exactly like cover being in
    place — somebody could go on leave believing approvals would continue, and
    they would not. The serializer refuses it; this pins the database's own
    refusal, which covers every other way a row can be written.
    """

    def test_the_database_refuses_an_empty_one(self, tenant):
        from django.db import IntegrityError, transaction

        from accounts.factories import UserFactory
        from accounts.models import Delegation

        principal = UserFactory(organization=tenant, full_name="Away Owner")
        delegate = UserFactory(organization=tenant, full_name="Covering Person")

        with pytest.raises(IntegrityError), transaction.atomic():
            Delegation.objects.create(
                organization=tenant,
                from_user=principal,
                to_user=delegate,
                role=None,
                codenames=[],
                starts_at=timezone.now(),
                ends_at=timezone.now() + timedelta(days=1),
            )

    def test_a_role_is_enough(self, tenant):
        from accounts.factories import RoleFactory, UserFactory
        from accounts.models import Delegation

        principal = UserFactory(organization=tenant, full_name="Away Owner")
        delegate = UserFactory(organization=tenant, full_name="Covering Person")

        delegation = Delegation.objects.create(
            organization=tenant,
            from_user=principal,
            to_user=delegate,
            role=RoleFactory(name="Stand-in approver"),
            starts_at=timezone.now(),
            ends_at=timezone.now() + timedelta(days=1),
        )

        assert delegation.pk

    def test_so_are_named_permissions(self, tenant):
        """Lending only the authority actually needed, rather than a whole
        role, is the careful way to do it and must stay possible."""
        from accounts.factories import UserFactory
        from accounts.models import Delegation
        from accounts.permissions_registry import PERM

        principal = UserFactory(organization=tenant, full_name="Away Owner")
        delegate = UserFactory(organization=tenant, full_name="Covering Person")

        delegation = Delegation.objects.create(
            organization=tenant,
            from_user=principal,
            to_user=delegate,
            codenames=[PERM.GATE_OUT_APPROVE],
            starts_at=timezone.now(),
            ends_at=timezone.now() + timedelta(days=1),
        )

        assert delegation.pk
