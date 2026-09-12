"""SMS adapter that logs instead of sending (design §12.0, T1.24).

The local stand-in for Ujumbe. Prints the message so a developer can read the
reset link or approval notification straight from the console — no provider
account, no credentials, no cost, and no risk of sending a real message to a
real technician from a development machine.
"""

from __future__ import annotations

import logging

from notifications.channels.base import DeliveryResult, RenderedMessage

logger = logging.getLogger("notifications.sms")


class LoggingSmsBackend:
    """Records the message and reports success."""

    name = "sms"

    def send(self, recipient: str, message: RenderedMessage) -> DeliveryResult:
        logger.info(
            "SMS (not sent — logging backend) to %s: %s", recipient, message.body
        )
        # Deliberately prints as well as logs, so it is visible when running the
        # dev server without log configuration.
        print(f"\n--- SMS to {recipient} ---\n{message.body}\n---\n")
        return DeliveryResult(succeeded=True, provider_reference="logged")
