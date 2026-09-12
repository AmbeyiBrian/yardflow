"""Isolation fixture for the notification endpoint (T1.20, A3).

Worth exercising even though the viewset filters by recipient as well as tenant:
the recipient filter is what keeps colleagues apart, and the tenant scoping is
what keeps organizations apart. Both have to hold.
"""

from core.isolation import register_isolation_fixture


def register() -> None:
    from accounts.models import User
    from notifications.models import NotificationDelivery, NotificationEvent

    def make_delivery(organization):
        recipient = User.objects.filter(organization=organization).first() or (
            User.objects.create_user(email="iso-recipient@example.com", organization=organization)
        )
        event = NotificationEvent.objects.create(
            organization=organization,
            event_key="approval.requested",
            target_type="dispatch.GateOut",
            target_id="1",
            target_label="GP-ISO-1",
        )
        return NotificationDelivery.objects.create(
            organization=organization,
            event=event,
            recipient=recipient,
            channel="in_app",
            subject="Isolation fixture",
            body="Isolation fixture",
        )

    register_isolation_fixture("notification", make_delivery)
