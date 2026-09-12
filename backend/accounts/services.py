"""Permission resolution and the guards protecting it (design §4.2, §7.2).

Requirements B3, B4.

Resolution answers one question — *what may this user do?* — and is the single
place that answers it, so the API, the frontend's ``/me`` payload and the
approval engine can never disagree.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.utils import timezone

from accounts.models import Delegation, Role, RolePermission, User, UserRole
from accounts.permissions_registry import OWNER_LEVEL_PERMISSIONS


@dataclass
class ResolvedPermissions:
    """A user's permissions, and where each came from.

    The source matters: F5 requires a delegated approval to be recorded as
    "X on behalf of Y", never as Y, so the caller has to be able to tell a
    delegated permission from a directly held one.
    """

    codenames: set[str] = field(default_factory=set)
    #: codename -> the delegation that granted it (T1.17). Empty until then.
    delegated_from: dict[str, int] = field(default_factory=dict)

    def has(self, codename: str) -> bool:
        return codename in self.codenames

    def is_delegated(self, codename: str) -> bool:
        return codename in self.delegated_from

    def __contains__(self, codename: object) -> bool:
        return codename in self.codenames


def resolve_permissions(user: User) -> ResolvedPermissions:
    """Return everything ``user`` may do.

    A deactivated user resolves to nothing. B3 keeps their historical records
    intact, but they must not be able to act.

    Delegations are folded in by T1.17; this function is where that happens, so
    no caller needs to know delegation exists.
    """
    if not user.is_authenticated or not user.is_active:
        return ResolvedPermissions()

    # A platform admin is cross-tenant and holds no tenant role. Tenant
    # permissions are deliberately not granted here: acting inside a tenant goes
    # through the platform admin console, which is audited separately (§2.2, M3).
    if user.is_platform_admin:
        return ResolvedPermissions()

    codenames = set(
        RolePermission.objects.filter(role__user_roles__user=user).values_list(
            "codename", flat=True
        )
    )

    # F5: fold in anything currently delegated to this user, tagging where it
    # came from so an approval can be attributed as "X on behalf of Y".
    delegated_from: dict[str, int] = {}
    now = timezone.now()
    active_delegations = (
        Delegation.objects.filter(
            to_user=user,
            is_revoked=False,
            starts_at__lte=now,
            ends_at__gte=now,
        )
        .select_related("role")
        .prefetch_related("role__permissions")
    )
    for delegation in active_delegations:
        for codename in delegation.delegated_codenames():
            # A permission the user already holds directly is theirs, not
            # delegated — otherwise their own approvals would be misattributed
            # to someone else.
            if codename not in codenames:
                delegated_from.setdefault(codename, delegation.pk)

    codenames |= set(delegated_from)

    return ResolvedPermissions(codenames=codenames, delegated_from=delegated_from)


def user_has_permission(user: User, codename: str) -> bool:
    """Convenience wrapper. Prefer resolving once per request."""
    return resolve_permissions(user).has(codename)


# --------------------------------------------------------------------------
# B4: the last owner cannot be removed
# --------------------------------------------------------------------------


class LastOwnerProtected(Exception):
    """Raised when an action would leave a tenant with no owner-level user.

    B4: "at least one active user must hold owner-level permissions at all
    times; the system prevents removing the last one."

    Without this, a tenant can lock itself out irrecoverably — nobody left who
    can grant permissions, and nobody left who can approve a gate-out. Recovery
    would require us to intervene in their database.
    """


def _owner_level_holders(query) -> set[int]:
    """Return the users in ``query`` holding *every* owner-level permission.

    A user may hold several roles, so the permissions have to be unioned per
    user before the subset test — checking role by role would miss someone who
    holds `users.manage` through one role and `gate_out.approve` through
    another.
    """
    holders: dict[int, set[str]] = {}
    for user_id, codename in query.values_list("user_id", "role__permissions__codename"):
        if codename:
            holders.setdefault(user_id, set()).add(codename)

    return {
        user_id
        for user_id, codenames in holders.items()
        if OWNER_LEVEL_PERMISSIONS.issubset(codenames)
    }


def owner_level_users(organization_id) -> set[int]:
    """Active users in this organization holding owner-level permissions."""
    return _owner_level_holders(
        UserRole.objects.filter(organization_id=organization_id, user__is_active=True)
    )


def assert_owner_level_remains(
    organization_id,
    *,
    excluding_user_id=None,
    excluding_role_assignment: tuple | None = None,
) -> None:
    """Raise unless someone would still hold owner-level permissions.

    Called before deactivating a user, removing a role assignment, or stripping
    permissions from a role. ``excluding_role_assignment`` is a
    ``(user_id, role_id)`` pair, so revoking one user's role does not
    accidentally discount every holder of that role.
    """
    query = UserRole.objects.filter(organization_id=organization_id, user__is_active=True)
    if excluding_user_id is not None:
        query = query.exclude(user_id=excluding_user_id)
    if excluding_role_assignment is not None:
        user_id, role_id = excluding_role_assignment
        query = query.exclude(user_id=user_id, role_id=role_id)

    if not _owner_level_holders(query):
        required = ", ".join(sorted(OWNER_LEVEL_PERMISSIONS))
        raise LastOwnerProtected(
            f"This would leave the organization with no active user holding "
            f"owner-level permissions ({required}). Grant them to someone else "
            f"first — otherwise nobody could administer this organization or "
            f"approve a gate-out (B4)."
        )


def deactivate_user(user: User) -> None:
    """Deactivate a user, refusing to remove the last owner (B3, B4).

    B3's edge case: "deactivating a user holding items in custody must warn and
    require the custody to be reassigned or returned first." Deactivating them
    anyway would leave the material somewhere with nobody accountable for it,
    which is the one outcome this whole system exists to prevent.
    """
    from custody.services import assert_can_deactivate

    assert_owner_level_remains(user.organization_id, excluding_user_id=user.pk)
    assert_can_deactivate(user)
    user.is_active = False
    user.save(update_fields=["is_active"])


def revoke_role(user: User, role: Role) -> None:
    """Remove a role from a user, refusing to remove the last owner (B4)."""
    assert_owner_level_remains(
        user.organization_id, excluding_role_assignment=(user.pk, role.pk)
    )
    UserRole.objects.filter(user=user, role=role).delete()


def can_be_deleted(user: User) -> bool:
    """B3: a user who has posted movements may only be deactivated.

    The ledger names who posted each movement (§3.2), and it is append-only. A
    deleted user would leave those rows pointing at nobody, so the audit trail
    would quietly lose the one fact it exists to hold.
    """
    from stock.models import StockMovement

    if StockMovement.objects.filter(posted_by=user).exists():
        return False

    # Custody is the same argument in the present tense: material on somebody's
    # record needs somebody to still exist.
    from stock.queries import custody_holdings

    return not custody_holdings(user=user).exists()
