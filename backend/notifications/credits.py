"""SMS credits (L4; design §9.1a).

One SMS is one credit, and one credit is KES 1. This is the only metered channel,
because it is the only one that costs money per message.

Two rules decide the shape of everything here:

**The ledger is the truth.** Every purchase and every message is an entry;
the balance is their sum. `OrganizationSettings.sms_credit_balance` caches that
sum so a screen need not add up a table, and is written in the same transaction
as the entry — so it can always be checked, and never becomes the only record.
The stock ledger works the same way, for the same reason.

**A failed send costs nothing.** The credit is taken before the provider is
called, because two events dispatching at once must not both spend the last one;
if the provider then refuses the message, the entry is reversed. Charging on
acceptance is what an owner would expect, and taking the credit first is what
makes the count correct under concurrency. Doing both means one reversal in the
uncommon case rather than an overdraft in the common one.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.db.models import Sum

from core.models import OrganizationSettings
from notifications.models import SmsCreditEntry

logger = logging.getLogger(__name__)

#: What one credit costs the tenant, in KES. Here rather than in settings
#: because it is a commercial fact of this product, not a per-deployment knob;
#: when it stops being one, it becomes a field on the organization.
CREDIT_PRICE_KES = 1


class NoCredit(Exception):
    """There is nothing left to spend."""


def balance(organization) -> int:
    """The cached balance — what every screen reads."""
    return (
        OrganizationSettings.objects.filter(organization=organization)
        .values_list("sms_credit_balance", flat=True)
        .first()
        or 0
    )


def ledger_balance(organization) -> int:
    """The balance recomputed from the ledger.

    The figure that settles an argument. If this and `balance()` ever disagree,
    the ledger is right and the cache is broken.
    """
    from core.tenancy import tenant_context

    # Entries are tenant-scoped rows. Establishing the context here means this
    # works from the platform console and a management shell as well as from
    # inside a request — the same reason `_apply` does it.
    with transaction.atomic(), tenant_context(organization):
        return (
            SmsCreditEntry.objects.filter(organization=organization).aggregate(
                total=Sum("quantity")
            )["total"]
            or 0
        )


def _apply(
    organization, *, kind, quantity, delivery=None, actor=None, note="", allow_overdraft=False
):
    """Write one entry and move the cached balance with it, under a row lock.

    The lock is the point: two dispatches running at once would otherwise read
    the same balance and both spend the last credit.

    The tenant context is established here rather than left to the caller. Two of
    the three callers do not have one — the platform console selling credits
    belongs to no tenant, and a management shell has none either — and without it
    Postgres refuses the insert under row-level security (§2.3). Establishing it
    inside the transaction is what makes the setting take effect at all; outside
    one it is discarded immediately (§2.2).
    """
    from core.tenancy import tenant_context

    with transaction.atomic(), tenant_context(organization):
        settings_row = OrganizationSettings.objects.select_for_update().get(
            organization=organization
        )

        if quantity < 0 and not allow_overdraft and settings_row.sms_credit_balance + quantity < 0:
            raise NoCredit(
                f"{organization.name} has {settings_row.sms_credit_balance} SMS credit(s) left."
            )

        entry = SmsCreditEntry.objects.create(
            organization=organization,
            kind=kind,
            quantity=quantity,
            delivery=delivery,
            created_by=actor,
            note=note,
        )
        settings_row.sms_credit_balance += quantity
        settings_row.save(update_fields=["sms_credit_balance"])
        return entry


def purchase(organization, quantity: int, *, actor=None, note: str = "") -> SmsCreditEntry:
    """Add credits the tenant has bought.

    Money changes hands outside this system; the entry records that it did, and
    who recorded it.
    """
    if quantity <= 0:
        raise ValueError("A purchase adds credits, so the quantity must be positive.")
    return _apply(
        organization,
        kind=SmsCreditEntry.Kind.PURCHASE,
        quantity=quantity,
        actor=actor,
        note=note,
    )


def adjust(organization, quantity: int, *, actor=None, note: str) -> SmsCreditEntry:
    """A correction — a refund, or a goodwill credit. Always with a reason."""
    if not note:
        raise ValueError("An adjustment needs a note saying why.")
    return _apply(
        organization,
        kind=SmsCreditEntry.Kind.ADJUSTMENT,
        quantity=quantity,
        actor=actor,
        note=note,
    )


def spend_one(organization, delivery, *, allow_overdraft=False, note="") -> SmsCreditEntry:
    """Take one credit for one message. Raises `NoCredit` when there is none.

    `allow_overdraft` exists for one case, and should stay that narrow: a
    message somebody needs in order to *get in at all*, to a person who has no
    email address. Refusing it would lock them out of the system entirely, and a
    balance of -1 that an administrator can see and settle is a far smaller
    problem than a technician who can never sign in. Everything else — every
    notification about work — stops at zero.
    """
    return _apply(
        organization,
        kind=SmsCreditEntry.Kind.CONSUMPTION,
        quantity=-1,
        delivery=delivery,
        allow_overdraft=allow_overdraft,
        note=note,
    )


def refund(entry: SmsCreditEntry, *, note: str) -> SmsCreditEntry:
    """Give back a credit taken for a message the provider then refused.

    A reversing entry rather than a deletion — the attempt happened, and the
    record of it is worth more than a tidy ledger.
    """
    return _apply(
        entry.organization,
        kind=SmsCreditEntry.Kind.ADJUSTMENT,
        quantity=-entry.quantity,
        delivery=entry.delivery,
        note=note,
    )


def is_low(organization) -> bool:
    """Worth warning about on screen, before messages quietly stop."""
    settings_row = OrganizationSettings.objects.filter(organization=organization).first()
    if settings_row is None:
        return False
    return settings_row.sms_credit_balance <= settings_row.sms_credit_low_threshold
