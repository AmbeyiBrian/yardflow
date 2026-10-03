"""Reading the ledger (design §3.3, §3.4, §10; E1, E2, E3, M1).

Two kinds of read, and the difference matters:

* **Now** — served from the ``StockBalance`` cache, because "do we have it?" is
  asked constantly and must answer inside two seconds at 100,000 movements (N-2).
* **As at a past date** — computed from the ledger, never from the cache. The
  cache only knows the present; the ledger knows every moment (§3.4, M1).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from django.db.models import (
    Case,
    Count,
    DecimalField,
    F,
    IntegerField,
    OuterRef,
    Q,
    QuerySet,
    Subquery,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Coalesce

from catalogue.models import TrackingMode
from locations.models import UNAVAILABLE_NODE_TYPES, LocationType, StockNode
from stock.labels import read_label
from stock.models import (
    Box,
    BoxBulkContent,
    Reel,
    ReelStatus,
    SerialUnit,
    StockBalance,
    StockMovement,
)


def stock_on_hand(
    *,
    item_type=None,
    node=None,
    owner_client=None,
    condition: str | None = None,
    available_only: bool = True,
) -> QuerySet[StockBalance]:
    """Current stock, from the cache (E1).

    ``available_only`` excludes quarantine, sites, clients, consumed and scrap —
    what E1 means by "do we have it?" is what could actually be issued today
    (§3.3, J1).
    """
    balances = StockBalance.objects.available() if available_only else (
        StockBalance.objects.nonzero()
    )

    if item_type is not None:
        balances = balances.filter(item_type=item_type)
    if node is not None:
        balances = balances.filter(node=node)
    if condition is not None:
        balances = balances.filter(condition=condition)

    # D3: own stock and a named client's stock are different questions, and
    # `owner_client=None` legitimately means "own stock only".
    if owner_client is not None:
        balances = balances.filter(owner_client=owner_client)

    return with_drum_split(balances.select_related("item_type", "node", "owner_client"))


def with_drum_split(balances: QuerySet[StockBalance]) -> QuerySet[StockBalance]:
    """D10 (§7.3c): annotate ``on_drums`` and ``loose`` for reel items.

    One subquery for the whole list, not one per row. Null for every other
    item. ``loose`` is quantity less open drums' remaining length, as
    ``post_movement`` computes it.
    """
    dec = DecimalField(max_digits=14, decimal_places=3)

    def drums(owner_filter):  # type: ignore[no-untyped-def]
        return Subquery(
            Reel.objects.filter(
                current_node=OuterRef("node"),
                item_type=OuterRef("item_type"),
                condition=OuterRef("condition"),
                status=ReelStatus.OPEN,
                **owner_filter,
            )
            .order_by()
            .values("current_node")
            .annotate(total=Sum("remaining_length"))
            .values("total"),
            output_field=dec,
        )

    is_reel = Q(item_type__default_tracking_mode=TrackingMode.REEL)
    return balances.annotate(
        on_drums=Case(
            When(
                is_reel,
                then=Coalesce(
                    Case(
                        When(owner_client__isnull=True, then=drums({"owner_client__isnull": True})),
                        default=drums({"owner_client": OuterRef("owner_client")}),
                        output_field=dec,
                    ),
                    Value(Decimal("0"), output_field=dec),
                ),
            ),
            default=None,
            output_field=dec,
        )
    ).annotate(
        loose=Case(
            When(is_reel, then=F("quantity") - F("on_drums")),
            default=None,
            output_field=dec,
        )
    )


def stock_as_at(
    organization_id,
    moment: datetime,
    *,
    item_type=None,
    available_only: bool = True,
) -> list[dict]:
    """Stock as at a past date, computed from the ledger (§3.4, M1).

    M1 asks for "stock on hand — current, **and as at any past date**". This
    cannot come from the cache: the cache knows only the present. It is the
    aggregation in §3.4 — inbound minus outbound, up to the moment asked for.

    ``occurred_at`` rather than ``posted_at`` is deliberate: a delivery entered
    the following morning belongs to the day it arrived, and using the posting
    time would make yesterday's figure change overnight.
    """
    movements = StockMovement.objects.filter(
        organization_id=organization_id, occurred_at__lte=moment
    )
    if item_type is not None:
        movements = movements.filter(item_type=item_type)

    totals: dict[tuple, Decimal] = {}

    inbound = movements.values(
        "to_node_id", "item_type_id", "owner_client_id", "condition", "uom"
    ).annotate(total=Sum("quantity"))
    for row in inbound:
        key = (
            row["to_node_id"],
            row["item_type_id"],
            row["owner_client_id"],
            row["condition"],
            row["uom"],
        )
        totals[key] = totals.get(key, Decimal("0")) + row["total"]

    outbound = movements.values(
        "from_node_id", "item_type_id", "owner_client_id", "condition", "uom"
    ).annotate(total=Sum("quantity"))
    for row in outbound:
        key = (
            row["from_node_id"],
            row["item_type_id"],
            row["owner_client_id"],
            row["condition"],
            row["uom"],
        )
        totals[key] = totals.get(key, Decimal("0")) - row["total"]

    node_ids = {key[0] for key in totals}
    nodes = {
        node.pk: node
        for node in StockNode.objects.filter(pk__in=node_ids).select_related("location")
    }

    rows: list[dict] = []
    for (node_id, item_type_id, owner_client_id, condition, uom), quantity in totals.items():
        if quantity == 0:
            continue

        node = nodes.get(node_id)
        if available_only and node is not None:
            if node.type in UNAVAILABLE_NODE_TYPES:
                continue
            if node.location is not None and node.location.type == LocationType.QUARANTINE:
                continue

        rows.append(
            {
                "node_id": node_id,
                "node_label": node.label if node else "",
                "item_type_id": item_type_id,
                "owner_client_id": owner_client_id,
                "condition": condition,
                "uom": uom,
                "quantity": quantity,
            }
        )

    return sorted(rows, key=lambda row: (row["node_label"], row["item_type_id"]))


def serial_history(serial_number: str) -> QuerySet[StockMovement]:
    """Every movement of one unit, oldest first (E2, T3.14).

    E2 calls this "the single most likely question from an operator audit", and
    T3.14 agrees. It has to answer with the whole life of the unit — received,
    issued, installed, recovered, returned, quarantined, disposed — in order.
    """
    return (
        StockMovement.objects.filter(serial_unit__serial_number=serial_number)
        .select_related(
            "from_node", "to_node", "item_type", "owner_client", "posted_by", "serial_unit"
        )
        .order_by("occurred_at", "id")
    )


def reel_history(drum_number: str) -> QuerySet[StockMovement]:
    """Every movement of one drum, oldest first (E3, T3.14)."""
    return (
        StockMovement.objects.filter(reel__drum_number=drum_number)
        .select_related("from_node", "to_node", "item_type", "owner_client", "posted_by", "reel")
        .order_by("occurred_at", "id")
    )


def item_history(item_type, *, node=None) -> QuerySet[StockMovement]:
    """Movement history for an item type (M1)."""
    movements = StockMovement.objects.filter(item_type=item_type)
    if node is not None:
        movements = movements.filter(Q(from_node=node) | Q(to_node=node))
    return movements.select_related("from_node", "to_node", "owner_client", "posted_by")


def _find_exact(identifier: str) -> dict | None:
    unit = (
        SerialUnit.objects.filter(
            Q(serial_number__iexact=identifier) | Q(asset_tag__iexact=identifier)
        )
        .select_related("item_type", "current_node", "owner_client")
        .first()
    )
    if unit is not None:
        return {"kind": "serial_unit", "object": unit}

    reel = (
        Reel.objects.filter(drum_number__iexact=identifier)
        .select_related("item_type", "current_node", "owner_client")
        .first()
    )
    if reel is not None:
        return {"kind": "reel", "object": reel}

    return None


def _find_box(code: str) -> dict | None:
    box = Box.objects.filter(code__iexact=code).select_related("current_node").first()
    if box is not None:
        return {"kind": "box", "object": box}
    return None


def annotated_boxes() -> QuerySet[Box]:
    """Boxes with their live counts, in one query (P4).

    ``units_now`` and ``bulk_lines_now`` cover the box and everything inside it
    (boxes nest three deep at most, P10), the same "now" as ``box_tree``. They
    are subqueries rather than joins: three joined counts multiply each other.
    """

    def within(model, path: str):  # type: ignore[no-untyped-def]
        return (
            model.objects.filter(
                Q(**{path: OuterRef("pk")})
                | Q(**{f"{path}__parent": OuterRef("pk")})
                | Q(**{f"{path}__parent__parent": OuterRef("pk")})
            )
            .order_by()
            .values("organization_id")
            .annotate(n=Count("pk"))
            .values("n")
        )

    return Box.objects.select_related("current_node", "parent").annotate(
        units_now=Coalesce(
            Subquery(within(SerialUnit, "box"), output_field=IntegerField()), 0
        ),
        bulk_lines_now=Coalesce(
            Subquery(within(BoxBulkContent, "box"), output_field=IntegerField()), 0
        ),
    )


def find_by_identifier(identifier: str) -> dict | None:
    """Resolve a scanned or typed identifier to whatever it is (E1, E2, D7, P2).

    A storekeeper with a barcode does not know whether it is a serial number, an
    internal asset tag or a drum number — and should not have to choose a search
    mode before scanning. One lookup covers all three.

    The value as given is tried first, so a plain serial behaves exactly as it
    always did. Only if that finds nothing is the label read (§4.15.6) and each
    serial in it tried in turn — a GS1 barcode or a product link then finds the
    unit it names. A box code is resolved last; gate-pass tokens are not resolved here.
    """
    scanned = identifier or ""
    identifier = scanned.strip()
    if not identifier:
        return None

    found = _find_exact(identifier) or _find_box(identifier)
    if found is not None:
        return found

    # The unstripped value: a GS1 string can end in a separator that strip() eats.
    reading = read_label(scanned)
    for serial in reading.serials:
        if serial == identifier:
            continue
        found = _find_exact(serial)
        if found is not None:
            return found

    if reading.box_code and reading.box_code != identifier:
        return _find_box(reading.box_code)

    return None


def custody_holdings(user=None) -> QuerySet[StockBalance]:
    """What people are currently holding (I1).

    Custody is simply the balance at a PERSON node (§4.10) — there is no separate
    custody ledger, which is why "what does this technician have?" and "what is in
    the yard?" can never disagree.
    """
    from locations.models import NodeType

    balances = StockBalance.objects.filter(node__type=NodeType.PERSON).nonzero()
    if user is not None:
        balances = balances.filter(node__user=user)
    return balances.select_related("node", "node__user", "item_type", "owner_client")


def installed_base(site=None) -> QuerySet[StockBalance]:
    """What is installed at a site (H2, M1).

    The same trick as custody: an installation is a balance at the site's node
    (§3.1), not a table of its own. So "what did we put in at Kileleshwa?" is
    answered by the ledger that recorded putting it there, and the answer cannot
    drift from the movements behind it.
    """
    from locations.models import NodeType

    balances = StockBalance.objects.filter(node__type=NodeType.SITE).nonzero()
    if site is not None:
        balances = balances.filter(node__site=site)
    return balances.select_related("node", "node__site", "item_type", "owner_client")


def installed_serials(site=None) -> QuerySet:
    """Serialized units currently installed (D3, H2, M1).

    A serial's whereabouts is denormalised onto the unit (§3.3), so the installed
    base for tracked equipment is a direct lookup rather than a ledger scan.
    """
    from locations.models import NodeType
    from stock.models import SerialUnit

    units = SerialUnit.objects.filter(current_node__type=NodeType.SITE)
    if site is not None:
        units = units.filter(current_node__site=site)
    return units.select_related("item_type", "current_node", "current_node__site")


#: Node types that count towards a client's position (M1).
#:
#: The counterparty nodes must be excluded or the answer is always zero: a
#: consignment receipt posts +10 at the yard and -10 at the client's issuing
#: store, and summing both nets to nothing. What an operator is asking is "how
#: much of my material are you holding?", so:
#:
#:  * LOCATION and PERSON count — it is in the yard, on a vehicle, or with a
#:    technician, and either way we have it
#:  * CLIENT counts — in transit back to them, and still our exposure until they
#:    acknowledge receipt (K3)
#:  * EXTERNAL, SITE, CONSUMED and SCRAP do not — respectively: never ours,
#:    installed into their network, used up, and destroyed
CLIENT_POSITION_NODE_TYPES = ("LOCATION", "PERSON", "CLIENT")


def client_owned_position(client=None) -> QuerySet[StockBalance]:
    """Client-owned stock we are holding, per client (M1).

    What an operator audit opens with.
    """
    balances = (
        StockBalance.objects.filter(
            owner_client__isnull=False, node__type__in=CLIENT_POSITION_NODE_TYPES
        )
        .nonzero()
    )
    if client is not None:
        balances = balances.filter(owner_client=client)
    return balances.select_related("owner_client", "item_type", "node")


def below_minimum_stock(organization_id) -> list[dict]:
    """Items whose available quantity is under their reorder level (E6).

    Only meaningful when the tenant has minimum stock enabled (C8); the caller
    checks that. Returns a row per item type rather than per node, because a
    reorder decision is about the yard as a whole.
    """
    from catalogue.models import ItemType

    items = ItemType.objects.filter(
        organization_id=organization_id, is_archived=False, min_stock_qty__isnull=False
    )

    low: list[dict] = []
    for item in items:
        available = stock_on_hand(item_type=item).aggregate(total=Sum("quantity"))[
            "total"
        ] or Decimal("0")
        if available < item.min_stock_qty:
            low.append(
                {
                    "item_type_id": item.pk,
                    "item_type": item.name,
                    "available": available,
                    "minimum": item.min_stock_qty,
                    "shortfall": item.min_stock_qty - available,
                    "uom": item.uom,
                }
            )
    return low
