"""Delivery channels (design §9).

Provider-agnostic by design (D19): every channel implements the same protocol, so
switching SMS provider or enabling WhatsApp is a settings change rather than a
code change (§9.2).
"""

from notifications.channels.base import (
    DeliveryResult,
    NotificationChannel,
    RenderedMessage,
    get_sms_backend,
)

__all__ = [
    "DeliveryResult",
    "NotificationChannel",
    "RenderedMessage",
    "get_channel",
    "get_sms_backend",
]


def get_channel(name: str):
    """Return the adapter for a channel, or ``None`` if it is unavailable.

    ``None`` covers two cases that are both correct outcomes rather than errors:
    a channel with no adapter, and a channel deliberately disabled — as WhatsApp
    is until Meta approves the sender (Q1, §9.2). The delivery is then recorded
    as skipped, not failed.
    """
    from django.conf import settings

    if name == "email":
        from notifications.channels.email import EmailChannel

        return EmailChannel()

    if name == "sms":
        return get_sms_backend()

    if name == "whatsapp":
        # Off unless a tenant has switched it on *and* the platform is
        # configured. Both are required: a tenant cannot enable a channel that
        # has no approved sender behind it.
        if not getattr(settings, "WHATSAPP_ACCESS_TOKEN", ""):
            return None
        from notifications.channels.whatsapp import WhatsAppChannel

        return WhatsAppChannel()

    return None
