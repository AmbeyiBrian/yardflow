"""T1.3 — Organization and OrganizationSettings (§4.1, A1, A2, C8)."""

import pytest

from core.factories import OrganizationFactory
from core.models import Organization, OrganizationSettings


class TestOrganizationSettingsDefaults:
    """C8: every default is the one the requirements specify.

    The three conservative defaults are the point of this test. A tenant that
    never opens the settings screen must still get controlled behaviour.
    """

    def test_factory_creates_organization_with_documented_defaults(self, db):
        organization = OrganizationFactory()

        settings = organization.settings

        # D16: quantities are the primary currency; money stays hidden.
        assert settings.money_tracking_enabled is False
        # F3: nobody approves their own request unless explicitly allowed.
        assert settings.allow_self_approval is False
        # M4: posted documents are immutable; corrections are reversals.
        assert settings.allow_document_amendment is False

    def test_remaining_defaults_match_the_design(self, db):
        settings = OrganizationFactory().settings

        assert settings.min_stock_enabled is False  # E6
        assert settings.qr_labels_enabled is False
        assert settings.asset_tag_enabled is False
        assert settings.client_waybill_enabled is False  # K2
        assert settings.attachments_required_gate_in is False  # D6
        assert settings.attachments_required_gate_out is False
        assert settings.signature_required_on_release is False  # G3
        assert settings.gate_pass_expiry_hours == 24  # Q3
        assert settings.approval_escalation_hours == 24  # F5
        assert settings.timezone == "Africa/Nairobi"  # N-9
        assert settings.currency == "KES"  # N-9

    def test_settings_are_created_with_the_organization(self, db):
        """§4.1: one-to-one, created together — never a missing row."""
        organization = Organization.objects.create(name="Direct", slug="direct")

        assert OrganizationSettings.objects.filter(organization=organization).exists()

    def test_settings_row_is_not_duplicated_on_update(self, db):
        organization = OrganizationFactory()
        organization.name = "Renamed"
        organization.save()

        assert OrganizationSettings.objects.filter(organization=organization).count() == 1


class TestOrganizationSubdomain:
    """A1: subdomains are unique across the platform and immutable."""

    def test_subdomain_is_unique_across_the_platform(self, db):
        OrganizationFactory(slug="silvertech")

        from django.db import IntegrityError

        with pytest.raises(IntegrityError):
            Organization.objects.create(name="Impostor", slug="silvertech")

    def test_subdomain_is_immutable_after_creation(self, db):
        """Changing it would break every bookmark, deep link and printed QR."""
        organization = OrganizationFactory(slug="silvertech")

        organization.slug = "renamed"

        with pytest.raises(ValueError, match="immutable"):
            organization.save()

    def test_subdomain_is_normalised_to_lowercase(self, db):
        organization = Organization.objects.create(name="Shouty", slug="SHOUTY")

        assert organization.slug == "shouty"

    def test_renaming_the_organization_does_not_trip_the_immutability_guard(self, db):
        organization = OrganizationFactory(slug="silvertech")

        organization.name = "Silvertech Limited"
        organization.save()

        organization.refresh_from_db()
        assert organization.name == "Silvertech Limited"
        assert organization.slug == "silvertech"

    @pytest.mark.parametrize("reserved", ["admin", "api", "www", "health"])
    def test_platform_subdomains_cannot_be_taken_by_a_tenant(self, db, reserved):
        """§2.2: these address the platform itself, not a tenant."""
        with pytest.raises(ValueError, match="reserved"):
            Organization.objects.create(name="Squatter", slug=reserved)


class TestOrganizationSuspension:
    """A2: suspension never deletes data."""

    def test_organizations_are_active_by_default(self, db):
        assert OrganizationFactory().status == Organization.Status.ACTIVE

    def test_suspension_is_a_status_not_a_deletion(self, db):
        organization = OrganizationFactory()

        organization.status = Organization.Status.SUSPENDED
        organization.save()

        organization.refresh_from_db()
        assert organization.is_suspended is True
        # The row, and everything hanging off it, is still there.
        assert Organization.objects.filter(pk=organization.pk).exists()
