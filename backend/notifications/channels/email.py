"""Email delivery (design §9).

Console in development, SES over SMTP in production (§12.0, §12.1) — the switch
is a settings change, not a code change.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import send_mail

from notifications.channels.base import DeliveryResult, RenderedMessage

logger = logging.getLogger(__name__)


class EmailChannel:
    name = "email"

    def send(self, recipient: str, message: RenderedMessage) -> DeliveryResult:
        if not recipient:
            return DeliveryResult(
                succeeded=False,
                error="No email address for this recipient.",
                retryable=False,
            )

        try:
            send_mail(
                subject=message.subject or "YardFlow",
                message=message.body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[recipient],
                fail_silently=False,
            )
        except Exception as exc:
            logger.warning("email delivery failed: %s", exc)
            return DeliveryResult(succeeded=False, error=str(exc), retryable=True)

        return DeliveryResult(succeeded=True, provider_reference="smtp")
