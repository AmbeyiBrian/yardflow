"""A startup check for PDF rendering (§11; G4, K2).

The documents degrade to HTML where WeasyPrint cannot be imported, which is
deliberate: a missing system library must not stop a driver leaving with
something in their hand. But the degradation is silent, and it stayed silent
here for the whole build — the dependency was pinned to ``WeasyPrint==69.1``, a
version that does not exist, so the install never happened, the import always
failed, and every gate pass anybody looked at was a web page. The tests accepted
"a PDF or HTML", so nothing said otherwise.

A warning at startup, in the same spirit as the SMS provider check: the server
says plainly which of the two it will produce, before somebody prints twenty
gate passes and discovers it at the gate.
"""

from __future__ import annotations

from django.core.checks import Warning, register


@register()
def check_pdf_rendering_is_available(app_configs, **kwargs):  # type: ignore[no-untyped-def]
    """Warn when documents will be served as HTML rather than PDF."""
    from dispatch.documents import pdf_available

    if pdf_available():
        return []

    return [
        Warning(
            "PDF rendering is unavailable, so gate passes, GRNs, waybills and "
            "certificates will be served as web pages instead of PDFs.",
            hint=(
                "WeasyPrint needs Pango and its GObject libraries on the host. "
                "On Linux install libpango-1.0-0, libpangoft2-1.0-0 and "
                "libharfbuzz0b. On Windows install the GTK3 runtime, then "
                "restart the server. Documents still work meanwhile — they "
                "arrive as HTML, which prints from a browser."
            ),
            id="dispatch.W001",
        )
    ]
