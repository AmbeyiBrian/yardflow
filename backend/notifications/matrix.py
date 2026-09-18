"""Notification events and the default matrix (design §9; L1, L2).

L2's table, verbatim, as the seeded default. A tenant edits it afterwards — L2's
whole point is "so that people are not spammed", and the right answer differs by
company.

Recipients are expressed as **roles or relationships**, never as named people. A
matrix naming individuals would break the first time someone left.
"""

from __future__ import annotations

from dataclasses import dataclass


class Channel:
    """Delivery channels (L1)."""

    IN_APP = "in_app"
    EMAIL = "email"
    SMS = "sms"
    # Q1, §9.2: written but shipped disabled, pending Meta sender approval.
    WHATSAPP = "whatsapp"


class Recipient:
    """Who to tell, resolved at send time (L2)."""

    #: Approvers for the level currently outstanding.
    APPROVERS = "approvers"
    REQUESTER = "requester"
    STOREKEEPERS = "storekeepers"
    OWNER = "owner"
    CUSTODY_HOLDER = "custody_holder"
    HOLDER = "holder"
    SUPERVISOR = "supervisor"
    FALLBACK_APPROVER = "fallback_approver"


class Event:
    """Every event the system emits (L2)."""

    GATE_OUT_AWAITING_APPROVAL = "gate_out.awaiting_approval"
    GATE_OUT_APPROVED = "gate_out.approved"
    GATE_OUT_REJECTED = "gate_out.rejected"
    GATE_OUT_RELEASED = "gate_out.released"
    GATE_OUT_EXPIRED = "gate_out.expired"
    APPROVAL_ESCALATED = "approval.escalated"
    ITEM_OVERDUE = "custody.overdue"
    RETURN_VARIANCE_RAISED = "variance.return_raised"
    RELEASE_VARIANCE_RAISED = "release_variance.raised"
    STOCK_BELOW_MINIMUM = "stock.below_minimum"
    JOB_CLOSED_WITH_UNACCOUNTED = "job.closed_with_unaccounted"
    CLIENT_RETURN_UNACKNOWLEDGED = "client_return.unacknowledged"
    # Phase 6. A disposal is a write-off, so its events go to the people who
    # answer for the stock rather than only to whoever raised it (J3).
    DISPOSAL_AWAITING_APPROVAL = "disposal.awaiting_approval"
    DISPOSAL_APPROVED = "disposal.approved"
    DISPOSAL_COMPLETED = "disposal.completed"
    DISPOSITION_POSTED = "disposition.posted"
    # T7.5: a queued export is finished. In-app only — it is a link, and a link
    # is useless in an SMS.
    REPORT_EXPORT_READY = "report.export_ready"
    # O7: an expensive release on a project, told to the owner after the fact.
    HIGH_VALUE_PROJECT_RELEASE = "project.high_value_release"


@dataclass(frozen=True)
class EventSpec:
    key: str
    label: str
    recipients: tuple[str, ...]
    channels: tuple[str, ...]
    #: Does silence here let something go unnoticed that the product exists to
    #: catch? L2 lets a tenant switch any event off — that decision belongs to
    #: whoever holds `settings.manage`, not to us — but the interface says so
    #: plainly before they do, and this flag is what it reads.
    #:
    #: It is a warning, never a block. Nothing here is *only* a notification:
    #: a request nobody was told about still sits on the Approvals screen, and an
    #: overdue item still appears on Custody. Muting a message never hides work.
    carries_a_control: bool = False


#: L2's default matrix, editable per tenant.
#:
#: WhatsApp appears here because L2 asks for it, but the channel ships **disabled**
#: (Q1, §9.2): the Business API needs an approved sender and pre-registered
#: templates, which takes weeks. Where L2 specifies WhatsApp, in-app carries the
#: message until the sender is approved — the approval loop must not wait on
#: Meta's queue.
DEFAULT_MATRIX: tuple[EventSpec, ...] = (
    EventSpec(
        Event.GATE_OUT_AWAITING_APPROVAL,
        "Gate-out awaiting approval",
        (Recipient.APPROVERS,),
        (Channel.WHATSAPP, Channel.IN_APP),
        carries_a_control=True,
    ),
    EventSpec(
        Event.GATE_OUT_APPROVED,
        "Gate-out approved",
        (Recipient.REQUESTER, Recipient.STOREKEEPERS),
        (Channel.IN_APP,),
    ),
    EventSpec(
        Event.GATE_OUT_REJECTED,
        "Gate-out rejected",
        (Recipient.REQUESTER,),
        (Channel.WHATSAPP, Channel.IN_APP),
    ),
    EventSpec(
        Event.GATE_OUT_RELEASED,
        "Gate-out released",
        (Recipient.REQUESTER, Recipient.CUSTODY_HOLDER),
        (Channel.IN_APP,),
    ),
    EventSpec(
        Event.APPROVAL_ESCALATED,
        "Approval escalated on timeout",
        (Recipient.FALLBACK_APPROVER, Recipient.OWNER),
        (Channel.WHATSAPP, Channel.SMS),
        carries_a_control=True,
    ),
    EventSpec(
        Event.ITEM_OVERDUE,
        "Item overdue for return",
        # I3's escalating chain: holder, then supervisor, then owner.
        (Recipient.HOLDER, Recipient.SUPERVISOR, Recipient.OWNER),
        (Channel.SMS, Channel.IN_APP),
        carries_a_control=True,
    ),
    EventSpec(
        Event.RETURN_VARIANCE_RAISED,
        "Return variance raised",
        (Recipient.STOREKEEPERS, Recipient.OWNER),
        (Channel.IN_APP,),
        carries_a_control=True,
    ),
    EventSpec(
        Event.STOCK_BELOW_MINIMUM,
        "Stock below minimum",
        (Recipient.STOREKEEPERS,),
        (Channel.IN_APP,),
    ),
    EventSpec(
        Event.JOB_CLOSED_WITH_UNACCOUNTED,
        "Job closed with unaccounted material",
        (Recipient.OWNER,),
        (Channel.WHATSAPP, Channel.IN_APP),
        carries_a_control=True,
    ),
    EventSpec(
        Event.CLIENT_RETURN_UNACKNOWLEDGED,
        "Client return unacknowledged after N days",
        (Recipient.STOREKEEPERS, Recipient.OWNER),
        (Channel.IN_APP,),
        carries_a_control=True,
    ),
    EventSpec(
        Event.DISPOSAL_AWAITING_APPROVAL,
        "Disposal awaiting approval",
        (Recipient.APPROVERS, Recipient.OWNER),
        (Channel.WHATSAPP, Channel.IN_APP),
        carries_a_control=True,
    ),
    EventSpec(
        Event.DISPOSAL_APPROVED,
        "Disposal approved",
        (Recipient.REQUESTER, Recipient.STOREKEEPERS),
        (Channel.IN_APP,),
    ),
    EventSpec(
        Event.HIGH_VALUE_PROJECT_RELEASE,
        "Expensive material approved on a project",
        # The manager already knows — they just approved it. This is for the
        # people who would otherwise never see it: with criticality routing off
        # for project material (D22) and self-approval permitted (O6), this
        # notification and the report behind it are the only things standing
        # between one signature and nobody noticing.
        (Recipient.OWNER,),
        (Channel.IN_APP,),
        carries_a_control=True,
    ),
    EventSpec(
        Event.DISPOSAL_COMPLETED,
        "Disposal completed",
        # The owner is told every time material leaves the books for good, and
        # that is not configurable away by accident: J3 exists so write-offs are
        # controlled, and a control nobody hears about is a control in name only.
        (Recipient.OWNER, Recipient.STOREKEEPERS),
        (Channel.IN_APP,),
    ),
    EventSpec(
        Event.DISPOSITION_POSTED,
        "Quarantine decision actioned",
        (Recipient.STOREKEEPERS,),
        (Channel.IN_APP,),
    ),
    EventSpec(
        Event.REPORT_EXPORT_READY,
        "Report export ready",
        (Recipient.REQUESTER,),
        (Channel.IN_APP,),
    ),
    # Not in L2's table, but the same shape and needed by G1's variance.
    EventSpec(
        Event.RELEASE_VARIANCE_RAISED,
        "Release variance raised",
        (Recipient.STOREKEEPERS, Recipient.OWNER),
        (Channel.IN_APP,),
        carries_a_control=True,
    ),
    EventSpec(
        Event.GATE_OUT_EXPIRED,
        "Gate pass expired before release",
        (Recipient.REQUESTER, Recipient.STOREKEEPERS),
        (Channel.IN_APP,),
    ),
)

MATRIX_BY_EVENT = {spec.key: spec for spec in DEFAULT_MATRIX}


#: Channels active by default (L1, Q1, §9.2).
#:
#: §9.2's recommendation, adopted: "ship v1 with SMS, email and in-app; leave the
#: WhatsApp adapter written but disabled behind the channel setting."
DEFAULT_ACTIVE_CHANNELS = {
    Channel.IN_APP: True,
    Channel.EMAIL: True,
    Channel.SMS: True,
    Channel.WHATSAPP: False,
}


def default_matrix_config() -> dict:
    """The seeded ``notification_matrix`` for a new tenant (C8, L2)."""
    return {
        spec.key: {
            "recipients": list(spec.recipients),
            "channels": list(spec.channels),
            "enabled": True,
        }
        for spec in DEFAULT_MATRIX
    }


def default_channels_config() -> dict:
    return dict(DEFAULT_ACTIVE_CHANNELS)


def channels_for(organization, event_key: str) -> list[str]:
    """Which channels this tenant wants for this event (L1, L2).

    A channel switched off tenant-wide beats the matrix: L1 is about "what our
    staff actually read", so a company with no SMS budget must not have SMS
    reintroduced by a per-event setting.
    """
    settings = organization.settings
    matrix = settings.notification_matrix or default_matrix_config()
    active = settings.notification_channels or default_channels_config()

    entry = matrix.get(event_key)
    if entry is None:
        spec = MATRIX_BY_EVENT.get(event_key)
        if spec is None:
            return []
        wanted = list(spec.channels)
    else:
        if not entry.get("enabled", True):
            return []
        wanted = list(entry.get("channels", []))

    return [channel for channel in wanted if active.get(channel, False)]


def is_enabled(organization, event_key: str) -> bool:
    """Has this tenant switched this event off entirely? (L2)

    Distinct from ``channels_for`` returning nothing: an event can be enabled and
    still reach nobody, because every channel it wanted is switched off
    tenant-wide. The settings screen has to tell those two apart — one is a
    choice about this event, the other about the whole company.
    """
    matrix = organization.settings.notification_matrix or default_matrix_config()
    entry = matrix.get(event_key)
    return True if entry is None else bool(entry.get("enabled", True))


def recipients_for(organization, event_key: str) -> list[str]:
    """Which recipient groups this tenant wants told (L2)."""
    settings = organization.settings
    matrix = settings.notification_matrix or default_matrix_config()

    entry = matrix.get(event_key)
    if entry is not None:
        return list(entry.get("recipients", []))

    spec = MATRIX_BY_EVENT.get(event_key)
    return list(spec.recipients) if spec else []
