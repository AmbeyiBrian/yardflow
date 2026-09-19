"""H1 — who may raise a job and assign it (§4.9; H1, O3).

H1 is explicit about the actor: *"as an admin **or storekeeper**, I want to
assign a site or job to a named person"*. Creating a job used to require
`job.closeout`, which a storekeeper does not hold — so the requirement named
somebody who could not carry it out, and nobody noticed because no screen ever
called the endpoint.

These tests exist so that cannot drift back. Raising work and finishing it are
different acts done by different people, and the permissions now say so.
"""

import pytest
from django.urls import reverse

from accounts.permissions_registry import DEFAULT_ROLES, PERM
from core.provisioning import provision_tenant
from core.tenancy import tenant_context

PASSWORD = "a good long password"


@pytest.mark.django_db
class TestTheSeededRoles:
    """H1 names admin and storekeeper, so both must actually hold it."""

    @pytest.mark.parametrize("role", ["Admin", "Storekeeper", "Project manager"])
    def test_they_can_raise_a_job(self, role):
        assert PERM.JOB_MANAGE in DEFAULT_ROLES[role], f"{role} cannot raise a job"

    def test_the_owner_holds_it_too(self):
        assert PERM.JOB_MANAGE in DEFAULT_ROLES["Owner"]

    def test_a_technician_does_not(self):
        """They close jobs out; they do not hand themselves the work."""
        assert PERM.JOB_MANAGE not in DEFAULT_ROLES["Technician"]
        assert PERM.JOB_CLOSEOUT in DEFAULT_ROLES["Technician"]


@pytest.fixture
def tenancy(db, client, settings):
    """A tenant with one user per seeded role, each signed in."""
    from accounts.models import User, UserRole
    from network.factories import SiteFactory

    settings.TENANT_BASE_DOMAIN = "localhost"
    result = provision_tenant(
        name="Silvertech", slug="silvertech", owner_email="owner@silvertech.co.ke"
    )
    organization = result["organization"]
    roles = result["roles"]
    client.defaults["HTTP_HOST"] = "silvertech.localhost"

    people: dict[str, object] = {}
    with tenant_context(organization):
        for role_name in ("Storekeeper", "Technician", "Admin"):
            user = User.objects.create_user(
                email=f"{role_name.lower()}@silvertech.co.ke",
                password=PASSWORD,
                organization=organization,
                full_name=role_name,
            )
            UserRole.objects.create(
                organization=organization, user=user, role=roles[role_name]
            )
            people[role_name] = user
        site = SiteFactory(internal_ref="SLV-H1", name="Kileleshwa")

    def token_for(role_name: str) -> str:
        return client.post(
            reverse("v1:auth:login"),
            {
                "identifier": f"{role_name.lower()}@silvertech.co.ke",
                "password": PASSWORD,
            },
            content_type="application/json",
        ).json()["access"]

    return client, token_for, people, site


def auth(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


@pytest.mark.django_db
class TestTheEndpoint:
    def payload(self, site, assignee):
        return {
            "reference": "JOB-H1",
            "client": site.client_id,
            "site": site.pk,
            "assignee": assignee.pk,
        }

    def test_a_storekeeper_may_raise_one(self, tenancy):
        """The actor H1 names, and the one the old permission locked out."""
        http, token_for, people, site = tenancy

        response = http.post(
            reverse("v1:job-list"),
            self.payload(site, people["Technician"]),
            content_type="application/json",
            **auth(token_for("Storekeeper")),
        )

        assert response.status_code == 201, response.content

    def test_an_admin_may_raise_one(self, tenancy):
        http, token_for, people, site = tenancy

        response = http.post(
            reverse("v1:job-list"),
            self.payload(site, people["Technician"]),
            content_type="application/json",
            **auth(token_for("Admin")),
        )

        assert response.status_code == 201, response.content

    def test_a_technician_may_not(self, tenancy):
        """They hold `job.closeout`, which used to be all it took — so this is
        the exact hole the change closed: a technician could hand themselves
        work while the storekeeper named in H1 could not."""
        http, token_for, people, site = tenancy

        response = http.post(
            reverse("v1:job-list"),
            self.payload(site, people["Technician"]),
            content_type="application/json",
            **auth(token_for("Technician")),
        )

        assert response.status_code == 403
