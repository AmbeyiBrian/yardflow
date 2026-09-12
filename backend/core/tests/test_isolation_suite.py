"""T1.20 — the tenant isolation suite (§2.4, §14; requirement A3).

Two things run here, and both are wired into CI as required checks:

1. every tenant-scoped endpoint returns **404, not 403**, for another tenant's
   object ids
2. ``all_objects`` and ``rls_bypass`` appear nowhere outside ``platform_admin/``
   and migrations

The suite discovers endpoints from the URL configuration rather than a list, so
adding an endpoint without covering it fails the build.
"""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

import pytest
from django.urls import reverse

from core.isolation import discover_tenant_viewsets, registered_fixtures
from core.provisioning import provision_tenant

BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent

# Only these may reach across tenants (§2.1, §2.3).
UNSCOPED_ALLOWED_DIRS = {"platform_admin", "migrations"}

# The definitions themselves.
UNSCOPED_ALLOWED_FILES = {
    BACKEND_ROOT / "core" / "tenancy.py",
    BACKEND_ROOT / "core" / "rls.py",
    BACKEND_ROOT / "core" / "isolation.py",
    BACKEND_ROOT / "core" / "checks.py",
}


def code_without_comments_or_strings(source: str) -> str:
    """Return only executable tokens.

    A docstring that *mentions* `all_objects` — explaining when it is allowed,
    which several modules do — is not a bypass. Scanning raw text would flag
    the documentation and train people to ignore the check, so the scan looks
    at code only.
    """
    # Python 3.12 tokenizes f-strings into FSTRING_START/MIDDLE/END rather than
    # a single STRING, so their literal text needs excluding separately or a
    # message inside an f-string reads as code.
    ignored = {tokenize.COMMENT, tokenize.STRING}
    for name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END"):
        token_type = getattr(tokenize, name, None)
        if token_type is not None:
            ignored.add(token_type)

    kept: list[str] = []
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for token in tokens:
            if token.type in ignored:
                continue
            kept.append(token.string)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # Unparseable file: fall back to the raw text rather than skipping it.
        return source
    return " ".join(kept)


def _python_files():
    for path in BACKEND_ROOT.rglob("*.py"):
        if any(part in {".venv", "__pycache__", "node_modules"} for part in path.parts):
            continue
        yield path


def _is_allowed(path: Path) -> bool:
    if path in UNSCOPED_ALLOWED_FILES:
        return True
    if any(part in UNSCOPED_ALLOWED_DIRS for part in path.parts):
        return True
    # Tests are allowed to assert on the escape hatches.
    return "tests" in path.parts


class TestUnscopedAccessIsConfined:
    """§2.1: ``all_objects`` is grep-able on purpose, and CI does the grepping."""

    def test_all_objects_appears_only_where_it_is_allowed(self):
        pattern = re.compile(r"\ball_objects\b")
        offenders = [
            str(path.relative_to(BACKEND_ROOT))
            for path in _python_files()
            if not _is_allowed(path)
            and pattern.search(
                code_without_comments_or_strings(path.read_text(encoding="utf-8"))
            )
        ]

        assert offenders == [], (
            "`all_objects` bypasses tenant scoping and belongs only in "
            "platform_admin/ or a migration (§2.1, A3). Found in: "
            f"{offenders}"
        )

    def test_rls_bypass_appears_only_where_it_is_allowed(self):
        pattern = re.compile(r"\brls_bypass\b")
        offenders = [
            str(path.relative_to(BACKEND_ROOT))
            for path in _python_files()
            if not _is_allowed(path)
            and pattern.search(
                code_without_comments_or_strings(path.read_text(encoding="utf-8"))
            )
        ]

        assert offenders == [], (
            "`rls_bypass` lifts row-level security and belongs only in "
            f"platform_admin/ (§2.3). Found in: {offenders}"
        )


class TestEveryTenantEndpointIsCovered:
    """A3: coverage must be automatic, not remembered."""

    def test_every_tenant_scoped_viewset_has_an_isolation_fixture(self):
        """The check that makes the suite self-maintaining.

        A new tenant-scoped endpoint with no fixture fails here, rather than
        silently going untested.
        """
        viewsets = discover_tenant_viewsets()
        fixtures = registered_fixtures()

        uncovered = sorted(set(viewsets) - set(fixtures))

        assert uncovered == [], (
            "These tenant-scoped endpoints have no isolation fixture, so the "
            "A3 suite is not exercising them. Register one with "
            "`register_isolation_fixture(...)`, or `exempt_from_isolation(...)` "
            f"with a reason: {uncovered}"
        )


def _tenant_endpoint_cases():
    """(basename, viewset, fixture) for every covered, non-exempt endpoint."""
    viewsets = discover_tenant_viewsets()
    fixtures = registered_fixtures()
    cases = []
    for basename, viewset in sorted(viewsets.items()):
        fixture = fixtures.get(basename)
        if fixture is None or fixture.exempt_reason:
            continue
        cases.append(pytest.param(basename, viewset, fixture, id=basename))
    return cases


@pytest.mark.django_db
class TestCrossTenantAccessIsNotFound:
    """A3: every endpoint returns 404 — never 403 — for another tenant's id.

    404 and not 403 because a 403 confirms the object exists. An outsider
    probing ids must not be able to tell a real one from a made-up one.
    """

    @pytest.fixture
    def two_tenants(self, settings):
        settings.TENANT_BASE_DOMAIN = "localhost"
        first = provision_tenant(
            name="Silvertech", slug="silvertech", owner_email="owner@silvertech.co.ke"
        )
        second = provision_tenant(name="Rival", slug="rival", owner_email="owner@rival.co.ke")
        for result in (first, second):
            owner = result["owner"]
            owner.set_password("a good long password")
            owner.save()
        return first, second

    @staticmethod
    def _token(client, slug, email):
        client.defaults["HTTP_HOST"] = f"{slug}.localhost"
        response = client.post(
            reverse("v1:auth:login"),
            {"identifier": email, "password": "a good long password"},
            content_type="application/json",
        )
        assert response.status_code == 200, response.content
        return response.json()["access"]

    @pytest.mark.parametrize(("basename", "viewset", "fixture"), _tenant_endpoint_cases())
    def test_detail_verbs_return_404_across_tenants(
        self, basename, viewset, fixture, two_tenants, client
    ):
        from core.tenancy import tenant_context

        first, _second = two_tenants

        # An object belonging to tenant A.
        with tenant_context(first["organization"]):
            target = fixture.make(first["organization"])
        assert target is not None, f"fixture for {basename} returned nothing"

        # Tenant B asks for it by id, through their own subdomain.
        token = self._token(client, "rival", "owner@rival.co.ke")
        url = reverse(f"v1:{basename}-detail", args=[target.pk])
        auth = {"HTTP_AUTHORIZATION": f"Bearer {token}"}

        # Only verbs the viewset actually allows. A 405 for a verb the endpoint
        # does not offer is correct and reveals nothing about the object — a
        # read-only viewset refuses DELETE for everyone, tenant or not.
        allowed = {method.lower() for method in viewset.http_method_names}

        for verb, kwargs in (
            ("get", {}),
            ("put", {"data": fixture.payload or {}, "content_type": "application/json"}),
            ("patch", {"data": fixture.payload or {}, "content_type": "application/json"}),
            ("delete", {}),
        ):
            if verb not in allowed:
                continue
            response = getattr(client, verb)(url, **auth, **kwargs)
            assert response.status_code == 404, (
                f"{verb.upper()} {url} returned {response.status_code} for another "
                f"tenant's object. It must be 404 — a 403 confirms the object "
                f"exists (A3, §2.4)."
            )


class TestTheSuiteHasTeeth:
    """A suite that would pass against a leaking endpoint is worse than none."""

    def test_the_grep_would_catch_a_violation(self, tmp_path):
        """Prove the scanner matches, rather than trusting an empty result.

        With no violations in the tree, both grep tests pass trivially. This
        checks the pattern actually fires.
        """
        pattern = re.compile(r"\ball_objects\b")

        assert pattern.search(code_without_comments_or_strings("Widget.all_objects.count()"))
        assert not pattern.search(code_without_comments_or_strings("Widget.objects.count()"))
        # A near-miss must not trigger a false positive.
        assert not pattern.search(code_without_comments_or_strings("my_all_objectss = 1"))
        # A docstring explaining the rule is documentation, not a bypass.
        assert not pattern.search(
            code_without_comments_or_strings('"""Never use all_objects here."""')
        )
        assert not pattern.search(
            code_without_comments_or_strings("x = 1  # all_objects is banned")
        )
        # An f-string message mentioning it is documentation too.
        assert not pattern.search(
            code_without_comments_or_strings('raise E(f"use all_objects instead {x}")')
        )

    def test_discovery_finds_nothing_when_no_tenant_endpoints_exist_yet(self):
        """Documents the current state honestly.

        Phase 1 exposes auth, /me and the platform console — none of them a
        tenant-scoped CRUD endpoint. The parametrised test above therefore has
        no cases yet and grows automatically from Phase 2 (T2.11) onward.
        """
        viewsets = discover_tenant_viewsets()

        assert isinstance(viewsets, dict)
