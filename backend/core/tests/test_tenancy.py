"""T1.4 — tenant context, manager and save guard (§2.1, A3).

The contract under test: **an unscoped read is a crash, never a silent leak.**
"""

import uuid

import pytest

from core.rls import rls_bypass
from core.tenancy import (
    TenantContextMissing,
    get_current_organization_id,
    tenant_context,
)
from core.tests.tenancy_app.models import Widget


class TestUnscopedQueriesRaise:
    """§2.1: no organization in context means an exception, not every row."""

    def test_query_without_tenant_context_raises(self, db):
        with pytest.raises(TenantContextMissing):
            list(Widget.objects.all())

    def test_count_without_tenant_context_raises(self, db):
        """Every entry point through the manager is covered, not just all()."""
        with pytest.raises(TenantContextMissing):
            Widget.objects.count()

    def test_get_without_tenant_context_raises(self, db):
        with pytest.raises(TenantContextMissing):
            Widget.objects.get(name="anything")

    def test_the_leak_this_prevents(self, organization, other_organization):
        """The regression this whole layer exists to stop.

        If ``get_queryset`` returned unfiltered rows when context was missing, a
        single forgotten filter would serve one tenant's stock to another.
        """
        with tenant_context(organization):
            Widget.objects.create(name="ours")
        with tenant_context(other_organization):
            Widget.objects.create(name="theirs")

        # Outside any context, the manager refuses rather than returning both.
        with pytest.raises(TenantContextMissing):
            list(Widget.objects.all())

        # Unscoped access exists, but you have to ask for it by name — and even
        # then row-level security still applies. Crossing tenants for real needs
        # `rls_bypass` as well, which is tested in test_rls.py.
        with rls_bypass():
            assert Widget.all_objects.count() == 2


class TestScopedQueries:
    def test_queries_see_only_the_active_organization(self, organization, other_organization):
        with tenant_context(organization):
            Widget.objects.create(name="ours")
        with tenant_context(other_organization):
            Widget.objects.create(name="theirs")

        with tenant_context(organization):
            assert [w.name for w in Widget.objects.all()] == ["ours"]

        with tenant_context(other_organization):
            assert [w.name for w in Widget.objects.all()] == ["theirs"]

    def test_another_tenants_object_is_not_found_by_primary_key(
        self, organization, other_organization
    ):
        """The basis of the 404-not-403 rule at the API layer (§2.4)."""
        with tenant_context(other_organization):
            theirs = Widget.objects.create(name="theirs")

        with tenant_context(organization), pytest.raises(Widget.DoesNotExist):
            Widget.objects.get(pk=theirs.pk)


class TestSaveStampsOrganization:
    """§2.1: callers cannot forget to set the organization."""

    def test_organization_is_stamped_from_context(self, tenant):
        widget = Widget.objects.create(name="stamped")

        assert widget.organization_id == tenant.pk

    def test_save_without_context_or_organization_raises(self, db):
        with pytest.raises(TenantContextMissing):
            Widget(name="orphan").save()


class TestSaveRejectsCrossTenantWrites:
    """§2.1: a leaked object from another tenant cannot be written to."""

    def test_saving_a_foreign_organizations_row_raises(self, organization, other_organization):
        with tenant_context(other_organization):
            theirs = Widget.objects.create(name="theirs")

        # Obtained out of band — the exact situation the guard is for.
        theirs.name = "tampered"
        with tenant_context(organization), pytest.raises(
            TenantContextMissing, match="cross-tenant"
        ):
            theirs.save()

    def test_creating_a_row_for_another_organization_raises(
        self, organization, other_organization
    ):
        with tenant_context(organization), pytest.raises(TenantContextMissing):
            Widget(organization=other_organization, name="planted").save()

    def test_a_context_set_from_a_string_still_permits_its_own_writes(
        self, organization
    ):
        """The guard compares ids, not their Python types.

        Found from a Celery task: an organization id is a UUID on a model
        instance but a *string* when it arrives as a task argument or a JWT
        claim, and `UUID(...) != "same-uuid"` is true. The guard therefore
        refused a write to the very organization in context, with a message
        naming the same id twice — which would have broken every scheduled sweep
        that writes (§2.2, T7.5).
        """
        # The instance carries a UUID, as any loaded row does.
        with tenant_context(organization):
            widget = Widget.objects.create(name="mine")
        assert isinstance(widget.organization_id, uuid.UUID)

        # The context carries a string, as a Celery task argument does.
        with tenant_context(str(organization.pk)):
            widget.name = "still mine"
            widget.save()
            widget.refresh_from_db()

        assert widget.name == "still mine"

        # And the guard still bites when the ids genuinely differ.
        with tenant_context(str(organization.pk)), pytest.raises(TenantContextMissing):
            Widget(organization_id=uuid.uuid4(), name="planted").save()


class TestContextManagement:
    """Nesting and restoration, so one request cannot bleed into the next."""

    def test_context_is_restored_after_the_block(self, organization):
        assert get_current_organization_id() is None

        with tenant_context(organization):
            assert get_current_organization_id() == organization.pk

        assert get_current_organization_id() is None

    def test_context_is_restored_even_when_the_block_raises(self, organization):
        with pytest.raises(RuntimeError), tenant_context(organization):
            raise RuntimeError("boom")

        assert get_current_organization_id() is None

    def test_contexts_nest(self, organization, other_organization):
        with tenant_context(organization):
            with tenant_context(other_organization):
                assert get_current_organization_id() == other_organization.pk
            assert get_current_organization_id() == organization.pk

    def test_context_accepts_an_organization_or_its_id(self, organization):
        with tenant_context(organization.pk):
            assert get_current_organization_id() == organization.pk


class TestUnscopedEscapeHatch:
    """``all_objects`` is the only way across tenants, and is grep-able (T1.20)."""

    def test_all_objects_removes_the_python_filter(self, organization, other_organization):
        """It lifts the manager's filter — not the database's.

        Row-level security is a separate barrier, so genuinely crossing tenants
        needs `rls_bypass` too (§2.3). That is deliberate: it means a missing
        tenant context can never silently return everyone's rows.
        """
        with tenant_context(organization):
            Widget.objects.create(name="ours")
        with tenant_context(other_organization):
            Widget.objects.create(name="theirs")

        # No exception, unlike `objects` — but the database still filters.
        assert Widget.all_objects.count() == 0

        with rls_bypass():
            assert Widget.all_objects.count() == 2

    def test_all_objects_needs_no_context(self, db):
        assert Widget.all_objects.count() == 0
