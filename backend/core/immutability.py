"""Database-level append-only enforcement (design §3.2, §4.2).

Requirements M3, M4.

Two tables in this system must be append-only, and for the same reason: they are
the evidence. ``AuditLog`` answers "who did what" (M3) and ``StockMovement`` is
the ledger every stock figure is derived from (§3.2).

Enforcing it in Python only would not be enough. An ORM guard protects against
the application's own mistakes but not against a raw query, a management
command, a data migration or a psql session. **The rule belongs in the
database**, so nothing that connects can break it — which is exactly what an
auditor needs to be told (M3).
"""

from __future__ import annotations

from django.db import migrations


def append_only_function_sql(function_name: str, reason: str) -> str:
    """SQL creating the trigger function that refuses a change."""
    return f"""
    CREATE OR REPLACE FUNCTION {function_name}() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION
            '% is not permitted on %: {reason}',
            TG_OP, TG_TABLE_NAME
            USING ERRCODE = 'restrict_violation';
    END;
    $$ LANGUAGE plpgsql;
    """


def append_only_trigger_sql(table: str, function_name: str) -> str:
    """SQL attaching the refusal to UPDATE and DELETE on ``table``.

    TRUNCATE is deliberately not covered here. It is a table-level privilege
    rather than a row operation, so it is controlled by not granting it to the
    application role (T1.7) — and covering it would break Django's own test
    database teardown, which truncates legitimately.
    """
    trigger = f"{table}_append_only"
    return f"""
    DROP TRIGGER IF EXISTS {trigger} ON "{table}";
    CREATE TRIGGER {trigger}
        BEFORE UPDATE OR DELETE ON "{table}"
        FOR EACH ROW EXECUTE FUNCTION {function_name}();
    """


def make_append_only(model_label: str, reason: str) -> migrations.RunPython:
    """Migration operation making a table append-only.

    Usage::

        operations = [
            make_append_only(
                "stock.StockMovement",
                "the stock ledger is append-only; correct it with a REVERSAL "
                "movement instead (M4)",
            )
        ]
    """
    app_label, model_name = model_label.split(".")
    function_name = f"{app_label}_{model_name.lower()}_append_only"

    def forwards(apps, schema_editor):  # type: ignore[no-untyped-def]
        model = apps.get_model(app_label, model_name)
        table = model._meta.db_table
        # params=None matters: the trigger body contains '%' in its RAISE
        # format string, and psycopg would otherwise read it as a placeholder.
        schema_editor.execute(append_only_function_sql(function_name, reason), params=None)
        schema_editor.execute(append_only_trigger_sql(table, function_name), params=None)

    def backwards(apps, schema_editor):  # type: ignore[no-untyped-def]
        model = apps.get_model(app_label, model_name)
        table = model._meta.db_table
        schema_editor.execute(
            f'DROP TRIGGER IF EXISTS {table}_append_only ON "{table}";', params=None
        )
        schema_editor.execute(f"DROP FUNCTION IF EXISTS {function_name}();", params=None)

    return migrations.RunPython(forwards, backwards)


class AppendOnlyModel:
    """Mixin refusing to modify or delete an existing row from Python.

    The database trigger is the real guarantee. This exists so that a mistake
    surfaces as a clear application-level error during development, instead of a
    raw ``restrict_violation`` from Postgres at the end of a long stack trace.
    """

    #: Overridden per model to explain what to do instead.
    append_only_reason = "this record is append-only"

    def save(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self.pk is not None and not self._state.adding:
            raise ValueError(
                f"{type(self).__name__} cannot be modified once written: "
                f"{self.append_only_reason}."
            )
        return super().save(*args, **kwargs)  # type: ignore[misc]

    def delete(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise ValueError(
            f"{type(self).__name__} cannot be deleted: {self.append_only_reason}."
        )
