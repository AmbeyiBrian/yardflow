"""Isolation fixtures for the site register endpoints (T1.20, A3)."""

from core.isolation import register_isolation_fixture


def register() -> None:
    from network.models import Client, Project, Site, SiteReference

    def make_client(organization):
        return Client.objects.create(organization=organization, name="Isolation Operator")

    def make_site(organization):
        return Site.objects.create(
            organization=organization,
            client=make_client(organization),
            internal_ref="ISO-1",
            name="Isolation site",
        )

    def make_site_reference(organization):
        return SiteReference.objects.create(
            organization=organization,
            site=make_site(organization),
            label="Operator ref",
            value="ISO-REF-1",
        )

    def make_project(organization):
        return Project.objects.create(
            organization=organization,
            client=make_client(organization),
            reference="ISO-WO-1",
        )

    register_isolation_fixture("client", make_client, payload={"name": "Renamed"})
    register_isolation_fixture(
        "site", make_site, payload={"name": "Renamed", "internal_ref": "ISO-1"}
    )
    register_isolation_fixture(
        "site-reference", make_site_reference, payload={"label": "Renamed", "value": "X"}
    )
    register_isolation_fixture(
        "project", make_project, payload={"reference": "ISO-WO-1"}
    )
