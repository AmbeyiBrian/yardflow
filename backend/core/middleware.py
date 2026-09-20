"""Tenant resolution per request (design §2.2, layer 2 of four).

Requirements A1, A3.

Resolution order:

1. the subdomain of the request host — ``silvertech.yardflow.co.ke``
2. the authenticated user's organization

If both are present and they disagree, the request is rejected with **404, not
403** (§2.4) — a 403 would confirm that the other tenant's resource exists.
"""

from __future__ import annotations

from django.conf import settings
from django.db import transaction
from django.http import Http404

from core.models import Organization
from core.tenancy import (
    activate_organization,
    reset_current_organization,
)


def extract_subdomain(host: str, base_domain: str) -> str | None:
    """Return the tenant label from a host, or ``None`` if there is none.

    ``silvertech.localhost`` with base ``localhost``        -> ``silvertech``
    ``silvertech.yardflow.co.ke`` with ``yardflow.co.ke``   -> ``silvertech``
    ``localhost`` / ``127.0.0.1`` / ``testserver``          -> ``None``

    Where several labels precede the base domain, the one closest to it is the
    tenant, so ``staging.silvertech.yardflow.co.ke`` still resolves Silvertech.
    """
    host = host.lower().split(":")[0].rstrip(".")
    base_domain = base_domain.lower().strip(".")

    if not host or host == base_domain:
        return None

    suffix = f".{base_domain}"
    if not host.endswith(suffix):
        return None

    label = host[: -len(suffix)]
    if not label:
        return None
    return label.split(".")[-1] or None


class TenantMiddleware:
    """Resolves the organization and publishes it to Python and Postgres.

    The request is wrapped in an explicit transaction. That is not incidental:
    ``ATOMIC_REQUESTS`` wraps only the *view*, so a transaction-local Postgres
    setting issued from middleware would be discarded before the view ran, and
    every row-level security policy would see an empty organization. Opening the
    transaction here means the setting covers the whole request (§2.2, §2.3).
    """

    def __init__(self, get_response):  # type: ignore[no-untyped-def]
        self.get_response = get_response

    def __call__(self, request):  # type: ignore[no-untyped-def]
        subdomain = extract_subdomain(
            request.get_host(), getattr(settings, "TENANT_BASE_DOMAIN", "localhost")
        )

        # Testing from a phone on the office Wi-Fi, the laptop is reached by its
        # IP address — and an IP has no subdomain to carry the tenant. Rather
        # than make somebody run a DNS trick to try the app on a real device,
        # development may name one tenant to fall back to.
        #
        # Guarded by DEBUG, and there is no production path that sets it: on a
        # deployed system the subdomain *is* the tenant, and a fallback there
        # would be a way to reach the wrong organization's data.
        if not subdomain and settings.DEBUG:
            subdomain = getattr(settings, "DEV_TENANT_SLUG", "") or None

        # The platform admin console is deliberately cross-tenant (§2.2), so it
        # resolves no organization and its code paths use `all_objects`.
        is_platform_console = subdomain == settings.PLATFORM_ADMIN_SUBDOMAIN

        organization = None
        if subdomain and not is_platform_console:
            organization = Organization.objects.filter(slug=subdomain).first()
            if organization is None:
                # An unknown subdomain must not reveal whether it could exist.
                raise Http404(f"No organization is addressed by '{subdomain}'.")

        request.tenant_subdomain = subdomain
        request.is_platform_console = is_platform_console
        request.organization = organization

        with transaction.atomic():
            organization = self._reconcile_with_user(request, organization)
            token = activate_organization(organization.pk if organization else None)
            try:
                return self.get_response(request)
            finally:
                reset_current_organization(token)

    @staticmethod
    def _reconcile_with_user(request, organization: Organization | None):
        """Apply the user fallback, and refuse a subdomain/user mismatch.

        Only session-authenticated users are visible this early. API callers
        authenticate with JWT inside the view, so the same reconciliation is
        repeated by :class:`core.authentication.TenantJWTAuthentication` — which
        runs inside this transaction and so can still set the database setting.
        """
        # `getattr` throughout: `request.user` is a `SimpleLazyObject`, and an
        # unauthenticated request may carry no user attribute at all. Touching
        # `is_authenticated` resolves the lazy object, which is what we want —
        # but only once, and only through an access that cannot raise.
        user = getattr(request, "user", None)
        if user is None or not getattr(user, "is_authenticated", False):
            return organization

        user_organization_id = getattr(user, "organization_id", None)

        if organization is None:
            if user_organization_id is None:
                return None
            request.organization = user.organization
            return user.organization

        if user_organization_id is not None and user_organization_id != organization.pk:
            # This user belongs to a different tenant than the subdomain
            # addresses. 404, never 403 (§2.4, A3).
            raise Http404("Organization mismatch.")

        return organization
