"""The UjumbeSMS adapter, against its documented contract (§9; L1, T8.11).

The response shape is the point of this file. UjumbeSMS answers

    {"status": {"code", "type", "description"}, "meta": {...}}

— the code is **nested**. An earlier version of this adapter read a top-level
``status`` string, so every successful send was recorded as *failed* and then
retried: the message went out two or three times while the delivery row said it
never arrived. That is worse than an outright failure, because the retry queue
turns the mistake into cost and the record into a lie.

T8.11's criterion is "unit-tested against a mocked Ujumbe response, delivery
status is recorded, and switching provider is a settings change". All three are
here; the delivery-status half is in `test_notifications.py`, where the delivery
rows live.
"""

from __future__ import annotations

import io
import json
import urllib.error
from unittest import mock

import pytest

from notifications.channels.base import RenderedMessage
from notifications.channels.ujumbe_sms import UjumbeSmsBackend, normalise_msisdn


def ujumbe_response(payload: dict):
    """A stand-in for `urlopen`'s context manager."""
    response = mock.MagicMock()
    response.read.return_value = json.dumps(payload).encode()
    response.__enter__ = lambda self: self
    response.__exit__ = lambda *args: None
    return response


SENT = {"status": {"code": "200", "type": "success", "description": "Sent"}}


@pytest.fixture
def configured(settings):
    """All three credentials, as a real deployment would have them."""
    settings.UJUMBE_SMS_API_KEY = "key"
    settings.UJUMBE_SMS_ACCOUNT_EMAIL = "ops@silvertech.co.ke"
    settings.UJUMBE_SMS_SENDER_ID = "SILVERTECH"
    return settings


class TestConfiguration:
    def test_it_names_the_credential_that_is_missing(self, configured):
        """"SMS is not configured" sends somebody hunting through three settings.

        UjumbeSMS authenticates on the API key *and* the account email, and a 401
        says nothing about which of the two was wrong — so the adapter checks
        before it asks.
        """
        configured.UJUMBE_SMS_ACCOUNT_EMAIL = ""

        result = UjumbeSmsBackend().send("0722000001", RenderedMessage(body="hi"))

        assert result.succeeded is False
        assert "UJUMBE_SMS_ACCOUNT_EMAIL" in result.error
        # A missing setting will not appear on its own, and retrying would bury
        # the real problem in a retry queue.
        assert result.retryable is False

    def test_the_host_is_a_setting(self, configured):
        """T8.11: switching provider — or host — is a settings change.

        UjumbeSMS publishes two hosts and an account is issued against one of
        them, so this cannot be a constant in the code.
        """
        configured.UJUMBE_SMS_BASE_URL = "https://api.ujumbe.co.ke"

        assert UjumbeSmsBackend().endpoint == "https://api.ujumbe.co.ke/api/messaging"


class TestTheRequest:
    def test_the_documented_request_is_what_gets_sent(self, configured):
        """Headers and body, against the published contract.

        ``X-Authorization`` rather than ``Authorization``, and the account email
        in a header of its own. Unusual enough that getting it wrong is likely,
        and the failure is a bare 401.
        """
        with mock.patch(
            "urllib.request.urlopen", return_value=ujumbe_response(SENT)
        ) as urlopen:
            UjumbeSmsBackend().send("0722000001", RenderedMessage(body="hello yard"))

        request = urlopen.call_args[0][0]
        assert request.full_url.endswith("/api/messaging")
        # urllib title-cases header keys on the way in.
        assert request.get_header("X-authorization") == "key"
        assert request.get_header("Email") == "ops@silvertech.co.ke"
        assert request.get_header("Cache-control") == "no-cache"

        body = json.loads(request.data.decode())
        bag = body["data"][0]["message_bag"]
        assert bag["message"] == "hello yard"
        assert bag["sender"] == "SILVERTECH"
        # Normalised to what the gateway accepts, not what somebody typed.
        assert bag["numbers"] == "254722000001"

    @pytest.mark.parametrize(
        ("typed", "expected"),
        [
            ("+254722000001", "254722000001"),
            ("0722000001", "254722000001"),
            ("722000001", "254722000001"),
            ("254722000001", "254722000001"),
            ("+254 722 000 001", "254722000001"),
            ("", ""),
        ],
    )
    def test_numbers_are_normalised(self, typed, expected):
        """B1 lets a technician be identified by phone alone, so the same person
        may be stored as +254…, 0… or 7… depending on who created them. A number
        the gateway cannot parse is a message nobody receives — with a delivery
        row that says "sent"."""
        assert normalise_msisdn(typed) == expected

    def test_a_number_with_nothing_usable_is_refused_before_the_call(self, configured):
        with mock.patch("urllib.request.urlopen") as urlopen:
            result = UjumbeSmsBackend().send("n/a", RenderedMessage(body="hi"))

        assert result.succeeded is False
        assert urlopen.call_count == 0, "no point spending a request on it"


class TestTheResponse:
    def test_a_successful_send_is_recorded_as_sent(self, configured):
        """The bug this whole file exists for: `status.code` is nested."""
        response = ujumbe_response(
            {
                "status": {"code": "200", "type": "success", "description": "Sent"},
                "meta": {
                    "recipients": 1,
                    "credits_deducted": 1,
                    "available_credits": "412",
                    "user_email": "ops@silvertech.co.ke",
                    "date_time": {"date": "2026-08-22 14:31:00.000000"},
                },
            }
        )

        with mock.patch("urllib.request.urlopen", return_value=response):
            result = UjumbeSmsBackend().send("0722000001", RenderedMessage(body="hi"))

        assert result.succeeded is True, result.error

    def test_a_1xxx_success_code_is_a_success(self, configured):
        """The live account's own answer, and the reason this adapter changed.

        UjumbeSMS does not use HTTP-style codes in the envelope: a real balance
        query returns ``{"code": "1008", "type": "success"}``. Judging success by
        "starts with 2" read that as a failure — which for a *send* means the
        delivery row says failed for a message that went out.
        """
        response = ujumbe_response(
            {
                "status": {
                    "code": "1008",
                    "type": "success",
                    "description": "Query Success",
                },
                "meta": {"credits": 5005, "rate": 0.5},
            }
        )

        with mock.patch("urllib.request.urlopen", return_value=response):
            result = UjumbeSmsBackend().send("0722000001", RenderedMessage(body="hi"))

        assert result.succeeded is True, result.error

    def test_an_error_type_beats_a_success_looking_code(self, configured):
        """`type` is the provider's classification, so it wins outright."""
        response = ujumbe_response(
            {"status": {"code": "200", "type": "error", "description": "Blacklisted"}}
        )

        with mock.patch("urllib.request.urlopen", return_value=response):
            result = UjumbeSmsBackend().send("0722000001", RenderedMessage(body="hi"))

        assert result.succeeded is False
        assert "Blacklisted" in result.error

    def test_an_envelope_we_cannot_classify_is_not_retried(self, configured, caplog):
        """The one case where "failed" might be wrong.

        No `type`, no readable code — so the message may or may not have gone
        out. Retrying would risk sending it twice, which is worse than a delivery
        row somebody has to look at, so it fails once and says so in the log.
        """
        with mock.patch(
            "urllib.request.urlopen",
            return_value=ujumbe_response({"status": {"code": "1093"}}),
        ):
            result = UjumbeSmsBackend().send("0722000001", RenderedMessage(body="hi"))

        assert result.succeeded is False
        assert result.retryable is False, "never send a message twice on a guess"
        assert "cannot classify" in caplog.text

    def test_a_gateway_refusal_is_not_retried(self, configured):
        """No credits, or an unapproved sender ID. Retrying fixes neither, and
        each attempt is a request that costs something."""
        response = ujumbe_response(
            {
                "status": {
                    "code": "1002",
                    "type": "error",
                    "description": "Insufficient credits",
                }
            }
        )

        with mock.patch("urllib.request.urlopen", return_value=response):
            result = UjumbeSmsBackend().send("0722000001", RenderedMessage(body="hi"))

        assert result.succeeded is False
        assert "Insufficient credits" in result.error
        assert result.retryable is False

    def test_a_flat_response_still_reads(self, configured):
        """Wrapper libraries and older accounts return the code at the top level.

        Read it rather than reject it: a message that went out must not be
        recorded as failed because the envelope moved.
        """
        with mock.patch(
            "urllib.request.urlopen", return_value=ujumbe_response({"code": "200"})
        ):
            result = UjumbeSmsBackend().send("0722000001", RenderedMessage(body="hi"))

        assert result.succeeded is True

    @pytest.mark.parametrize(("status", "retryable"), [(503, True), (400, False)])
    def test_server_errors_retry_and_client_errors_do_not(
        self, configured, status, retryable
    ):
        """L3: a blip deserves another go; a malformed request never will."""
        failure = urllib.error.HTTPError(
            url="https://ujumbesms.co.ke/api/messaging",
            code=status,
            msg="nope",
            hdrs=None,
            fp=io.BytesIO(b"detail"),
        )

        with mock.patch("urllib.request.urlopen", side_effect=failure):
            result = UjumbeSmsBackend().send("0722000001", RenderedMessage(body="hi"))

        assert result.succeeded is False
        assert result.retryable is retryable

    def test_html_instead_of_json_is_a_retry_not_a_crash(self, configured):
        """A captive portal or a proxy error page — common on a site connection."""
        response = mock.MagicMock()
        response.read.return_value = b"<html>gateway timeout</html>"
        response.__enter__ = lambda self: self
        response.__exit__ = lambda *args: None

        with mock.patch("urllib.request.urlopen", return_value=response):
            result = UjumbeSmsBackend().send("0722000001", RenderedMessage(body="hi"))

        assert result.succeeded is False
        assert result.retryable is True
        assert "not JSON" in result.error

    def test_a_timeout_is_retryable(self, configured):
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("slow")):
            result = UjumbeSmsBackend().send("0722000001", RenderedMessage(body="hi"))

        assert result.succeeded is False
        assert result.retryable is True


class TestTheBalanceCheck:
    """What `manage.py sms_selftest` uses.

    The moment a customer hands over an API key the question is "does this
    work?", and answering it by texting a real technician is a poor first move.
    """

    def test_it_reports_credits_when_the_credentials_work(self, configured):
        """The real response, copied from a live account (code 1008, key `credits`)."""
        response = ujumbe_response(
            {
                "status": {
                    "code": "1008",
                    "type": "success",
                    "description": "Query Success",
                },
                "meta": {
                    "user": "ops@silvertech.co.ke",
                    "credits": 5005,
                    "rate": 0.5,
                    "date_time": {"date": "2026-08-22 16:09:52.221681"},
                },
            }
        )

        with mock.patch("urllib.request.urlopen", return_value=response):
            result = UjumbeSmsBackend().balance()

        assert result["ok"] is True
        assert result["credits"] == 5005

    def test_it_reports_the_refusal_when_they_do_not(self, configured):
        failure = urllib.error.HTTPError(
            url="https://ujumbesms.co.ke/api/balance",
            code=401,
            msg="unauthorised",
            hdrs=None,
            fp=io.BytesIO(b"bad key"),
        )

        with mock.patch("urllib.request.urlopen", side_effect=failure):
            result = UjumbeSmsBackend().balance()

        assert result["ok"] is False
        assert "401" in result["error"]

    def test_it_says_what_is_missing_before_asking(self, configured):
        configured.UJUMBE_SMS_API_KEY = ""

        with mock.patch("urllib.request.urlopen") as urlopen:
            result = UjumbeSmsBackend().balance()

        assert result["ok"] is False
        assert "UJUMBE_SMS_API_KEY" in result["error"]
        assert urlopen.call_count == 0
