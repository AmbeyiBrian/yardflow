"""DRF permission classes (design §4.2, §7.2).

The frontend's `<Can>` component hides controls the user may not use, but that
is UX only. **Every call is re-checked here** — the server never trusts the
client's view of what a user may do (§7.2).
"""

from __future__ import annotations

from rest_framework.permissions import BasePermission

from accounts.services import resolve_permissions


class HasPermission(BasePermission):
    """Requires a permission codename, declared on the view.

    Usage::

        class GateOutViewSet(TenantScopedViewSet):
            permission_classes = [IsAuthenticated, HasPermission]
            required_permission = PERM.GATE_OUT_REQUEST

    Or per action::

            required_permissions = {
                "create": PERM.GATE_OUT_REQUEST,
                "release": PERM.GATE_OUT_RELEASE,
            }

    On a plain ``APIView`` there is no action, so the key is the HTTP method::

            required_permissions = {"patch": PERM.SETTINGS_MANAGE}

    That fallback is not a convenience. Without it `OrganizationSettingsView`
    declared ``{"patch": PERM.SETTINGS_MANAGE}`` and enforced nothing: `action`
    is set by DRF's ViewSet machinery and is `None` on an `APIView`, so the map
    was skipped, no codename was found, and the check returned True for every
    authenticated member. Any technician could turn on self-approval, extend the
    gate-pass expiry window or change the retention period. Declaring a
    permission has to be the same thing as enforcing one.
    """

    message = "You do not have permission to perform this action."

    def has_permission(self, request, view) -> bool:  # type: ignore[no-untyped-def]
        codename = self._required_codename(view, request)
        if codename is None:
            # A view that declares nothing requires nothing beyond
            # authentication. Failing open here would be wrong, so views that
            # need a permission must say so; T1.20's suite asserts they do.
            return True

        user = request.user
        if not user or not user.is_authenticated:
            return False

        return resolve_permissions(user).has(codename)

    @staticmethod
    def _required_codename(view, request=None) -> str | None:  # type: ignore[no-untyped-def]
        per_action = getattr(view, "required_permissions", None)
        if per_action:
            action = getattr(view, "action", None)
            if action:
                if action in per_action:
                    return per_action[action]
            elif request is not None:
                # No action: a plain APIView. The verb is what identifies the
                # operation. Deliberately only when there is no action at all —
                # a ViewSet action missing from the map keeps falling through to
                # `required_permission`, as it always has.
                by_method = per_action.get(request.method.lower())
                if by_method:
                    return by_method
        return getattr(view, "required_permission", None)


class IsPlatformAdmin(BasePermission):
    """Cross-tenant platform administration only (§3, A1, A2)."""

    message = "Platform administration is restricted."

    def has_permission(self, request, view) -> bool:  # type: ignore[no-untyped-def]
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and user.is_active
            and user.is_platform_admin
            and user.is_superuser
        )
