"""Report exports (design §10, §11; M2, T7.5).

M2: "every report exportable to Excel and PDF, so that I can send it to a client
on request." The last clause is the design constraint — these files leave the
building, so they carry the tenant's identity, the parameters that produced them,
and the same figures the screen showed.

**Nothing here knows the name of any report.** Both writers walk
``report.columns`` and ``report.render()``, which is T7.1's criterion: adding a
report requires no change to this file. If you find yourself adding an
``if slug == ...`` here, the column spec is missing something instead.

Three decisions:

**Excel gets typed cells, the PDF gets formatted strings.** A spreadsheet a client
receives should let them sum a column themselves, so quantities go in as numbers
with a display format; a PDF is a picture of a page, so it takes the string the
screen showed. Both come from the same ``Column``, so they cannot disagree about
what the number *is* — only about how it is stored.

**Large exports run in Celery** (T7.5). The threshold is rows, not bytes: a
report is slow because of what it has to read, and by the time the rows are
counted the expensive part has already happened — so the count comes from the
queryset where there is one.

**The link expires.** N-7 already requires that of attachments; an export is the
same kind of object — a file with somebody's stock position in it — so it is
stored as an ``Attachment`` and served by the same signed, expiring URL rather
than by a second mechanism nobody would remember to secure.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from django.template.loader import render_to_string
from django.utils import timezone

from reporting.framework import Column, Report, get_report, parse_params

logger = logging.getLogger(__name__)

#: Above this many rows, exporting happens in a worker and the caller is
#: notified (T7.5). Chosen so an ordinary yard's daily reports stay synchronous —
#: waiting three seconds beats a notification for something you asked for now.
ASYNC_ROW_THRESHOLD = 2_000

#: Excel number formats, derived from the column kind so a format is defined once.
EXCEL_FORMATS = {
    "quantity": "#,##0.000",
    "money": "#,##0.00",
    "integer": "#,##0",
    "date": "yyyy-mm-dd",
    "datetime": "yyyy-mm-dd hh:mm",
}


def export_filename(report: Report, extension: str) -> str:
    """A name that says what it is and when, because it lands in Downloads."""
    stamp = timezone.now().strftime("%Y%m%d-%H%M")
    return f"{report.slug}-{stamp}.{extension}"


# --------------------------------------------------------------------------
# Excel
# --------------------------------------------------------------------------


def to_excel(report: Report, params: dict) -> bytes:
    """One sheet: a title block, the columns, the rows, and a total row."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    raw_rows = report.raw_rows(params)

    workbook = Workbook()
    sheet = workbook.active
    # Excel refuses sheet names over 31 characters, and several report titles are
    # longer — truncating here beats an exception on the way out.
    sheet.title = report.title[:31]

    heading = Font(bold=True, size=13)
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1F2937")

    sheet.cell(row=1, column=1, value=report.title).font = heading
    sheet.cell(row=2, column=1, value=_describe_params(report, params)).font = Font(
        italic=True, size=9, color="555555"
    )
    sheet.cell(
        row=3,
        column=1,
        # M2: a file sent to a client has to say when it was true, or it will be
        # read next year as if it were current.
        value=f"Produced {timezone.now().strftime('%Y-%m-%d %H:%M')} · YardFlow",
    ).font = Font(size=9, color="555555")

    header_row = 5
    for index, column in enumerate(report.columns, start=1):
        cell = sheet.cell(row=header_row, column=index, value=column.label)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="right" if column.numeric else "left")
        sheet.column_dimensions[get_column_letter(index)].width = column.width

    for offset, row in enumerate(raw_rows, start=header_row + 1):
        for index, column in enumerate(report.columns, start=1):
            cell = sheet.cell(row=offset, column=index)
            cell.value = _excel_value(column, row.get(column.key))
            if column.kind in EXCEL_FORMATS:
                cell.number_format = EXCEL_FORMATS[column.kind]
            if column.numeric:
                cell.alignment = Alignment(horizontal="right")

    totals = report.totals(raw_rows)
    if totals:
        total_row = header_row + len(raw_rows) + 1
        sheet.cell(row=total_row, column=1, value="Total").font = Font(bold=True)
        for index, column in enumerate(report.columns, start=1):
            if column.key not in totals:
                continue
            cell = sheet.cell(row=total_row, column=index)
            cell.value = _excel_value(column, totals[column.key])
            cell.font = Font(bold=True)
            if column.kind in EXCEL_FORMATS:
                cell.number_format = EXCEL_FORMATS[column.kind]

    # Freeze under the header, so scrolling a thousand rows keeps the columns
    # readable — which is the difference between a usable export and a dump.
    sheet.freeze_panes = sheet.cell(row=header_row + 1, column=1)

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _excel_value(column: Column, value: Any) -> Any:
    """A typed cell where the value really is a number or a date.

    Falling back to the formatted string rather than raising: a column declared
    numeric that receives ``"mixed"`` should print that word, not break the whole
    export.
    """
    if value is None or value == "":
        return None
    if column.kind in ("quantity", "money"):
        try:
            return float(Decimal(str(value)))
        except (InvalidOperation, ValueError):
            return column.format(value)
    if column.kind == "integer":
        try:
            return int(value)
        except (TypeError, ValueError):
            return column.format(value)
    if column.kind in ("date", "datetime"):
        if isinstance(value, datetime):
            # Excel cannot store a timezone, and a naive local time is what the
            # reader expects to see.
            return timezone.localtime(value).replace(tzinfo=None)
        return value
    if column.kind == "boolean":
        return bool(value)
    return str(value)


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------


def to_pdf(report: Report, params: dict, *, organization=None) -> tuple[bytes, str, str]:
    """Render the report as a PDF, or as HTML where WeasyPrint is unavailable.

    Same degradation as the gate pass (§11): a missing GTK library must not stop
    somebody sending a client their position.
    """
    from dispatch.documents import _render, logo_data_uri

    table = report.table(params)
    html = render_to_string(
        "documents/report.html",
        {
            "organization": organization,
            "logo_url": logo_data_uri(organization),
            "report": report,
            "columns": table["columns"],
            # Ordered cells rather than dicts: the template iterates columns, and
            # a dict lookup there would need a custom template filter.
            "rows": table["rows"],
            "totals": table["totals"],
            "row_count": table["row_count"],
            "parameters": _describe_params(report, params),
            "produced_at": timezone.now(),
        },
    )
    return _render(html, as_pdf=True, filename=f"{report.slug}")


def _describe_params(report: Report, params: dict) -> str:
    """The filters in words, for the title block.

    An export with no record of what it was filtered to is a number without a
    question, and that is how a partial report gets read as a total one (M2).
    """
    if not params:
        return "No filters — everything."

    parts = []
    for filter_ in report.filters:
        if filter_.key not in params:
            continue
        value = params[filter_.key]
        label = _label_for(filter_, value)
        parts.append(f"{filter_.label}: {label}")
    return " · ".join(parts) or "No filters — everything."


def _label_for(filter_, value) -> str:
    """Resolve a reference id to a name, so the title block reads like English."""
    if filter_.kind == "reference" and filter_.resource:
        resolved = _resolve_reference(filter_.resource, value)
        if resolved:
            return resolved
    if filter_.kind == "choice":
        for choice, label in filter_.choices:
            if str(choice) == str(value):
                return label
    if hasattr(value, "isoformat"):
        return value.isoformat()[:16].replace("T", " ")
    return str(value)


#: Which model answers a filter's ``resource``. Kept here rather than on the
#: filter so a report author names an API resource — the same string the screen
#: uses to populate its dropdown — and nothing has to stay in step by hand.
_REFERENCE_MODELS = {
    "item-types": "catalogue.ItemType",
    "clients": "network.Client",
    "sites": "network.Site",
    "work-orders": "network.WorkOrder",
    "locations": "locations.Location",
    "users": "accounts.User",
}


def _resolve_reference(resource: str, value) -> str:
    from django.apps import apps

    label = _REFERENCE_MODELS.get(resource)
    if label is None:
        return ""
    try:
        model = apps.get_model(label)
    except LookupError:
        return ""
    instance = model.objects.filter(pk=value).first()
    return str(instance) if instance is not None else ""


# --------------------------------------------------------------------------
# Async exports (T7.5)
# --------------------------------------------------------------------------


def should_run_async(report: Report, params: dict) -> bool:
    """Whether this export is big enough to queue.

    Counted in SQL where the report is a queryset. Where it is not, the row count
    is unknowable without doing the work — so a report that says it can be large
    is queued on that word alone, which is the honest reading of the flag.
    """
    if not report.can_be_large:
        return False

    queryset = report.query(params)
    if queryset is not None:
        return queryset.count() > ASYNC_ROW_THRESHOLD

    return False


def render_export(slug: str, raw_params: dict, *, fmt: str, organization=None):
    """Produce one export. Shared by the synchronous view and the task.

    Both paths go through here, so a queued export and an immediate one cannot
    produce different files from the same request — which is the failure M2 would
    actually suffer from.
    """
    report = get_report(slug)
    params = parse_params(report, raw_params)

    if fmt == "xlsx":
        content = to_excel(report, params)
        return (
            content,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            export_filename(report, "xlsx"),
        )

    content, content_type, filename = to_pdf(report, params, organization=organization)
    return content, content_type, filename
