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

from django.db.models import F, Sum

from stock.models import (
    Box,
    BoxBulkContent,
    BoxStatus,
    Reel,
    SerialUnit,
    StockBalance,
    StockMovement,
)

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
    boxes_checked: int = 0
    drifts: list[Drift] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.drifts

    def summary(self) -> str:
        return (
            f"{self.balances_checked} balances, "
            f"{self.serial_units_checked} serial units and "
            f"{self.reels_checked} drums and "
            f"{self.boxes_checked} boxes checked; "
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


def _verify_boxes(organization_id, result: VerificationResult) -> None:
    """Check the box projection against the ledger (§4.15.1, §4.15.12; P8).

    A box is a projection kept beside the ledger, so when it disagrees the
    ledger wins and the disagreement is reported, never repaired. Each check is
    one aggregate query or one pass over the org's boxes in memory, never a
    query per box: boxes are many and a nightly check must stay cheap (N-2).
    """
    # 1. A unit in a box sits where the box is, and the box is open. §4.15.3
    #    rule 1 clears a unit's box when it moves; a unit that kept it has
    #    drifted from the box it claims.
    for unit in SerialUnit.objects.filter(
        organization_id=organization_id, box__isnull=False
    ).select_related("box"):
        box = unit.box
        if unit.current_node_id != box.current_node_id:
            result.drifts.append(
                Drift(
                    kind="box unit position",
                    description=f"unit {unit.serial_number} in box {box.code}",
                    cached=f"unit at {unit.current_node_id}",
                    ledger=f"box at {box.current_node_id}",
                )
            )
        if box.status != BoxStatus.OPEN:
            result.drifts.append(
                Drift(
                    kind="box unit in closed box",
                    description=f"unit {unit.serial_number} in box {box.code}",
                    cached=f"box {box.status}",
                    ledger="a unit's box must be open",
                )
            )

    # 2. Claims are slices of a lot, so the open boxes at a node can never claim
    #    more of a lot than the balance holds. A missing balance row is zero.
    balances = {
        (row["node_id"], row["item_type_id"], row["owner_client_id"], row["condition"]): row[
            "quantity"
        ]
        for row in StockBalance.objects.filter(organization_id=organization_id).values(
            "node_id", "item_type_id", "owner_client_id", "condition", "quantity"
        )
    }
    claims = (
        BoxBulkContent.objects.filter(organization_id=organization_id, box__status=BoxStatus.OPEN)
        .values(
            node=F("box__current_node_id"),
            item=F("item_type_id"),
            owner=F("owner_client_id"),
            cond=F("condition"),
        )
        .annotate(total=Sum("quantity"))
    )
    for row in claims:
        key = (row["node"], row["item"], row["owner"], row["cond"])
        held = balances.get(key, Decimal("0"))
        if row["total"] > held:
            result.drifts.append(
                Drift(
                    kind="box claims exceed balance",
                    description=f"node {row['node']}, item {row['item']}, condition {row['cond']}",
                    cached=f"claimed {row['total']}",
                    ledger=f"balance {held}",
                )
            )

    # 3. Tree shape, from one query: no cycles, depth 1-3 and parent's + 1, a
    #    child where its parent is, and an open child never under a closed
    #    parent (P10).
    boxes = {box.pk: box for box in Box.objects.filter(organization_id=organization_id)}
    result.boxes_checked += len(boxes)
    for box in boxes.values():
        parent = boxes.get(box.parent_id) if box.parent_id else None

        # Walk up with a visited set, so a loop in the data cannot hang the check.
        visited = {box.pk}
        ancestor = parent
        while ancestor is not None:
            if ancestor.pk in visited:
                result.drifts.append(
                    Drift(
                        kind="box cycle",
                        description=f"box {box.code}",
                        cached=f"parent {box.parent_id}",
                        ledger="a box tree has no cycles",
                    )
                )
                break
            visited.add(ancestor.pk)
            ancestor = boxes.get(ancestor.parent_id) if ancestor.parent_id else None

        expected_depth = parent.depth + 1 if parent is not None else 1
        if box.depth != expected_depth or not 1 <= box.depth <= 3:
            result.drifts.append(
                Drift(
                    kind="box depth",
                    description=f"box {box.code}",
                    cached=str(box.depth),
                    ledger=f"{expected_depth} (1-3 allowed)",
                )
            )
        if parent is None:
            continue
        if box.current_node_id != parent.current_node_id:
            result.drifts.append(
                Drift(
                    kind="box child location",
                    description=f"box {box.code} in box {parent.code}",
                    cached=f"child at {box.current_node_id}",
                    ledger=f"parent at {parent.current_node_id}",
                )
            )
        if box.status == BoxStatus.OPEN and parent.status == BoxStatus.CLOSED:
            result.drifts.append(
                Drift(
                    kind="open box in closed box",
                    description=f"box {box.code} in box {parent.code}",
                    cached="child OPEN",
                    ledger="parent CLOSED",
                )
            )

    # 4. P5: a closed box holds nothing. Two set queries, not one per box.
    closed = {pk: box for pk, box in boxes.items() if box.status == BoxStatus.CLOSED}
    if closed:
        holding = {
            "units": set(
                SerialUnit.objects.filter(
                    organization_id=organization_id, box_id__in=closed
                ).values_list("box_id", flat=True)
            ),
            "bulk contents": set(
                BoxBulkContent.objects.filter(
                    organization_id=organization_id, box_id__in=closed
                ).values_list("box_id", flat=True)
            ),
            "open child boxes": {
                box.parent_id
                for box in boxes.values()
                if box.status == BoxStatus.OPEN and box.parent_id in closed
            },
        }
        for what, box_ids in holding.items():
            for box_id in sorted(box_ids):
                result.drifts.append(
                    Drift(
                        kind="closed box not empty",
                        description=f"box {closed[box_id].code} still holds {what}",
                        cached=f"CLOSED with {what}",
                        ledger="a closed box holds nothing",
                    )
                )


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

    # 4. §4.15 (P8): the box projection against the ledger.
    _verify_boxes(organization_id, result)

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
