"""T1.18 and T1.19 — provisioning, suspension, platform console (A1, A2)."""

import pytest
from django.core import mail
from django.db import IntegrityError, transaction
from django.urls import reverse

from accounts.models import Role, User, UserRole
from accounts.permissions_registry import ALL_CODENAMES, DEFAULT_ROLES
from core.models import Organization
from core.provisioning import provision_tenant
from core.tenancy import tenant_context


class TestProvisionTenant:
    """A1: one call produces a usable tenant."""

    def test_provisioning_creates_the_organization_and_settings(self, db):
        result = provision_tenant(
            name="Silvertech", slug="silvertech", owner_email="owner@silvertech.co.ke"
        )

        organization = result["organization"]
        assert organization.slug == "silvertech"
        assert organization.status == Organization.Status.ACTIVE
        # C8 defaults are guaranteed present, not merely likely.
        assert organization.settings.allow_self_approval is False

    def test_provisioning_seeds_the_default_roles(self, db):
        result = provision_tenant(
            name="Silvertech", slug="silvertech", owner_email="owner@silvertech.co.ke"
        )

        with tenant_context(result["organization"]):
            names = set(Role.objects.values_list("name", flat=True))

        assert names == set(DEFAULT_ROLES)

    def test_the_seeded_owner_role_holds_every_permission(self, db):
        result = provision_tenant(
            name="Silvertech", slug="silvertech", owner_email="owner@silvertech.co.ke"
        )

        with tenant_context(result["organization"]):
            owner_role = Role.objects.get(name="Owner")
            assert owner_role.codenames == set(ALL_CODENAMES)

    def test_the_owner_is_created_and_given_the_owner_role(self, db):
        result = provision_tenant(
            name="Silvertech",
            slug="silvertech",
            owner_email="owner@silvertech.co.ke",
            owner_full_name="Sam Owner",
        )

        owner = result["owner"]
        assert owner.email == "owner@silvertech.co.ke"
        assert owner.full_name == "Sam Owner"
        with tenant_context(result["organization"]):
            assert UserRole.objects.filter(user=owner, role__name="Owner").exists()

    def test_the_owner_is_invited_rather_than_issued_a_password(self, db):
        """A1: "the owner receives an invitation to set their password".

        No temporary password is generated, so none can be intercepted, shared
        or left unchanged.
        """
        result = provision_tenant(
            name="Silvertech", slug="silvertech", owner_email="owner@silvertech.co.ke"
        )

        assert result["owner"].has_usable_password() is False
        assert len(mail.outbox) == 1
        assert "Set your password" in mail.outbox[0].body

    def test_an_owner_with_only_a_phone_number_is_allowed(self, db):
        """B1: field staff — and small-company owners — may have no email."""
        result = provision_tenant(
            name="Silvertech", slug="silvertech", owner_phone="0722123456"
        )

        assert result["owner"].phone == "+254722123456"

    def test_an_owner_with_neither_identifier_is_refused(self, db):
        with pytest.raises(ValueError, match="email address or a phone number"):
            provision_tenant(name="Silvertech", slug="silvertech")

    def test_provisioning_is_atomic(self, db):
        """A half-provisioned tenant is worse than none.

        The subdomain is immutable once taken (A1), so a partial failure would
        permanently burn it.
        """
        provision_tenant(name="First", slug="taken", owner_email="a@example.com")

        # The unique constraint on the subdomain is what fails, and it
        # must take the whole provisioning with it.
        with pytest.raises(IntegrityError), transaction.atomic():
            provision_tenant(name="Second", slug="taken", owner_email="b@example.com")

        assert Organization.objects.filter(slug="taken").count() == 1


class TestPlatformConsoleAccess:
    """A1/A2: the console is restricted to platform admins."""

    @pytest.fixture
    def admin_client_and_token(self, db, client, settings):
        settings.TENANT_BASE_DOMAIN = "localhost"
        client.defaults["HTTP_HOST"] = "admin.localhost"
        User.objects.create_superuser(
            email="ops@yardflow.co.ke", password="platform password"
        )
        response = client.post(
            reverse("v1:auth:login"),
            {"identifier": "ops@yardflow.co.ke", "password": "platform password"},
            content_type="application/json",
        )
        assert response.status_code == 200, response.content
        return client, response.json()["access"]

    def test_a_platform_admin_can_list_tenants(self, admin_client_and_token):
        client, token = admin_client_and_token
        provision_tenant(name="Silvertech", slug="silvertech", owner_email="o@s.co.ke")

        response = client.get(
            reverse("platform-admin:organization-list"),
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        assert response.status_code == 200
        assert {row["slug"] for row in response.json()["results"]} == {"silvertech"}

    def test_an_anonymous_caller_is_refused(self, db, client, settings):
        settings.TENANT_BASE_DOMAIN = "localhost"
        client.defaults["HTTP_HOST"] = "admin.localhost"

        assert client.get(reverse("platform-admin:organization-list")).status_code == 401

    def test_a_tenant_user_cannot_reach_the_console(self, db, client, settings):
        """The most important negative case in the console.

        A tenant admin with every permission their organization can grant still
        has no business listing other customers.
        """
        settings.TENANT_BASE_DOMAIN = "localhost"
        result = provision_tenant(
            name="Silvertech", slug="silvertech", owner_email="owner@silvertech.co.ke"
        )
        owner = result["owner"]
        owner.set_password("owner password")
        owner.save()

        client.defaults["HTTP_HOST"] = "silvertech.localhost"
        token = client.post(
            reverse("v1:auth:login"),
            {"identifier": "owner@silvertech.co.ke", "password": "owner password"},
            content_type="application/json",
        ).json()["access"]

        response = client.get(
            reverse("platform-admin:organization-list"),
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        assert response.status_code == 403

    def test_provisioning_through_the_api(self, admin_client_and_token):
        """A1's definition of done: one API call produces a usable tenant."""
        client, token = admin_client_and_token

        response = client.post(
            reverse("platform-admin:organization-list"),
            {
                "name": "Silvertech",
                "slug": "silvertech",
                "owner_email": "owner@silvertech.co.ke",
                "owner_full_name": "Sam Owner",
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        assert response.status_code == 201, response.content
        organization = Organization.objects.get(slug="silvertech")
        with tenant_context(organization):
            assert Role.objects.count() == len(DEFAULT_ROLES)
            assert User.objects.filter(organization=organization).count() == 1

    def test_a_reserved_subdomain_is_refused(self, admin_client_and_token):
        client, token = admin_client_and_token

        response = client.post(
            reverse("platform-admin:organization-list"),
            {"name": "Squatter", "slug": "admin", "owner_email": "a@b.com"},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        assert response.status_code == 400

    def test_a_duplicate_subdomain_is_refused(self, admin_client_and_token):
        client, token = admin_client_and_token
        provision_tenant(name="First", slug="silvertech", owner_email="a@b.com")

        response = client.post(
            reverse("platform-admin:organization-list"),
            {"name": "Impostor", "slug": "silvertech", "owner_email": "c@d.com"},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        assert response.status_code == 400

    def test_tenants_cannot_be_deleted_through_the_console(self, admin_client_and_token):
        """A2: "suspension never deletes data"."""
        client, token = admin_client_and_token
        result = provision_tenant(name="Silvertech", slug="silvertech", owner_email="a@b.com")

        response = client.delete(
            reverse("platform-admin:organization-detail", args=[result["organization"].pk]),
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        assert response.status_code == 405


class TestSuspension:
    """A2: a suspended tenant can log in and read, but cannot post."""

    @pytest.fixture
    def suspended_tenant_client(self, db, client, settings):
        settings.TENANT_BASE_DOMAIN = "localhost"
        result = provision_tenant(
            name="Silvertech", slug="silvertech", owner_email="owner@silvertech.co.ke"
        )
        owner = result["owner"]
        owner.set_password("owner password")
        owner.save()

        organization = result["organization"]
        organization.status = Organization.Status.SUSPENDED
        organization.save(update_fields=["status"])

        client.defaults["HTTP_HOST"] = "silvertech.localhost"
        return client, organization, owner

    def test_a_suspended_tenants_user_can_still_log_in(self, suspended_tenant_client):
        client, _organization, _owner = suspended_tenant_client

        response = client.post(
            reverse("v1:auth:login"),
            {"identifier": "owner@silvertech.co.ke", "password": "owner password"},
            content_type="application/json",
        )

        assert response.status_code == 200

    def test_a_suspended_tenant_can_still_read(self, suspended_tenant_client):
        """Their records may be needed to answer an audit."""
        client, _organization, _owner = suspended_tenant_client
        token = client.post(
            reverse("v1:auth:login"),
            {"identifier": "owner@silvertech.co.ke", "password": "owner password"},
            content_type="application/json",
        ).json()["access"]

        response = client.get(reverse("v1:me"), HTTP_AUTHORIZATION=f"Bearer {token}")

        assert response.status_code == 200

    def test_suspension_never_deletes_data(self, suspended_tenant_client):
        _client, organization, owner = suspended_tenant_client

        assert Organization.objects.filter(pk=organization.pk).exists()
        assert User.objects.filter(pk=owner.pk).exists()
        with tenant_context(organization):
            assert Role.objects.count() == len(DEFAULT_ROLES)

    def test_suspend_and_reinstate_through_the_console(self, db, client, settings):
        settings.TENANT_BASE_DOMAIN = "localhost"
        client.defaults["HTTP_HOST"] = "admin.localhost"
        User.objects.create_superuser(email="ops@yardflow.co.ke", password="pw platform 1")
        token = client.post(
            reverse("v1:auth:login"),
            {"identifier": "ops@yardflow.co.ke", "password": "pw platform 1"},
            content_type="application/json",
        ).json()["access"]

        organization = provision_tenant(
            name="Silvertech", slug="silvertech", owner_email="a@b.com"
        )["organization"]

        suspend = client.post(
            reverse("platform-admin:organization-suspend", args=[organization.pk]),
            {"reason": "Non-payment"},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        assert suspend.status_code == 200
        assert suspend.json()["status"] == "SUSPENDED"

        reinstate = client.post(
            reverse("platform-admin:organization-reinstate", args=[organization.pk]),
            {"reason": "Paid"},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        assert reinstate.status_code == 200
        assert reinstate.json()["status"] == "ACTIVE"

    def test_suspension_is_recorded_in_the_tenants_own_audit_trail(self, db, client, settings):
        """M3: their admin should be able to see what happened to them."""
        from core.models import AuditAction, AuditLog

        settings.TENANT_BASE_DOMAIN = "localhost"
        client.defaults["HTTP_HOST"] = "admin.localhost"
        User.objects.create_superuser(email="ops@yardflow.co.ke", password="pw platform 1")
        token = client.post(
            reverse("v1:auth:login"),
            {"identifier": "ops@yardflow.co.ke", "password": "pw platform 1"},
            content_type="application/json",
        ).json()["access"]
        organization = provision_tenant(
            name="Silvertech", slug="silvertech", owner_email="a@b.com"
        )["organization"]

        client.post(
            reverse("platform-admin:organization-suspend", args=[organization.pk]),
            {"reason": "Non-payment"},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        with tenant_context(organization):
            entry = AuditLog.objects.get(action=AuditAction.ORGANIZATION_SUSPENDED)
            assert entry.before == {"status": "ACTIVE"}
            assert entry.after == {"status": "SUSPENDED"}
            assert entry.note == "Non-payment"
