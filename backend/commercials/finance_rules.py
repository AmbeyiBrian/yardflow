"""The allowance rules, as pure functions (§4.17.5; R2, R5).

No queries and no models are imported here: the service gathers the facts under
its lock and asks these what they mean. That keeps the rules testable without a
database, and means a replayed offline request (R6) is judged by exactly the
code an online one is.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

#: Types that may not overlap themselves (R5). A second FLOAT is allowed (R2)
#: and OTHER is a one-off, so neither is a daily entitlement.
OVERLAP_TYPES = frozenset({"TRANSPORT", "NIGHT_OUT", "TEAM_ALLOWANCE"})

#: Statuses an earlier request must be in to block a new one: it is still
#: alive, or it was paid. A REJECTED one never counted (R5).
OVERLAP_BLOCKING_STATUSES = frozenset(
    {"PENDING_PM", "PENDING_FINANCE", "APPROVED", "PAID"}
)


class DatedRequest(Protocol):
    """What the overlap rule reads from an earlier request."""

    number: str
    status: str
    from_date: date
    to_date: date


def days_between(from_date: date, to_date: date) -> int:
    """Inclusive: a one-day request has ``from_date == to_date`` (§4.17.2)."""
    return (to_date - from_date).days + 1


def ranges_intersect(a_from: date, a_to: date, b_from: date, b_to: date) -> bool:
    """Whether two inclusive date ranges share a day.

    Inclusive at both ends, so a request ending on the 5th and one starting on
    the 5th overlap: that day would be claimed twice.
    """
    return a_from <= b_to and b_from <= a_to


def find_overlap(
    allowance_type: str,
    from_date: date,
    to_date: date,
    earlier: Iterable[DatedRequest],
) -> DatedRequest | None:
    """The earliest-numbered earlier request this one overlaps, if any (R5).

    ``earlier`` is the recorder's requests **of the same type**; the service
    selects them, and this decides which of them count. Exempt types return
    ``None`` without looking.
    """
    if allowance_type not in OVERLAP_TYPES:
        return None
    blocking = [
        item
        for item in earlier
        if item.status in OVERLAP_BLOCKING_STATUSES
        and ranges_intersect(from_date, to_date, item.from_date, item.to_date)
    ]
    return min(blocking, key=lambda item: item.number) if blocking else None


def limit_key(allowance_type: str, transport_scope: str = "") -> str:
    """Which entry of ``allowance_limits`` governs this request (§4.17.5)."""
    if allowance_type == "TRANSPORT":
        return f"TRANSPORT_{transport_scope}"
    return allowance_type


@dataclass(frozen=True)
class LimitBreach:
    """A request outside its daily limit, with what a person needs to read."""

    key: str
    daily: Decimal
    bound: Decimal
    #: ``"min"`` or ``"max"``.
    side: str


def _bound(limits: Mapping[str, Any], key: str, side: str) -> Decimal | None:
    entry = limits.get(key) or {}
    value = entry.get(side)
    if value is None or value == "":
        return None
    return Decimal(str(value))


def check_limit(
    key: str, amount: Decimal, days: int, limits: Mapping[str, Any]
) -> LimitBreach | None:
    """Compare the whole amount with ``min × days`` and ``max × days`` (R5).

    Multiplied rather than dividing the amount down, so no rounding can push a
    request that is exactly on the limit over it. A key with no entry, or a
    null bound, is unbounded: FLOAT and OTHER are never keyed at all.
    """
    daily = amount / days
    minimum = _bound(limits, key, "min")
    maximum = _bound(limits, key, "max")
    if minimum is not None and amount < minimum * days:
        return LimitBreach(key=key, daily=daily, bound=minimum, side="min")
    if maximum is not None and amount > maximum * days:
        return LimitBreach(key=key, daily=daily, bound=maximum, side="max")
    return None


def float_balance(amount: Decimal, spent: Decimal, returned: Decimal) -> Decimal:
    """What is left of a float (§4.17.2).

    May go negative, and then reads "owed to you": a float may be overspent
    (assumption 4), and settling that is a new request.
    """
    return amount - spent - returned
