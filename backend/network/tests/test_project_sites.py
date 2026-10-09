"""T18.1 — ``Project.sites`` through model and the PO columns (§4.19; R10, R12)."""

from datetime import date

import pytest
from django.db import IntegrityError, ProgrammingError, connection, transaction
from django.db.migrations.executor import MigrationExecutor

from accounts.factories import RoleFactory, UserFactory, UserRoleFactory
from accounts.permissions_registry import PERM
from commercials.tests.api_helpers import PASSWORD, Api, results
from core.rls import rls_bypass
from core.tenancy import tenant_context
from network.factories import ClientFactory, ProjectFactory, SiteFactory
from network.models import Project, ProjectSite


def _link(project, *sites):
    project.sites.add(*sites, through_defaults={"organization_id": project.organization_id})


class TestThroughModel:
    def test_the_many_to_many_reads_and_writes_as_before(self, tenant):
        client = ClientFactory()
        project = ProjectFactory(client=client)
        first, second = SiteFactory(client=client), SiteFactory(client=client)

        _link(project, first, second)

        assert set(project.sites.all()) == {first, second}
        assert list(first.projects.all()) == [project]
        assert Project.objects.filter(sites=first).get() == project
        assert project.sites.count() == 2

    def test_the_through_row_carries_the_tenant_and_empty_dates(self, tenant):
        project = ProjectFactory()
        site = SiteFactory(client=project.client)
        _link(project, site)

        link = ProjectSite.objects.get(project=project, site=site)

        assert link.organization_id == tenant.pk
        assert link.mobilised_on is None and link.accepted_on is None
        assert link.created_at is not None

    def test_dates_are_stored_on_the_link(self, tenant):
        project = ProjectFactory()
        site = SiteFactory(client=project.client)
        _link(project, site)

        ProjectSite.objects.filter(project=project, site=site).update(
            mobilised_on=date(2026, 3, 1), accepted_on=date(2026, 4, 1)
        )

        link = ProjectSite.objects.get(project=project, site=site)
        assert (link.mobilised_on, link.accepted_on) == (date(2026, 3, 1), date(2026, 4, 1))

    def test_a_site_is_linked_to_a_project_once(self, tenant):
        project = ProjectFactory()
        site = SiteFactory(client=project.client)
        ProjectSite.objects.create(project=project, site=site)

        with pytest.raises(IntegrityError), transaction.atomic():
            ProjectSite.objects.create(project=project, site=site)

    def test_a_bulk_add_without_the_organization_is_refused(self, tenant):
        """A caller that forgets through_defaults fails loudly (NOT NULL, or RLS first)."""
        project = ProjectFactory()
        site = SiteFactory(client=project.client)

        with pytest.raises((IntegrityError, ProgrammingError)), transaction.atomic():
            project.sites.add(site)

    def test_the_link_is_invisible_to_another_tenant(self, organization, other_organization):
        with tenant_context(organization):
            project = ProjectFactory()
            _link(project, SiteFactory(client=project.client))

        with tenant_context(other_organization):
            assert ProjectSite.objects.count() == 0
        with tenant_context(organization):
            assert ProjectSite.objects.count() == 1

    def test_row_level_security_is_on_for_the_table(self, db):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT relrowsecurity FROM pg_class WHERE relname = 'network_project_sites'"
            )
            assert cursor.fetchone() == (True,)

    def test_rls_hides_the_rows_even_when_the_orm_filter_is_bypassed(
        self, organization, other_organization
    ):
        with tenant_context(organization):
            project = ProjectFactory()
            _link(project, SiteFactory(client=project.client))

        with tenant_context(other_organization):
            assert ProjectSite.all_objects.count() == 0


class TestPoColumns:
    def test_new_columns_default_to_empty(self, tenant):
        project = ProjectFactory()
        project.refresh_from_db()

        assert project.po_issue_date is None
        assert project.payment_terms == ""
        assert project.payment_terms_days is None
        assert project.po_recorded_at is None

    def test_they_store_what_the_po_says(self, tenant):
        project = ProjectFactory()
        project.po_issue_date = date(2026, 5, 2)
        project.payment_terms = "30 days from certified invoice"
        project.payment_terms_days = 30
        project.save()

        project.refresh_from_db()
        assert project.po_issue_date == date(2026, 5, 2)
        assert project.payment_terms_days == 30
        assert project.payment_terms.startswith("30 days")


@pytest.fixture(autouse=True)
def _domain(settings):
    settings.TENANT_BASE_DOMAIN = "localhost"


@pytest.fixture
def api(tenant, client):
    role = RoleFactory(codenames=[PERM.CATALOGUE_MANAGE])
    user = UserFactory(organization=tenant, password=PASSWORD)
    UserRoleFactory(user=user, role=role)
    return Api(client, user)


class TestCreationPaths:
    """Every way a project and its sites come together still works."""

    def test_the_project_api_writes_and_updates_sites(self, tenant, api):
        client = ClientFactory()
        first, second = SiteFactory(client=client), SiteFactory(client=client)

        created = api.post("projects", {"client": client.pk, "sites": [first.pk]})
        assert created.status_code == 201, created.content
        project_id = created.json()["id"]
        assert created.json()["sites"] == [first.pk]

        patched = api.patch(f"projects/{project_id}", {"sites": [first.pk, second.pk]})
        assert patched.status_code == 200, patched.content
        assert sorted(patched.json()["sites"]) == sorted([first.pk, second.pk])
        links = ProjectSite.objects.filter(project_id=project_id)
        assert links.count() == 2
        assert {link.organization_id for link in links} == {tenant.pk}

        shrunk = api.patch(f"projects/{project_id}", {"sites": [second.pk]})
        assert shrunk.json()["sites"] == [second.pk]

    def test_a_patch_without_sites_leaves_them(self, tenant, api):
        project = ProjectFactory()
        _link(project, SiteFactory(client=project.client))

        response = api.patch(f"projects/{project.pk}", {"description": "x"})

        assert response.status_code == 200, response.content
        assert len(response.json()["sites"]) == 1

    def test_the_site_filter_still_works(self, tenant, api):
        project = ProjectFactory()
        site = SiteFactory(client=project.client)
        _link(project, site)
        ProjectFactory()

        response = api.get("projects", site=site.pk)

        assert [row["id"] for row in results(response)] == [project.pk]

@pytest.mark.django_db(transaction=True)
class TestMigration:
    """§4.19.14: links created before the migration keep their rows and gain the tenant."""

    def test_existing_links_survive_and_are_backfilled(self, organization, other_organization):
        executor = MigrationExecutor(connection)
        # Every app, not only network: rolling network back also unapplies the
        # migrations that depend on it (the supplier on a purchase), and they
        # must come back for the rest of this worker's tests.
        latest = executor.loader.graph.leaf_nodes()
        executor.migrate([("network", "0012_supplier_rls")])
        old_apps = executor.loader.project_state(
            [("network", "0012_supplier_rls")]
        ).apps

        try:
            Client = old_apps.get_model("network", "Client")
            Site = old_apps.get_model("network", "Site")
            OldProject = old_apps.get_model("network", "Project")
            expected = []
            with transaction.atomic(), rls_bypass():
                rows = ((organization, "M-1", ""), (other_organization, "M-2", "PO-9"))
                for org, ref, po in rows:
                    client = Client.objects.create(organization_id=org.pk, name=f"C {ref}")
                    site = Site.objects.create(
                        organization_id=org.pk, client=client, internal_ref=ref, name=ref
                    )
                    project = OldProject.objects.create(
                        organization_id=org.pk, client=client, reference=ref, po_number=po,
                        **(_po_columns(org) if po else {}),
                    )
                    project.sites.add(site)
                    expected.append((project.pk, site.pk, org.pk, bool(po)))

            executor = MigrationExecutor(connection)
            executor.migrate(latest)

            with transaction.atomic(), rls_bypass():
                rows = {
                    (row.project_id, row.site_id): row
                    for row in ProjectSite.all_objects.all()
                }
                assert set(rows) == {(p, s) for p, s, _, _ in expected}
                for project_id, site_id, org_id, has_po in expected:
                    row = rows[(project_id, site_id)]
                    assert row.organization_id == org_id
                    assert row.mobilised_on is None and row.accepted_on is None
                    project = Project.all_objects.get(pk=project_id)
                    assert (project.po_recorded_at == project.opened_at) is has_po
                    assert (project.po_recorded_at is None) is (not has_po)
        finally:
            MigrationExecutor(connection).migrate(latest)


def _any_user(organization) -> int:
    return UserFactory(organization=organization).pk  # type: ignore[attr-defined]


def _po_columns(organization) -> dict:
    return {"manager_id": _any_user(organization), "contract_value": 1, "cost_budget": 1}
