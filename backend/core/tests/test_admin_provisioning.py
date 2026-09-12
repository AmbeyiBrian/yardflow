"""Creating a customer from the Django admin (A1, A2).

The platform owner has no screen for this in the product — a tenant must never be
able to create another tenant — so onboarding happened through the REST console,
which in practice meant an API docs page. This puts it where an administrator
already is.

The rule worth testing is not "a row appears". It is that the admin form goes
through `provision_tenant`, so a tenant created here is indistinguishable from one
created through the console: an owner who can be invited, seeded roles, settings.
A hollow `Organization` row is the failure this guards against — it looks fine in
a list and nobody can log into it.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from accounts.models import User
from core.models import Organization

pytestmark = pytest.mark.django_db


@pytest.fixture
def platform_admin(db):
    # `is_platform_admin` is derived, not stored: it means "belongs to no
    # organization", which is exactly what makes the account cross-tenant.
    return User.objects.create_superuser(
        email="platform@yardflow.co.ke", password="a good long password"
    )


@pytest.fixture
def console(client, platform_admin):
    """A signed-in platform owner. Named to avoid pytest-django's `admin_client`,
    which logs in a user of its own."""
    client.force_login(platform_admin)
    return client


class TestCreatingATenant:
    def test_the_form_provisions_an_owner_and_roles(self, console):
        response = console.post(
            reverse("admin:core_organization_add"),
            {
                "name": "Global Connect",
                "slug": "globalconnect",
                "owner_email": "owner@globalconnect.co.ke",
                "owner_full_name": "Their Owner",
                "legal_name": "",
                "email": "",
                "phone": "",
                "address": "",
                "tax_pin": "",
                # The settings inline.
                "settings-TOTAL_FORMS": "0",
                "settings-INITIAL_FORMS": "0",
                "settings-MIN_NUM_FORMS": "0",
                "settings-MAX_NUM_FORMS": "1",
            },
        )

        assert response.status_code in (200, 302), response.content[:400]
        organization = Organization.objects.get(slug="globalconnect")
        assert organization.name == "Global Connect"

        # The point of the test: not a hollow row.
        owner = User.objects.get(email="owner@globalconnect.co.ke")
        assert owner.organization_id == organization.id
        # Read from inside the tenant. Row-level security is enforced in
        # Postgres, so rows belonging to an organization are invisible from
        # outside its context no matter which manager asks — `all_objects`
        # bypasses the Django filter, not the database policy.
        from accounts.models import UserRole
        from core.tenancy import tenant_context

        with tenant_context(organization):
            assert UserRole.objects.filter(user=owner).exists(), (
                "an owner with no role cannot do anything"
            )
        assert hasattr(organization, "settings")

    def test_no_password_is_created_for_the_owner(self, console):
        """A1: they are invited to set one, so none can be intercepted."""
        console.post(
            reverse("admin:core_organization_add"),
            {
                "name": "Global Connect",
                "slug": "globalconnect",
                "owner_email": "owner@globalconnect.co.ke",
                "owner_full_name": "",
                "legal_name": "",
                "email": "",
                "phone": "",
                "address": "",
                "tax_pin": "",
                "settings-TOTAL_FORMS": "0",
                "settings-INITIAL_FORMS": "0",
                "settings-MIN_NUM_FORMS": "0",
                "settings-MAX_NUM_FORMS": "1",
            },
        )

        owner = User.objects.get(email="owner@globalconnect.co.ke")
        assert not owner.has_usable_password()

    def test_a_tenant_nobody_can_sign_into_is_refused(self, console):
        response = console.post(
            reverse("admin:core_organization_add"),
            {
                "name": "Global Connect",
                "slug": "globalconnect",
                "owner_email": "",
                "owner_phone": "",
                "owner_full_name": "",
                "legal_name": "",
                "email": "",
                "phone": "",
                "address": "",
                "tax_pin": "",
                "settings-TOTAL_FORMS": "0",
                "settings-INITIAL_FORMS": "0",
                "settings-MIN_NUM_FORMS": "0",
                "settings-MAX_NUM_FORMS": "1",
            },
        )

        assert response.status_code == 200, "the form should come back with an error"
        assert not Organization.objects.filter(slug="globalconnect").exists()

    def test_a_reserved_subdomain_is_refused(self, console):
        response = console.post(
            reverse("admin:core_organization_add"),
            {
                "name": "Not Allowed",
                "slug": "admin",
                "owner_email": "owner@example.co.ke",
                "owner_full_name": "",
                "legal_name": "",
                "email": "",
                "phone": "",
                "address": "",
                "tax_pin": "",
                "settings-TOTAL_FORMS": "0",
                "settings-INITIAL_FORMS": "0",
                "settings-MIN_NUM_FORMS": "0",
                "settings-MAX_NUM_FORMS": "1",
            },
        )

        assert response.status_code == 200
        assert not Organization.objects.filter(slug="admin").exists()


class TestWhatTheAdminWillNotDo:
    def test_a_tenant_cannot_be_deleted(self, console, organization):
        """A2 replaces deletion with suspension: a tenant's ledger is somebody's
        audit trail, so there is no button for destroying it."""
        response = console.post(
            reverse("admin:core_organization_delete", args=[organization.pk]),
            {"post": "yes"},
        )

        assert response.status_code in (403, 302)
        assert Organization.objects.filter(pk=organization.pk).exists()

    def test_a_staff_user_who_is_not_a_platform_admin_sees_nothing(
        self, client, organization
    ):
        """Belonging to a tenant is what disqualifies them, staff flag or not."""
        ordinary = User.objects.create_user(
            email="staff@silvertech.co.ke",
            password="a good long password",
            organization=organization,
        )
        ordinary.is_staff = True
        ordinary.save(update_fields=["is_staff"])
        assert ordinary.is_platform_admin is False
        client.force_login(ordinary)

        response = client.get(reverse("admin:core_organization_changelist"))

        assert response.status_code in (302, 403), (
            "tenant administration is not for ordinary staff accounts"
        )


class TestEditingSettingsFromTheAdmin:
    """Changing a tenant's settings has to actually persist.

    Reported from use: settings set on the organization page in the Django admin
    were still at their defaults when the tenant's own Settings screen read them.
    """

    def _change_post(self, organization, **overrides):
        """The full change form, since Django treats a missing field as cleared."""
        settings_row = organization.settings
        data = {
            "name": organization.name,
            "legal_name": organization.legal_name,
            "email": organization.email,
            "phone": organization.phone,
            "address": organization.address,
            "tax_pin": organization.tax_pin,
            "settings-TOTAL_FORMS": "1",
            "settings-INITIAL_FORMS": "1",
            "settings-MIN_NUM_FORMS": "0",
            "settings-MAX_NUM_FORMS": "1",
            "settings-0-organization": str(organization.pk),
            "settings-0-asset_tag_prefix_format": settings_row.asset_tag_prefix_format,
            "settings-0-gate_pass_expiry_hours": str(settings_row.gate_pass_expiry_hours),
            "settings-0-approval_escalation_hours": str(
                settings_row.approval_escalation_hours
            ),
            "settings-0-retention_months": str(settings_row.retention_months),
            "settings-0-timezone": settings_row.timezone,
            "settings-0-currency": settings_row.currency,
            "settings-0-notification_channels": "{}",
            "settings-0-notification_matrix": "{}",
            "settings-0-sms_credit_balance": str(settings_row.sms_credit_balance),
            "settings-0-sms_credit_low_threshold": str(
                settings_row.sms_credit_low_threshold
            ),
        }
        # Checkboxes are present only when ticked.
        for field in (
            "money_tracking_enabled",
            "min_stock_enabled",
            "qr_labels_enabled",
            "asset_tag_enabled",
            "client_waybill_enabled",
            "attachments_enabled",
            "attachments_required_gate_in",
            "attachments_required_gate_out",
            "signature_required_on_release",
            "allow_self_approval",
            "allow_document_amendment",
        ):
            if overrides.get(field, getattr(settings_row, field)):
                data[f"settings-0-{field}"] = "on"
        data.update({k: v for k, v in overrides.items() if not isinstance(v, bool)})
        return data

    def test_a_switch_turned_on_in_the_admin_is_stored(self, console, organization):
        response = console.post(
            reverse("admin:core_organization_change", args=[organization.pk]),
            self._change_post(
                organization,
                signature_required_on_release=True,
                min_stock_enabled=True,
            ),
        )

        assert response.status_code in (200, 302)
        organization.settings.refresh_from_db()
        assert organization.settings.signature_required_on_release is True
        assert organization.settings.min_stock_enabled is True

    def test_a_number_changed_in_the_admin_is_stored(self, console, organization):
        console.post(
            reverse("admin:core_organization_change", args=[organization.pk]),
            self._change_post(organization, **{"settings-0-gate_pass_expiry_hours": "8"}),
        )

        organization.settings.refresh_from_db()
        assert organization.settings.gate_pass_expiry_hours == 8

    def test_what_the_admin_stores_is_what_the_api_serves(self, console, organization):
        """The tenant's own Settings screen reads this endpoint, so the two must
        agree — that is the whole of the report."""
        from accounts.admin_api import OrganizationSettingsSerializer

        console.post(
            reverse("admin:core_organization_change", args=[organization.pk]),
            self._change_post(organization, client_waybill_enabled=True),
        )

        organization.settings.refresh_from_db()
        served = OrganizationSettingsSerializer(organization.settings).data
        assert served["client_waybill_enabled"] is True
