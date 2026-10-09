"""A project's sites, their dates and what the yard did for them (R10; §4.19.6).

``ProjectSite`` is the through row of ``Project.sites``. This module is where the
rows are linked and read; the pieces that are *derived* (accepted, first
collection, last dispatch) are computed here and never stored (D23).

Two decisions worth stating:

* **Accepted needs a document.** ``accepted_on`` alone is a date somebody typed;
  the site counts as accepted only with the acceptance certificate attached too.
* **"Which project" is the engine's answer.** A gate pass reaches its project
  through its job (``approvals.engine.project_of``). A pass addressed to a site
  has no project destination (exactly one destination), so for a pass with a
  ``site`` the project is its job's project; the query below says that in SQL.
"""

from __future__ import annotations

from datetime import date

from django.db.models import CharField, Exists, Max, Min, OuterRef, Q, QuerySet, Subquery
from django.db.models.functions import Cast

from core.exceptions import DomainError

CERTIFICATE_CAPTION = "Acceptance certificate"
SITE_TARGET = "network.ProjectSite"


class SiteHasProjectData(DomainError):
    code = "SITE_HAS_PROJECT_DATA"
    status_code = 409
    default_message = (
        "This site has dates or an acceptance certificate on the project, so it "
        "cannot be taken off it."
    )


def with_site_facts(queryset: QuerySet) -> QuerySet:
    """Annotate ``ProjectSite`` rows with the certificate and the yard dates.

    One query for any number of rows: each fact is a correlated subquery, so a
    page of a hundred sites costs what a page of one does.
    """
    from core.models import Attachment
    from dispatch.models import GateOut

    certificates = Attachment.objects.filter(
        target_type=SITE_TARGET,
        target_id=Cast(OuterRef("pk"), output_field=CharField()),
    ).filter(Q(caption=CERTIFICATE_CAPTION) | Q(caption=""))

    passes = GateOut.objects.filter(
        site_id=OuterRef("site_id"),
        job__project_id=OuterRef("project_id"),
        released_at__isnull=False,
    ).order_by().values("site_id")

    return queryset.annotate(
        has_certificate=Exists(certificates),
        first_collection_at=Subquery(passes.annotate(v=Min("released_at")).values("v")),
        last_dispatch_at=Subquery(passes.annotate(v=Max("released_at")).values("v")),
    )


def is_accepted(accepted_on: date | None, has_certificate: bool) -> bool:
    """R10: accepted is a date **and** a certificate."""
    return accepted_on is not None and bool(has_certificate)


def project_site_rows(project) -> list:  # type: ignore[no-untyped-def]
    """The project's ``ProjectSite`` rows, annotated, in one query."""
    from network.models import ProjectSite

    return list(
        with_site_facts(ProjectSite.objects.filter(project=project))
        .select_related("site")
        .order_by("site__internal_ref", "id")
    )


def accepted_dates_of(project) -> list[date | None]:  # type: ignore[no-untyped-def]
    """One entry per site: when it was accepted, or ``None`` while it is not."""
    return [
        row.accepted_on if is_accepted(row.accepted_on, row.has_certificate) else None
        for row in project_site_rows(project)
    ]


def set_project_sites(project, sites) -> None:  # type: ignore[no-untyped-def]
    """Make ``sites`` the project's sites.

    Replaces ``project.sites.set(...)`` everywhere: a bulk ``add`` never calls
    ``save()``, so the through row's tenant column has to be passed explicitly.
    Refuses to remove a site that has a date or a certificate on this project
    (``SITE_HAS_PROJECT_DATA``): taking it off would silently lose them.
    """
    from network.models import ProjectSite

    wanted = {site.pk for site in sites}
    holding = with_site_facts(
        ProjectSite.objects.filter(project=project).exclude(site_id__in=wanted)
    )
    blocked = [
        row
        for row in holding
        if row.mobilised_on or row.accepted_on or row.has_certificate
    ]
    if blocked:
        raise SiteHasProjectData(
            details={"sites": [row.site_id for row in blocked]},
        )
    project.sites.set(sites, through_defaults={"organization_id": project.organization_id})
