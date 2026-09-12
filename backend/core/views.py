"""Core views (design §4.13, N-7)."""

from __future__ import annotations

from django.core import signing
from django.http import FileResponse, Http404
from django.views.decorators.http import require_GET

from core.attachments import DEFAULT_EXPIRY_SECONDS, resolve_signed_token


@require_GET
def attachment_download(request, token: str):
    """Serve an attachment through a signed, expiring URL (N-7).

    The local counterpart of an S3 pre-signed GET. A tampered or expired token
    is a **404, not a 403**: distinguishing them would confirm that an
    attachment exists (§2.4).
    """
    try:
        expires_in = int(request.GET.get("expires_in", DEFAULT_EXPIRY_SECONDS))
    except (TypeError, ValueError):
        expires_in = DEFAULT_EXPIRY_SECONDS

    try:
        attachment = resolve_signed_token(token, expires_in=expires_in)
    except (signing.BadSignature, signing.SignatureExpired) as exc:
        raise Http404("No such attachment.") from exc

    if attachment is None:
        raise Http404("No such attachment.")

    return FileResponse(
        attachment.file.open("rb"),
        as_attachment=True,
        filename=attachment.filename,
        content_type=attachment.content_type or None,
    )
