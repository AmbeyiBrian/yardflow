"""What the yard is worth (design §10; O11, D27).

The ledger has carried a unit cost on every own-stock movement since phase 10,
and the catalogue carries a unit cost per item type. Nobody had asked either
what the material on hand adds up to. This does.

Two rules, both stated on the screen rather than left to be discovered:

* **Own stock only.** Client-owned material sits in the yard but is not ours to
  value; it appears in the client-position report as quantity, and pricing it
  here would inflate the figure by somebody else's assets.
* **Valued at the catalogue cost, and marked where there is none.** An item
  type with no unit cost contributes a quantity and no value, and the row says
  so — the same honesty §10 applies to project cost. A total that quietly
  skipped the unpriced items would be a total nobody should sign.
"""

from __future__ import annotations

from decimal import Decimal

from accounts.permissions_registry import PERM
from reporting.framework import Filter, Report, flag, money, quantity, register, text

ZERO = Decimal("0")


@register
class StockValuationReport(Report):
    """O11: own stock on hand, at catalogue cost, by location and item."""

    slug = "stock-valuation"
    title = "Stock valuation"
    category = "Finance"
    description = (
        "Own material on hand valued at its catalogue unit cost, by location "
        "and item. Client-owned stock is excluded — it is not ours to value. "
        "Items with no unit cost are listed and flagged rather than silently "
        "left out of the total."
    )
    requirement = "O11"
    required_permission = PERM.PROJECT_VIEW_COST
    can_be_large = False

    filters = (
        Filter("location", "Location", kind="reference", resource="locations"),
        Filter("item_type", "Item", kind="reference", resource="item-types"),
        Filter(
            "valued",
            "Show",
            kind="choice",
            choices=(
                ("all", "Everything"),
                ("valued", "Priced items only"),
                ("unvalued", "Unpriced items only"),
            ),
        ),
    )

    columns = (
        text("location", "Location", width=22),
        text("item", "Item", width=28),
        text("condition", "Condition", width=12, wide_only=True),
        quantity("on_hand", "On hand"),
        text("uom", "Unit", width=8, wide_only=True),
        money("unit_cost", "Unit cost", wide_only=True),
        money("value", "Value"),
        flag("is_valued", "Priced"),
    )

    def query(self, params: dict):
        from locations.models import NodeType
        from stock.models import StockBalance

        queryset = (
            StockBalance.objects.filter(
                node__type=NodeType.LOCATION,
                owner_client__isnull=True,
                quantity__gt=0,
            )
            .select_related("node__location", "item_type")
            .order_by("node__location__name", "item_type__name", "condition")
        )
        if params.get("location"):
            queryset = queryset.filter(node__location_id=params["location"])
        if params.get("item_type"):
            queryset = queryset.filter(item_type_id=params["item_type"])
        if params.get("valued") == "valued":
            queryset = queryset.filter(item_type__unit_cost__isnull=False)
        elif params.get("valued") == "unvalued":
            queryset = queryset.filter(item_type__unit_cost__isnull=True)
        return queryset

    def rows(self, params: dict):
        for balance in self.query(params):
            item = balance.item_type
            unit_cost = item.unit_cost
            yield {
                "location": balance.node.location.name if balance.node.location else "",
                "item": item.name,
                "condition": balance.get_condition_display(),
                "on_hand": balance.quantity,
                "uom": item.uom,
                "unit_cost": unit_cost,
                "value": (balance.quantity * unit_cost) if unit_cost is not None else None,
                "is_valued": unit_cost is not None,
            }

    def totals(self, rows):
        # Priced rows only. The unpriced ones are each flagged on their own
        # line; a count under a yes/no column would render as "yes".
        return {"value": sum((row["value"] for row in rows if row["value"] is not None), ZERO)}
