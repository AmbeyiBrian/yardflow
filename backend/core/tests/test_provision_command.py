"""Provisioning a real tenant from the command line (A1, D7; §12.2).

The counterpart to `seed_demo`, which refuses to run in production because every
account it makes shares a password written in the repository. This one has no
password to leak: the owner is created unusable and invited to set their own.

The tests worth having are the ones about that guarantee, and about the failure
that would be worst — two tenants sharing a subdomain, which is the thing the
whole of §2.2 resolves a tenant by.
"""

import pytest
from django.core import mail
from django.core.management import CommandError, call_command

from core.models import Organization


@pytest.fixture(autouse=True)
def base_domain(settings):
    settings.TENANT_BASE_DOMAIN = "yardflow.buniva.co.ke"


def provision(**kwargs):
    options = {
        "name": "Silvertech Networks Limited",
        "slug": "silvertech",
        "owner_email": "owner@silvertech.co.ke",
    }
    options.update(kwargs)
    call_command("provision_tenant", **options)


@pytest.mark.django_db
class TestWhatItCreates:
    def test_the_organization_and_its_roles(self):
        provision()

        from accounts.models import Role
        from core.tenancy import tenant_context

        organization = Organization.objects.get(slug="silvertech")
        assert organization.name == "Silvertech Networks Limited"

        # Without roles the owner can sign in and do nothing at all.
        with tenant_context(organization):
            assert Role.objects.count() > 0

    def test_the_owner_cannot_log_in_until_they_set_a_password(self):
        """A1: no temporary password is generated, so none can be intercepted
        or left unchanged."""
        from accounts.models import User

        provision()

        owner = User.objects.get(email="owner@silvertech.co.ke")
        assert not owner.has_usable_password()

    def test_the_owner_is_invited(self):
        provision()

        assert len(mail.outbox) == 1
        assert "owner@silvertech.co.ke" in mail.outbox[0].to

    def test_the_invitation_can_be_skipped(self):
        """Before SES is verified, mail to an unverified address is accepted and
        discarded. The operator uses the printed link instead."""
        provision(no_invitation=True)

        assert mail.outbox == []
        # Created all the same — the link is what finishes the job.
        assert Organization.objects.filter(slug="silvertech").exists()


@pytest.mark.django_db
class TestWhatItRefuses:
    def test_a_subdomain_already_in_use(self):
        """The failure that matters: a subdomain *is* the tenant (§2.2), so two
        organizations sharing one would resolve to whichever was found first."""
        provision()

        with pytest.raises(CommandError, match="already exists"):
            provision(name="Someone Else Entirely", owner_email="other@example.com")

        assert Organization.objects.filter(slug="silvertech").count() == 1

    def test_an_owner_with_no_way_to_be_reached(self):
        """A1, B1: the invitation is the only way in, so an owner with neither
        an email address nor a phone number is an organization nobody can open."""
        with pytest.raises(CommandError, match="email address or a phone number"):
            call_command(
                "provision_tenant", name="Nowhere Ltd", slug="nowhere", owner_email=""
            )

        assert not Organization.objects.filter(slug="nowhere").exists()

    def test_nothing_is_left_behind_when_it_refuses(self):
        """A half-made tenant is worse than a refused one."""
        before = Organization.objects.count()

        with pytest.raises(CommandError):
            call_command("provision_tenant", name="Nowhere", slug="nowhere")

        assert Organization.objects.count() == before
