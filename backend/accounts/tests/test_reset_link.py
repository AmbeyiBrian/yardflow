"""Where an invitation link sends somebody (A1, B1, §2.2).

From a real failure. A tenant was provisioned from the Django admin and its new
owner was emailed a link to `http://127.0.0.1:8000/reset-password?...` — the API,
on the host the *administrator* happened to be using, not the app, and not their
tenant. `reset_url` had built the right host and then thrown it away whenever the
connection was not HTTPS.

The rule: the link is addressed from the account, never from the request. Whoever
sends an invitation has nothing to do with where the recipient must go.
"""

from __future__ import annotations

import pytest
from django.test import RequestFactory

from accounts.models import User
from accounts.reset import make_reset_token, reset_url, send_password_reset
from core.provisioning import provision_tenant

pytestmark = pytest.mark.django_db


@pytest.fixture
def tenant_owner(db, settings):
    settings.TENANT_BASE_DOMAIN = "yardflow.co.ke"
    result = provision_tenant(
        name="Global Connect",
        slug="globalconnect",
        owner_email="owner@globalconnect.co.ke",
        send_invitation=False,
    )
    return result["owner"]


def test_the_link_goes_to_the_tenants_own_subdomain(tenant_owner, settings):
    settings.APP_URL_TEMPLATE = "https://{host}"

    url = reset_url(tenant_owner, make_reset_token(tenant_owner))

    assert url.startswith("https://globalconnect.yardflow.co.ke/reset-password?")


def test_an_insecure_request_does_not_change_the_destination(tenant_owner, settings):
    """The bug, pinned.

    An invitation sent while provisioning from the admin console arrives with a
    request whose host is the console's. That must not leak into the link.
    """
    settings.APP_URL_TEMPLATE = "https://{host}"
    request = RequestFactory().post("/django-admin/core/organization/add/")
    request.META["HTTP_HOST"] = "127.0.0.1:8000"

    url = reset_url(tenant_owner, make_reset_token(tenant_owner), request=request)

    assert "127.0.0.1" not in url, "the admin's host is not where the owner goes"
    assert url.startswith("https://globalconnect.yardflow.co.ke/")


def test_development_links_carry_the_app_port(tenant_owner, settings):
    """The app is on 5173 in development while the API is on 8000, so the
    template has to be able to say so."""
    settings.APP_URL_TEMPLATE = "http://{host}:5173"

    url = reset_url(tenant_owner, make_reset_token(tenant_owner))

    assert url.startswith("http://globalconnect.yardflow.co.ke:5173/reset-password?")


def test_a_platform_admin_is_sent_to_the_console_subdomain(db, settings):
    """They belong to no organization, so there is no tenant subdomain to use."""
    settings.TENANT_BASE_DOMAIN = "yardflow.co.ke"
    settings.PLATFORM_ADMIN_SUBDOMAIN = "admin"
    settings.APP_URL_TEMPLATE = "https://{host}"
    platform_admin = User.objects.create_superuser(
        email="platform@yardflow.co.ke", password="a good long password"
    )

    url = reset_url(platform_admin, make_reset_token(platform_admin))

    assert url.startswith("https://admin.yardflow.co.ke/reset-password?")


class TestTheInvitationBySms:
    """B2: a technician may have a phone and no email, so invitations go by SMS.

    It used to call the provider directly — a real message at real cost, with no
    credit deducted and no record anywhere, so a tenant's balance said one thing
    and their Ujumbe bill said another. Every other SMS in the system is metered
    (L4); there is no reason this one should not be.
    """

    def test_somebody_with_a_phone_is_texted(self, tenant_owner, settings):
        from unittest import mock

        from notifications import credits

        settings.SMS_BACKEND = "notifications.channels.logging_sms.LoggingSmsBackend"
        credits.purchase(tenant_owner.organization, 5, note="For the test")
        tenant_owner.phone = "+254722000123"
        tenant_owner.save(update_fields=["phone"])

        with mock.patch("accounts.reset.get_sms_backend") as backend:
            send_password_reset(tenant_owner, is_invitation=True)

        assert backend.return_value.send.called, "an invitation should reach them"
        sent_to, message = backend.return_value.send.call_args[0]
        assert sent_to == "+254722000123"
        assert "reset-password" in message.body, "the link is the whole message"

    def test_it_costs_a_credit(self, tenant_owner, settings):
        from unittest import mock

        from notifications import credits

        settings.SMS_BACKEND = "notifications.channels.logging_sms.LoggingSmsBackend"
        credits.purchase(tenant_owner.organization, 5, note="For the test")
        tenant_owner.phone = "+254722000123"
        tenant_owner.save(update_fields=["phone"])

        with mock.patch("accounts.reset.get_sms_backend"):
            send_password_reset(tenant_owner, is_invitation=True)

        assert credits.balance(tenant_owner.organization) == 4

    def test_with_no_credit_somebody_who_has_an_email_gets_that_instead(
        self, tenant_owner, settings, mailoutbox
    ):
        """They have another way in, so the message can wait for a top-up."""
        from unittest import mock

        settings.SMS_BACKEND = "notifications.channels.logging_sms.LoggingSmsBackend"
        tenant_owner.phone = "+254722000123"
        tenant_owner.save(update_fields=["phone"])

        with mock.patch("accounts.reset.get_sms_backend") as backend:
            send_password_reset(tenant_owner, is_invitation=True)

        assert not backend.return_value.send.called, "no credit, no message"
        assert len(mailoutbox) == 1, "the email is how they still get in"


    def test_somebody_with_no_email_is_texted_even_at_zero(self, settings):
        """The exception, and the only one.

        B1 exists so a technician with no company email can be a user. If the
        balance is empty, refusing this SMS locks that person out of the system
        permanently over one shilling — so it overdraws instead, visibly, and an
        administrator settles it. Every message about *work* still stops at zero.
        """
        from unittest import mock

        from core.tenancy import tenant_context
        from notifications import credits

        settings.TENANT_BASE_DOMAIN = "yardflow.co.ke"
        settings.SMS_BACKEND = "notifications.channels.logging_sms.LoggingSmsBackend"
        result = provision_tenant(
            name="Silvertech",
            slug="silvertech",
            owner_email="owner@silvertech.co.ke",
            send_invitation=False,
        )
        organization = result["organization"]
        with tenant_context(organization):
            phone_only = User.objects.create_user(
                phone="+254722000555", organization=organization, full_name="No Email"
            )

        with mock.patch("accounts.reset.get_sms_backend") as backend:
            send_password_reset(phone_only, is_invitation=True)

        assert backend.return_value.send.called, "otherwise they can never sign in"
        assert credits.balance(organization) == -1, "visible, and settled by a top-up"
