"""T1.12 — authentication endpoints (§6, B1, B4).

These go through the real HTTP stack, including ``TenantMiddleware``, so the
subdomain resolution proved in T1.5 is exercised here too.
"""

import pytest
from django.urls import reverse

from accounts.factories import RoleFactory, UserFactory, UserRoleFactory
from accounts.permissions_registry import PERM
from core.factories import OrganizationFactory
from core.tenancy import tenant_context

SUBDOMAIN_HOST = "silvertech.localhost"


@pytest.fixture
def api(client, settings):
    """A client addressing the Silvertech subdomain."""
    settings.TENANT_BASE_DOMAIN = "localhost"
    client.defaults["HTTP_HOST"] = SUBDOMAIN_HOST
    return client


@pytest.fixture
def silvertech(db):
    return OrganizationFactory(name="Silvertech", slug="silvertech")


@pytest.fixture
def storekeeper(silvertech):
    # Role and assignment are tenant-scoped, so building them needs an active
    # tenant context — exactly as a request would have (§2.1).
    with tenant_context(silvertech):
        user = UserFactory(
            organization=silvertech,
            email="store@silvertech.co.ke",
            phone="0722123456",
            full_name="Jane Storekeeper",
        )
        user.set_password("correct horse battery")
        user.save()
        UserRoleFactory(
            user=user,
            role=RoleFactory(
                name="Storekeeper",
                codenames=[PERM.GATE_IN_POST, PERM.GATE_OUT_REQUEST],
            ),
        )
    return user


def login(api, identifier, password):
    return api.post(
        reverse("v1:auth:login"),
        {"identifier": identifier, "password": password},
        content_type="application/json",
    )


class TestLoginWithEitherIdentifier:
    """B1: email *or* phone, so field staff without email can still log in."""

    def test_login_with_an_email_address(self, api, storekeeper):
        response = login(api, "store@silvertech.co.ke", "correct horse battery")

        assert response.status_code == 200
        assert "access" in response.json()
        assert "refresh" in response.json()

    def test_login_with_a_phone_number(self, api, storekeeper):
        response = login(api, "0722123456", "correct horse battery")

        assert response.status_code == 200
        assert "access" in response.json()

    def test_login_with_a_differently_formatted_phone_number(self, api, storekeeper):
        """A user who types their own number with spaces must still get in."""
        response = login(api, "0722 123 456", "correct horse battery")

        assert response.status_code == 200

    def test_email_is_case_insensitive(self, api, storekeeper):
        response = login(api, "Store@Silvertech.CO.KE", "correct horse battery")

        assert response.status_code == 200

    def test_the_response_carries_the_user_and_permissions(self, api, storekeeper):
        """§7.2: the client drives navigation from this payload."""
        payload = login(api, "store@silvertech.co.ke", "correct horse battery").json()

        assert payload["user"]["full_name"] == "Jane Storekeeper"
        assert set(payload["user"]["permissions"]) == {
            PERM.GATE_IN_POST,
            PERM.GATE_OUT_REQUEST,
        }
        assert payload["user"]["organization"]["slug"] == "silvertech"


class TestLoginFailures:
    def test_a_wrong_password_is_rejected(self, api, storekeeper):
        assert login(api, "store@silvertech.co.ke", "wrong").status_code == 400

    def test_an_unknown_identifier_is_rejected(self, api, silvertech):
        assert login(api, "nobody@silvertech.co.ke", "whatever").status_code == 400

    def test_the_message_does_not_reveal_whether_the_account_exists(self, api, storekeeper):
        """Otherwise the endpoint enumerates a company's staff list."""
        wrong_password = login(api, "store@silvertech.co.ke", "wrong").json()
        unknown_user = login(api, "nobody@silvertech.co.ke", "wrong").json()

        assert wrong_password == unknown_user

    def test_a_deactivated_user_cannot_log_in(self, api, storekeeper):
        """B3: their records survive; their access does not."""
        storekeeper.is_active = False
        storekeeper.save()

        assert login(api, "store@silvertech.co.ke", "correct horse battery").status_code == 400

    def test_a_user_without_a_password_set_cannot_log_in(self, api, silvertech):
        """A1/B3: invited staff have an unusable password until they set one."""
        UserFactory(organization=silvertech, email="invited@silvertech.co.ke")

        assert login(api, "invited@silvertech.co.ke", "").status_code == 400

    def test_failures_render_the_error_envelope(self, api, storekeeper):
        """§6.1: one error shape everywhere."""
        body = login(api, "store@silvertech.co.ke", "wrong").json()

        assert body["error"]["code"] == "VALIDATION_ERROR"


class TestLoginIsTenantScoped:
    """B1/A3: identifiers are unique per tenant, so login must be scoped."""

    def test_a_user_of_another_tenant_cannot_log_in_through_this_subdomain(
        self, api, silvertech
    ):
        """The bug this prevents is severe and easy to write.

        Two tenants may each have a user with the same email address. An
        unscoped lookup would let one company's staff into the other's yard.
        """
        rival = OrganizationFactory(name="Rival", slug="rival")
        intruder = UserFactory(organization=rival, email="shared@example.com")
        intruder.set_password("their password")
        intruder.save()

        response = login(api, "shared@example.com", "their password")

        assert response.status_code == 400

    def test_the_right_user_is_chosen_when_two_tenants_share_an_address(
        self, api, silvertech
    ):
        ours = UserFactory(organization=silvertech, email="shared@example.com")
        ours.set_password("our password")
        ours.save()

        rival = OrganizationFactory(name="Rival", slug="rival")
        theirs = UserFactory(organization=rival, email="shared@example.com")
        theirs.set_password("their password")
        theirs.save()

        # Our password works through our subdomain...
        assert login(api, "shared@example.com", "our password").status_code == 200
        # ...and theirs does not.
        assert login(api, "shared@example.com", "their password").status_code == 400


class TestMe:
    def test_me_requires_authentication(self, api, silvertech):
        assert api.get(reverse("v1:me")).status_code == 401

    def test_me_returns_resolved_permissions(self, api, storekeeper):
        access = login(api, "store@silvertech.co.ke", "correct horse battery").json()["access"]

        response = api.get(reverse("v1:me"), HTTP_AUTHORIZATION=f"Bearer {access}")

        assert response.status_code == 200
        assert set(response.json()["permissions"]) == {
            PERM.GATE_IN_POST,
            PERM.GATE_OUT_REQUEST,
        }

    def test_me_reports_the_organization_settings_the_client_needs(self, api, storekeeper):
        """C8: the client cannot decide what to render without these."""
        access = login(api, "store@silvertech.co.ke", "correct horse battery").json()["access"]

        settings_payload = api.get(
            reverse("v1:me"), HTTP_AUTHORIZATION=f"Bearer {access}"
        ).json()["organization"]["settings"]

        assert settings_payload["money_tracking_enabled"] is False
        assert settings_payload["currency"] == "KES"
        assert settings_payload["timezone"] == "Africa/Nairobi"


class TestRefreshAndLogout:
    """B1: refresh tokens are revocable, so logout genuinely ends a session."""

    def test_a_refresh_token_yields_a_new_access_token(self, api, storekeeper):
        refresh = login(api, "store@silvertech.co.ke", "correct horse battery").json()["refresh"]

        response = api.post(
            reverse("v1:auth:token-refresh"), {"refresh": refresh}, content_type="application/json"
        )

        assert response.status_code == 200
        assert "access" in response.json()

    def test_logout_blacklists_the_refresh_token(self, api, storekeeper):
        """T1.12's stated criterion: a blacklisted refresh token is rejected."""
        tokens = login(api, "store@silvertech.co.ke", "correct horse battery").json()

        logout = api.post(
            reverse("v1:auth:logout"),
            {"refresh": tokens["refresh"]},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {tokens['access']}",
        )
        assert logout.status_code == 204

        # The revoked token must no longer buy a new access token.
        reused = api.post(
            reverse("v1:auth:token-refresh"),
            {"refresh": tokens["refresh"]},
            content_type="application/json",
        )
        assert reused.status_code == 401

    def test_logout_requires_authentication(self, api, storekeeper):
        response = api.post(
            reverse("v1:auth:logout"), {}, content_type="application/json"
        )
        assert response.status_code == 401


class TestPermissionCatalogue:
    def test_the_catalogue_lists_every_registered_permission(self, api, storekeeper):
        from accounts.permissions_registry import ALL_CODENAMES

        access = login(api, "store@silvertech.co.ke", "correct horse battery").json()["access"]

        response = api.get(
            reverse("v1:permission-catalogue"), HTTP_AUTHORIZATION=f"Bearer {access}"
        )

        assert response.status_code == 200
        assert {row["codename"] for row in response.json()} == set(ALL_CODENAMES)
