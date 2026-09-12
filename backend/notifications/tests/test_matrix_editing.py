"""A tenant may decide who hears what (L2, C8).

The first build shipped the matrix read-only, on the grounds that "who is told
what is a control, not a preference". That reasoning conflated two questions the
system already answers separately: *who may change it* is a permission, and
`settings.manage` is held only by Owner and Admin; *whether the change is visible
afterwards* is the audit trail. Locking the decision away from the people
accountable for it was not a safeguard — it was the editing surface not existing.

So: editable behind the permission, audited, and validated, because a typo here
is otherwise silent — an unknown key never matches anything, and the tenant
believes they configured something they did not.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from accounts.models import Role, User, UserRole
from core.provisioning import provision_tenant
from core.tenancy import tenant_context
from notifications.matrix import Channel, Event, channels_for, is_enabled

pytestmark = pytest.mark.django_db


@pytest.fixture
def signed_in(db, client, settings):
    settings.TENANT_BASE_DOMAIN = "localhost"
    result = provision_tenant(
        name="Silvertech", slug="silvertech", owner_email="owner@silvertech.co.ke"
    )
    owner = result["owner"]
    owner.set_password("a good long password")
    owner.save()

    client.defaults["HTTP_HOST"] = "silvertech.localhost"
    token = client.post(
        reverse("v1:auth:login"),
        {"identifier": "owner@silvertech.co.ke", "password": "a good long password"},
        content_type="application/json",
    ).json()["access"]
    return client, token, result["organization"]


@pytest.fixture
def storekeeper_token(signed_in):
    """Somebody without `settings.manage`, to prove the gate is real."""
    http, _owner_token, organization = signed_in
    with tenant_context(organization):
        store = User.objects.create_user(
            email="store@silvertech.co.ke",
            password="a good long password",
            organization=organization,
        )
        UserRole.objects.create(
            organization=organization,
            user=store,
            role=Role.objects.get(name="Storekeeper"),
        )
    return http.post(
        reverse("v1:auth:login"),
        {"identifier": "store@silvertech.co.ke", "password": "a good long password"},
        content_type="application/json",
    ).json()["access"]


def auth(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def patch_settings(http, token, payload):
    return http.patch(
        reverse("v1:organization-settings"),
        payload,
        content_type="application/json",
        **auth(token),
    )


class TestWhoMayChangeIt:
    def test_an_owner_may_turn_an_event_off(self, signed_in):
        http, token, organization = signed_in

        response = patch_settings(
            http,
            token,
            {
                "notification_matrix": {
                    Event.STOCK_BELOW_MINIMUM: {
                        "recipients": ["storekeepers"],
                        "channels": ["in_app"],
                        "enabled": False,
                    }
                }
            },
        )

        assert response.status_code == 200, response.content
        organization.settings.refresh_from_db()
        assert is_enabled(organization, Event.STOCK_BELOW_MINIMUM) is False

    def test_a_storekeeper_may_not(self, signed_in, storekeeper_token):
        """`settings.manage` is described as close to owner-level, and this sits
        behind it — the same gate as self-approval and the expiry window."""
        http, _owner_token, _organization = signed_in

        response = patch_settings(
            http, storekeeper_token, {"notification_channels": {"sms": False}}
        )

        assert response.status_code == 403

    def test_anyone_may_read_what_they_will_be_told(self, signed_in, storekeeper_token):
        """A notification nobody expected reads as spam, so seeing it stays open."""
        http, _owner_token, _organization = signed_in

        response = http.get(
            reverse("v1:notification-preferences"), **auth(storekeeper_token)
        )

        assert response.status_code == 200
        assert response.json()["events"]


class TestTheChangeIsRecorded:
    def test_turning_something_off_is_audited(self, signed_in):
        """A variance nobody heard about should have a traceable reason."""
        http, token, organization = signed_in

        patch_settings(http, token, {"notification_channels": {"sms": False}})

        from core.models import AuditLog

        with tenant_context(organization):
            assert AuditLog.objects.filter(
                target_label="Organization settings"
            ).exists()


class TestWhatCannotBeSaved:
    def test_an_unknown_channel_is_refused(self, signed_in):
        http, token, _organization = signed_in

        response = patch_settings(
            http, token, {"notification_channels": {"telegram": True}}
        )

        assert response.status_code == 400
        assert "telegram" in response.content.decode()

    def test_an_unknown_event_is_refused(self, signed_in):
        http, token, _organization = signed_in

        response = patch_settings(
            http, token, {"notification_matrix": {"gate_out.teleported": {"enabled": False}}}
        )

        assert response.status_code == 400

    def test_an_unknown_recipient_is_refused(self, signed_in):
        """A role that does not resolve produces an event with nobody to tell,
        which looks exactly like a message that was never sent."""
        http, token, _organization = signed_in

        response = patch_settings(
            http,
            token,
            {
                "notification_matrix": {
                    Event.GATE_OUT_APPROVED: {"recipients": ["the_night_watchman"]}
                }
            },
        )

        assert response.status_code == 400

    def test_a_switch_must_be_a_boolean(self, signed_in):
        http, token, _organization = signed_in

        response = patch_settings(
            http, token, {"notification_channels": {"sms": "yes please"}}
        )

        assert response.status_code == 400


class TestTheTenantWideSwitchStillWins:
    def test_a_channel_switched_off_beats_the_matrix(self, signed_in):
        """L1 is about what staff actually read: a company with no SMS budget must
        not have SMS reintroduced by a per-event setting."""
        http, token, organization = signed_in

        patch_settings(
            http,
            token,
            {
                "notification_channels": {"sms": False, "in_app": True},
                "notification_matrix": {
                    Event.ITEM_OVERDUE: {
                        "recipients": ["holder"],
                        "channels": [Channel.SMS, Channel.IN_APP],
                        "enabled": True,
                    }
                },
            },
        )

        organization.settings.refresh_from_db()
        resolved = channels_for(organization, Event.ITEM_OVERDUE)

        assert Channel.SMS not in resolved
        assert Channel.IN_APP in resolved


class TestWarningTheOwner:
    def test_events_that_carry_a_control_say_so(self, signed_in):
        """So the screen can state what silence costs before somebody chooses it.
        A warning, never a block: the work stays visible in the app either way."""
        http, token, _organization = signed_in

        response = http.get(reverse("v1:notification-preferences"), **auth(token))

        by_event = {row["event"]: row for row in response.json()["events"]}
        assert by_event[Event.GATE_OUT_AWAITING_APPROVAL]["carries_a_control"] is True
        assert by_event[Event.ITEM_OVERDUE]["carries_a_control"] is True
        # Informational: muting it hides no work.
        assert by_event[Event.GATE_OUT_APPROVED]["carries_a_control"] is False


class TestWhatANonAdministratorMaySee:
    """Reading is open; the company's money is not (L4).

    Everybody may see *what they will be told* — a notification nobody expected
    reads as spam, so the matrix is readable. But the screen also carries the SMS
    credit balance, which is the company's commercial position, and a storekeeper
    reading how many credits are left is a leak rather than a feature. That
    distinction was missed when the balance was added to an endpoint that was
    already open, which is how it usually happens.
    """

    def test_a_storekeeper_does_not_see_the_credit_balance(
        self, signed_in, storekeeper_token
    ):
        http, _owner_token, _organization = signed_in

        response = http.get(
            reverse("v1:notification-preferences"), **auth(storekeeper_token)
        )

        assert response.status_code == 200
        body = response.json()
        assert "sms" not in body, "the balance is withheld, not merely hidden"
        # They can still see what they will be told, which is the point of the
        # endpoint being open at all.
        assert body["events"]

    def test_an_owner_does(self, signed_in):
        http, token, _organization = signed_in

        response = http.get(reverse("v1:notification-preferences"), **auth(token))

        sms = response.json()["sms"]
        assert "credit_balance" in sms
        assert sms["credit_price_kes"] == 1
