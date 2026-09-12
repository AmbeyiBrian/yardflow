"""Printed documents and QR lookup (design §11; G4, G5, A4, K2).

G4: "print or share a gate pass PDF, so that the driver carries a document as
they do today." That last clause is the requirement — the yard runs on paper
today, and a system that removes the paper before people trust it will be worked
around rather than adopted.

WeasyPrint needs GTK libraries on the host, which are absent on many development
machines. So rendering degrades to HTML rather than failing: a storekeeper who
cannot print still gets a page they can screenshot or share, and the deployment
gains the PDF without a code change.
"""

from __future__ import annotations

import base64
import io
import logging

from django.core import signing
from django.template.loader import render_to_string
from django.urls import reverse

logger = logging.getLogger(__name__)

QR_SALT = "dispatch.document.qr"


class PdfUnavailable(RuntimeError):
    """WeasyPrint is not installed, or its native libraries are missing."""


# --------------------------------------------------------------------------
# T4.20 — QR codes and lookup
# --------------------------------------------------------------------------


def qr_token(document) -> str:
    """A signed token identifying a document, for its QR code (G5, §11).

    Signed rather than a bare id, for two reasons: a scanned code cannot be
    edited into another tenant's document, and a printed pass cannot be forged
    with a guessed number. The token still authorises nothing on its own — the
    scanner must be logged in and permitted (T4.20).
    """
    return signing.dumps(
        {
            "type": document._meta.label,
            "id": str(document.pk),
            "org": str(document.organization_id),
        },
        salt=QR_SALT,
    )


def resolve_qr_token(token: str):
    """Return the document a scanned token refers to, or ``None``.

    Raises :class:`django.core.signing.BadSignature` for a tampered token. The
    organization travels inside the signed payload, so a code from one tenant
    cannot resolve in another even if the id happened to exist (A3).
    """
    from django.apps import apps

    from core.tenancy import tenant_context

    payload = signing.loads(token, salt=QR_SALT)

    try:
        model = apps.get_model(payload["type"])
    except LookupError:
        return None

    with tenant_context(payload["org"]):
        return model.objects.filter(pk=payload["id"]).first()


def qr_png_data_uri(document, *, box_size: int = 6) -> str:
    """The QR code as a data URI, for embedding in a printed document (§11).

    Embedded rather than linked: the PDF has to be readable when it is printed,
    forwarded by WhatsApp, or opened on a phone with no connection.
    """
    import qrcode

    code = qrcode.QRCode(box_size=box_size, border=2)
    code.add_data(qr_scan_url(document))
    code.make(fit=True)

    image = code.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def logo_data_uri(organization) -> str:
    """The company's logo, embedded rather than linked (A4).

    The same reasoning as the QR code above, and a sharper edge: WeasyPrint is
    handed a string with no base URL, so a relative ``/media/...`` src resolves
    to nothing and the logo is simply absent from the PDF — no error, no
    warning, just a document that came out plain. Reading the bytes here means
    the mark travels with the file wherever it is forwarded.

    Returns an empty string when there is no logo, or when the file has gone
    missing from storage: a document without a letterhead still beats no gate
    pass at all.
    """
    if not organization or not organization.logo:
        return ""

    name = organization.logo.name.lower()
    if name.endswith(".svg"):
        media_type = "image/svg+xml"
    elif name.endswith((".jpg", ".jpeg")):
        media_type = "image/jpeg"
    else:
        media_type = "image/png"

    try:
        with organization.logo.open("rb") as handle:
            encoded = base64.b64encode(handle.read()).decode("ascii")
    except (OSError, ValueError) as exc:
        logger.warning("Logo could not be read for a document: %s", exc)
        return ""
    return f"data:{media_type};base64,{encoded}"


def qr_scan_url(document) -> str:
    """The URL a scanned QR code opens (G5)."""
    return reverse("v1:qr-scan") + f"?token={qr_token(document)}"


# --------------------------------------------------------------------------
# T4.19 — the documents themselves
# --------------------------------------------------------------------------


def render_gate_pass(gate_out, *, as_pdf: bool = True) -> tuple[bytes, str, str]:
    """Render a gate pass (G4, A4).

    Returns ``(content, content_type, filename)``. Falls back to HTML when
    WeasyPrint is unavailable, so a missing system library never stops a driver
    leaving with a document.
    """
    context = gate_pass_context(gate_out)
    html = render_to_string("documents/gate_pass.html", context)
    return _render(html, as_pdf=as_pdf, filename=f"gate-pass-{gate_out.number or gate_out.pk}")


def render_grn(gate_in, *, as_pdf: bool = True) -> tuple[bytes, str, str]:
    """Render a goods received note (D1, A4)."""
    context = grn_context(gate_in)
    html = render_to_string("documents/grn.html", context)
    return _render(html, as_pdf=as_pdf, filename=f"grn-{gate_in.number or gate_in.pk}")


def _render(html: str, *, as_pdf: bool, filename: str) -> tuple[bytes, str, str]:
    if not as_pdf:
        return html.encode("utf-8"), "text/html; charset=utf-8", f"{filename}.html"

    try:
        from weasyprint import HTML
    except (ImportError, OSError) as exc:
        # OSError covers a present package with missing GTK libraries, which is
        # the common case on Windows and in a slim container.
        logger.warning("PDF rendering unavailable, serving HTML instead: %s", exc)
        return html.encode("utf-8"), "text/html; charset=utf-8", f"{filename}.html"

    pdf = HTML(string=html).write_pdf()
    return pdf, "application/pdf", f"{filename}.pdf"


def pdf_available() -> bool:
    """Whether real PDF rendering is possible on this host."""
    try:
        import weasyprint  # noqa: F401
    except (ImportError, OSError):
        return False
    return True


def gate_pass_context(gate_out) -> dict:
    """Everything the printed gate pass shows (G4, A4, E1)."""
    organization = gate_out.organization

    lines = []
    for line in gate_out.lines.select_related("item_type", "owner_client").all():
        serials = [str(entry.serial_unit) for entry in line.serials.select_related("serial_unit")]
        drums = [
            f"{entry.reel.drum_number} ({entry.length_requested} {line.uom})"
            for entry in line.reels.select_related("reel")
        ]
        lines.append(
            {
                "number": line.line_number,
                "item": str(line.item_type),
                "quantity": line.requested_qty,
                "released": line.released_qty,
                "uom": line.uom,
                "condition": line.get_condition_display(),
                # E1: client-owned material is distinguishable wherever it
                # appears — including on the paper the driver carries.
                "owner": str(line.owner_client) if line.owner_client_id else "Own stock",
                "is_client_owned": bool(line.owner_client_id),
                "serials": serials,
                "drums": drums,
                "returnable": line.is_returnable,
                "return_by": line.expected_return_date,
            }
        )

    approvals = []
    from approvals.models import ApprovalAction

    for action in (
        ApprovalAction.objects.filter(
            approval_request__document_type="dispatch.GateOut",
            approval_request__document_id=str(gate_out.pk),
        )
        .select_related("actor", "on_behalf_of", "approval_request")
        .order_by("decided_at")
    ):
        approvals.append(
            {
                # F5: reads as "X on behalf of Y", never as Y.
                "who": action.attribution,
                "decision": action.get_decision_display(),
                "when": action.decided_at,
                "method": action.get_auth_method_display(),
            }
        )

    return {
        "organization": organization,
        "logo_url": logo_data_uri(organization),
        "gate_out": gate_out,
        "lines": lines,
        "approvals": approvals,
        "qr": qr_png_data_uri(gate_out),
        "destination": gate_out.destination_label,
    }


def grn_context(gate_in) -> dict:
    """Everything the printed GRN shows (D1, D6, A4)."""
    organization = gate_in.organization

    lines = []
    for line in gate_in.lines.select_related("item_type", "owner_client").all():
        lines.append(
            {
                "number": line.line_number,
                "item": str(line.item_type),
                "quantity": line.quantity,
                "uom": line.uom,
                "condition": line.get_condition_display(),
                "owner": str(line.owner_client) if line.owner_client_id else "Own stock",
                "is_client_owned": bool(line.owner_client_id),
                "serials": [entry.serial_number for entry in line.serials.all()],
                "drums": [
                    f"{entry.drum_number} ({entry.length} {line.uom})" for entry in line.reels.all()
                ],
            }
        )

    return {
        "organization": organization,
        "logo_url": logo_data_uri(organization),
        "gate_in": gate_in,
        "lines": lines,
        "qr": qr_png_data_uri(gate_in),
    }


# --------------------------------------------------------------------------
# T6.2, T6.5 — Phase 6 documents
# --------------------------------------------------------------------------


def render_disposal_certificate(disposal, *, as_pdf: bool = True) -> tuple[bytes, str, str]:
    """Render a certificate of disposal (J3, §11).

    J3 asks for a disposal *record* so write-offs are "controlled and
    auditable". Auditable means somebody outside the company can check it, which
    is why the handler's own reference and the approval trail are on the page
    rather than in the database only.
    """
    context = disposal_certificate_context(disposal)
    html = render_to_string("documents/disposal_certificate.html", context)
    return _render(html, as_pdf=as_pdf, filename=f"disposal-{disposal.number or disposal.pk}")


def render_return_waybill(gate_out, *, as_pdf: bool = True) -> tuple[bytes, str, str]:
    """Render a return waybill (K2, §11).

    Gated on ``client_waybill_enabled`` by the caller, not here: whether a tenant
    uses our paperwork is a setting, but rendering has to work in a test and in a
    preview regardless.
    """
    context = return_waybill_context(gate_out)
    html = render_to_string("documents/return_waybill.html", context)
    return _render(html, as_pdf=as_pdf, filename=f"return-waybill-{gate_out.number or gate_out.pk}")


def disposal_certificate_context(disposal) -> dict:
    """Everything the certificate shows (J3, A4, E1, C8)."""
    from approvals.models import ApprovalAction
    from disposition.services import written_off_value

    lines = []
    for line in disposal.lines.select_related("item_type", "owner_client", "serial_unit", "reel"):
        lines.append(
            {
                "number": line.line_number,
                "item": str(line.item_type),
                "quantity": line.quantity,
                "uom": line.uom,
                "condition": line.get_condition_display(),
                "owner": str(line.owner_client) if line.owner_client_id else "Own stock",
                "is_client_owned": bool(line.owner_client_id),
                "serials": ([line.serial_unit.serial_number] if line.serial_unit_id else []),
                "drums": [line.reel.drum_number] if line.reel_id else [],
                "notes": line.notes,
            }
        )

    approvals = []
    for action in (
        ApprovalAction.objects.filter(
            approval_request__document_type=disposal._meta.label,
            approval_request__document_id=str(disposal.pk),
        )
        .select_related("actor", "on_behalf_of", "approval_request")
        .order_by("decided_at")
    ):
        approvals.append(
            {
                # §4.2: "X on behalf of Y", never just Y.
                "who": action.attribution,
                "decision": action.get_decision_display(),
                "when": action.decided_at,
                "method": action.get_auth_method_display(),
                "reason": action.reason,
            }
        )

    organization = disposal.organization
    return {
        "organization": organization,
        "logo_url": logo_data_uri(organization),
        "disposal": disposal,
        "lines": lines,
        "approvals": approvals,
        "written_off_value": written_off_value(disposal),
        "currency": organization.settings.currency,
        "qr": qr_png_data_uri(disposal),
    }


def return_waybill_context(gate_out) -> dict:
    """Everything the waybill shows (K1, K2, K3, A4)."""
    lines = []
    for line in gate_out.lines.select_related("item_type", "owner_client").all():
        serials = [str(entry.serial_unit) for entry in line.serials.select_related("serial_unit")]
        drums = [
            f"{entry.reel.drum_number} ({entry.length_requested} {line.uom})"
            for entry in line.reels.select_related("reel")
        ]
        lines.append(
            {
                "number": line.line_number,
                "item": str(line.item_type),
                "quantity": line.requested_qty,
                "released": line.released_qty or None,
                "uom": line.uom,
                "condition": line.get_condition_display(),
                # K1: "for recoveries, the originating site." The client's
                # receiving store often wants to know which of their sites it
                # came off, and that is on the unit rather than the line.
                "origin": _origin_of(line),
                "serials": serials,
                "drums": drums,
            }
        )

    organization = gate_out.organization
    return {
        "organization": organization,
        "logo_url": logo_data_uri(organization),
        "gate_out": gate_out,
        "lines": lines,
        "ack": getattr(gate_out, "client_return_ack", None),
        "qr": qr_png_data_uri(gate_out),
    }


def _origin_of(line) -> str:
    """The site a recovered unit came off, when there is one (K1)."""
    for entry in line.serials.select_related("serial_unit__origin_site"):
        site = getattr(entry.serial_unit, "origin_site", None)
        if site is not None:
            return site.internal_ref or site.name
    return ""
