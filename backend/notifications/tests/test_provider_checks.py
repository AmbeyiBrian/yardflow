"""The startup check on the SMS provider (§9.3; L1).

This exists because of a real hour lost. The credentials were in `.env`, the
account was funded, and nothing sent — because the lines were written
``KEY = value`` and django-environ only matches ``KEY=value``. Every screen
worked; the only symptom would have been a technician who was never told.
"""

from __future__ import annotations

import pytest

from notifications.checks import check_sms_provider_is_configured

UJUMBE = "notifications.channels.ujumbe_sms.UjumbeSmsBackend"
LOGGING = "notifications.channels.logging_sms.LoggingSmsBackend"


@pytest.fixture
def configured(settings):
    settings.SMS_BACKEND = UJUMBE
    settings.UJUMBE_SMS_API_KEY = "key"
    settings.UJUMBE_SMS_ACCOUNT_EMAIL = "ops@silvertech.co.ke"
    settings.UJUMBE_SMS_SENDER_ID = "SILVERTECH"
    return settings


def test_a_fully_configured_provider_says_nothing(configured):
    assert check_sms_provider_is_configured(None) == []


def test_the_development_default_is_not_nagged_about(configured):
    """The logging adapter is *meant* to send nothing, so it is not a finding.

    A check that warned here would warn on every developer's machine, every
    command, and be filtered out by everyone before it ever caught anything.
    """
    configured.SMS_BACKEND = LOGGING

    assert check_sms_provider_is_configured(None) == []


def test_a_missing_credential_is_reported_by_name(configured):
    """The failure this file exists for."""
    configured.UJUMBE_SMS_API_KEY = ""

    findings = check_sms_provider_is_configured(None)

    assert len(findings) == 1
    assert findings[0].id == "notifications.W001"
    assert "UJUMBE_SMS_API_KEY" in findings[0].msg
    # The hint has to name the actual trap, not just the setting.
    assert "space before the equals" in findings[0].hint


def test_all_three_missing_are_listed_together(configured):
    """One pass, not three rounds of edit-and-rerun."""
    configured.UJUMBE_SMS_API_KEY = ""
    configured.UJUMBE_SMS_ACCOUNT_EMAIL = ""
    configured.UJUMBE_SMS_SENDER_ID = ""

    message = check_sms_provider_is_configured(None)[0].msg

    assert "UJUMBE_SMS_API_KEY" in message
    assert "UJUMBE_SMS_ACCOUNT_EMAIL" in message
    assert "UJUMBE_SMS_SENDER_ID" in message
    assert "are empty" in message, "plural, because three of them are"


def test_a_mistyped_adapter_path_is_caught_at_startup(configured):
    """`ujumbe_sws` for `ujumbe_sms` — a typo that otherwise waits for a send."""
    configured.SMS_BACKEND = "notifications.channels.ujumbe_sws.UjumbeSmsBackend"

    findings = check_sms_provider_is_configured(None)

    assert len(findings) == 1
    assert findings[0].id == "notifications.W002"
    assert "cannot be imported" in findings[0].msg
