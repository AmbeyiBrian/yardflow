"""SMS delivery via UjumbeSMS (design §9; L1, T8.11).

Written against UjumbeSMS's documented HTTP contract:

* ``POST {base}/api/messaging``
* headers ``X-Authorization`` (the API key), ``Email`` (the account's login
  email), ``Content-Type: application/json`` and ``Cache-Control: no-cache``
* body ``{"data": [{"message_bag": {"numbers", "message", "sender"}}]}``
* response ``{"status": {"code", "type", "description"}, "meta": {...}}``

The response shape is the part worth being careful about. **The status code is
nested inside ``status``**, not at the top level — an earlier version of this
adapter read a top-level ``status`` string, so every successful send was recorded
as *failed* and then retried, which would have sent every message twice or more.
A provider that answers "200 Sent" and is recorded as broken is worse than one
that fails outright, because the retry queue turns the mistake into cost.

Credentials come from the environment, never the repository (N-4). Locally the
logging adapter stands in (§12.0), so no message can reach a real technician from
a development machine.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any

from django.conf import settings

from notifications.channels.base import DeliveryResult, RenderedMessage

logger = logging.getLogger(__name__)

#: Where the API lives. A setting because UjumbeSMS publishes two hosts —
#: ``ujumbesms.co.ke`` and ``api.ujumbe.co.ke`` — and an account's own dashboard
#: is the authority on which one it was issued against. One env var, no release.
DEFAULT_BASE_URL = "https://ujumbesms.co.ke"

#: Kenyan MSISDN, as the gateway wants it: no plus, country code included.
_KE_COUNTRY_CODE = "254"

#: How UjumbeSMS says yes.
#:
#: Not by HTTP-style codes. A live balance query against a real account answers
#: ``{"status": {"code": "1008", "type": "success", "description": "Query
#: Success"}}`` — a 1xxx code that means *success*. An earlier version of this
#: adapter treated "2xx" as the test for success, which would have read that as a
#: failure. ``type`` is the field the provider classifies with, so ``type`` is
#: what this reads; the code goes in the log, where an unrecognised one is
#: evidence rather than a guess.
_SUCCESS_TYPES = frozenset({"success"})


def base_url() -> str:
    return (getattr(settings, "UJUMBE_SMS_BASE_URL", "") or DEFAULT_BASE_URL).rstrip("/")


def _status_of(payload: dict[str, Any]) -> tuple[str, str, str]:
    """``(code, type, description)`` from either envelope shape.

    The documented shape nests these under ``status``; wrapper libraries and
    older accounts put the code at the top level. Read both — a message that went
    out must not be recorded as failed because the envelope moved.
    """
    status = payload.get("status")
    if isinstance(status, dict):
        return (
            str(status.get("code", "")).strip(),
            str(status.get("type", "")).strip().lower(),
            str(status.get("description", "")).strip(),
        )
    return (
        str(payload.get("code", status or "")).strip(),
        str(payload.get("type", "")).strip().lower(),
        str(payload.get("description", "")).strip(),
    )


def _succeeded(code: str, type_: str) -> bool:
    """Did the provider accept it?

    ``type`` first, because that is the provider's own classification and it
    survives codes this adapter has never seen. The 2xx fallback is only for a
    response that carries no ``type`` at all.
    """
    if type_ in _SUCCESS_TYPES:
        return True
    if type_:  # an explicit "error" is an error, whatever the code says
        return False
    return code.startswith("2")


def normalise_msisdn(number: str) -> str:
    """Put a Kenyan number in the form the gateway accepts.

    Users are stored with whatever they typed (B1 lets a technician be
    identified by phone alone), so the same person may be ``+254722…``,
    ``0722…`` or ``722…`` depending on who created them. The gateway wants
    ``254722…``, and a number it cannot parse is a message nobody receives —
    with a delivery row that says "sent".
    """
    digits = re.sub(r"[^\d]", "", number or "")
    if not digits:
        return ""
    if digits.startswith(_KE_COUNTRY_CODE):
        return digits
    if digits.startswith("0"):
        return _KE_COUNTRY_CODE + digits[1:]
    # A bare subscriber number: 7xx or 1xx.
    if len(digits) == 9 and digits[0] in "71":
        return _KE_COUNTRY_CODE + digits
    # Anything else is left alone — a non-Kenyan number is the gateway's problem
    # to reject, and mangling it here would hide that.
    return digits


class UjumbeSmsBackend:
    """The production SMS channel (T8.11)."""

    name = "sms"

    @property
    def endpoint(self) -> str:
        return f"{base_url()}/api/messaging"

    @property
    def balance_endpoint(self) -> str:
        return f"{base_url()}/api/balance"

    # -- configuration ------------------------------------------------------

    @staticmethod
    def credentials() -> tuple[str, str, str]:
        """``(api_key, account_email, sender_id)`` from the environment."""
        return (
            getattr(settings, "UJUMBE_SMS_API_KEY", ""),
            getattr(settings, "UJUMBE_SMS_ACCOUNT_EMAIL", ""),
            getattr(settings, "UJUMBE_SMS_SENDER_ID", ""),
        )

    @classmethod
    def missing_configuration(cls) -> list[str]:
        """Which credentials are absent. Named individually on purpose.

        "SMS is not configured" sends somebody hunting through three settings;
        naming the missing one is the difference between a five-minute fix and an
        afternoon.
        """
        api_key, account_email, sender_id = cls.credentials()
        missing = []
        if not api_key:
            missing.append("UJUMBE_SMS_API_KEY")
        if not account_email:
            missing.append("UJUMBE_SMS_ACCOUNT_EMAIL")
        if not sender_id:
            missing.append("UJUMBE_SMS_SENDER_ID")
        return missing

    def _headers(self, api_key: str, account_email: str) -> dict[str, str]:
        return {
            # Documented header names. `X-Authorization` rather than
            # `Authorization`, which is unusual enough to be worth stating.
            "X-Authorization": api_key,
            "Email": account_email,
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
        }

    # -- sending ------------------------------------------------------------

    def send(self, recipient: str, message: RenderedMessage) -> DeliveryResult:
        api_key, account_email, sender_id = self.credentials()

        missing = self.missing_configuration()
        if missing:
            return DeliveryResult(
                succeeded=False,
                error=f"UjumbeSMS is not configured: {', '.join(missing)} not set.",
                # Not retryable: a missing API key will not appear on its own, and
                # retrying would bury the real problem in a retry queue.
                retryable=False,
            )

        number = normalise_msisdn(recipient)
        if not number:
            return DeliveryResult(
                succeeded=False,
                error="No usable phone number for this recipient.",
                retryable=False,
            )

        body = {
            "data": [
                {
                    "message_bag": {
                        "numbers": number,
                        "message": message.body,
                        "sender": sender_id,
                    }
                }
            ]
        }

        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers=self._headers(api_key, account_email),
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            # 5xx is worth another go; 4xx means the request or the credentials
            # are wrong and every retry costs a request for nothing.
            return DeliveryResult(
                succeeded=False, error=detail, retryable=exc.code >= 500
            )
        except json.JSONDecodeError as exc:
            # A gateway that answered with something other than JSON — a captive
            # portal, an HTML error page. Worth retrying, and worth saying so
            # rather than reporting a parse error nobody can act on.
            return DeliveryResult(
                succeeded=False,
                error=f"UjumbeSMS returned a response that was not JSON: {exc}",
                retryable=True,
            )
        except Exception as exc:
            return DeliveryResult(succeeded=False, error=str(exc), retryable=True)

        return self._interpret(payload)

    @staticmethod
    def _interpret(payload: dict[str, Any]) -> DeliveryResult:
        """Read the response shape.

        ``{"status": {"code", "type", "description"}, "meta": {...}}``

        Success is ``status.type``, not the code — see ``_SUCCESS_TYPES``. A
        refusal is never retried: it is no credits, an unapproved sender ID or a
        number the gateway will not take, and none of those are fixed by asking
        again. That also holds for a response this adapter cannot classify, and
        deliberately so — a message that may already have gone out must not be
        sent a second time on the strength of an envelope we did not recognise.
        The delivery row keeps the raw code for whoever looks.
        """
        code, type_, description = _status_of(payload)
        meta = payload.get("meta") or {}

        if _succeeded(code, type_):
            # L1 asks that delivery status be visible to admins, and the
            # commonest silent failure is running out of credits — so whatever
            # the provider says about the balance goes in the log while it is
            # still cheap. The key differs between endpoints.
            credits_left = meta.get("available_credits", meta.get("credits"))
            logger.info(
                "sms sent via ujumbe (%s %s): %s recipient(s), %s credits left",
                code or "no code",
                type_ or "no type",
                meta.get("recipients", 1),
                credits_left if credits_left is not None else "unreported",
            )
            date_time = meta.get("date_time")
            return DeliveryResult(
                succeeded=True,
                provider_reference=(
                    str(date_time.get("date", "")) if isinstance(date_time, dict) else ""
                ),
            )

        if not type_ and not code.startswith(("4", "5")):
            # Neither a classification nor a code we can read. Say so loudly:
            # this is the shape of contract drift, and it is cheaper to notice
            # here than in a month of delivery rows that all say "failed".
            logger.warning(
                "ujumbe returned an envelope this adapter cannot classify: %s",
                json.dumps(payload)[:400],
            )

        return DeliveryResult(
            succeeded=False,
            error=f"{code}: {description}".strip(": ") or json.dumps(payload)[:400],
            retryable=False,
        )

    # -- diagnostics --------------------------------------------------------

    def balance(self) -> dict[str, Any]:
        """Remaining credits, for checking that credentials actually work.

        Used by ``manage.py sms_selftest``. Worth having: the moment a customer
        hands over an API key, the question is "does this work?", and answering it
        by sending a real SMS to somebody is a poor first move.
        """
        api_key, account_email, _sender = self.credentials()
        missing = self.missing_configuration()
        if missing:
            return {"ok": False, "error": f"Not configured: {', '.join(missing)}."}

        request = urllib.request.Request(
            self.balance_endpoint,
            data=b"",
            headers=self._headers(api_key, account_email),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return {
                "ok": False,
                "error": f"HTTP {exc.code}: "
                f"{exc.read().decode('utf-8', errors='replace')[:200]}",
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        code, type_, description = _status_of(payload)
        meta = payload.get("meta") or {}
        return {
            "ok": _succeeded(code, type_),
            "code": code,
            "description": description,
            # Named plainly, because the caller wants one number: what is left.
            "credits": meta.get("credits", meta.get("available_credits")),
            "meta": meta,
        }
