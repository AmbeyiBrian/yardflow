"""WhatsApp delivery — written, and shipped disabled (design §9.2; Q1, T8.12).

§9.2's recommendation, adopted in full:

> The WhatsApp Business API requires an approved sender and pre-registered
> message templates, which takes weeks and carries per-message cost. **Ship v1
> with SMS, email and in-app; leave the WhatsApp adapter written but disabled
> behind the channel setting.** The approval loop is the system's core value and
> must not be blocked waiting on Meta's approval queue.

So this adapter exists and is testable against a mock, and
``notification_channels["whatsapp"]`` defaults to ``False``. Enabling it later is
a settings change — no code change, no deployment.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from django.conf import settings

from notifications.channels.base import DeliveryResult, RenderedMessage

logger = logging.getLogger(__name__)


class WhatsAppChannel:
    """Meta Cloud API adapter."""

    name = "whatsapp"
    api_root = "https://graph.facebook.com/v21.0"

    def send(self, recipient: str, message: RenderedMessage) -> DeliveryResult:
        token = getattr(settings, "WHATSAPP_ACCESS_TOKEN", "")
        phone_number_id = getattr(settings, "WHATSAPP_PHONE_NUMBER_ID", "")

        if not token or not phone_number_id:
            return DeliveryResult(
                succeeded=False,
                error=(
                    "WhatsApp is not configured. It needs an approved sender and "
                    "registered templates before it can be enabled (Q1)."
                ),
                retryable=False,
            )
        if not recipient:
            return DeliveryResult(
                succeeded=False,
                error="No phone number for this recipient.",
                retryable=False,
            )

        payload = self._build_payload(recipient, message)

        request = urllib.request.Request(
            f"{self.api_root}/{phone_number_id}/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            # A 4xx means a bad request or an unapproved template: retrying will
            # not help, and each attempt costs money.
            return DeliveryResult(succeeded=False, error=detail, retryable=exc.code >= 500)
        except Exception as exc:
            return DeliveryResult(succeeded=False, error=str(exc), retryable=True)

        messages = body.get("messages") or []
        reference = messages[0].get("id", "") if messages else ""
        return DeliveryResult(succeeded=True, provider_reference=reference)

    @staticmethod
    def _build_payload(recipient: str, message: RenderedMessage) -> dict:
        """Build the request body.

        Free-text is only permitted inside a 24-hour customer service window;
        outside it Meta requires a pre-registered template. An approval request is
        usually the first contact of the day, so a template is the normal path —
        which is exactly why §9.2 treats the template approval as the blocker.
        """
        payload: dict = {"messaging_product": "whatsapp", "to": recipient}

        if message.template_name:
            payload["type"] = "template"
            payload["template"] = {
                "name": message.template_name,
                "language": {"code": "en"},
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": str(value)}
                            for value in message.template_variables.values()
                        ],
                    }
                ],
            }
        else:
            payload["type"] = "text"
            payload["text"] = {"body": message.body}

        return payload
