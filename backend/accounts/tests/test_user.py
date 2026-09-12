"""T1.9 — the custom user model (§4.2, B1).

B1: log in with either an email address or a phone number, because field staff
without email must still be able to use the system.
"""

import pytest
from django.db import IntegrityError, transaction

from accounts.models import User, normalise_phone
from core.factories import OrganizationFactory


class TestIdentifierRequirement:
    """B1: at least one identifier, or the user could never log in."""

    def test_email_only_is_allowed(self, organization):
        user = User.objects.create_user(email="store@silvertech.co.ke", organization=organization)

        assert user.email == "store@silvertech.co.ke"
        assert user.phone is None

    def test_phone_only_is_allowed(self, organization):
        """The technician case: no email address at all."""
        user = User.objects.create_user(phone="0722123456", organization=organization)

        assert user.phone == "+254722123456"
        assert user.email is None

    def test_neither_identifier_is_rejected(self, organization):
        with pytest.raises(ValueError, match="email address or a phone number"):
            User.objects.create_user(organization=organization)

    def test_the_database_also_refuses_a_user_with_neither(self, organization):
        """Belt and braces: the constraint holds even if the manager is bypassed."""
        with pytest.raises(IntegrityError), transaction.atomic():
            User.objects.create(organization=organization, email=None, phone=None)


class TestPerTenantUniqueness:
    """B1: both identifiers are unique *within* a tenant, not globally."""

    def test_the_same_email_may_exist_in_two_organizations(self, db):
        """The explicit requirement in T1.9's definition of done.

        Two customer companies may reuse an address such as info@ or share a
        contractor. Global uniqueness would make the second tenant unusable.
        """
        first = OrganizationFactory(slug="silvertech")
        second = OrganizationFactory(slug="rival")

        User.objects.create_user(email="shared@example.com", organization=first)
        User.objects.create_user(email="shared@example.com", organization=second)

        assert User.objects.filter(email="shared@example.com").count() == 2

    def test_the_same_phone_may_exist_in_two_organizations(self, db):
        first = OrganizationFactory(slug="a-co")
        second = OrganizationFactory(slug="b-co")

        User.objects.create_user(phone="+254722123456", organization=first)
        User.objects.create_user(phone="+254722123456", organization=second)

        assert User.objects.filter(phone="+254722123456").count() == 2

    def test_duplicate_email_within_one_organization_is_rejected(self, organization):
        User.objects.create_user(email="dup@example.com", organization=organization)

        with pytest.raises(IntegrityError), transaction.atomic():
            User.objects.create_user(email="dup@example.com", organization=organization)

    def test_duplicate_phone_within_one_organization_is_rejected(self, organization):
        User.objects.create_user(phone="0722123456", organization=organization)

        with pytest.raises(IntegrityError), transaction.atomic():
            User.objects.create_user(phone="0722123456", organization=organization)

    def test_duplicate_platform_admin_email_is_rejected(self, db):
        """Platform admins have a NULL organization.

        Postgres treats NULLs as distinct in a unique constraint, so the
        per-organization constraints do not constrain platform admins at all.
        A dedicated partial constraint covers them.
        """
        User.objects.create_superuser(email="ops@yardflow.co.ke", password="x")

        with pytest.raises(IntegrityError), transaction.atomic():
            User.objects.create_superuser(email="ops@yardflow.co.ke", password="y")


class TestNormalisation:
    """Field staff type numbers and addresses inconsistently."""

    @pytest.mark.parametrize(
        ("entered", "stored"),
        [
            # Every way one person writes their own number, ending in one
            # string. The national form is expanded rather than merely stripped
            # of punctuation: 0722123456 and +254722123456 are the same phone,
            # and storing them differently meant the uniqueness check missed the
            # duplicate and the person could not sign in with the other form.
            ("0722 123 456", "+254722123456"),
            ("0722-123-456", "+254722123456"),
            ("+254 722 123 456", "+254722123456"),
            ("(0722) 123456", "+254722123456"),
            ("254722123456", "+254722123456"),
            ("00254722123456", "+254722123456"),
            # Already international, and not Kenyan: left alone. A foreign
            # supplier's number must not be mangled into a local one.
            ("+44 20 7946 0000", "+442079460000"),
            ("", None),
            (None, None),
        ],
    )
    def test_phone_is_normalised(self, entered, stored):
        assert normalise_phone(entered) == stored

    def test_the_two_forms_of_one_number_are_the_same_user(self, organization):
        """The point of the change, from a real login failure: somebody invited
        as +254… and typing 0722… is the same person."""
        User.objects.create_user(phone="+254722123456", organization=organization)

        with pytest.raises(IntegrityError), transaction.atomic():
            User.objects.create_user(phone="0722123456", organization=organization)

    def test_email_is_lowercased(self, organization):
        user = User.objects.create_user(email="Store@Silvertech.CO.KE", organization=organization)

        assert user.email == "store@silvertech.co.ke"

    def test_normalisation_makes_the_uniqueness_constraint_effective(self, organization):
        """Without normalisation, these two would both be accepted."""
        User.objects.create_user(phone="0722123456", organization=organization)

        with pytest.raises(IntegrityError), transaction.atomic():
            User.objects.create_user(phone="0722 123 456", organization=organization)

    def test_normalisation_applies_on_a_direct_save_too(self, organization):
        user = User(organization=organization, email="MiXeD@Example.COM", phone="0722 999 888")
        user.save()

        user.refresh_from_db()
        assert user.email == "mixed@example.com"
        assert user.phone == "+254722999888"


class TestPlatformAdmin:
    """§3: a platform admin is exactly a user with no organization."""

    def test_a_platform_admin_has_no_organization(self, db):
        admin = User.objects.create_superuser(email="ops@yardflow.co.ke", password="x")

        assert admin.organization_id is None
        assert admin.is_platform_admin is True

    def test_a_tenant_user_is_not_a_platform_admin(self, organization):
        user = User.objects.create_user(email="store@example.com", organization=organization)

        assert user.is_platform_admin is False


class TestPasswordHandling:
    def test_a_user_created_without_a_password_cannot_log_in_yet(self, organization):
        """B3/A1: staff are created by an admin, then invited to set a password."""
        user = User.objects.create_user(email="invited@example.com", organization=organization)

        assert user.has_usable_password() is False

    def test_a_password_is_hashed_not_stored(self, organization):
        user = User.objects.create_user(
            email="store@example.com", password="correct horse", organization=organization
        )

        assert user.password != "correct horse"
        assert user.check_password("correct horse") is True


class TestDeactivation:
    """B3: deactivating preserves history; deletion is not the mechanism."""

    def test_users_are_active_by_default(self, organization):
        assert User.objects.create_user(email="a@b.com", organization=organization).is_active

    def test_deactivation_keeps_the_record(self, organization):
        user = User.objects.create_user(email="a@b.com", organization=organization)

        user.is_active = False
        user.save()

        user.refresh_from_db()
        assert user.is_active is False
        assert User.objects.filter(pk=user.pk).exists()
