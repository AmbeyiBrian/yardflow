"""OpenAPI schema extensions (design §6, requirement N-10).

N-10 asks for a documented API "so that future integrations are
straightforward". A schema full of warnings is a schema nobody trusts, so T1.21
requires it to generate cleanly — and that means teaching drf-spectacular about
the project's own classes rather than silencing it.
"""

from drf_spectacular.extensions import OpenApiAuthenticationExtension


class TenantJWTAuthenticationExtension(OpenApiAuthenticationExtension):
    """Describe ``TenantJWTAuthentication`` as standard bearer-token auth.

    It authenticates exactly like ``JWTAuthentication`` from the client's point
    of view; the tenant reconciliation it adds (§2.2) is server-side and not
    part of the wire contract.
    """

    target_class = "core.authentication.TenantJWTAuthentication"
    name = "jwtAuth"

    def get_security_definition(self, auto_schema):  # type: ignore[no-untyped-def]
        return {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": (
                "Access token from POST /api/v1/auth/login. Requests are also "
                "scoped by the tenant subdomain they are addressed to."
            ),
        }


# --------------------------------------------------------------------------
# Generating the schema outside a request
# --------------------------------------------------------------------------

import uuid  # noqa: E402
from collections.abc import Iterator  # noqa: E402
from contextlib import contextmanager  # noqa: E402


@contextmanager
def schema_generation_context() -> Iterator[None]:
    """Activate a throwaway organization while the schema is built.

    ``django_filters.utils.resolve_field`` resolves lookups by touching
    ``model._default_manager.all()``. The tenant manager rightly refuses that
    outside a request (§2.1), which would make schema generation impossible —
    so a random UUID is activated instead: it satisfies the manager and matches
    no rows in any tenant.

    This is not a hole in the isolation. Nothing is read; only Django's query
    machinery is asked what a field looks like.
    """
    from core.tenancy import tenant_context

    with tenant_context(uuid.uuid4()):
        yield
