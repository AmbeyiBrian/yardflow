"""The channel protocol (design §9).

One protocol for email, SMS, WhatsApp and in-app, so that:

* the notification matrix (L2) can select channels per tenant per event without
  knowing what any of them are
* WhatsApp can ship written but disabled behind a setting (Q1, §9.2)
* local development substitutes a logging adapter for a real provider with no
  code change (§12.0)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from django.conf import settings
from django.utils.module_loading import import_string


@dataclass
class RenderedMessage:
    """A message already rendered for its channel.

    Rendering happens before dispatch so a channel never has to know about
    templates, and a failed render cannot be mistaken for a failed send.
    """

    subject: str = ""
    body: str = ""
    #: WhatsApp requires pre-registered template names rather than free text
    #: (§9.2), so the template and its variables travel with the message.
    template_name: str = ""
    template_variables: dict = field(default_factory=dict)


@dataclass
class DeliveryResult:
    """The outcome of one send attempt.

    L3: "failure of a notification never blocks the underlying transaction."
    So a failure is a returned value to be recorded and retried, not an
    exception that unwinds the caller's work.
    """

    succeeded: bool
    provider_reference: str = ""
    error: str = ""
    #: True when retrying could plausibly succeed — a timeout rather than an
    #: invalid number.
    retryable: bool = True


class NotificationChannel(Protocol):
    """§9: ``send(recipient, message) -> DeliveryResult``."""

    name: str

    def send(self, recipient: str, message: RenderedMessage) -> DeliveryResult:
        ...


def get_sms_backend() -> NotificationChannel:
    """Load the configured SMS backend.

    Local development and tests use a logging adapter that prints the message
    (§12.0); production uses Ujumbe (T8.11). Selected by the ``SMS_BACKEND``
    setting, so no caller knows the difference.
    """
    path = getattr(
        settings, "SMS_BACKEND", "notifications.channels.logging_sms.LoggingSmsBackend"
    )
    return import_string(path)()
