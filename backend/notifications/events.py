"""Emitting and dispatching notifications (design §9.1; L1, L2, L3).

§9.1's flow, and the ordering is the requirement:

1. domain code emits an event **inside** the transaction
2. ``transaction.on_commit`` enqueues the delivery — so **a notification never
   blocks or rolls back the underlying business transaction** (L3)
3. recipients are resolved from roles, rendered per channel, one delivery row each
4. failures retry with capped backoff; terminal failures are visible to admins

Step 2 is the whole point. An approval that rolled back because an SMS gateway
was down would be an outage caused by a courtesy message.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from notifications.matrix import (
    MATRIX_BY_EVENT,
    Channel,
    Event,
    Recipient,
    channels_for,
    recipients_for,
)
from notifications.models import DeliveryStatus, NotificationDelivery, NotificationEvent

logger = logging.getLogger(__name__)


def emit(event_key: str, document, *, payload: dict | None = None) -> NotificationEvent | None:
    """Record that something happened, and schedule telling people (§9.1).

    Safe to call inside a transaction — that is where it belongs. Nothing is sent
    until the transaction commits, so a rolled-back approval sends nothing and a
    failed send cannot roll back an approval (L3).
    """
    organization_id = getattr(document, "organization_id", None)
    if organization_id is None:
        logger.warning("notification event %s has no organization", event_key)
        return None

    event = NotificationEvent.objects.create(
        organization_id=organization_id,
        event_key=event_key,
        target_type=document._meta.label,
        target_id=str(document.pk),
        target_label=str(document),
        payload=payload or _payload_for(document),
    )

    # L3: after commit, never during. If this transaction rolls back, the event
    # row goes with it and nothing is sent — which is correct.
    #
    # The organization is passed explicitly rather than looked up: a dispatch
    # runs outside any request, so it has no tenant context of its own and must
    # be told which tenant it is acting for (§2.2). That also means no unscoped
    # query is needed to find the event.
    transaction.on_commit(lambda: dispatch_event(event.pk, organization_id=organization_id))

    return event


def _payload_for(document) -> dict:
    """Enough context to render a message without re-querying the document."""
    payload = {"label": str(document)}

    for attribute in ("number", "status", "purpose_type"):
        value = getattr(document, attribute, None)
        if value:
            payload[attribute] = str(value)

    destination = getattr(document, "destination_label", None)
    if destination:
        payload["destination"] = destination

    holder = getattr(document, "custody_holder", None)
    if holder is not None:
        payload["custody_holder"] = str(holder)

    return payload


def dispatch_event(event_id, *, organization_id=None) -> int:
    """Resolve recipients, render, and write a delivery row for each (§9.1).

    Called after commit — normally through Celery, and inline when
    ``CELERY_TASK_ALWAYS_EAGER`` is set for local development (§12.0).

    ``organization_id`` is required: a dispatch has no request and therefore no
    tenant context, so it must be told which tenant it acts for (§2.2). Passing
    it also means the event can be found with the ordinary scoped manager rather
    than an unscoped one.
    """
    from core.tenancy import TenantContextMissing, get_current_organization_id, tenant_context

    organization_id = organization_id or get_current_organization_id()
    if organization_id is None:
        raise TenantContextMissing(
            "dispatch_event needs an organization_id: it runs outside a request "
            "and cannot infer which tenant the event belongs to (§2.2)."
        )

    # The transaction is not incidental. `tenant_context` publishes the
    # organization to Postgres as a **transaction-local** setting, so outside a
    # transaction it is discarded immediately and every row-level security policy
    # sees an empty organization (§2.2, §2.3). A dispatch runs from
    # `transaction.on_commit`, which is by definition outside one — so without
    # this the lookup below could not even see its own event, and every
    # notification would be silently dropped.
    #
    # One transaction for the whole dispatch is also right on its own terms: the
    # delivery rows for one event belong together.
    with transaction.atomic(), tenant_context(organization_id):
        event = NotificationEvent.objects.filter(pk=event_id).select_related("organization").first()
        if event is None:
            return 0
        return _dispatch(event)


def _dispatch(event: NotificationEvent) -> int:
    organization = event.organization
    channels = channels_for(organization, event.event_key)
    groups = recipients_for(organization, event.event_key)

    if not channels or not groups:
        # Deliberately configured silent, which is a valid answer to L2's "so
        # that people are not spammed".
        return 0

    document = _load_target(event)
    recipients = resolve_recipients(organization, groups, document)

    spec = MATRIX_BY_EVENT.get(event.event_key)
    subject = spec.label if spec else event.event_key
    body = render_body(event)

    created = 0
    for user in recipients:
        for channel in channels:
            destination = _destination_for(user, channel)
            if not destination and channel != Channel.IN_APP:
                # No address on that channel for this person. Recorded as skipped
                # rather than failed: nothing went wrong, they just cannot be
                # reached that way (B1 allows a user with only a phone, or only
                # an email).
                _record(event, user, channel, subject, body, "", DeliveryStatus.SKIPPED)
                continue

            delivery, was_new = _record(
                event, user, channel, subject, body, destination, DeliveryStatus.PENDING
            )
            if was_new:
                created += 1
                send_delivery(delivery)

    return created


def _record(event, user, channel, subject, body, destination, status):
    delivery, was_new = NotificationDelivery.objects.get_or_create(
        organization_id=event.organization_id,
        event=event,
        recipient=user,
        channel=channel,
        defaults={
            "subject": subject,
            "body": body,
            "destination": destination,
            "status": status,
        },
    )
    return delivery, was_new


def send_delivery(delivery: NotificationDelivery) -> bool:
    """Send one delivery, recording the outcome (L3).

    Never raises. A failure is a recorded state, not an exception that unwinds
    the caller — which is what makes L3 true rather than aspirational.
    """
    from notifications.channels import get_channel

    if delivery.channel == Channel.IN_APP:
        # An in-app message is delivered by existing; there is nothing to send.
        delivery.status = DeliveryStatus.SENT
        delivery.sent_at = timezone.now()
        delivery.attempts += 1
        delivery.save(update_fields=["status", "sent_at", "attempts", "updated_at"])
        return True

    channel = get_channel(delivery.channel)
    if channel is None:
        delivery.status = DeliveryStatus.SKIPPED
        delivery.last_error = f"No adapter for channel '{delivery.channel}'."
        delivery.attempts += 1
        delivery.save(update_fields=["status", "last_error", "attempts", "updated_at"])
        return False

    from notifications.channels import RenderedMessage

    # L4: SMS is metered. Take the credit before calling the provider — two
    # dispatches running at once must not both spend the last one — and give it
    # back below if the message is refused, so a failed send costs nothing.
    charged = None
    if delivery.channel == Channel.SMS:
        from notifications import credits

        try:
            charged = credits.spend_one(delivery.organization, delivery)
        except credits.NoCredit as exhausted:
            # Not retryable, and deliberately so: retrying a message there is no
            # credit for burns attempts and hides the reason it stopped. The
            # business transaction is long committed either way (L3).
            delivery.status = DeliveryStatus.FAILED
            delivery.last_error = f"No SMS credit. {exhausted}"[:500]
            delivery.attempts += 1
            delivery.save(update_fields=["status", "last_error", "attempts", "updated_at"])
            logger.warning(
                "sms not sent for organization %s: out of credit",
                delivery.organization_id,
            )
            return False

    result = channel.send(
        delivery.destination,
        RenderedMessage(subject=delivery.subject, body=delivery.body),
    )

    if charged is not None and not result.succeeded:
        from notifications import credits

        credits.refund(charged, note="Provider did not accept the message.")

    delivery.attempts += 1
    if result.succeeded:
        delivery.status = DeliveryStatus.SENT
        delivery.sent_at = timezone.now()
        delivery.provider_reference = result.provider_reference
        delivery.last_error = ""
    elif result.retryable and delivery.attempts < MAX_ATTEMPTS:
        delivery.status = DeliveryStatus.PENDING
        delivery.last_error = result.error[:500]
    else:
        # L3: "terminal failures are visible to admins."
        delivery.status = DeliveryStatus.ABANDONED
        delivery.last_error = result.error[:500]

    delivery.save(
        update_fields=[
            "status",
            "sent_at",
            "attempts",
            "last_error",
            "provider_reference",
            "updated_at",
        ]
    )
    return result.succeeded


#: Capped, per L3's "retries with capped exponential backoff". Five attempts over
#: a few hours is enough for a provider blip; beyond that a human is needed and
#: more retries only delay them being told.
MAX_ATTEMPTS = 5


def retry_pending(organization_id, *, limit: int = 200) -> int:
    """Retry deliveries still pending (L3). Run by beat."""
    pending = NotificationDelivery.objects.filter(
        organization_id=organization_id, status=DeliveryStatus.PENDING
    ).exclude(channel=Channel.IN_APP)[:limit]

    sent = 0
    for delivery in pending:
        if send_delivery(delivery):
            sent += 1
    return sent


# --------------------------------------------------------------------------
# recipient resolution
# --------------------------------------------------------------------------


def resolve_recipients(organization, groups, document) -> list:
    """Turn recipient *groups* into people (L2).

    Groups rather than names, so the matrix survives someone leaving. Duplicates
    are removed: an owner who is also the requester gets one message, not two.
    """
    from accounts.models import User

    users: dict[int, User] = {}

    for group in groups:
        for user in _resolve_group(organization, group, document):
            if user is not None and user.is_active:
                users[user.pk] = user

    return list(users.values())


def _resolve_group(organization, group: str, document) -> list:
    from accounts.permissions_registry import PERM

    if group == Recipient.REQUESTER:
        return [getattr(document, "requested_by", None)]

    if group == Recipient.CUSTODY_HOLDER:
        return [getattr(document, "custody_holder", None)]

    if group == Recipient.HOLDER:
        return [getattr(document, "holder", None) or getattr(document, "custody_holder", None)]

    if group == Recipient.APPROVERS:
        return _users_with_permission(organization, PERM.GATE_OUT_APPROVE)

    if group == Recipient.STOREKEEPERS:
        return _users_with_permission(organization, PERM.GATE_IN_POST)

    if group in (Recipient.OWNER, Recipient.FALLBACK_APPROVER):
        # Q6 settles the escalation chain as holder -> storekeeper -> owner, with
        # no supervisor role in the model. "Owner" is therefore whoever holds
        # owner-level permissions (B4).
        return _users_with_permission(organization, PERM.USERS_MANAGE)

    if group == Recipient.SUPERVISOR:
        # Q6: "there is no explicit supervisor role. Assumed escalation goes
        # technician -> storekeeper -> owner." So the storekeepers are the
        # supervisory step.
        return _users_with_permission(organization, PERM.GATE_IN_POST)

    return []


def _users_with_permission(organization, codename: str) -> list:
    from accounts.models import User

    return list(
        User.objects.filter(
            organization=organization,
            is_active=True,
            user_roles__role__permissions__codename=codename,
        ).distinct()
    )


def _destination_for(user, channel: str) -> str:
    if channel == Channel.EMAIL:
        return user.email or ""
    if channel in (Channel.SMS, Channel.WHATSAPP):
        return user.phone or ""
    return ""


def _load_target(event: NotificationEvent):
    """Re-load the document an event was about, if it still exists."""
    from django.apps import apps

    if not event.target_type or not event.target_id:
        return None
    try:
        model = apps.get_model(event.target_type)
    except LookupError:
        return None

    return model.objects.filter(pk=event.target_id).first()


def render_body(event: NotificationEvent) -> str:
    """Render a message for an event (§9.1).

    Plain text, deliberately: it has to read sensibly as an SMS, a WhatsApp
    template variable and an in-app line. Rich formatting would only survive one
    of the three.
    """
    payload = event.payload or {}
    number = payload.get("number") or payload.get("label") or ""

    if event.event_key == Event.GATE_OUT_AWAITING_APPROVAL:
        destination = payload.get("destination")
        where = f" — for {destination}" if destination else ""
        return f"Gate pass {number} needs your approval{where}."
    if event.event_key == Event.GATE_OUT_APPROVED:
        return f"Gate pass {number} has been approved and can be released."
    if event.event_key == Event.GATE_OUT_REJECTED:
        return f"Gate pass {number} was rejected."
    if event.event_key == Event.GATE_OUT_RELEASED:
        holder = payload.get("custody_holder", "")
        return f"Gate pass {number} was released{f' to {holder}' if holder else ''}."
    if event.event_key == Event.GATE_OUT_EXPIRED:
        return f"Gate pass {number} expired before it was released and needs resubmitting."
    if event.event_key == Event.RELEASE_VARIANCE_RAISED:
        return f"A release variance was recorded on gate pass {number} and needs acknowledging."

    if event.event_key == Event.DISPOSAL_AWAITING_APPROVAL:
        return f"Disposal {number} needs approval before anything is written off."
    if event.event_key == Event.DISPOSAL_APPROVED:
        return f"Disposal {number} was approved and can be actioned."
    if event.event_key == Event.DISPOSAL_COMPLETED:
        return f"Disposal {number} is done — that material has left the books."
    if event.event_key == Event.DISPOSITION_POSTED:
        return f"{number} was actioned and the material has left quarantine."
    if event.event_key == Event.REPORT_EXPORT_READY:
        report = payload.get("report") or "Your report"
        return f"{report} is ready to download."
    if event.event_key == Event.CLIENT_RETURN_UNACKNOWLEDGED:
        days = payload.get("days_outstanding")
        outstanding = f" {days} days ago" if days else ""
        return (
            f"The client has not acknowledged return {number}, released"
            f"{outstanding}. Until they do, that material is still our exposure."
        )

    spec = MATRIX_BY_EVENT.get(event.event_key)
    return f"{spec.label if spec else event.event_key}: {number}".strip(": ")
