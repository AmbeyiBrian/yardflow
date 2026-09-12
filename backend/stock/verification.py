"""Verifying the balance cache against the ledger (design §3.3, §13; T3.6).

§13: "Ledger drift — nightly ``verify_ledger``; **alert, never auto-correct**."

The "never auto-correct" is the important half. If drift appears, something wrote
a balance outside :func:`stock.services.post_movement`, or a transaction did not
commit as a unit. Silently rewriting the cache would hide the bug and leave the
real cause to corrupt the next figure too. So this reports and alerts, and a
person decides.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import Sum

from stock.models import Reel, SerialUnit, StockBalance, StockMovement

logger = logging.getLogger(__name__)


@dataclass
class Drift:
    """One disagreement between the cache and the ledger."""

    kind: str
    description: str
    cached: str
    ledger: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.description} — cached {self.cached}, ledger {self.ledger}"


@dataclass
class VerificationResult:
    balances_checked: int = 0
    serial_units_checked: int = 0
    reels_checked: int = 0
    drifts: list[Drift] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.drifts

    def summary(self) -> str:
        return (
            f"{self.balances_checked} balances, "
            f"{self.serial_units_checked} serial units and "
            f"{self.reels_checked} drums checked; "
            f"{len(self.drifts)} disagreement(s)."
        )


def _ledger_totals(organization_id) -> dict[tuple, Decimal]:
    """Recompute every balance from movements, in two queries.

    Two aggregate queries rather than one per balance: at 100,000 movements the
    per-balance approach would take minutes, and a nightly check that times out
    is a check nobody runs (N-2).
    """
    totals: dict[tuple, Decimal] = {}

    inbound = (
        StockMovement.objects.filter(organization_id=organization_id)
        .values("to_node_id", "item_type_id", "owner_client_id", "condition")
        .annotate(total=Sum("quantity"))
    )
    for row in inbound:
        key = (
            row["to_node_id"],
            row["item_type_id"],
            row["owner_client_id"],
            row["condition"],
        )
        totals[key] = totals.get(key, Decimal("0")) + row["total"]

    outbound = (
        StockMovement.objects.filter(organization_id=organization_id)
        .values("from_node_id", "item_type_id", "owner_client_id", "condition")
        .annotate(total=Sum("quantity"))
    )
    for row in outbound:
        key = (
            row["from_node_id"],
            row["item_type_id"],
            row["owner_client_id"],
            row["condition"],
        )
        totals[key] = totals.get(key, Decimal("0")) - row["total"]

    return totals


def verify_ledger(organization_id) -> VerificationResult:
    """Recompute balances from the ledger and report any drift (§3.3)."""
    result = VerificationResult()
    ledger = _ledger_totals(organization_id)

    # 1. Every cached balance must match the ledger.
    seen: set[tuple] = set()
    for balance in StockBalance.objects.filter(organization_id=organization_id).select_related(
        "node", "item_type"
    ):
        key = (balance.node_id, balance.item_type_id, balance.owner_client_id, balance.condition)
        seen.add(key)
        result.balances_checked += 1

        expected = ledger.get(key, Decimal("0"))
        if balance.quantity != expected:
            result.drifts.append(
                Drift(
                    kind="balance",
                    description=(
                        f"{balance.item_type} at {balance.node.label} "
                        f"({balance.condition})"
                    ),
                    cached=str(balance.quantity),
                    ledger=str(expected),
                )
            )

    # 2. And every ledger total must have a cached balance. A missing row is
    #    drift too: it reads as zero on every screen.
    for key, expected in ledger.items():
        if key in seen or expected == 0:
            continue
        result.drifts.append(
            Drift(
                kind="missing balance",
                description=f"node {key[0]}, item {key[1]}, condition {key[3]}",
                cached="(no row)",
                ledger=str(expected),
            )
        )

    # 3. §3.5: the denormalised position of each serialized unit and drum must
    #    match its latest movement. These are what the stock screens read, so
    #    drift here is visible to users even while balances look right.
    for unit in SerialUnit.objects.filter(organization_id=organization_id).select_related(
        "current_node"
    ):
        result.serial_units_checked += 1
        latest = unit.movements.order_by("-occurred_at", "-id").first()
        if latest is None:
            continue
        if latest.to_node_id != unit.current_node_id:
            result.drifts.append(
                Drift(
                    kind="serial unit position",
                    description=f"{unit.serial_number}",
                    cached=str(unit.current_node_id),
                    ledger=str(latest.to_node_id),
                )
            )

    for reel in Reel.objects.filter(organization_id=organization_id):
        result.reels_checked += 1
        consumed = reel.movements.filter(
            to_node__type__in=("SITE", "CONSUMED")
        ).aggregate(total=Sum("quantity"))["total"] or Decimal("0")
        expected_remaining = reel.initial_length - consumed
        if reel.remaining_length != expected_remaining:
            result.drifts.append(
                Drift(
                    kind="reel remainder",
                    description=f"drum {reel.drum_number}",
                    cached=str(reel.remaining_length),
                    ledger=str(expected_remaining),
                )
            )

    if result.drifts:
        # Loud, and never corrected here: drift means something wrote a balance
        # outside post_movement, and rewriting the cache would hide the cause.
        logger.error(
            "stock ledger drift detected",
            extra={
                "organization_id": str(organization_id),
                "drift_count": len(result.drifts),
                "drifts": [str(drift) for drift in result.drifts[:20]],
            },
        )

    return result
