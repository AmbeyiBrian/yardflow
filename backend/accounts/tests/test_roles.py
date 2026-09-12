"""T1.10 and T1.11 — roles, the permission registry, resolution and guards.

Design §4.2, §7.2. Requirements B3, B4.
"""

import pytest

from accounts.factories import RoleFactory, UserFactory, UserRoleFactory
from accounts.models import Role, RolePermission
from accounts.permissions_registry import (
    ALL_CODENAMES,
    ALL_PERMISSIONS,
    DEFAULT_ROLES,
    OWNER_LEVEL_PERMISSIONS,
    PERM,
    permission_groups,
    validate_codename,
)
from accounts.services import (
    LastOwnerProtected,
    assert_owner_level_remains,
    deactivate_user,
    resolve_permissions,
    revoke_role,
)


class TestPermissionRegistry:
    """§4.2: permissions are code, because they must match what code checks."""

    def test_every_codename_in_the_design_is_registered(self):
        """The list is quoted verbatim from §4.2."""
        expected = {
            "gate_out.request",
            "gate_out.approve",
            "gate_out.release",
            "gate_in.post",
            "catalogue.manage",
            "settings.manage",
            "users.manage",
            "stock.adjust",
            "disposal.approve",
            "report.view_all",
            "job.closeout",
            "job.close_with_variance",
            "custody.transfer",
        }

        assert expected <= ALL_CODENAMES

    def test_codenames_are_unique(self):
        codenames = [spec.codename for spec in ALL_PERMISSIONS]

        assert len(codenames) == len(set(codenames))

    def test_an_unknown_codename_is_rejected(self):
        """A permission no code checks looks like protection and is not."""
        with pytest.raises(ValueError, match="not a known permission"):
            validate_codename("gate_out.aprove")  # typo

    def test_permissions_are_grouped_for_the_role_editor(self):
        groups = permission_groups()

        assert "Dispatch" in groups
        assert {spec.codename for spec in groups["Dispatch"]} == {
            PERM.GATE_OUT_REQUEST,
            PERM.GATE_OUT_APPROVE,
            PERM.GATE_OUT_RELEASE,
        }

    def test_release_is_a_separate_permission_from_approve(self):
        """G1, Q7: so a dedicated gate guard needs no code change."""
        assert PERM.GATE_OUT_APPROVE != PERM.GATE_OUT_RELEASE
        assert {PERM.GATE_OUT_APPROVE, PERM.GATE_OUT_RELEASE} <= ALL_CODENAMES

    def test_default_roles_only_reference_real_permissions(self):
        for role_name, codenames in DEFAULT_ROLES.items():
            unknown = set(codenames) - ALL_CODENAMES
            assert unknown == set(), f"{role_name} references unknown {unknown}"

    def test_the_seeded_technician_role_cannot_approve(self):
        """A technician requests material; they do not authorise it (F1, F3)."""
        assert PERM.GATE_OUT_APPROVE not in DEFAULT_ROLES["Technician"]
        assert PERM.GATE_OUT_REQUEST in DEFAULT_ROLES["Technician"]

    def test_the_seeded_owner_role_holds_everything(self):
        assert set(DEFAULT_ROLES["Owner"]) == ALL_CODENAMES


class TestRolePermissions:
    """B4: a tenant ticks the permissions each role holds."""

    def test_set_permissions_grants_them(self, tenant):
        role = RoleFactory()

        role.set_permissions([PERM.GATE_OUT_APPROVE, PERM.REPORT_VIEW_ALL])

        assert role.codenames == {PERM.GATE_OUT_APPROVE, PERM.REPORT_VIEW_ALL}

    def test_set_permissions_replaces_rather_than_adds(self, tenant):
        role = RoleFactory(codenames=[PERM.GATE_OUT_APPROVE, PERM.REPORT_VIEW_ALL])

        role.set_permissions([PERM.REPORT_VIEW_ALL])

        assert role.codenames == {PERM.REPORT_VIEW_ALL}

    def test_set_permissions_rejects_an_unknown_codename(self, tenant):
        role = RoleFactory()

        with pytest.raises(ValueError, match="not a known permission"):
            role.set_permissions(["nonsense.permission"])

    def test_setting_the_same_permissions_twice_is_idempotent(self, tenant):
        role = RoleFactory(codenames=[PERM.GATE_IN_POST])

        role.set_permissions([PERM.GATE_IN_POST])

        assert RolePermission.objects.filter(role=role).count() == 1

    def test_role_names_are_unique_within_a_tenant(self, tenant):
        from django.db import IntegrityError, transaction

        RoleFactory(name="Storekeeper")

        with pytest.raises(IntegrityError), transaction.atomic():
            Role.objects.create(name="Storekeeper")

    def test_the_same_role_name_may_exist_in_another_tenant(
        self, organization, other_organization
    ):
        from core.tenancy import tenant_context

        with tenant_context(organization):
            RoleFactory(name="Storekeeper")
        with tenant_context(other_organization):
            RoleFactory(name="Storekeeper")

        with tenant_context(organization):
            assert Role.objects.filter(name="Storekeeper").count() == 1


class TestPermissionResolution:
    """§4.2: one place answers "what may this user do?"."""

    def test_a_user_with_no_roles_has_no_permissions(self, tenant):
        user = UserFactory(organization=tenant)

        assert resolve_permissions(user).codenames == set()

    def test_permissions_come_from_assigned_roles(self, tenant):
        user = UserFactory(organization=tenant)
        role = RoleFactory(codenames=[PERM.GATE_OUT_APPROVE])
        UserRoleFactory(user=user, role=role)

        assert resolve_permissions(user).has(PERM.GATE_OUT_APPROVE)

    def test_permissions_from_several_roles_are_unioned(self, tenant):
        """B3: a user may hold more than one role."""
        user = UserFactory(organization=tenant)
        UserRoleFactory(user=user, role=RoleFactory(codenames=[PERM.GATE_IN_POST]))
        UserRoleFactory(user=user, role=RoleFactory(codenames=[PERM.GATE_OUT_RELEASE]))

        resolved = resolve_permissions(user)

        assert {PERM.GATE_IN_POST, PERM.GATE_OUT_RELEASE} <= resolved.codenames

    def test_a_deactivated_user_resolves_to_nothing(self, tenant):
        """B3 keeps their records; it must not keep their access."""
        user = UserFactory(organization=tenant)
        UserRoleFactory(user=user, role=RoleFactory(codenames=[PERM.GATE_OUT_APPROVE]))

        user.is_active = False
        user.save()

        assert resolve_permissions(user).codenames == set()

    def test_another_users_roles_do_not_leak(self, tenant):
        holder = UserFactory(organization=tenant)
        bystander = UserFactory(organization=tenant)
        UserRoleFactory(user=holder, role=RoleFactory(codenames=[PERM.GATE_OUT_APPROVE]))

        assert resolve_permissions(bystander).codenames == set()

    def test_nothing_is_reported_as_delegated_before_delegation_exists(self, tenant):
        """F5's attribution needs this distinction; T1.17 fills it in."""
        user = UserFactory(organization=tenant)
        UserRoleFactory(user=user, role=RoleFactory(codenames=[PERM.GATE_OUT_APPROVE]))

        resolved = resolve_permissions(user)

        assert resolved.is_delegated(PERM.GATE_OUT_APPROVE) is False


class TestLastOwnerGuard:
    """B4: "the system prevents removing the last one"."""

    def _make_owner(self, organization):
        owner = UserFactory(organization=organization)
        role = RoleFactory(name="Owner", codenames=sorted(OWNER_LEVEL_PERMISSIONS))
        UserRoleFactory(user=owner, role=role)
        return owner, role

    def test_deactivating_the_last_owner_is_refused(self, tenant):
        """Otherwise a tenant can lock itself out with no way back."""
        owner, _ = self._make_owner(tenant)

        with pytest.raises(LastOwnerProtected, match="owner-level"):
            deactivate_user(owner)

        owner.refresh_from_db()
        assert owner.is_active is True

    def test_deactivating_one_of_two_owners_is_allowed(self, tenant):
        first, role = self._make_owner(tenant)
        second = UserFactory(organization=tenant)
        UserRoleFactory(user=second, role=role)

        deactivate_user(first)

        first.refresh_from_db()
        assert first.is_active is False

    def test_revoking_the_last_owner_role_is_refused(self, tenant):
        owner, role = self._make_owner(tenant)

        with pytest.raises(LastOwnerProtected):
            revoke_role(owner, role)

    def test_revoking_a_role_from_one_of_two_owners_is_allowed(self, tenant):
        first, role = self._make_owner(tenant)
        second = UserFactory(organization=tenant)
        UserRoleFactory(user=second, role=role)

        revoke_role(first, role)

        assert resolve_permissions(first).codenames == set()

    def test_owner_level_needs_both_permissions_not_just_one(self, tenant):
        """Holding only half of owner-level does not count.

        Someone who can approve but cannot manage users cannot restore access;
        someone who can manage users but cannot approve cannot release material.
        """
        half = UserFactory(organization=tenant)
        UserRoleFactory(user=half, role=RoleFactory(codenames=[PERM.USERS_MANAGE]))

        with pytest.raises(LastOwnerProtected):
            assert_owner_level_remains(tenant.pk)

    def test_permissions_unioned_across_roles_satisfy_the_guard(self, tenant):
        """The subtle case: owner-level held via two separate roles."""
        user = UserFactory(organization=tenant)
        UserRoleFactory(user=user, role=RoleFactory(codenames=[PERM.USERS_MANAGE]))
        UserRoleFactory(user=user, role=RoleFactory(codenames=[PERM.GATE_OUT_APPROVE]))

        # Must not raise: this user does hold owner-level permissions.
        assert_owner_level_remains(tenant.pk)

    def test_an_inactive_owner_does_not_satisfy_the_guard(self, tenant):
        active_owner, role = self._make_owner(tenant)
        dormant = UserFactory(organization=tenant, is_active=False)
        UserRoleFactory(user=dormant, role=role)

        with pytest.raises(LastOwnerProtected):
            deactivate_user(active_owner)

    def test_another_tenants_owner_does_not_satisfy_this_tenants_guard(
        self, organization, other_organization
    ):
        """The isolation bug that would be easy to write and hard to notice."""
        from core.tenancy import tenant_context

        with tenant_context(other_organization):
            self._make_owner(other_organization)

        with tenant_context(organization):
            ours, _ = self._make_owner(organization)
            with pytest.raises(LastOwnerProtected):
                deactivate_user(ours)
