"""Permission classes enforcing organization-level state (design §4.1, §13).

Requirement A2: "a suspended tenant's users can log in but cannot post any
movement; they see a notice."
"""

from __future__ import annotations

from rest_framework.permissions import SAFE_METHODS, BasePermission

from core.exceptions import OrganizationSuspended


class OrganizationIsActive(BasePermission):
    """Reads continue for a suspended tenant; writes are refused (A2).

    Suspension is a commercial lever, not a punishment: the customer keeps
    access to their records — which they may be legally required to produce —
    but cannot add to them. Deleting or hiding data would be the wrong tool, and
    A2 says so explicitly: "suspension never deletes data."

    Raises rather than returning False so the client receives the
    ``ORGANIZATION_SUSPENDED`` code and can render the notice A2 asks for,
    instead of a bare 403 with no explanation.
    """

    def has_permission(self, request, view) -> bool:  # type: ignore[no-untyped-def]
        if request.method in SAFE_METHODS:
            return True

        organization = getattr(request, "organization", None)
        if organization is None:
            user = getattr(request, "user", None)
            organization = getattr(user, "organization", None)

        if organization is not None and organization.is_suspended:
            raise OrganizationSuspended()

        return True
