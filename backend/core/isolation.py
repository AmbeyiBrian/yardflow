"""Machinery for the tenant isolation suite (design §2.4, §14; requirement A3).

A3's acceptance criterion is a test suite, not a code review: "**a test suite**
asserting that every API endpoint returns 404 (not 403) for another tenant's
object IDs."

The hard part is *every*. A suite listing endpoints by hand rots the moment
someone adds one — and the endpoint that got missed is exactly the one that
leaks. So the suite walks the URL configuration to discover endpoints, and this
registry says how to build a test object for each. A tenant-scoped viewset with
no registered fixture **fails the suite**, which makes coverage the default
rather than something to remember.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from django.urls import URLPattern, URLResolver, get_resolver


@dataclass(frozen=True)
class IsolationFixture:
    """How to exercise one tenant-scoped endpoint.

    ``make`` is called with the organization active and must return a saved
    instance belonging to it. ``payload`` is a minimal valid request body for the
    write verbs; ``None`` skips those verbs for endpoints that are read-only.
    """

    make: Callable
    payload: dict | None = None
    #: Set when an endpoint is deliberately not tenant-scoped, with the reason.
    exempt_reason: str = ""


#: basename -> fixture. Keyed by the router basename so registration lives next
#: to the viewset it describes.
_FIXTURES: dict[str, IsolationFixture] = {}


def register_isolation_fixture(
    basename: str,
    make: Callable,
    *,
    payload: dict | None = None,
) -> None:
    """Declare how the isolation suite should exercise an endpoint."""
    _FIXTURES[basename] = IsolationFixture(make=make, payload=payload)


def exempt_from_isolation(basename: str, reason: str) -> None:
    """Record that an endpoint is deliberately not tenant-scoped.

    Requires a reason, so an exemption is a decision someone wrote down rather
    than a gap. Used for the platform admin console, which is cross-tenant by
    design (§2.2).
    """
    if not reason:
        raise ValueError("An isolation exemption must state why.")
    _FIXTURES[basename] = IsolationFixture(make=lambda *_: None, exempt_reason=reason)


def registered_fixtures() -> dict[str, IsolationFixture]:
    return dict(_FIXTURES)


def discover_tenant_viewsets() -> dict[str, type]:
    """Return every ``TenantScopedViewSet`` reachable from the URL config.

    Discovery rather than a hand-written list: an endpoint someone forgot to add
    to a list is precisely the one that would leak (A3).
    """
    from core.api import TenantScopedViewSet

    found: dict[str, type] = {}

    def walk(patterns, prefix=""):  # type: ignore[no-untyped-def]
        for entry in patterns:
            if isinstance(entry, URLResolver):
                walk(entry.url_patterns, prefix)
                continue
            if not isinstance(entry, URLPattern):
                continue

            callback = entry.callback
            viewset = getattr(callback, "cls", None)
            if viewset is None:
                continue
            if not isinstance(viewset, type) or not issubclass(viewset, TenantScopedViewSet):
                continue

            # Match only the canonical route names. A custom @action route is
            # named "{basename}-{url_name}", so splitting on the last hyphen
            # would invent basenames like "item-category-custom" from
            # "item-category-custom-fields".
            name = entry.name or ""
            for suffix in ("-list", "-detail"):
                if name.endswith(suffix):
                    found[name[: -len(suffix)]] = viewset
                    break

    walk(get_resolver().url_patterns)
    return found
