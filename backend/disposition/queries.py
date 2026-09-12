"""Client-owned position, split by state (design §4.12, §10; K1, K3, M1).

T6.3 asks that returned material "leaves available stock but still reports under
the client's position **as in transit**", and T6.4 that "the client-owned position
report **distinguishes in-transit from acknowledged**".

That distinction cannot come from balances. A balance at the CLIENT node knows
how much is there and whose it is, but not *which return* put it there — and
acknowledgement is a property of the return, not of the quantity. So the split is
computed from the ledger: every movement into a CLIENT node names the gate-out
that caused it, and each of those gate-outs either has an acknowledgement or does
not.

Which is the honest way round anyway. "Has the client signed for it?" is a
question about a document, and answering it from a cached number would mean
maintaining a second truth about liability.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import Sum

from locations.models import NodeType


class ClientMaterialState:
    """Where a client's material stands with us (K1, K3)."""

    #: In our yard, or with one of our people. Ours to look after.
    HELD = "HELD"
    #: Sent back, not yet acknowledged. **Still our exposure** (K1).
    IN_TRANSIT = "IN_TRANSIT"
    #: They have signed for it. Liability ends here (K3).
    ACKNOWLEDGED = "ACKNOWLEDGED"


@dataclass
class ClientPositionRow:
    client_id: int
    client: str
    item_type_id: int
    item_type: str
    uom: str
    state: str
    quantity: Decimal = Decimal("0")
    #: The returns making up an IN_TRANSIT or ACKNOWLEDGED figure, so the row is
    #: something somebody can act on rather than a number to query about.
    documents: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "client_id": self.client_id,
            "client": self.client,
            "item_type_id": self.item_type_id,
            "item_type": self.item_type,
            "uom": self.uom,
            "state": self.state,
            "quantity": str(self.quantity),
            "documents": self.documents,
        }


def client_position(client=None) -> list[dict]:
    """Every client's material, split into held, in transit and acknowledged.

    One pass over held balances and one over returns, because they answer
    different halves of the same question and neither can be derived from the
    other.
    """
    rows: dict[tuple, ClientPositionRow] = {}

    _add_held(rows, client)
    _add_returns(rows, client)

    ordered = sorted(
        rows.values(),
        key=lambda row: (row.client, row.item_type, _STATE_ORDER.get(row.state, 9)),
    )
    return [row.as_dict() for row in ordered]


#: Held first, then what is out of our hands but still ours, then what is closed.
#: The reading order an owner wants: what am I looking after, what am I waiting
#: on, what is done.
_STATE_ORDER = {
    ClientMaterialState.HELD: 0,
    ClientMaterialState.IN_TRANSIT: 1,
    ClientMaterialState.ACKNOWLEDGED: 2,
}


def _key(row: ClientPositionRow) -> tuple:
    return (row.client_id, row.item_type_id, row.state)


def _row(rows, *, client_obj, item_type, state) -> ClientPositionRow:
    key = (client_obj.pk, item_type.pk, state)
    if key not in rows:
        rows[key] = ClientPositionRow(
            client_id=client_obj.pk,
            client=str(client_obj),
            item_type_id=item_type.pk,
            item_type=str(item_type),
            uom=item_type.uom,
            state=state,
        )
    return rows[key]


def _add_held(rows, client) -> None:
    """What we are holding of theirs: our locations and our people (K1, M1)."""
    from stock.models import StockBalance

    balances = StockBalance.objects.filter(
        owner_client__isnull=False,
        node__type__in=(NodeType.LOCATION, NodeType.PERSON),
    ).nonzero()
    if client is not None:
        balances = balances.filter(owner_client=client)

    for balance in balances.select_related("owner_client", "item_type", "node"):
        row = _row(
            rows,
            client_obj=balance.owner_client,
            item_type=balance.item_type,
            state=ClientMaterialState.HELD,
        )
        row.quantity += balance.quantity


def _add_returns(rows, client) -> None:
    """What has gone back, and whether they have signed for it (K1, K3).

    Read from movements into the CLIENT node rather than from the balance there,
    because the balance cannot say which return it came from — and without that,
    "acknowledged" is unanswerable.
    """
    from dispatch.models import GateOut
    from stock.models import StockMovement

    movements = StockMovement.objects.filter(
        to_node__type=NodeType.CLIENT,
        owner_client__isnull=False,
        document_type="dispatch.GateOut",
    )
    if client is not None:
        movements = movements.filter(owner_client=client)

    grouped = (
        movements.values("document_id", "owner_client", "item_type")
        .annotate(quantity=Sum("quantity"))
        .order_by()
    )
    if not grouped:
        return

    gate_out_ids = {entry["document_id"] for entry in grouped}
    gate_outs = {
        str(gate_out.pk): gate_out
        for gate_out in GateOut.objects.filter(pk__in=gate_out_ids).select_related(
            "client", "client_return_ack"
        )
    }

    from catalogue.models import ItemType
    from network.models import Client

    item_types = {
        item.pk: item
        for item in ItemType.objects.filter(
            pk__in={entry["item_type"] for entry in grouped}
        )
    }
    clients = {
        row.pk: row
        for row in Client.objects.filter(
            pk__in={entry["owner_client"] for entry in grouped}
        )
    }

    for entry in grouped:
        gate_out = gate_outs.get(str(entry["document_id"]))
        item_type = item_types.get(entry["item_type"])
        client_obj = clients.get(entry["owner_client"])
        if item_type is None or client_obj is None:
            continue

        acknowledged = gate_out is not None and hasattr(gate_out, "client_return_ack")
        state = (
            ClientMaterialState.ACKNOWLEDGED
            if acknowledged
            else ClientMaterialState.IN_TRANSIT
        )

        row = _row(rows, client_obj=client_obj, item_type=item_type, state=state)
        row.quantity += entry["quantity"]

        if gate_out is not None:
            ack = getattr(gate_out, "client_return_ack", None)
            row.documents.append(
                {
                    "gate_out_id": gate_out.pk,
                    "number": gate_out.number,
                    "released_at": (
                        gate_out.released_at.isoformat() if gate_out.released_at else None
                    ),
                    "acknowledged_ref": ack.acknowledged_ref if ack else "",
                    "acknowledged_at": (
                        ack.acknowledged_at.isoformat() if ack else None
                    ),
                }
            )


def exposure_for(client=None) -> Decimal:
    """What we are still answerable for: held plus in transit (K1, K3).

    The figure an operator audit is really asking about, and the reason
    acknowledged material is excluded rather than merely labelled.
    """
    return sum(
        (
            Decimal(row["quantity"])
            for row in client_position(client)
            if row["state"] != ClientMaterialState.ACKNOWLEDGED
        ),
        Decimal("0"),
    )
