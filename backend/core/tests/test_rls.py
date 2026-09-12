"""T1.6 — Postgres row-level security (§2.3, A3).

These are the tests that matter most in the whole tenancy layer. Everything
above them is Python and can be bypassed by a raw query; this cannot.
"""

import uuid

import pytest
from django.core.checks import Error
from django.db import connection

from core.checks import check_tenant_models_have_rls_policies, concrete_tenant_models
from core.rls import POLICY_NAME, rls_bypass, tables_with_policy
from core.tenancy import set_database_organization, tenant_context
from core.tests.tenancy_app.models import Widget


def raw_widget_names() -> list[str]:
    """Read the table with raw SQL, bypassing every Python-level protection."""
    with connection.cursor() as cursor:
        cursor.execute('SELECT name FROM "tenancy_app_widget" ORDER BY name')
        return [row[0] for row in cursor.fetchall()]


@pytest.mark.rls
class TestRawSqlCannotCrossTenants:
    """The barrier that holds when the application layer is wrong."""

    def test_raw_sql_returns_only_the_active_tenants_rows(
        self, organization, other_organization
    ):
        """T1.6's definition of done, stated exactly.

        No manager, no queryset, no filter — just SQL against the table.
        """
        with tenant_context(organization):
            Widget.objects.create(name="ours")
        with tenant_context(other_organization):
            Widget.objects.create(name="theirs")

        with tenant_context(organization):
            assert raw_widget_names() == ["ours"]

        with tenant_context(other_organization):
            assert raw_widget_names() == ["theirs"]

    def test_raw_sql_with_no_organization_set_returns_nothing(
        self, organization, other_organization
    ):
        """A missing tenant means no rows, never every row.

        This is why the policy uses NULLIF: `current_setting` returns an empty
        string here, and `''::uuid` would raise instead of hiding the rows.
        """
        with tenant_context(organization):
            Widget.objects.create(name="ours")
        with tenant_context(other_organization):
            Widget.objects.create(name="theirs")

        set_database_organization(None)

        assert raw_widget_names() == []

    def test_an_unrelated_organization_id_sees_nothing(self, organization):
        with tenant_context(organization):
            Widget.objects.create(name="ours")

        set_database_organization(uuid.uuid4())

        assert raw_widget_names() == []

    def test_the_policy_applies_to_the_table_owner_too(self, organization):
        """Why FORCE ROW LEVEL SECURITY, not just ENABLE.

        A plain ENABLE exempts the owner. Whether the application role happens
        to own the tables varies by environment, so without FORCE the guarantee
        would hold in production and quietly not hold in CI.
        """
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT relforcerowsecurity FROM pg_class WHERE relname = %s",
                ["tenancy_app_widget"],
            )
            forced = cursor.fetchone()[0]

        assert forced is True

    def test_insert_for_another_organization_is_refused_by_the_database(
        self, organization, other_organization
    ):
        """The WITH CHECK half of the policy.

        Even with the Python guard removed from the picture, the database will
        not accept a row belonging to a tenant other than the active one.
        """
        from django.db import ProgrammingError, transaction

        with tenant_context(organization):
            with pytest.raises((ProgrammingError, Exception)) as caught, transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        'INSERT INTO "tenancy_app_widget" (organization_id, name) '
                        "VALUES (%s, %s)",
                        [str(other_organization.pk), "planted"],
                    )

        assert "row-level security" in str(caught.value).lower()


@pytest.mark.rls
class TestDeliberateCrossTenantAccess:
    """``all_objects`` plus ``rls_bypass`` — visible, and confined to platform admin."""

    def test_all_objects_alone_is_not_enough_under_row_level_security(
        self, organization, other_organization
    ):
        """Worth stating plainly, because it is a trap.

        ``all_objects`` removes the *Python* filter. The database filter is still
        there, which is the whole point of having a second barrier.
        """
        with tenant_context(organization):
            Widget.objects.create(name="ours")
        with tenant_context(other_organization):
            Widget.objects.create(name="theirs")

        set_database_organization(None)

        assert Widget.all_objects.count() == 0

    def test_bypass_plus_all_objects_crosses_tenants(self, organization, other_organization):
        with tenant_context(organization):
            Widget.objects.create(name="ours")
        with tenant_context(other_organization):
            Widget.objects.create(name="theirs")

        set_database_organization(None)

        with rls_bypass():
            assert Widget.all_objects.count() == 2
            assert raw_widget_names() == ["ours", "theirs"]

    def test_the_bypass_is_released_afterwards(self, organization, other_organization):
        with tenant_context(organization):
            Widget.objects.create(name="ours")
        with tenant_context(other_organization):
            Widget.objects.create(name="theirs")

        set_database_organization(None)

        with rls_bypass():
            assert Widget.all_objects.count() == 2

        assert Widget.all_objects.count() == 0

    def test_the_bypass_is_released_even_when_the_block_raises(self, organization):
        with tenant_context(organization):
            Widget.objects.create(name="ours")
        set_database_organization(None)

        with pytest.raises(RuntimeError), rls_bypass():
            raise RuntimeError("boom")

        assert Widget.all_objects.count() == 0


@pytest.mark.rls
class TestPolicyCoverage:
    def test_every_tenant_table_carries_the_policy(self, db):
        policied = tables_with_policy()

        missing = [
            model._meta.label
            for model in concrete_tenant_models()
            if model._meta.db_table not in policied
        ]

        assert missing == [], f"tenant models without an RLS policy: {missing}"

    def test_the_system_check_reports_a_model_with_no_policy(self, db):
        """T1.6's other stated criterion: the check catches an unpolicied model.

        The policy is dropped for the duration of the test, which is the honest
        way to prove the check would fire — asserting on a hand-built Error
        object would only test the assertion.
        """
        with connection.cursor() as cursor:
            cursor.execute(f'DROP POLICY {POLICY_NAME} ON "tenancy_app_widget"')

        errors = check_tenant_models_have_rls_policies(app_configs=None)

        assert any(
            isinstance(error, Error)
            and error.id == "core.E001"
            and "tenancy_app_widget" in error.msg
            for error in errors
        ), f"expected core.E001 for the unpolicied table, got {errors}"

    def test_the_system_check_passes_when_every_policy_is_present(self, db):
        assert check_tenant_models_have_rls_policies(app_configs=None) == []
