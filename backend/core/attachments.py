"""Attachment storage and signed access (design §4.13, §12.0; N-7).

N-7: "attachments stored in S3 with pre-signed, time-limited URLs. Never
publicly readable."

The important design point is that **development behaves like production**. If
local reads went through a plain ``/media/`` path, every authorisation mistake
would be invisible until the first deployment. So both paths issue a signed URL
that expires, and the calling code cannot tell them apart.
"""

from __future__ import annotations

from django.conf import settings
from django.core import signing
from django.urls import reverse

#: Default lifetime of a download link. Long enough to click, short enough that
#: a link pasted into a chat is useless tomorrow.
DEFAULT_EXPIRY_SECONDS = 300

SIGNING_SALT = "core.attachments.download"


def signed_download_url(attachment, *, expires_in: int | None = None) -> str:
    """Return a signed, expiring URL for ``attachment``."""
    expires_in = expires_in or DEFAULT_EXPIRY_SECONDS

    storage = attachment.file.storage
    # S3 (and any storage offering query-string auth) signs natively.
    if getattr(storage, "querystring_auth", False):
        return attachment.file.url

    token = signing.dumps(
        {"id": attachment.pk, "org": str(attachment.organization_id)},
        salt=SIGNING_SALT,
    )
    path = reverse("attachment-download", kwargs={"token": token})
    return f"{path}?expires_in={expires_in}"


def resolve_signed_token(token: str, *, expires_in: int = DEFAULT_EXPIRY_SECONDS):
    """Return the attachment a token refers to, or raise.

    Raises :class:`django.core.signing.BadSignature` for a tampered token and
    :class:`django.core.signing.SignatureExpired` for an old one — both of which
    the view turns into a 404 rather than an explanation.
    """
    from core.models import Attachment

    payload = signing.loads(token, salt=SIGNING_SALT, max_age=expires_in)

    # The organization travels inside the signed payload, so a link cannot be
    # replayed against another tenant even if an id were guessed (A3).
    from core.tenancy import tenant_context

    with tenant_context(payload["org"]):
        return Attachment.objects.filter(pk=payload["id"]).first()


def store_attachment(
    *,
    target,
    uploaded_file,
    kind: str,
    uploaded_by=None,
    organization=None,
):
    """Save an uploaded file against ``target``.

    Records the declared content type and size, so a later listing does not have
    to touch storage to describe an attachment.
    """
    from core.models import Attachment

    attachment = Attachment(
        target_type=target._meta.label,
        target_id=str(target.pk),
        filename=getattr(uploaded_file, "name", "attachment"),
        content_type=getattr(uploaded_file, "content_type", "") or "",
        size=getattr(uploaded_file, "size", 0) or 0,
        kind=kind,
        uploaded_by=uploaded_by,
    )
    if organization is not None:
        attachment.organization = organization
    attachment.file = uploaded_file
    attachment.save()
    return attachment


def attachments_for(target):
    """Every attachment on one record."""
    from core.models import Attachment

    return Attachment.objects.filter(
        target_type=target._meta.label, target_id=str(target.pk)
    )


def max_upload_bytes() -> int:
    return getattr(settings, "ATTACHMENT_MAX_BYTES", 25 * 1024 * 1024)
