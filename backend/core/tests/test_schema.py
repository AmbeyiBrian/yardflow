"""T1.21 — the OpenAPI schema (§6, requirement N-10)."""

from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command

from core.schema import schema_generation_context

SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "api-schema.yml"


def generate_schema() -> str:
    """Generate the schema exactly as `manage.py openapi` does.

    `fail_on_warn` is the point: a schema that generates with warnings is one
    nobody trusts, and an untrusted schema does not serve N-10.
    """
    output = StringIO()
    with schema_generation_context():
        call_command("spectacular", stdout=output, validate=True, fail_on_warn=True)
    return output.getvalue()


class TestSchemaGeneration:
    def test_the_schema_generates_with_no_warnings(self, db):
        """T1.21's stated criterion.

        ``fail_on_warn`` is the whole point: a schema that generates with
        warnings is one nobody trusts, and an untrusted schema does not serve
        N-10's purpose of making integrations straightforward.
        """
        schema = generate_schema()

        assert "openapi:" in schema

    def test_the_committed_schema_matches_the_code(self, db):
        """CI check for drift.

        The schema is committed so that a reviewer can see an API change in the
        diff, and so a client generator has something stable to read. That is
        only true if it cannot fall behind the code.
        """
        assert SCHEMA_PATH.exists(), (
            f"{SCHEMA_PATH.name} is missing. Regenerate it with:\n"
            f"  python manage.py openapi --file api-schema.yml"
        )

        committed = SCHEMA_PATH.read_text(encoding="utf-8")

        assert committed.strip() == generate_schema().strip(), (
            "The committed OpenAPI schema no longer matches the code. "
            "Regenerate it with:\n"
            "  python manage.py openapi --file api-schema.yml"
        )


class TestSchemaContent:
    """A few assertions on the contract itself, not just that it generates."""

    def test_the_documented_auth_endpoints_are_present(self, db):
        schema = generate_schema()

        for path in (
            "/api/v1/auth/login",
            "/api/v1/auth/refresh",
            "/api/v1/auth/logout",
            "/api/v1/auth/password-reset",
            "/api/v1/me",
        ):
            assert path in schema, f"{path} is missing from the schema (§6)"

    def test_bearer_authentication_is_described(self, db):
        """So a generated client knows how to authenticate."""
        schema = generate_schema()

        assert "jwtAuth" in schema
        assert "bearer" in schema

    @pytest.mark.parametrize(
        "secret",
        ["password", "HomeManager", "secret_key"],
    )
    def test_the_schema_leaks_no_credentials(self, db, secret):
        """N-4: nothing secret belongs in a committed artefact.

        `password` appears as a field name, which is expected; what must not
        appear is a value.
        """
        committed = SCHEMA_PATH.read_text(encoding="utf-8")

        assert "HomeManager" not in committed
        assert "test-only-secret-key" not in committed
