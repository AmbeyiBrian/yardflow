"""Material by site (design §4.16.8; Q5).

What each site was sent, what is still waiting in the yard for it, and what
went elsewhere. The first three come from the earmark ledger
(``EarmarkEvent``) over a period; "still in the yard" is the live earmarks and
is not period-bound. Everything is a handful of grouped queries, never one per
site.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.db.models import Case, Count, DecimalField, F, Sum, Value, When
from django.db.models.functions import Coalesce

from reporting.framework import Column, Filter, Report, register, text

ZERO = Decimal("0")
_FIELDS = ("received", "sent", "waiting", "diverted")


@dataclass(frozen=True)
class CountOrQuantity(Column):
    """A quantity column in which a count of units prints as a whole number."""

    def format(self, value: Any) -> str:
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        return super().format(value)


def _amount(key: str, label: str) -> Column:
    return CountOrQuantity(key, label, kind="quantity", numeric=True, width=16)


@register
class MaterialBySiteReport(Report):
    slug = "material-by-site"
    title = "Material by site"
    category = "Stock"
    description = (
        "What each site was sent, what is still waiting in the yard for it, "
        "and what went elsewhere."
    )
    requirement = "Q5"
    can_be_large = False

    filters = (
        Filter("from_date", "From", kind="date"),
        Filter("to_date", "To", kind="date"),
        Filter("client", "Client", kind="reference", resource="clients"),
        Filter("project", "Project", kind="reference", resource="projects"),
        Filter("site", "Site", kind="reference", resource="sites"),
    )

    columns = (
        text("site", "Site", width=28),
        text("item", "Item", width=30),
        text("uom", "Unit", width=8),
        _amount("received", "Received for"),
        _amount("sent", "Sent to"),
        _amount("waiting", "Still in the yard"),
        _amount("diverted", "Diverted away"),
    )

    def rows(self, params: dict):
        from catalogue.models import ItemType, TrackingMode
        from jobs.models import Job
        from network.models import Site

        sites = Site.objects.all()
        if params.get("client"):
            sites = sites.filter(client_id=params["client"])
        if params.get("site"):
            sites = sites.filter(pk=params["site"])
        if params.get("project"):
            sites = sites.filter(
                pk__in=Job.objects.filter(project_id=params["project"]).values("site_id")
            )
        names = dict(sites.values_list("pk", "name"))
        if not names:
            return

        figures: dict[tuple[int, int], dict[str, Decimal]] = {}
        for field, grouped in (*self._ledger(params, list(names)), *self._current(list(names))):
            for row in grouped:
                cell = figures.setdefault(
                    (row["site_key"], row["item_type_id"]), dict.fromkeys(_FIELDS, ZERO)
                )
                cell[field] += row["total"] or ZERO

        items = ItemType.objects.in_bulk({item for _site, item in figures})
        ordered = sorted(
            (key for key, cell in figures.items() if any(cell.values())),
            key=lambda key: (names[key[0]].lower(), items[key[1]].name.lower(), key),
        )
        for site_id, item_id in ordered:
            item = items[item_id]
            counted = item.default_tracking_mode == TrackingMode.SERIALIZED
            out: dict[str, Any] = {"site": names[site_id], "item": item.name, "uom": item.uom}
            for field in _FIELDS:
                value = figures[(site_id, item_id)][field]
                out[field] = int(value) if counted else value
            yield out

    # -- the queries ---------------------------------------------------------

    @staticmethod
    def _ledger(params: dict, site_ids: list[int]):
        from stock.models import EarmarkAction, EarmarkEvent

        events = EarmarkEvent.objects.filter(item_type__isnull=False)
        if params.get("from_date"):
            events = events.filter(occurred_at__date__gte=params["from_date"])
        if params.get("to_date"):
            events = events.filter(occurred_at__date__lte=params["to_date"])
        # A unit is one, whatever quantity its event carries.
        amount = Sum(
            Case(
                When(serial_unit__isnull=False, then=Value(Decimal("1"))),
                default=Coalesce("quantity", Value(ZERO)),
                output_field=DecimalField(max_digits=14, decimal_places=3),
            )
        )

        def grouped(action: str, site_field: str):
            return (
                events.filter(action=action, **{f"{site_field}__in": site_ids})
                .annotate(site_key=F(f"{site_field}_id"))
                .values("site_key", "item_type_id")
                .annotate(total=amount)
            )

        yield "received", grouped(EarmarkAction.EARMARKED, "site")
        yield "received", grouped(EarmarkAction.CHANGED, "to_site")
        yield "sent", grouped(EarmarkAction.DELIVERED, "site")
        yield "diverted", grouped(EarmarkAction.DIVERTED, "site")

    @staticmethod
    def _current(site_ids: list[int]):
        from stock.models import BulkEarmark, Reel, ReelStatus, SerialUnit

        yield "waiting", (
            SerialUnit.objects.filter(earmark_site__in=site_ids)
            .annotate(site_key=F("earmark_site_id"))
            .values("site_key", "item_type_id")
            .annotate(total=Count("pk"))
        )
        yield "waiting", (
            Reel.objects.filter(earmark_site__in=site_ids, status=ReelStatus.OPEN)
            .annotate(site_key=F("earmark_site_id"))
            .values("site_key", "item_type_id")
            .annotate(total=Sum("remaining_length"))
        )
        yield "waiting", (
            BulkEarmark.objects.filter(site__in=site_ids)
            .annotate(site_key=F("site_id"))
            .values("site_key", "item_type_id")
            .annotate(total=Sum("quantity"))
        )
