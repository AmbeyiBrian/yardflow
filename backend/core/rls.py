"""Postgres row-level security (design §2.3, layer 3 of four).

Requirement A3. This is the barrier that holds when the application layer is
wrong: a forgotten filter, a raw query, a bug in a manager. It lives in the
database, so no Python mistake can switch it off.

Three deliberate departures from the SQL as written in §2.3, each for a concrete
reason:

1. **``NULLIF`` around ``current_setting``.** ``current_setting('app.current_org',
   true)`` returns an *empty string*, not NULL, when the setting has been set to
   ``''`` — which is exactly what happens when a request resolves no tenant.
   ``''::uuid`` raises ``invalid input syntax for type uuid`` and turns a clean
   "no rows" into a 500. ``NULLIF(..., '')::uuid`` yields NULL, and
   ``organization_id = NULL`` is NULL, so the row is correctly not visible.

2. **``FORCE ROW LEVEL SECURITY``.** A plain ``ENABLE`` exempts the table's
   owner. §2.3 relies on the application connecting as a non-owner to make the
   policy bite, which is true in production but silently untrue anywhere the
   connecting role happens to own the tables — including a developer's machine
   and CI. ``FORCE`` makes the policy apply to the owner too, so the guarantee
   no longer depends on who ran the migration.

3. **An explicit bypass GUC.** With a strict policy, ``all_objects`` — the
   documented way for platform admin code to cross tenants (§2.1) — would return
   nothing, because the database filters what the manager deliberately did not.
   Rather than weaken the policy to "see everything when no tenant is set"
   (which would turn every missing-context bug into a silent leak), crossing
   tenants requires saying so through :func:`rls_bypass`. It is as grep-able as
   ``all_objects`` and, like it, belongs only in ``platform_admin/``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from django.db import connection, migrations

#: Session setting carrying the active organization (§2.2).
ORGANIZATION_SETTING = "app.current_org"

#: Session setting that, when 'on', lets deliberately cross-tenant code read
#: across organizations. Set only by :func:`rls_bypass`.
BYPASS_SETTING = "app.bypass_rls"

POLICY_NAME = "tenant_isolation"

# The predicate every tenant table is guarded by. Note NULLIF (see 1 above).
_PREDICATE = f"""(
    organization_id = NULLIF(current_setting('{ORGANIZATION_SETTING}', true), '')::uuid
    OR current_setting('{BYPASS_SETTING}', true) = 'on'
)"""


def rls_sql(table: str) -> str:
    """Return the SQL enabling tenant isolation on ``table``."""
    return f"""
    ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY;
    ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS {POLICY_NAME} ON "{table}";
    CREATE POLICY {POLICY_NAME} ON "{table}"
        USING {_PREDICATE}
        WITH CHECK {_PREDICATE};
    """


def rls_reverse_sql(table: str) -> str:
    """Return the SQL removing tenant isolation, so migrations stay reversible."""
    return f"""
    DROP POLICY IF EXISTS {POLICY_NAME} ON "{table}";
    ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY;
    """


def enable_rls(*model_labels: str) -> migrations.RunPython:
    """Migration operation enabling row-level security on the given models.

    Usage in a migration::

        operations = [enable_rls("stock.StockMovement", "stock.StockBalance")]

    Takes model labels rather than table names so that a renamed table cannot
    leave a policy pointing at nothing; the table is resolved from the model at
    migration time.
    """

    def forwards(apps, schema_editor):  # type: ignore[no-untyped-def]
        for label in model_labels:
            app_label, model_name = label.split(".")
            model = apps.get_model(app_label, model_name)
            schema_editor.execute(rls_sql(model._meta.db_table), params=None)

    def backwards(apps, schema_editor):  # type: ignore[no-untyped-def]
        for label in model_labels:
            app_label, model_name = label.split(".")
            model = apps.get_model(app_label, model_name)
            schema_editor.execute(rls_reverse_sql(model._meta.db_table), params=None)

    return migrations.RunPython(forwards, backwards)


def tables_with_policy() -> set[str]:
    """Return the tables that currently carry the isolation policy."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tablename FROM pg_policies "
            "WHERE policyname = %s AND schemaname = current_schema()",
            [POLICY_NAME],
        )
        return {row[0] for row in cursor.fetchall()}


@contextmanager
def rls_bypass() -> Iterator[None]:
    """Read across organizations, deliberately and visibly.

    For platform admin code only, and paired with ``all_objects``. Like
    ``all_objects`` this is grep-able on purpose: CI fails the build when it
    appears outside ``platform_admin/`` (T1.20).

    Transaction-scoped, like the organization setting itself, so it cannot leak
    into another request.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting(%s, true)", [BYPASS_SETTING])
        row = cursor.fetchone()
        previous = (row[0] if row else "") or ""
        cursor.execute("SELECT set_config(%s, 'on', true)", [BYPASS_SETTING])
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config(%s, %s, true)", [BYPASS_SETTING, previous])
