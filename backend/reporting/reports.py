"""The day-one report set (design §10; M1, T7.2–T7.4).

M1 lists nine reports an operator audit and the internal ISO requirement need.
Each is a class here, and nothing outside this module knows any of their names —
the API, the Excel writer and the PDF renderer read the registry (T7.1).

Every one of these is deliberately a thin wrapper over a query that already
existed and is already tested. A report that re-derived "what is in the yard" its
own way would be a second answer to a question the ledger has already answered,
and the two would drift. So `stock_on_hand`, `stock_as_at`, `reconcile_site`,
`client_position` and the rest are called, not reimplemented — that is also what
makes T7.3's criterion ("the consumption report reconciles with T5.7") true by
construction rather than by luck.
"""

from __future__ import annotations

from decimal import Decimal

from django.utils import timezone

from reporting.framework import (
    Filter,
    Report,
    flag,
    integer,
    money,
    on_date,
    quantity,
    register,
    text,
    when,
)

# --------------------------------------------------------------------------
# Shared filters. Declared once so the panels stay consistent between reports.
# --------------------------------------------------------------------------

ITEM_FILTER = Filter("item_type", "Item", kind="reference", resource="item-types")
CLIENT_FILTER = Filter("client", "Client", kind="reference", resource="clients")
SITE_FILTER = Filter("site", "Site", kind="reference", resource="sites")
LOCATION_FILTER = Filter("location", "Location", kind="reference", resource="locations")
HOLDER_FILTER = Filter("holder", "Holder", kind="reference", resource="users")
FROM_FILTER = Filter("from_date", "From", kind="date")
TO_FILTER = Filter("to_date", "To", kind="date")


def _client_name(row) -> str:
    """E1: client-owned material is distinguishable wherever it appears."""
    return str(row.owner_client) if row.owner_client_id else "Own stock"


# --------------------------------------------------------------------------
# T7.2 — stock
# --------------------------------------------------------------------------


@register
class StockOnHandReport(Report):
    """M1: "stock on hand — current"."""

    slug = "stock-on-hand"
    title = "Stock on hand"
    description = "What is in the yard now and could be issued today."
    requirement = "M1"
    columns = (
        text("item_type", "Item", width=34),
        text("node_label", "Where", width=24),
        text("condition", "Condition", width=16),
        text("owner", "Owner", width=22),
        quantity("quantity", "Quantity"),
        text("uom", "UoM", width=8),
    )
    filters = (ITEM_FILTER, LOCATION_FILTER, CLIENT_FILTER)

    def rows(self, params: dict):
        from locations.models import StockNode
        from stock.queries import stock_on_hand

        balances = stock_on_hand(
            item_type=params.get("item_type"),
            owner_client=params.get("client"),
        )
        location_id = params.get("location")
        if location_id:
            node_ids = StockNode.objects.filter(location_id=location_id).values_list(
                "pk", flat=True
            )
            balances = balances.filter(node_id__in=list(node_ids))

        for balance in balances.order_by("item_type__name", "node__label"):
            yield {
                "item_type": str(balance.item_type),
                "node_label": balance.node.label,
                "condition": balance.get_condition_display(),
                "owner": _client_name(balance),
                "quantity": balance.quantity,
                "uom": balance.uom,
            }


@register
class StockAsAtReport(Report):
    """M1: "and **as at any past date**".

    Computed from the ledger rather than the cache, because the cache knows only
    the present (§3.4). This is the report that answers "what did we have on the
    31st?" three months later — the question an auditor actually asks.
    """

    slug = "stock-as-at"
    title = "Stock as at a date"
    description = "What the yard held on a past date, recomputed from the ledger."
    requirement = "M1"
    columns = (
        text("item_type", "Item", width=34),
        text("node_label", "Where", width=24),
        text("condition", "Condition", width=16),
        quantity("quantity", "Quantity"),
        text("uom", "UoM", width=8),
    )
    filters = (
        Filter(
            "as_at",
            "As at",
            kind="datetime",
            required=True,
            help_text="Computed from movements up to this moment, so it never changes afterwards.",
        ),
        ITEM_FILTER,
    )

    def rows(self, params: dict):
        from catalogue.models import ItemType
        from core.tenancy import require_current_organization_id
        from stock.queries import stock_as_at

        organization_id = require_current_organization_id()
        item_type = None
        if params.get("item_type"):
            item_type = ItemType.objects.filter(pk=params["item_type"]).first()

        rows = stock_as_at(organization_id, params["as_at"], item_type=item_type)
        names = {
            item.pk: str(item)
            for item in ItemType.objects.filter(
                pk__in={row["item_type_id"] for row in rows}
            )
        }

        for row in rows:
            yield {
                "item_type": names.get(row["item_type_id"], ""),
                "node_label": row["node_label"],
                "condition": row["condition"],
                "quantity": row["quantity"],
                "uom": row["uom"],
            }


@register
class MovementHistoryReport(Report):
    """M1: "movement history by item, by serial, by drum"."""

    slug = "movement-history"
    title = "Movement history"
    description = "Every movement, filtered by item, serial number or drum."
    requirement = "M1"
    columns = (
        when("occurred_at", "When"),
        text("movement_type", "What", width=14),
        text("item_type", "Item", width=30),
        text("identifier", "Serial / drum", width=22),
        quantity("quantity", "Quantity"),
        text("uom", "UoM", width=8),
        text("from_label", "From", width=22),
        text("to_label", "To", width=22),
        text("document_number", "Document", width=16),
        text("posted_by", "Who", width=20, wide_only=True),
    )
    filters = (
        ITEM_FILTER,
        Filter("serial", "Serial number"),
        Filter("drum", "Drum number"),
        FROM_FILTER,
        TO_FILTER,
    )

    def query(self, params: dict):
        from stock.models import StockMovement

        movements = StockMovement.objects.all()
        if params.get("item_type"):
            movements = movements.filter(item_type_id=params["item_type"])
        if params.get("serial"):
            movements = movements.filter(
                serial_unit__serial_number__iexact=params["serial"]
            )
        if params.get("drum"):
            movements = movements.filter(reel__drum_number__iexact=params["drum"])
        if params.get("from_date"):
            movements = movements.filter(occurred_at__date__gte=params["from_date"])
        if params.get("to_date"):
            movements = movements.filter(occurred_at__date__lte=params["to_date"])
        return movements.select_related(
            "item_type", "from_node", "to_node", "serial_unit", "reel", "posted_by"
        ).order_by("-occurred_at", "-id")

    def rows(self, params: dict):
        for movement in self.query(params):
            yield {
                "occurred_at": movement.occurred_at,
                "movement_type": movement.get_movement_type_display(),
                "item_type": str(movement.item_type),
                "identifier": (
                    movement.serial_unit.serial_number
                    if movement.serial_unit_id
                    else movement.reel.drum_number
                    if movement.reel_id
                    else ""
                ),
                "quantity": movement.quantity,
                "uom": movement.uom,
                "from_label": movement.from_node.label if movement.from_node_id else "",
                "to_label": movement.to_node.label,
                "document_number": movement.document_number,
                "posted_by": str(movement.posted_by) if movement.posted_by_id else "system",
            }

    def totals(self, rows):
        """No total: summing movements across units and directions is meaningless.

        A hundred metres issued and a hundred returned is not two hundred of
        anything, and a footer saying so would be worse than none.
        """
        return None


@register
class SerialHistoryReport(Report):
    """E2, M1: one unit's whole life, oldest first."""

    slug = "serial-history"
    title = "Serial history"
    description = "Everywhere one identified unit has been, in order."
    requirement = "M1, E2"
    can_be_large = False
    columns = (
        when("occurred_at", "When"),
        text("movement_type", "What", width=14),
        text("from_label", "From", width=24),
        text("to_label", "To", width=24),
        text("condition", "Condition", width=16),
        text("document_number", "Document", width=16),
        text("posted_by", "Who", width=20),
    )
    filters = (
        Filter(
            "serial",
            "Serial number",
            required=True,
            help_text="The number on the label.",
        ),
    )

    def rows(self, params: dict):
        from stock.queries import serial_history

        for movement in serial_history(params["serial"]):
            yield {
                "occurred_at": movement.occurred_at,
                "movement_type": movement.get_movement_type_display(),
                "from_label": movement.from_node.label if movement.from_node_id else "",
                "to_label": movement.to_node.label,
                "condition": movement.get_condition_display(),
                "document_number": movement.document_number,
                "posted_by": str(movement.posted_by) if movement.posted_by_id else "system",
            }


@register
class ClientPositionReport(Report):
    """M1: "client-owned stock position, per client".

    Three states rather than one figure (K1, K3). An operator audit asks what we
    have of theirs; the defensible answer separates what we are holding from what
    is on its way back and what they have already signed for.
    """

    slug = "client-position"
    title = "Client-owned position"
    description = "Per client: held, in transit back to them, and acknowledged."
    requirement = "M1, K1, K3"
    columns = (
        text("client", "Client", width=26),
        text("item_type", "Item", width=32),
        text("state", "State", width=16),
        quantity("quantity", "Quantity"),
        text("uom", "UoM", width=8),
        text("documents", "Returns", width=24, wide_only=True),
    )
    filters = (CLIENT_FILTER,)

    STATE_LABELS = {
        "HELD": "We hold it",
        "IN_TRANSIT": "In transit to them",
        "ACKNOWLEDGED": "They signed for it",
    }

    def rows(self, params: dict):
        from disposition.queries import client_position
        from network.models import Client

        client = None
        if params.get("client"):
            client = Client.objects.filter(pk=params["client"]).first()

        for row in client_position(client):
            yield {
                "client": row["client"],
                "item_type": row["item_type"],
                "state": self.STATE_LABELS.get(row["state"], row["state"]),
                "quantity": Decimal(row["quantity"]),
                "uom": row["uom"],
                "documents": ", ".join(
                    entry["number"] for entry in row["documents"] if entry["number"]
                ),
            }


# --------------------------------------------------------------------------
# T7.3 — movement and accountability
# --------------------------------------------------------------------------


@register
class OutstandingGateOutsReport(Report):
    """M1: "outstanding gate-outs" — approved and not yet fully released."""

    slug = "outstanding-gate-outs"
    title = "Outstanding gate passes"
    description = "Approved or part-released, and still waiting to leave."
    requirement = "M1, F7"
    columns = (
        text("number", "Number", width=14),
        text("status", "Status", width=18),
        text("destination", "Destination", width=26),
        text("custody_holder", "For", width=22),
        integer("lines", "Lines"),
        quantity("outstanding", "Outstanding"),
        on_date("approved_at", "Approved"),
        on_date("expires_at", "Expires"),
        flag("is_expired", "Expired"),
    )
    filters = (Filter("site", "Site", kind="reference", resource="sites"),)

    def rows(self, params: dict):
        from dispatch.models import GateOut, GateOutStatus

        gate_outs = GateOut.objects.filter(
            status__in=(GateOutStatus.APPROVED, GateOutStatus.PARTIALLY_RELEASED)
        )
        if params.get("site"):
            gate_outs = gate_outs.filter(site_id=params["site"])

        for gate_out in gate_outs.select_related(
            "site", "client", "to_location", "custody_holder"
        ).prefetch_related("lines"):
            lines = list(gate_out.lines.all())
            yield {
                "number": gate_out.number,
                "status": gate_out.get_status_display(),
                "destination": gate_out.destination_label,
                "custody_holder": str(gate_out.custody_holder),
                "lines": len(lines),
                "outstanding": sum(
                    (line.outstanding_qty for line in lines), Decimal("0")
                ),
                "approved_at": gate_out.approved_at,
                "expires_at": gate_out.expires_at,
                "is_expired": gate_out.is_expired,
            }


@register
class OverdueCustodyReport(Report):
    """M1: "overdue returns and current custody" (I3, I4)."""

    slug = "overdue-custody"
    title = "Overdue returns and custody"
    description = "Who is holding what, and what is past its return date."
    requirement = "M1, I3, I4"
    columns = (
        text("holder", "Holder", width=24),
        text("item_type", "Item", width=32),
        quantity("quantity", "Quantity"),
        text("uom", "UoM", width=8),
        on_date("due", "Due back"),
        integer("days_overdue", "Days late"),
        text("state", "State", width=16),
    )
    filters = (
        HOLDER_FILTER,
        Filter(
            "overdue_only",
            "Overdue only",
            kind="choice",
            choices=(("true", "Only what is late"), ("false", "Everything held")),
        ),
    )

    def rows(self, params: dict):
        from custody.models import CustodyExpectation, ExpectationStatus
        from stock.queries import custody_holdings

        overdue_only = str(params.get("overdue_only", "false")).lower() == "true"
        today = timezone.now().date()

        expectations = CustodyExpectation.objects.filter(
            status__in=(ExpectationStatus.OPEN, ExpectationStatus.OVERDUE)
        ).select_related("holder", "item_type")
        if params.get("holder"):
            expectations = expectations.filter(holder_id=params["holder"])

        expected_keys = set()
        for expectation in expectations:
            late = (
                (today - expectation.expected_return_date).days
                if expectation.expected_return_date
                else 0
            )
            if overdue_only and late <= 0:
                continue
            expected_keys.add((expectation.holder_id, expectation.item_type_id))
            yield {
                "holder": str(expectation.holder),
                "item_type": str(expectation.item_type),
                "quantity": expectation.outstanding_quantity,
                "uom": expectation.item_type.uom,
                "due": expectation.expected_return_date,
                "days_overdue": max(late, 0),
                "state": "Overdue" if late > 0 else "Due back",
            }

        if overdue_only:
            return

        # I1, §4.10: custody is a balance at a PERSON node. Material held with no
        # return date attached is still held, and a report that showed only the
        # expectations would understate what is out there.
        holdings = custody_holdings()
        if params.get("holder"):
            holdings = holdings.filter(node__user_id=params["holder"])
        for balance in holdings:
            if (balance.node.user_id, balance.item_type_id) in expected_keys:
                continue
            yield {
                "holder": balance.node.label,
                "item_type": str(balance.item_type),
                "quantity": balance.quantity,
                "uom": balance.uom,
                "due": None,
                "days_overdue": 0,
                "state": "Held",
            }


@register
class ConsumptionReport(Report):
    """M1: "consumption per site and per project".

    Reads `reconcile_site` and `reconcile_project` — the same functions T5.7
    is tested against. That is what makes T7.3's criterion ("the consumption
    report reconciles with T5.7") structural rather than coincidental: there is
    only one implementation of the four figures.
    """

    slug = "consumption"
    title = "Consumption per site or project"
    description = "Issued, installed, consumed, returned — and what is unexplained."
    requirement = "M1, H4"
    columns = (
        text("item_type", "Item", width=32),
        quantity("issued", "Issued"),
        quantity("installed", "Installed"),
        quantity("consumed", "Consumed"),
        quantity("returned", "Returned"),
        quantity("unaccounted", "Unaccounted"),
        text("uom", "UoM", width=8),
    )
    filters = (
        SITE_FILTER,
        Filter("project", "Project", kind="reference", resource="projects"),
    )

    def rows(self, params: dict):
        from jobs.reconciliation import reconcile_project, reconcile_site
        from network.models import Project, Site

        if params.get("site"):
            site = Site.objects.filter(pk=params["site"]).first()
            result = reconcile_site(site) if site else {"items": []}
        elif params.get("project"):
            order = Project.objects.filter(pk=params["project"]).first()
            result = reconcile_project(order) if order else {"items": []}
        else:
            # Neither named: every site with anything issued against it. Slower,
            # but "which sites have material unaccounted for?" is the question an
            # owner opens this report to ask.
            result = {"items": []}
            for site in Site.objects.all():
                for item in reconcile_site(site)["items"]:
                    result["items"].append({**item, "site": str(site)})

        for item in result["items"]:
            yield {
                "item_type": item["item_type"],
                "issued": item["issued"],
                "installed": item["installed"],
                "consumed": item["consumed"],
                "returned": item["returned"],
                "unaccounted": item["unaccounted"],
                "uom": item["uom"],
            }


@register
class ExceptionsReport(Report):
    """M1: "variance and exceptions register"."""

    slug = "exceptions"
    title = "Variance and exceptions register"
    description = "Everything unresolved: variances, short releases, overdue custody."
    requirement = "M1, H3"
    columns = (
        text("kind", "Kind", width=18),
        text("reference", "Reference", width=26),
        text("summary", "What happened", width=44),
        quantity("expected", "Expected"),
        quantity("actual", "Actual"),
        text("uom", "UoM", width=8),
        when("raised_at", "Raised"),
    )
    filters = (
        Filter(
            "kind",
            "Kind",
            kind="choice",
            choices=(
                ("variance", "Variances"),
                ("release_variance", "Short releases"),
                ("custody", "Overdue custody"),
                ("sync", "Sync conflicts"),
            ),
        ),
    )

    KIND_LABELS = {
        "variance": "Variance",
        "release_variance": "Short release",
        "custody": "Overdue custody",
        "sync": "Sync conflict",
    }

    def rows(self, params: dict):
        from core.tenancy import require_current_organization_id
        from jobs.views import (
            _open_variances,
            _overdue_custody,
            _sync_conflicts,
            _unacknowledged_release_variances,
        )

        kind = params.get("kind") or ""
        organization_id = require_current_organization_id()
        entries = []
        if kind in ("", "variance"):
            entries.extend(_open_variances())
        if kind in ("", "release_variance"):
            entries.extend(_unacknowledged_release_variances())
        if kind in ("", "custody"):
            entries.extend(_overdue_custody(organization_id))
        # §8.4: the same register, so there is one place to look (M1, T8.7).
        if kind in ("", "sync"):
            entries.extend(_sync_conflicts(organization_id))

        for entry in sorted(entries, key=lambda row: row["raised_at"] or "", reverse=True):
            yield {
                "kind": self.KIND_LABELS.get(entry["kind"], entry["kind"]),
                "reference": entry["reference"],
                "summary": entry["summary"],
                "expected": entry["expected"],
                "actual": entry["actual"],
                "uom": entry["uom"],
                "raised_at": entry["raised_at"],
            }

    def totals(self, rows):
        """Counts, not sums: these are different units and different problems."""
        return None


# --------------------------------------------------------------------------
# T7.4 — recovery and disposal
# --------------------------------------------------------------------------


@register
class RecoveriesReport(Report):
    """M1: "recoveries by originating site".

    T7.4's criterion — "a recovery from a decommissioned site appears against
    that site" — is why the grouping is on the gate-in's `origin_site` rather
    than on anything about the site's current state. A decommissioned site still
    happened; a report that dropped it would lose exactly the history somebody is
    looking for.
    """

    slug = "recoveries"
    title = "Recoveries by originating site"
    description = "What has come back off sites, grouped by where it came from."
    requirement = "M1, D5"
    columns = (
        text("site", "Origin site", width=30),
        text("site_ref", "Reference", width=16),
        text("item_type", "Item", width=32),
        quantity("quantity", "Quantity"),
        text("uom", "UoM", width=8),
        text("condition", "Condition", width=18),
        text("owner", "Owner", width=20),
        integer("receipts", "Receipts"),
    )
    filters = (SITE_FILTER, FROM_FILTER, TO_FILTER)

    def rows(self, params: dict):
        from receiving.models import DocumentStatus, GateInLine, GateInSource

        lines = GateInLine.objects.filter(
            gate_in__source_type=GateInSource.RECOVERY,
            gate_in__status=DocumentStatus.POSTED,
        ).select_related(
            "gate_in", "gate_in__origin_site", "item_type", "owner_client"
        )
        if params.get("site"):
            lines = lines.filter(gate_in__origin_site_id=params["site"])
        if params.get("from_date"):
            lines = lines.filter(gate_in__received_at__date__gte=params["from_date"])
        if params.get("to_date"):
            lines = lines.filter(gate_in__received_at__date__lte=params["to_date"])

        grouped: dict[tuple, dict] = {}
        for line in lines:
            site = line.gate_in.origin_site
            key = (
                site.pk if site else None,
                line.item_type_id,
                line.condition,
                line.owner_client_id,
            )
            row = grouped.get(key)
            if row is None:
                grouped[key] = {
                    "site": str(site) if site else "Not recorded",
                    "site_ref": site.internal_ref if site else "",
                    "item_type": str(line.item_type),
                    "quantity": line.quantity,
                    "uom": line.uom,
                    "condition": line.get_condition_display(),
                    "owner": _client_name(line),
                    "receipts": 1,
                }
            else:
                row["quantity"] += line.quantity
                row["receipts"] += 1

        yield from sorted(
            grouped.values(), key=lambda row: (row["site"], row["item_type"])
        )

    def totals(self, rows):
        summed = super().totals(rows)
        if summed:
            # A count of receipts summed across sites is meaningful; a quantity
            # across mixed units is not, but the report is normally read filtered
            # to one site, so both are kept and the UoM column stays visible.
            return summed
        return None


@register
class DisposalsReport(Report):
    """M1: "disposals and write-offs" (J3)."""

    slug = "disposals"
    title = "Disposals and write-offs"
    description = "What has been destroyed or written off, how, and on whose authority."
    requirement = "M1, J3"
    columns = (
        text("number", "Number", width=14),
        text("status", "Status", width=14),
        on_date("disposed_at", "Disposed"),
        text("method", "Method", width=30),
        text("handler", "Handler", width=24),
        text("item_type", "Item", width=30),
        quantity("quantity", "Quantity"),
        text("uom", "UoM", width=8),
        text("owner", "Owner", width=20),
        money("value", "Value", wide_only=True),
        text("authorised_by", "Authorised by", width=24, wide_only=True),
    )
    filters = (
        Filter(
            "status",
            "Status",
            kind="choice",
            choices=(
                ("DISPOSED", "Completed"),
                ("PENDING_APPROVAL", "Awaiting approval"),
                ("APPROVED", "Approved, not yet actioned"),
                ("REJECTED", "Rejected"),
            ),
        ),
        CLIENT_FILTER,
        FROM_FILTER,
        TO_FILTER,
    )

    def rows(self, params: dict):
        from approvals.models import ApprovalAction
        from disposition.models import Disposal, DisposalLine

        lines = DisposalLine.objects.select_related(
            "disposal", "item_type", "owner_client"
        )
        if params.get("status"):
            lines = lines.filter(disposal__status=params["status"])
        if params.get("client"):
            lines = lines.filter(owner_client_id=params["client"])
        if params.get("from_date"):
            lines = lines.filter(disposal__disposed_at__date__gte=params["from_date"])
        if params.get("to_date"):
            lines = lines.filter(disposal__disposed_at__date__lte=params["to_date"])

        lines = list(lines)
        # M3: who authorised each one. One query for all of them rather than one
        # per line, because a write-off report with fifty lines is normal.
        authorisers: dict[str, str] = {}
        disposal_ids = {str(line.disposal_id) for line in lines}
        if disposal_ids:
            for action in ApprovalAction.objects.filter(
                approval_request__document_type=Disposal._meta.label,
                approval_request__document_id__in=disposal_ids,
                decision__in=("APPROVED", "AUTO"),
            ).select_related("approval_request", "actor", "on_behalf_of"):
                authorisers.setdefault(
                    action.approval_request.document_id,
                    # §4.2: "X on behalf of Y", never just Y.
                    action.attribution or "auto-approved",
                )

        for line in lines:
            disposal = line.disposal
            yield {
                "number": disposal.number,
                "status": disposal.get_status_display(),
                "disposed_at": disposal.disposed_at,
                "method": disposal.get_method_display(),
                "handler": disposal.handler_name
                + (f" ({disposal.handler_reference})" if disposal.handler_reference else ""),
                "item_type": str(line.item_type),
                "quantity": line.quantity,
                "uom": line.uom,
                "owner": _client_name(line),
                "value": line.written_off_value,
                "authorised_by": authorisers.get(str(disposal.pk), "not authorised"),
            }


@register
class InstalledBaseReport(Report):
    """H2, M1: what is installed at each site.

    Not named in M1's list, but it is the other half of the consumption report
    and the question a client asks first — "what of ours is in the ground?" —
    which the ledger already answers as a balance at the SITE node (§3.1).
    """

    slug = "installed-base"
    title = "Installed base"
    description = "What is installed at each site, from the ledger."
    requirement = "M1, H2"
    columns = (
        text("site", "Site", width=30),
        text("item_type", "Item", width=32),
        quantity("quantity", "Quantity"),
        text("uom", "UoM", width=8),
        text("owner", "Owner", width=22),
    )
    filters = (SITE_FILTER, CLIENT_FILTER)

    def rows(self, params: dict):
        from network.models import Site
        from stock.queries import installed_base

        site = None
        if params.get("site"):
            site = Site.objects.filter(pk=params["site"]).first()

        balances = installed_base(site=site)
        if params.get("client"):
            balances = balances.filter(owner_client_id=params["client"])

        for balance in balances.order_by("node__label", "item_type__name"):
            yield {
                "site": balance.node.label,
                "item_type": str(balance.item_type),
                "quantity": balance.quantity,
                "uom": balance.uom,
                "owner": _client_name(balance),
            }


__all__ = [
    "ClientPositionReport",
    "ConsumptionReport",
    "DisposalsReport",
    "ExceptionsReport",
    "InstalledBaseReport",
    "MovementHistoryReport",
    "OutstandingGateOutsReport",
    "OverdueCustodyReport",
    "RecoveriesReport",
    "SerialHistoryReport",
    "StockAsAtReport",
    "StockOnHandReport",
]
