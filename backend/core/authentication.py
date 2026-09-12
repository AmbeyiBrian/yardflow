"""Tenant-aware JWT authentication (design §2.2).

``TenantMiddleware`` resolves the organization from the subdomain, but an API
caller's identity is not known until DRF authenticates them — which happens
inside the view, and therefore inside the transaction the middleware opened.
This class completes the resolution at that point: it applies the user fallback
and refuses a subdomain/user mismatch with 404 (A3).
"""

from __future__ import annotations

from django.http import Http404
from rest_framework_simplejwt.authentication import JWTAuthentication

from core.tenancy import (
    activate_organization,
    get_current_organization_id,
)


class TenantJWTAuthentication(JWTAuthentication):
    """JWT authentication that reconciles the caller with the active tenant."""

    def authenticate(self, request):  # type: ignore[no-untyped-def]
        result = super().authenticate(request)
        if result is None:
            return None

        user, token = result
        subdomain_organization_id = get_current_organization_id()
        user_organization_id = getattr(user, "organization_id", None)

        if subdomain_organization_id is None:
            # No subdomain addressed a tenant, so fall back to the user's own
            # organization. Platform admins have none and stay unscoped.
            if user_organization_id is not None:
                activate_organization(user_organization_id)
                request.organization = user.organization
        elif user_organization_id is None:
            # A platform admin acting through a tenant subdomain. Allowed: the
            # subdomain's organization stays active, and the audit trail records
            # who acted (M3).
            pass
        elif user_organization_id != subdomain_organization_id:
            # Token issued for a different tenant than this subdomain serves.
            # 404, never 403 — a 403 confirms the tenant exists (§2.4).
            raise Http404("Organization mismatch.")

        return user, token
