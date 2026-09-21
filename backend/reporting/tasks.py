"""Asynchronous exports (design §10, §12; M2, N-7, T7.5).

T7.5's criterion: "a large export completes asynchronously **and the link
expires**." Both halves matter and the second is the security one — an export is
a file containing somebody's whole stock position, and a permanent URL to it is a
leak waiting for a forwarded email.

So the file is stored as an ``Attachment`` and served through the same signed,
expiring URL as every other attachment (N-7). Not because it is convenient, but
because a second file-serving mechanism is a second place to forget the
authorisation — and the one that only handles exports is the one nobody reviews.
"""

from __future__ import annotations

import logging

from celery import shared_task
from django.core.files.base import ContentFile

from core.tasks import TenantTask

logger = logging.getLogger(__name__)


@shared_task(base=TenantTask, bind=True)
def build_export(  # type: ignore[no-untyped-def]
    self,
    *,
    organization_id,
    slug: str,
    fmt: str,
    filters: dict,
    requested_by_id=None,
) -> dict:
    """Build one export, store it, and tell the person who asked (T7.5, L1).

    Runs under ``TenantTask``, so the organization is in context and row-level
    security applies exactly as it would in a request (§2.2) — an export task
    that read across tenants would be the worst possible leak, since its whole
    output is a file somebody then sends to a client.
    """
    from accounts.models import User
    from core.attachments import store_attachment
    from core.models import AttachmentKind, Organization
    from notifications.events import emit
    from reporting.exports import render_export
    from reporting.framework import get_report

    organization = Organization.objects.filter(pk=organization_id).first()
    requester = (
        User.objects.filter(pk=requested_by_id).first() if requested_by_id else None
    )
    report = get_report(slug)

    content, content_type, filename = render_export(
        slug, filters, fmt=fmt, organization=organization
    )

    # `store_attachment` reads the type off the file object, as it does for a
    # browser upload. A `ContentFile` has none, so the stored export was typed
    # as nothing and the download had to guess from the name. Say what it is.
    uploaded = ContentFile(content, name=filename)
    uploaded.content_type = content_type  # type: ignore[attr-defined]

    attachment = store_attachment(
        # The report itself has no database row, so the export hangs off the
        # organization — the one record every tenant certainly has.
        target=organization,
        uploaded_file=uploaded,
        kind=AttachmentKind.DOCUMENT,
        uploaded_by=requester,
        organization=organization,
    )

    emit(
        "report.export_ready",
        attachment,
        payload={
            "number": filename,
            "label": report.title,
            "report": report.title,
            "slug": slug,
            "format": fmt,
            # N-7: signed and expiring. A link that outlived the conversation
            # would be a stock position sitting on a URL.
            "download_url": attachment.download_url(),
            "requested_by_id": requested_by_id,
        },
    )

    logger.info(
        "export %s built for organization %s (%s bytes)",
        slug,
        organization_id,
        len(content),
    )
    return {
        "attachment_id": attachment.pk,
        "filename": filename,
        "bytes": len(content),
    }


def stale_exports(organization_id, *, older_than_days: int = 7):
    """Exports old enough to clear out.

    Not deleted by a sweep yet — M5 is emphatic that retention "**never
    deletes**", and an export is a copy rather than a record, so removing one
    loses nothing. This is the query a cleanup would use; wiring it to a schedule
    is a decision for whoever operates the deployment, and the honest thing is to
    leave the files there until someone makes it.
    """
    from datetime import timedelta

    from django.utils import timezone

    from core.models import Attachment, AttachmentKind

    cutoff = timezone.now() - timedelta(days=older_than_days)
    return Attachment.objects.filter(
        organization_id=organization_id,
        kind=AttachmentKind.DOCUMENT,
        target_type="core.Organization",
        created_at__lt=cutoff,
    )
