"""The SMS credits a new tenant starts with (L4).

Metering arrived with an opening balance for every organization that predated
it, so nobody's messages stopped without warning. A tenant provisioned *after*
that got nothing — so the very first thing a new organization does, inviting its
owner, could not go by SMS. Silver Tech's invitation went email-only for exactly
this reason, which is fine for somebody with an email address and a dead end for
a technician who has only a phone.

The same opening balance, for the same reason: enough to get a yard working
while a purchase is arranged, and small enough that nobody mistakes it for a
gift.
"""

from __future__ import annotations

#: Matches the balance granted to tenants that predated metering.
OPENING_BALANCE = 100


def seed_opening_sms_credits(organization) -> dict:
    from notifications import credits

    credits.adjust(
        organization,
        OPENING_BALANCE,
        note="Opening balance for a new organization.",
    )
    return {"sms_credits": OPENING_BALANCE}
