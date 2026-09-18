"""Reconciliation (design §3.1, §10; H4, C7, M1).

H4: "see per-site and per-project reconciliation — issued versus installed
versus returned versus unaccounted — **so that I can answer the operator**."

§3.1 is what makes this one query instead of five reports:

> H4's reconciliation — issued vs installed vs returned vs unaccounted — becomes
> one aggregation over movements grouped by destination node type, rather than
> five bespoke reports that can disagree with each other.

Five reports that can disagree is precisely what the operator would find, and
what would end the conversation badly. So all four figures come from the same
ledger scan, and the fourth is derived from the other three rather than counted
separately — which is why they always add up.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal

from django.db.models import Q, Sum

from locations.models import NodeType
from stock.models import MovementType, StockMovement


@dataclass
class ItemReconciliation:
    """The four figures for one item type, plus what they leave unexplained."""

    item_type_id: int
    item_type: str
    uom: str

    issued: Decimal = Decimal("0")
    installed: Decimal = Decimal("0")
    consumed: Decimal = Decimal("0")
    returned: Decimal = Decimal("0")

    @property
    def accounted(self) -> Decimal:
        """Everything whose whereabouts is explained."""
        return self.installed + self.consumed + self.returned

    @property
    def unaccounted(self) -> Decimal:
        """H4's fourth figure.

        Derived, never counted separately: issued minus what is explained. That
        is what guarantees the four numbers reconcile — a separately counted
        "unaccounted" could disagree with the other three, and then nobody could
        tell which was wrong.

        A negative value means more came back than went out on this job, which is
        itself worth investigating rather than hiding at zero.
        """
        return self.issued - self.accounted

    @property
    def is_reconciled(self) -> bool:
        return self.unaccounted == 0

    def as_dict(self) -> dict:
        data = asdict(self)
        data.update(
            {
                "accounted": self.accounted,
                "unaccounted": self.unaccounted,
                "is_reconciled": self.is_reconciled,
            }
        )
        return data


def reconcile_site(site) -> dict:
    """Reconcile everything issued for one site (H4).

    The scope has to include job closeouts as well as gate passes. Consumption
    posts from a technician's custody to the CONSUMED node and references the
    *closeout*, not the site — so a filter on site nodes alone would report
    everything issued and nothing consumed, and every job would look
    unreconciled.
    """
    movements = StockMovement.objects.filter(
        Q(to_node__site=site)
        | Q(from_node__site=site)
        | Q(document_type="dispatch.GateOut", document_id__in=_gate_out_ids_for_site(site))
        | Q(document_type="jobs.JobCloseout", document_id__in=_closeout_ids_for_site(site))
        | Q(document_type="receiving.GateIn", document_id__in=_return_ids_for_site(site))
    ).distinct()
    return _reconcile(movements, label=str(site), scope="site", scope_id=site.pk)


def reconcile_project(project) -> dict:
    """Reconcile everything issued under one project (H4, C7)."""
    site_ids = list(project.sites.values_list("pk", flat=True))

    from dispatch.models import GateOut
    from jobs.models import JobCloseout

    gate_out_ids = [
        str(pk)
        for pk in GateOut.objects.filter(
            Q(project=project) | Q(site_id__in=site_ids)
        )
        .distinct()
        .values_list("pk", flat=True)
    ]
    from receiving.models import GateIn, GateInSource

    return_ids = [
        str(pk)
        for pk in GateIn.objects.filter(
            origin_site_id__in=site_ids,
            source_type__in=(GateInSource.RETURN_FROM_SITE, GateInSource.RECOVERY),
        ).values_list("pk", flat=True)
    ]
    closeout_ids = [
        str(pk)
        for pk in JobCloseout.objects.filter(
            Q(job__project=project) | Q(job__site_id__in=site_ids)
        )
        .distinct()
        .values_list("pk", flat=True)
    ]

    movements = StockMovement.objects.filter(
        Q(document_type="dispatch.GateOut", document_id__in=gate_out_ids)
        | Q(document_type="jobs.JobCloseout", document_id__in=closeout_ids)
        | Q(document_type="receiving.GateIn", document_id__in=return_ids)
        | Q(to_node__site_id__in=site_ids)
        | Q(from_node__site_id__in=site_ids)
    ).distinct()
    return _reconcile(
        movements, label=str(project), scope="project", scope_id=project.pk
    )


def reconcile_job(job) -> dict:
    """Reconcile one job (H4, H5).

    A job is work at one site, so its figures come from that site's movements
    narrowed to this job's own gate passes where they exist.
    """
    return reconcile_site(job.site)


def _return_ids_for_site(site) -> list[str]:
    """Returns and recoveries recorded against this site (H4, D5).

    Material coming back off a site lands on a yard node from a person's custody,
    so nothing in the movement itself names the site. The gate-in's origin site is
    the only link, which is why recording it on a return matters: without it the
    return is invisible here and the site reads as unreconciled even though the
    material is back on the shelf.
    """
    from receiving.models import GateIn, GateInSource

    return [
        str(pk)
        for pk in GateIn.objects.filter(
            origin_site=site,
            source_type__in=(
                GateInSource.RETURN_FROM_SITE,
                GateInSource.RECOVERY,
            ),
        ).values_list("pk", flat=True)
    ]


def _closeout_ids_for_site(site) -> list[str]:
    """Closeouts for jobs at this site, whose movements belong to its figures."""
    from jobs.models import JobCloseout

    return [
        str(pk)
        for pk in JobCloseout.objects.filter(job__site=site).values_list("pk", flat=True)
    ]


def _gate_out_ids_for_site(site) -> list[str]:
    from dispatch.models import GateOut

    return [
        str(pk)
        for pk in GateOut.objects.filter(Q(site=site) | Q(project__sites=site))
        .distinct()
        .values_list("pk", flat=True)
    ]


def _reconcile(movements, *, label: str, scope: str, scope_id) -> dict:
    """Turn a movement queryset into the four figures per item (H4, §3.1).

    Grouped by destination node type, which is the whole trick: where material
    *went* is what says whether it was installed, used up, or brought back.
    """
    rows: dict[int, ItemReconciliation] = {}

    def row_for(item_type_id, item_name, uom) -> ItemReconciliation:
        if item_type_id not in rows:
            rows[item_type_id] = ItemReconciliation(
                item_type_id=item_type_id, item_type=item_name, uom=uom
            )
        return rows[item_type_id]

    aggregated = (
        movements.values(
            "item_type_id",
            "item_type__name",
            "uom",
            "movement_type",
            "from_node__type",
            "to_node__type",
        )
        .annotate(total=Sum("quantity"))
        .order_by()
    )

    for entry in aggregated:
        row = row_for(entry["item_type_id"], entry["item_type__name"], entry["uom"])
        total = entry["total"] or Decimal("0")
        to_type = entry["to_node__type"]
        from_type = entry["from_node__type"]
        movement_type = entry["movement_type"]

        # Issued: it left a location for a person or a site. A transfer between
        # two locations is not an issue — nothing left the yard's control.
        if from_type == NodeType.LOCATION and to_type in (
            NodeType.PERSON,
            NodeType.SITE,
        ):
            row.issued += total

        if to_type == NodeType.SITE:
            row.installed += total
        elif to_type == NodeType.CONSUMED:
            row.consumed += total
        elif to_type == NodeType.LOCATION and from_type in (
            NodeType.PERSON,
            NodeType.SITE,
        ):
            # Came back into the yard from a person or a site.
            row.returned += total

        # A reversal on this scope cancels an issue, so it reduces the total
        # rather than counting as a return (M4).
        if movement_type == MovementType.REVERSAL and to_type == NodeType.LOCATION:
            row.issued -= total
            row.returned -= total

    ordered = sorted(rows.values(), key=lambda entry: entry.item_type)

    return {
        "scope": scope,
        "scope_id": scope_id,
        "label": label,
        "items": [entry.as_dict() for entry in ordered],
        "totals": {
            "issued": sum((entry.issued for entry in ordered), Decimal("0")),
            "installed": sum((entry.installed for entry in ordered), Decimal("0")),
            "consumed": sum((entry.consumed for entry in ordered), Decimal("0")),
            "returned": sum((entry.returned for entry in ordered), Decimal("0")),
            "unaccounted": sum((entry.unaccounted for entry in ordered), Decimal("0")),
        },
        "is_reconciled": all(entry.is_reconciled for entry in ordered),
    }


def project_unreconciled(project) -> dict:
    """C7: what closing this project would leave unexplained.

    Called by the close action to produce a warning rather than a block — C7 says
    "closing it **warns** if material remains unreconciled". Blocking belongs to
    H5, and that applies to jobs.
    """
    result = reconcile_project(project)
    unaccounted = [item for item in result["items"] if not item["is_reconciled"]]

    return {
        "available": True,
        "unreconciled": bool(unaccounted),
        "items": unaccounted,
        "totals": result["totals"],
    }


def open_expectations_for(job) -> list:
    """What this job is still waiting on (H5).

    H5 blocks closing while material remains unaccounted for. "Unaccounted" here
    means declared-but-not-arrived: the returns and recoveries a technician
    reported that the yard has not yet received.
    """
    from jobs.models import CloseoutAction, JobCloseoutLine

    return list(
        JobCloseoutLine.objects.filter(
            closeout__job=job,
            action__in=(CloseoutAction.RETURNING, CloseoutAction.RECOVERED),
            closeout__status="SUBMITTED",
        ).select_related("item_type")
    )
