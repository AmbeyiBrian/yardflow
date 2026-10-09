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
    if document._meta.label in _FINANCE_TARGETS:
        return _finance_payload(document)

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


#: Money out (R4, §4.17.9): the two documents that go through finance approval.
_FINANCE_TARGETS = frozenset(
    {
        "commercials.ProjectExpense",
        "commercials.AllowanceRequest",
        # R7, R8 (§4.19.12): a site purchase is routed as an expense, a
        # subcontract payment has the PM level only and no PAID stage.
        "commercials.SitePurchase",
        "commercials.SubcontractPayment",
    }
)


def _kes(amount) -> str:
    """``KES 1,200``, with cents only when there are some."""
    from decimal import Decimal

    value = Decimal(amount)
    return f"KES {value:,.0f}" if value == value.to_integral_value() else f"KES {value:,.2f}"


def _purchase_payload(entry) -> dict:
    """A site purchase (R7, §4.19.12): the same shape, plus supplier and destination."""
    supplier = entry.supplier.name if entry.supplier_id else ""
    payload: dict = {
        "kind": "purchase",
        "number": entry.number,
        "label": f"{entry.number} {_kes(entry.amount)} · {supplier}".strip(" ·"),
        "amount": f"{entry.amount:.2f}",
        "amount_display": _kes(entry.amount),
        "category": supplier or "site purchase",
        "supplier": supplier,
        "destination": entry.destination,
        "site": entry.site.name if entry.site_id else "",
        "project": str(entry.project),
        "recorded_by": entry.recorded_by.full_name or entry.recorded_by.email,
        "status": entry.status,
    }
    if entry.over_budget_by is not None:
        payload["over_budget_by"] = f"{entry.over_budget_by:.2f}"
        payload["over_budget_reason"] = entry.over_budget_reason
    if entry.decision_reason:
        payload["reason"] = entry.decision_reason
    if entry.payment_reference:
        payload["reference"] = entry.payment_reference
    return payload


def _finance_payload(entry) -> dict:
    """What an approver needs to decide, and what a recorder needs to know (§4.17.9).

    Built once at emit time so the message does not re-query the entry, and so it
    says what was true when it happened: a rejection reads with the reason it was
    given even if the entry is later amended and sent again.
    """
    from commercials.models import AllowanceRequest, SitePurchase, SubcontractPayment

    if isinstance(entry, SubcontractPayment):
        details = _subcontract_payment_payload(entry)
        _add_decision_details(entry, details)
        return details
    if isinstance(entry, SitePurchase):
        details = _purchase_payload(entry)
        _add_decision_details(entry, details)
        return details

    is_request = isinstance(entry, AllowanceRequest)
    if is_request:
        what = entry.get_type_display()
    else:
        what = entry.category.name

    payload: dict = {
        "kind": "request" if is_request else "expense",
        # Two places always: the entry may still hold the Decimal it was given.
        "amount": f"{entry.amount:.2f}",
        "amount_display": _kes(entry.amount),
        "category": what,
        "site": entry.site.name if entry.site_id else "",
        "project": str(entry.project),
        "recorded_by": entry.recorded_by.full_name or entry.recorded_by.email,
        "status": entry.status,
    }
    if is_request:
        payload["number"] = entry.number
        payload["label"] = f"{entry.number} {what}".strip()
        # R2: a second float is allowed, but the approver is told of the first.
        from commercials.finance import open_float_warning

        warning = open_float_warning(entry.recorded_by, exclude=entry)
        if warning is not None:
            payload["float_warning"] = {
                "number": warning["number"],
                "balance": str(warning["balance"]),
            }
    else:
        # Expenses have no number, so the label is the readable stand-in.
        payload["label"] = f"Expense {_kes(entry.amount)} · {what}"
        # Same three states as the Approvals sheet (O16, §4.17.8).
        if entry.is_evidenced:
            payload["evidence_state"] = "ATTACHED"
        elif entry.photos_expected:
            payload["evidence_state"] = "ARRIVING"
        else:
            payload["evidence_state"] = "NONE"
    _add_decision_details(entry, payload)
    return payload


def _add_decision_details(entry, payload: dict) -> None:  # type: ignore[no-untyped-def]
    """What every kind shares: the over-budget flag, the reason, the reference."""
    over = getattr(entry, "over_budget_by", None)
    if over:
        # R7 (§4.19.12): an approver sees the overrun and what the recorder said.
        payload["over_budget_by"] = f"{over:.2f}"
        payload["over_budget_by_display"] = _kes(over)
        if getattr(entry, "over_budget_reason", ""):
            payload["over_budget_reason"] = entry.over_budget_reason
    if entry.decision_reason:
        payload["reason"] = entry.decision_reason
    reference = getattr(entry, "payment_reference", "")
    if reference:
        payload["reference"] = reference


def _subcontract_payment_payload(payment) -> dict:  # type: ignore[no-untyped-def]
    """A subcontract payment (R8, §4.19.12): what the PM needs to judge it."""
    from decimal import Decimal

    from commercials.budget import subcontract_work_done
    from commercials.models import ExpenseStatus, SubcontractPayment

    contract = payment.subcontract
    paid = sum(
        (
            row.signed_amount
            for row in SubcontractPayment.objects.filter(
                subcontract=contract, status=ExpenseStatus.APPROVED
            )
        ),
        Decimal("0"),
    )
    return {
        "kind": "subcontract_payment",
        "number": contract.number,
        "label": f"Subcontract payment {_kes(payment.amount)} · {contract}",
        "amount": f"{payment.amount:.2f}",
        "amount_display": _kes(payment.amount),
        "category": "subcontract payment",
        "site": "",
        "project": str(contract.project),
        "subcontractor": str(contract.subcontractor),
        "recorded_by": payment.recorded_by.full_name or payment.recorded_by.email,
        "status": payment.status,
        "work_done": f"{subcontract_work_done(contract):.2f}",
        "paid": f"{paid:.2f}",
        "contract_value": f"{contract.contract_value:.2f}",
        "reference": payment.reference,
    }


def emit_yard_delivery_expected(purchase) -> NotificationEvent | None:  # type: ignore[no-untyped-def]
    """R7 (§4.19.3): a yard purchase was approved, so a delivery is on its way.

    For the purchase service to call when it creates the draft gate-in.
    """
    supplier = getattr(purchase, "supplier", None)
    return emit(
        Event.PURCHASE_YARD_DELIVERY_EXPECTED,
        purchase,
        payload={
            "label": str(purchase),
            "number": purchase.number,
            "project": str(purchase.project),
            "supplier": str(supplier) if supplier is not None else "",
            "amount_display": _kes(purchase.amount),
        },
    )


def emit_po_attached(project, *, days_without_po: int, actor) -> NotificationEvent | None:  # type: ignore[no-untyped-def]
    """R12: Finance is told a PO arrived after the work started."""
    return emit(
        Event.PO_ATTACHED,
        project,
        payload={
            "label": project.reference,
            "project": project.reference,
            "po_number": project.po_number,
            "contract_value_display": _kes(project.contract_value or 0),
            "days_without_po": days_without_po,
            "attached_by": actor.full_name or actor.email,
        },
    )


def emit_milestone_notice(  # type: ignore[no-untyped-def]
    event_key: str, project, milestone, state
) -> NotificationEvent | None:
    """R11: a milestone is due to invoice, or its payment is overdue.

    Emitted on the *project*, so the notification links to the project page.
    """
    return emit(
        event_key,
        project,
        payload={
            "label": f"{project}: {milestone.name}",
            "project": str(project),
            "milestone": milestone.name,
            "amount_display": _kes(state.amount) if state.amount is not None else "",
            "outstanding_display": _kes(state.invoiced - state.received),
            "due_by": state.due_by.isoformat() if state.due_by else "",
            "latest_invoice_date": (
                state.latest_invoice_date.isoformat() if state.latest_invoice_date else ""
            ),
        },
    )


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
    recipients = resolve_recipients(organization, groups, document, event.payload)
    # The PM's copy is for information: in-app only, unless they are also told
    # in their own right through another group (§4.19.7).
    in_app_only = _in_app_only_ids(organization, groups, document, event.payload)

    spec = MATRIX_BY_EVENT.get(event.event_key)
    subject = (
        _finance_headline(event)
        or _attendance_headline(event)
        or (spec.label if spec else event.event_key)
    )
    body = render_body(event)

    created = 0
    for user in recipients:
        for channel in channels:
            if user.pk in in_app_only and channel != Channel.IN_APP:
                continue
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


def _in_app_only_ids(organization, groups, document, payload) -> set[int]:  # type: ignore[no-untyped-def]
    copy_groups = [g for g in groups if g in _IN_APP_ONLY_GROUPS]
    if not copy_groups:
        return set()
    full = resolve_recipients(
        organization, [g for g in groups if g not in _IN_APP_ONLY_GROUPS], document, payload
    )
    copied = resolve_recipients(organization, copy_groups, document, payload)
    full_ids = {user.pk for user in full}
    return {user.pk for user in copied if user.pk not in full_ids}


#: Groups that are told in-app only, whatever channels the event carries.
_IN_APP_ONLY_GROUPS = frozenset({Recipient.PROJECT_MANAGER})


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


def resolve_recipients(organization, groups, document, payload: dict | None = None) -> list:
    """Turn recipient *groups* into people (L2).

    Groups rather than names, so the matrix survives someone leaving. Duplicates
    are removed: an owner who is also the requester gets one message, not two.
    """
    from accounts.models import User

    users: dict[int, User] = {}

    for group in groups:
        for user in _resolve_group(organization, group, document, payload):
            if user is not None and user.is_active:
                users[user.pk] = user

    return list(users.values())


def _resolve_group(organization, group: str, document, payload: dict | None = None) -> list:
    from accounts.permissions_registry import PERM

    if group == Recipient.REQUESTER:
        # Money entries name their requester `recorded_by` (R4).
        # A work day names its person (R13).
        return [
            getattr(document, "requested_by", None)
            or getattr(document, "recorded_by", None)
            or getattr(document, "person", None)
        ]

    if group == Recipient.LEVEL_APPROVERS:
        return _level_approvers(
            organization, document, request_id=(payload or {}).get("approval_request_id")
        )

    if group == Recipient.FINANCE_APPROVERS:
        return _users_with_permission(organization, PERM.FINANCE_APPROVE)

    if group == Recipient.PROJECT_MANAGER:
        is_project = document._meta.label == "network.Project"
        project = document if is_project else getattr(document, "project", None)
        return [getattr(project, "manager", None)]

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


def _level_approvers(organization, document, request_id=None) -> list:
    """Who the lowest open level of a finance entry is addressed to (R4, §4.17.9).

    Mirrors ``can_approve``: a named user is that user's alone; a permission
    level goes to those who hold it *directly* (a delegation does not lend a
    Finance signature, D22); a role level to the role's holders. The recorder is
    never told to approve their own entry, whatever they hold.
    """
    from accounts.models import User
    from approvals.engine import document_type_of
    from approvals.models import ApprovalRequest, ApprovalRequestStatus

    if document is None:
        return []

    open_levels = ApprovalRequest.objects.filter(
        document_type=document_type_of(document),
        document_id=str(document.pk),
        status__in=(ApprovalRequestStatus.PENDING, ApprovalRequestStatus.ESCALATED),
    ).order_by("level", "id")
    first = open_levels.first()
    if first is None:
        return []
    # Every open request at the lowest level: a work day spanning two PMs has
    # two parallel level-1 slices, and both are told (R13, §4.18.5 change 5).
    # Every other document has one request per level, as before.
    current_requests = list(open_levels.filter(level=first.level))
    if request_id is not None:
        # One notification per work-day slice: only that slice's addressee is told,
        # not the other manager of a two-manager day (R13, §4.18.10).
        current_requests = [item for item in open_levels if item.pk == request_id]

    people: list = []
    seen: set[int] = set()
    for current in current_requests:
        if current.required_user_id is not None:
            found = list(User.objects.filter(pk=current.required_user_id, is_active=True))
        elif current.required_permission:
            found = _users_with_permission(organization, current.required_permission)
        elif current.required_role_id is not None:
            found = list(
                User.objects.filter(
                    organization=organization,
                    is_active=True,
                    user_roles__role_id=current.required_role_id,
                ).distinct()
            )
        else:
            found = []
        for person in found:
            if person.pk not in seen:
                seen.add(person.pk)
                people.append(person)

    recorder_id = getattr(document, "recorded_by_id", None) or getattr(
        document, "registered_by_id", None
    )
    return [person for person in people if person.pk != recorder_id]


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

    headline = _finance_headline(event)
    if headline:
        return _finance_body(event, headline)
    if event.event_key in _ATTENDANCE_EVENTS:
        return _attendance_body(event)
    if event.event_key in _PO_EVENTS:
        return _po_body(event)

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
    if event.event_key == Event.ASSET_EXPIRY_DUE:
        return _asset_expiry_body(payload)
    if event.event_key == Event.CLIENT_RETURN_UNACKNOWLEDGED:
        days = payload.get("days_outstanding")
        outstanding = f" {days} days ago" if days else ""
        return (
            f"The client has not acknowledged return {number}, released"
            f"{outstanding}. Until they do, that material is still our exposure."
        )

    spec = MATRIX_BY_EVENT.get(event.event_key)
    return f"{spec.label if spec else event.event_key}: {number}".strip(": ")


def _asset_expiry_body(payload: dict) -> str:
    """R14, §4.20.9: "Insurance for Hilux (KDA 123A) expires on 2026-11-01 (in 12 days)."."""
    asset = payload.get("asset", "A vehicle")
    if payload.get("tag"):
        asset = f"{asset} ({payload['tag']})"
    document = payload.get("document", "Document")
    on = payload.get("expires_on", "")
    days = payload.get("days_left")
    if payload.get("expired"):
        return f"{document} for {asset} expired on {on}. Renew it and update the register."
    when = "today" if days == 0 else f"in {days} day{'' if days == 1 else 's'}"
    return f"{document} for {asset} expires on {on} ({when}). Renew it and update the register."


# --------------------------------------------------------------------------
# money out wording (R4, §4.17.9)
# --------------------------------------------------------------------------

_FINANCE_VERBS = {
    Event.FINANCE_AWAITING_APPROVAL: "waiting for your approval",
    Event.FINANCE_APPROVED: "approved",
    Event.FINANCE_REJECTED: "rejected",
    Event.FINANCE_PAID: "paid",
    # R15, §4.20.2: only ever a supplier.
    Event.SUPPLIER_DETAILS_CHANGED: "payment details changed",
}

#: How the sensitive supplier fields read to a person (§4.20.2).
_SUPPLIER_FIELD_LABELS = {
    "kra_pin": "KRA PIN",
    "bank_name": "bank name",
    "account_number": "account number",
    "mpesa_type": "M-Pesa type",
    "mpesa_number": "M-Pesa number",
    "mpesa_account": "M-Pesa account",
}


def _finance_headline(event: NotificationEvent) -> str:
    """One plain sentence, used as the email subject and the start of the body.

    "Expense waiting for your approval: KES 1,200 fuel at Ruiru by John". Empty
    for any other event, which keeps its own wording.
    """
    verb = _FINANCE_VERBS.get(event.event_key)
    if verb is None:
        return ""
    payload = event.payload or {}
    if payload.get("kind") == "supplier":
        headline = f"Supplier {verb}: {payload.get('label', '')}".strip()
        if event.event_key == Event.FINANCE_AWAITING_APPROVAL and payload.get("recorded_by"):
            headline += f" added by {payload['recorded_by']}"
        elif event.event_key == Event.SUPPLIER_DETAILS_CHANGED and payload.get("changed_by"):
            headline += f" by {payload['changed_by']}"
        return headline
    if payload.get("kind") == "request":
        noun = f"Allowance request {payload['number']}" if payload.get("number") else "Request"
    elif payload.get("kind") == "purchase":
        noun = f"Site purchase {payload['number']}" if payload.get("number") else "Site purchase"
    elif payload.get("kind") == "subcontract_payment":
        noun = "Subcontract payment"
    else:
        noun = "Expense"
    what = f"{payload.get('amount_display', '')} {str(payload.get('category', '')).lower()}".strip()
    if payload.get("kind") == "purchase":
        what = payload.get("amount_display", "")
        if payload.get("supplier"):
            what += f" from {payload['supplier']}"
    elif payload.get("kind") == "subcontract_payment":
        what = payload.get("amount_display", "")
        if payload.get("subcontractor"):
            what += f" to {payload['subcontractor']}"
    place = f"at {payload['site']}" if payload.get("site") else f"on {payload.get('project', '')}"
    headline = f"{noun} {verb}: {what} {place}".strip()
    if event.event_key == Event.FINANCE_AWAITING_APPROVAL and payload.get("recorded_by"):
        headline += f" by {payload['recorded_by']}"
    return headline


def _supplier_body(event: NotificationEvent, parts: list[str]) -> str:
    payload = event.payload or {}
    if event.event_key == Event.FINANCE_AWAITING_APPROVAL:
        parts.append("Open Approvals to check the KRA PIN and payment details, then decide.")
    elif event.event_key == Event.FINANCE_APPROVED:
        parts.append("They can now be paid.")
    elif event.event_key == Event.FINANCE_REJECTED:
        if payload.get("reason"):
            parts.append(f"Reason: {payload['reason']}")
        parts.append("You can correct it and send it again.")
    elif event.event_key == Event.SUPPLIER_DETAILS_CHANGED:
        changed = [_SUPPLIER_FIELD_LABELS.get(f, f) for f in payload.get("changed") or []]
        if changed:
            parts.append(f"Changed: {', '.join(changed)}.")
        parts.append("They stay approved, so check the change was meant before paying them.")
    return " ".join(parts)


def _finance_body(event: NotificationEvent, headline: str) -> str:
    payload = event.payload or {}
    parts = [f"{headline}."]
    if payload.get("kind") == "supplier":
        return _supplier_body(event, parts)
    if event.event_key == Event.FINANCE_AWAITING_APPROVAL:
        if payload.get("over_budget_by_display"):
            line = f"Over the project's budget by {payload['over_budget_by_display']}."
            if payload.get("over_budget_reason"):
                line += f" Reason given: {payload['over_budget_reason']}"
            parts.append(line)
        if payload.get("kind") == "purchase":
            if payload.get("destination") == "INTO_YARD":
                parts.append("Goods are for the yard, so approval creates a delivery.")
            else:
                parts.append("Goods are used at the site.")
        elif payload.get("kind") == "subcontract_payment":
            parts.append(
                f"Work done {_kes(payload['work_done'])}, paid so far {_kes(payload['paid'])}, "
                f"contract value {_kes(payload['contract_value'])}."
            )
        evidence = payload.get("evidence_state")
        if evidence == "NONE":
            parts.append("No receipt has been attached.")
        elif evidence == "ARRIVING":
            parts.append("The receipt photos have not arrived yet.")
        warning = payload.get("float_warning")
        if warning:
            parts.append(
                f"They still hold float {warning['number']} with a balance of "
                f"{_kes(warning['balance'])}."
            )
        parts.append("Open Approvals to decide.")
    elif event.event_key == Event.FINANCE_REJECTED:
        if payload.get("reason"):
            parts.append(f"Reason: {payload['reason']}")
        parts.append("You can correct it and send it again.")
    elif event.event_key == Event.FINANCE_PAID and payload.get("reference"):
        parts.append(f"Payment reference: {payload['reference']}.")
    return " ".join(parts)


# --------------------------------------------------------------------------
# work day wording (R13, §4.18.10)
# --------------------------------------------------------------------------

_ATTENDANCE_EVENTS = frozenset(
    {Event.ATTENDANCE_AWAITING_APPROVAL, Event.ATTENDANCE_REJECTED, Event.ATTENDANCE_UNROUTED}
)


def _attendance_headline(event: NotificationEvent) -> str:
    """"Work day waiting for your approval: Wanjiru Worker, Mon 2 Mar 2026, 8.5 h"."""
    if event.event_key not in _ATTENDANCE_EVENTS:
        return ""
    payload = event.payload or {}
    verb = {
        Event.ATTENDANCE_AWAITING_APPROVAL: "waiting for your approval",
        Event.ATTENDANCE_REJECTED: "rejected",
        Event.ATTENDANCE_UNROUTED: "has nobody to approve it",
    }[event.event_key]
    what = ", ".join(
        part
        for part in (
            payload.get("person"),
            payload.get("date_display"),
            payload.get("hours_display"),
        )
        if part
    )
    corrected = " (corrected)" if payload.get("corrected") else ""
    return f"Work day {verb}{corrected}: {what}".strip()


def _attendance_body(event: NotificationEvent) -> str:
    payload = event.payload or {}
    parts = [f"{_attendance_headline(event)}."]
    flags = [flag for flag in payload.get("flags") or [] if flag != "corrected"]
    if flags and event.event_key != Event.ATTENDANCE_REJECTED:
        parts.append(f"Flags: {', '.join(flags)}.")
    if event.event_key == Event.ATTENDANCE_AWAITING_APPROVAL:
        parts.append("Open Approvals to decide.")
    elif event.event_key == Event.ATTENDANCE_REJECTED:
        if payload.get("approver"):
            parts.append(f"Rejected by {payload['approver']}.")
        if payload.get("reason"):
            parts.append(f"Reason: {payload['reason']}")
        parts.append("You can correct it in My time within 30 days.")
    elif event.event_key == Event.ATTENDANCE_UNROUTED:
        parts.append(
            "No project manager or Director can approve it. Assign one so the day can be approved."
        )
    return " ".join(parts)


# --------------------------------------------------------------------------
# PO and milestone wording (R11, R12, §4.19.12)
# --------------------------------------------------------------------------

_PO_EVENTS = frozenset(
    {
        Event.PO_MILESTONE_DUE,
        Event.PO_MILESTONE_OVERDUE,
        Event.PO_ATTACHED,
        Event.PURCHASE_YARD_DELIVERY_EXPECTED,
    }
)


def _po_body(event: NotificationEvent) -> str:
    payload = event.payload or {}
    project = payload.get("project", "")
    if event.event_key == Event.PO_MILESTONE_DUE:
        amount = f" ({payload['amount_display']})" if payload.get("amount_display") else ""
        return (
            f"Milestone \"{payload.get('milestone', '')}\"{amount} on {project} is due. "
            "Raise the invoice."
        )
    if event.event_key == Event.PO_MILESTONE_OVERDUE:
        owed = payload.get("outstanding_display", "")
        invoiced_on = payload.get("latest_invoice_date")
        since = f" Invoiced {invoiced_on}." if invoiced_on else ""
        return (
            f"Milestone \"{payload.get('milestone', '')}\" on {project} is overdue: "
            f"{owed} is still unpaid.{since} Chase the client."
        )
    if event.event_key == Event.PO_ATTACHED:
        days = payload.get("days_without_po")
        late = f" after {days} day{'' if days == 1 else 's'} without one" if days else ""
        return (
            f"PO {payload.get('po_number', '')} ({payload.get('contract_value_display', '')}) "
            f"was attached to project {project}{late} by {payload.get('attached_by', '')}. "
            "Review the default milestones and fill in their shares."
        )
    supplier = f" from {payload['supplier']}" if payload.get("supplier") else ""
    return (
        f"Purchase {payload.get('number', '')}{supplier} for {project} "
        f"({payload.get('amount_display', '')}) was approved for the yard. "
        "A delivery is expected."
    ).replace("  ", " ")
