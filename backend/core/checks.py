"""System checks guarding the tenancy invariants (design §2.3).

A new tenant-scoped model is easy to add and easy to forget to protect. This
check makes that omission fail loudly at startup rather than becoming a quiet
cross-tenant leak in production (A3).
"""

from __future__ import annotations

from django.core.checks import Error, Tags, register
from django.db import OperationalError, ProgrammingError, connection


def concrete_tenant_models() -> list[type]:
    """Every concrete model inheriting :class:`~core.tenancy.TenantModel`."""
    from django.apps import apps

    from core.tenancy import TenantModel

    return [
        model
        for model in apps.get_models()
        if issubclass(model, TenantModel) and not model._meta.abstract
    ]


@register(Tags.database)
def check_tenant_models_have_rls_policies(app_configs, **kwargs):
    """Fail when a tenant-scoped table has no row-level security policy.

    Skipped when the database is unreachable — ``manage.py check`` must still be
    usable without one, and the migration that creates a policy obviously cannot
    have run yet on an empty database.
    """
    from core.rls import POLICY_NAME, tables_with_policy

    models = concrete_tenant_models()
    if not models:
        return []

    try:
        # Skip while migrations are outstanding. Otherwise this check blocks the
        # very `migrate` run that would create the missing policy — the check
        # runs before the command does.
        if _has_unapplied_migrations():
            return []

        policied = tables_with_policy()
    except (OperationalError, ProgrammingError):
        return []

    if not connection.introspection.table_names():
        return []

    existing_tables = set(connection.introspection.table_names())

    errors = []
    for model in models:
        table = model._meta.db_table
        # A table that has not been created yet is a pending migration, not a
        # missing policy.
        if table not in existing_tables:
            continue
        if table not in policied:
            errors.append(
                Error(
                    f"{model._meta.label} is tenant-scoped but its table "
                    f"'{table}' has no '{POLICY_NAME}' row-level security policy.",
                    hint=(
                        "Add `enable_rls('"
                        f"{model._meta.app_label}.{model.__name__}"
                        "')` to a migration for this app. Row-level security is "
                        "the barrier that holds when the application layer is "
                        "wrong (A3, §2.3)."
                    ),
                    obj=model,
                    id="core.E001",
                )
            )
    return errors


def _has_unapplied_migrations() -> bool:
    """True when the database is not fully migrated.

    Used to stand the RLS check down: asserting that a policy exists is only
    meaningful once every migration — including the one that creates it — has
    been applied.
    """
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    targets = executor.loader.graph.leaf_nodes()
    return bool(executor.migration_plan(targets))
