"""T1.13 — password reset (§6, requirement B2)."""

import re
from datetime import timedelta

import pytest
from django.core import mail
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time

from accounts.factories import UserFactory
from accounts.reset import encode_uid, make_reset_token, send_password_reset
from core.factories import OrganizationFactory


@pytest.fixture
def api(client, settings):
    settings.TENANT_BASE_DOMAIN = "localhost"
    client.defaults["HTTP_HOST"] = "silvertech.localhost"
    return client


@pytest.fixture
def silvertech(db):
    return OrganizationFactory(name="Silvertech", slug="silvertech")


@pytest.fixture
def user(silvertech):
    person = UserFactory(organization=silvertech, email="store@silvertech.co.ke")
    person.set_password("original password")
    person.save()
    return person


def request_reset(api, identifier):
    return api.post(
        reverse("v1:auth:password-reset"),
        {"identifier": identifier},
        content_type="application/json",
    )


def confirm_reset(api, uid, token, password):
    return api.post(
        reverse("v1:auth:password-reset-confirm"),
        {"uid": uid, "token": token, "password": password},
        content_type="application/json",
    )


class TestRequestingAReset:
    def test_an_email_is_sent_with_a_link(self, api, user):
        response = request_reset(api, "store@silvertech.co.ke")

        assert response.status_code == 200
        assert len(mail.outbox) == 1
        assert "reset-password?uid=" in mail.outbox[0].body

    def test_the_link_addresses_the_tenants_own_subdomain(self, api, user):
        """§2.2: a link on the bare domain would resolve no organization."""
        request_reset(api, "store@silvertech.co.ke")

        assert "silvertech" in mail.outbox[0].body

    def test_an_unknown_identifier_reports_the_same_thing(self, api, silvertech):
        """Otherwise the endpoint discovers who works at a company."""
        known = request_reset(api, "store@silvertech.co.ke")
        unknown = request_reset(api, "nobody@silvertech.co.ke")

        assert known.status_code == unknown.status_code == 200
        assert known.json() == unknown.json()

    def test_no_email_is_sent_for_an_unknown_identifier(self, api, silvertech):
        request_reset(api, "nobody@silvertech.co.ke")

        assert mail.outbox == []

    def test_a_deactivated_user_gets_no_reset_link(self, api, user):
        """B3: deactivation must not be recoverable by the user themselves."""
        user.is_active = False
        user.save()

        request_reset(api, "store@silvertech.co.ke")

        assert mail.outbox == []

    def test_a_user_of_another_tenant_gets_nothing_through_this_subdomain(self, api, silvertech):
        rival = OrganizationFactory(name="Rival", slug="rival")
        UserFactory(organization=rival, email="them@rival.com")

        request_reset(api, "them@rival.com")

        assert mail.outbox == []

    def test_sms_delivery_for_a_user_with_only_a_phone_number(self, api, silvertech, capsys):
        """B2 allows SMS, which is the only channel that reaches a technician."""
        technician = UserFactory(organization=silvertech, email=None, phone="0722123456")
        technician.set_password("x")
        technician.save()

        request_reset(api, "0722123456")

        assert mail.outbox == []
        # The canonical form: what was typed was 0722…, what is stored and
        # dialled is +254722….
        assert "SMS to +254722123456" in capsys.readouterr().out


class TestConfirmingAReset:
    def test_a_valid_token_sets_the_new_password(self, api, user):
        token = make_reset_token(user)

        response = confirm_reset(api, encode_uid(user), token, "a much better password")

        assert response.status_code == 204
        user.refresh_from_db()
        assert user.check_password("a much better password")

    def test_the_new_password_works_for_login(self, api, user):
        token = make_reset_token(user)
        confirm_reset(api, encode_uid(user), token, "a much better password")

        login = api.post(
            reverse("v1:auth:login"),
            {"identifier": "store@silvertech.co.ke", "password": "a much better password"},
            content_type="application/json",
        )

        assert login.status_code == 200

    def test_a_token_is_single_use(self, api, user):
        """T1.13's stated criterion.

        Derived from the password hash, so setting a password invalidates every
        outstanding token — a leaked link cannot be replayed later.
        """
        token = make_reset_token(user)
        uid = encode_uid(user)

        assert confirm_reset(api, uid, token, "first new password").status_code == 204
        assert confirm_reset(api, uid, token, "second new password").status_code == 400

        user.refresh_from_db()
        assert user.check_password("first new password")

    def test_a_tampered_token_is_rejected(self, api, user):
        response = confirm_reset(api, encode_uid(user), "not-a-real-token", "x1234567!")
        assert response.status_code == 400

    def test_a_tampered_uid_is_rejected(self, api, user):
        response = confirm_reset(api, "bm90LWEtdWlk", make_reset_token(user), "x1234567!")
        assert response.status_code == 400

    @override_settings(PASSWORD_RESET_TIMEOUT=3600)
    def test_an_expired_token_is_rejected(self, api, user):
        """A reset link must not stay valid indefinitely.

        Time has to actually move for this to mean anything — a zero timeout
        still accepts a token minted in the same second.
        """
        # Mint the token two hours in the past, then present it now.
        with freeze_time(timezone.now() - timedelta(hours=2)):
            token = make_reset_token(user)
        uid = encode_uid(user)

        response = confirm_reset(api, uid, token, "a much better password")

        assert response.status_code == 400

    def test_a_weak_password_is_rejected_with_field_errors(self, api, user):
        """§6.1: the message must attach to the password field on the form."""
        token = make_reset_token(user)

        response = confirm_reset(api, encode_uid(user), token, "1234")

        assert response.status_code == 400
        assert "password" in response.json()["error"]["field_errors"]


class TestInvitation:
    """A1: the owner receives an invitation to set their password."""

    def test_an_invitation_sends_a_link_and_no_temporary_password(self, silvertech):
        invited = UserFactory(organization=silvertech, email="owner@silvertech.co.ke")

        send_password_reset(invited, is_invitation=True)

        assert len(mail.outbox) == 1
        body = mail.outbox[0].body
        assert "Set your password" in body
        assert "An account has been created for you at Silvertech" in body

    def test_an_invited_user_can_set_their_first_password(self, api, silvertech):
        invited = UserFactory(organization=silvertech, email="owner@silvertech.co.ke")
        assert invited.has_usable_password() is False

        send_password_reset(invited, is_invitation=True)
        link = re.search(r"uid=([\w-]+)&token=([\w-]+)", mail.outbox[0].body)
        assert link is not None

        response = confirm_reset(api, link.group(1), link.group(2), "my first password")

        assert response.status_code == 204
        invited.refresh_from_db()
        assert invited.check_password("my first password")
