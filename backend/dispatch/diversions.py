"""What a gate pass would divert, worked out once (design §4.16.5; Q3).

One function, :func:`pass_diversions`, answers "which earmarked material on this
pass is going somewhere other than the site it was kept for?" for submit, the
detail read and the approval payload, so the three can never disagree. It
applies the same rules as ``stock.earmark_hooks`` does at release: named units
and drums are diverted when their earmark's site is not a destination site; a
bulk quantity is drawn from own-site earmarks first, then free stock, then other
sites' earmarks by site name, and what comes from the last is the diversion.

Queries per pass, however many lines: the destination sites (a few), one for the
earmarks of every bulk lot, one for their balances, and one for the names of the
sites involved. Units and drums are read from the lines' prefetched children.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.db.models.functions import Lower

from catalogue.models import TrackingMode
from dispatch.models import GateOut, GateOutStatus
from locations.nodes import node_for_location
from network.models import Site

ZERO = Decimal("0")

#: A pass in one of these has nothing left to divert.
_SETTLED = (GateOutStatus.RELEASED, GateOutStatus.CLOSED, GateOutStatus.CANCELLED)


def destination_sites(gate_out: GateOut) -> frozenset[Site]:
    """The sites this pass delivers to (§4.16.5).

    The pass's site; else its job's site; for a project destination the sites of
    that project's jobs. A person with no job, a client, or another location has
    none: a pass to another location is a transfer inside the perimeter, so
    earmarks move with it and nothing is a diversion.
    """
    site, job = gate_out.site, gate_out.job
    if site is not None:
        return frozenset({site})
    if job is not None:
        return frozenset({job.site})
    if gate_out.project_id is not None:
        from jobs.models import Job

        job_sites = Job.objects.filter(project_id=gate_out.project_id).values("site_id")
        return frozenset(Site.objects.filter(pk__in=job_sites))
    return frozenset()


def _moves_earmarks_only(gate_out: GateOut) -> bool:
    """Is the material staying inside the perimeter, or never inside it?

    A pass between two stores inside the yard carries earmarks and diverts
    nothing; a pass out of a store that is not inside the perimeter is outside
    the earmark rules altogether (the hook does not apply them there).
    """
    from stock.counting import is_inside_perimeter

    if not is_inside_perimeter(gate_out.from_location):
        return True
    to_location = gate_out.to_location
    return to_location is not None and is_inside_perimeter(to_location)


def _entry(site: Site, **fields: Any) -> dict[str, Any]:
    return {"site": site.pk, "site_name": site.name, **fields}


def _group(rows: list[tuple], sites: dict[int, Site]) -> list[tuple[Site, list[tuple]]]:
    """Rows whose first element is a site id, grouped by site, in site-name order."""
    grouped: dict[int, list[tuple]] = {}
    for row in rows:
        grouped.setdefault(row[0], []).append(row)
    ordered = sorted(grouped, key=lambda sid: (sites[sid].name.lower(), sid))
    return [(sites[sid], grouped[sid]) for sid in ordered]


def pass_diversions(gate_out: GateOut, lines=None) -> dict[int, list[dict[str, Any]]]:
    """Line id -> what releasing the rest of the line would divert (empty if nothing).

    ``lines`` defaults to ``gate_out.lines.all()`` (so a prefetch is honoured);
    submit passes a fresh list. Every line of the pass is a key. Each entry is
    ``{"site", "site_name", "quantity", "serials"?, "drums"?}``: units carry
    ``serials`` and their count as ``quantity``; drums carry ``drums`` and their
    length; bulk carries ``quantity``. Quantities are strings.
    """
    lines = list(gate_out.lines.all() if lines is None else lines)
    result: dict[int, list[dict[str, Any]]] = {line.pk: [] for line in lines}
    if gate_out.status in _SETTLED or not lines or _moves_earmarks_only(gate_out):
        return result

    own = {s.pk for s in destination_sites(gate_out)}
    units: dict[int, list[tuple[int, str]]] = {}
    drums: dict[int, list[tuple[int, str, Decimal]]] = {}
    bulk_lines = []
    for line in lines:
        if line.tracking_mode == TrackingMode.SERIALIZED:
            for entry in line.serials.all():
                site_id = entry.serial_unit.earmark_site_id
                if not entry.released and site_id is not None and site_id not in own:
                    units.setdefault(line.pk, []).append(
                        (site_id, entry.serial_unit.serial_number)
                    )
        elif line.tracking_mode == TrackingMode.REEL:
            for entry in line.reels.all():
                site_id = entry.reel.earmark_site_id
                left = entry.length_requested - entry.length_released
                if left > 0 and site_id is not None and site_id not in own:
                    drums.setdefault(line.pk, []).append((site_id, entry.reel.drum_number, left))
        elif line.outstanding_qty > 0:
            bulk_lines.append(line)

    bulk = _draw_bulk(gate_out, bulk_lines, own)

    wanted = {row[0] for rows in units.values() for row in rows}
    wanted |= {row[0] for rows in drums.values() for row in rows}
    wanted |= {site.pk for found in bulk.values() for site, _ in found}
    sites = {s.pk: s for s in Site.objects.filter(pk__in=wanted)} if wanted else {}

    for line_id, rows in units.items():
        result[line_id] = [
            _entry(site, quantity=str(len(group)), serials=sorted(r[1] for r in group))
            for site, group in _group(rows, sites)
        ]
    for line_id, drum_rows in drums.items():
        result[line_id] = [
            _entry(
                site,
                quantity=str(sum((r[2] for r in group), ZERO)),
                drums=sorted(r[1] for r in group),
            )
            for site, group in _group(drum_rows, sites)
        ]
    for line_id, found in bulk.items():
        result[line_id] = [_entry(site, quantity=str(amount)) for site, amount in found]
    return result


def _draw_bulk(gate_out: GateOut, bulk_lines, own: set[int]):  # type: ignore[no-untyped-def]
    """Line id -> [(site, quantity)] of other sites' earmarks the line would draw.

    Mirrors ``stock.earmark_hooks.apply_bulk_rule``: own-site earmarks, then free
    stock (balance less every claim), then other sites by name. Lines of one lot
    draw in order, as release posts them.
    """
    from stock.models import BulkEarmark, StockBalance

    found: dict[int, list[tuple[Site, Decimal]]] = {}
    if not bulk_lines:
        return found
    source = node_for_location(gate_out.from_location)
    item_ids = {line.item_type_id for line in bulk_lines}
    claims_by_lot: dict[tuple, list[list]] = {}
    for claim in (
        BulkEarmark.objects.filter(node=source, item_type_id__in=item_ids)
        .select_related("site")
        .order_by(Lower("site__name"), "pk")
    ):
        lot = (claim.item_type_id, claim.owner_client_id, claim.condition)
        claims_by_lot.setdefault(lot, []).append([claim.site, claim.quantity])
    if not claims_by_lot:
        return found

    balances = {
        (b.item_type_id, b.owner_client_id, b.condition): b.quantity
        for b in StockBalance.objects.filter(node=source, item_type_id__in=item_ids)
    }
    free: dict[tuple, Decimal] = {
        lot: max(balances.get(lot, ZERO) - sum((c[1] for c in claims), ZERO), ZERO)
        for lot, claims in claims_by_lot.items()
    }
    for line in bulk_lines:
        lot = (line.item_type_id, line.owner_client_id, line.condition)
        claims = claims_by_lot.get(lot)
        if not claims:
            continue
        remaining = line.outstanding_qty
        for claim in claims:
            if claim[0].pk in own and remaining > 0:
                take = min(claim[1], remaining)
                claim[1] -= take
                remaining -= take
        take = min(free[lot], remaining)
        free[lot] -= take
        remaining -= take
        merged: dict[int, list] = {}
        for claim in claims:
            if claim[0].pk not in own and remaining > 0 and claim[1] > 0:
                take = min(claim[1], remaining)
                claim[1] -= take
                remaining -= take
                merged[claim[0].pk] = [claim[0], take]
        if merged:
            found[line.pk] = [(site, amount) for site, amount in merged.values()]
    return found


def _say(quantity: str) -> str:
    text = quantity.rstrip("0").rstrip(".") if "." in quantity else quantity
    return text or "0"


def diversion_message(gate_out: GateOut, line, entries: list[dict[str, Any]]) -> str:
    """The sentence a storekeeper reads when a line needs a reason (Q3)."""
    item = str(line.item_type)
    parts = []
    singular = True
    for entry in entries:
        qty = _say(entry["quantity"])
        if entry.get("drums"):
            what = f"Drum {', '.join(entry['drums'])} of {item}"
            singular = singular and len(entry["drums"]) == 1
        elif entry.get("serials"):
            what = f"{qty} {item}"
            singular = singular and len(entry["serials"]) == 1
        else:
            what = f"{qty} {line.uom} of {item}"
            singular = False
        parts.append((what, entry["site_name"]))
    verb = "is" if singular and len(parts) == 1 else "are"
    sentence = f"{parts[0][0]} {verb} earmarked for {parts[0][1]}"
    for what, site in parts[1:]:
        sentence += f" and {what} for {site}"
    sites = destination_sites(gate_out)
    where = " or ".join(sorted(s.name for s in sites)) if sites else gate_out.destination_label
    return f"{sentence}. Say why they are going to {where}, or take free stock."
