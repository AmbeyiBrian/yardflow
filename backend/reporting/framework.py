"""The report framework (design §10; M1, M2).

§10: "each report in M1 is a class with a ``query(params) -> QuerySet`` and a
column spec, so the API view, the Excel writer and the PDF renderer all read one
definition. That is what keeps the export consistent with what the screen showed
(M2)."

T7.1's criterion is the test of whether that worked: **adding a report must
require no changes to the export code.** So nothing here knows the name of any
report, and nothing in `exports.py` does either — a report is registered, and the
three consumers walk its columns.

The design decisions worth stating:

**Columns own their formatting, not the exporters.** A quantity is three decimal
places in Excel and in the PDF and on the screen, because the column says so
once. The alternative — each exporter formatting by inspecting types — is how an
export ends up disagreeing with the screen it came from, which is exactly what M2
is about.

**Rows are dicts, not model instances.** A report may aggregate, join or compute;
tying the contract to a queryset of one model would mean the aggregating reports
need a second path, and then the exporters need to know which kind they have.
``rows()`` returns dicts and ``query()`` stays available for the reports that
genuinely are a queryset (so filters and pagination can push down to SQL).

**Every report declares its filters.** Not for validation's sake — for the
screen. T7.7 renders a filter panel per report from this, so a new report gets a
usable UI without anybody writing one.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any


class ReportError(Exception):
    """A report was asked for something it cannot answer."""


@dataclass(frozen=True)
class Column:
    """One column, and everything all three consumers need to know about it."""

    key: str
    label: str
    #: How to render a value. ``text`` is the default; the others carry alignment
    #: and number formats through to Excel and the PDF.
    kind: str = "text"
    #: Right-aligned in every output when true. Set automatically for numbers.
    numeric: bool = False
    #: Hidden on a phone (§7.3). The exports always include it.
    wide_only: bool = False
    #: Column width hint for Excel, in characters.
    width: int = 18

    def format(self, value: Any) -> str:
        """The value as it appears to a person, in any output.

        One implementation, so the screen, the spreadsheet and the PDF cannot
        disagree about what a number looks like (M2).
        """
        if value is None:
            return ""
        if self.kind == "quantity":
            return f"{Decimal(str(value)):.3f}"
        if self.kind == "money":
            return f"{Decimal(str(value)):,.2f}"
        if self.kind == "integer":
            return f"{int(value)}"
        if self.kind == "date":
            return value.isoformat()[:10] if hasattr(value, "isoformat") else str(value)
        if self.kind == "datetime":
            if hasattr(value, "strftime"):
                return value.strftime("%Y-%m-%d %H:%M")
            return str(value)[:16].replace("T", " ")
        if self.kind == "boolean":
            return "yes" if value else "no"
        return str(value)

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "numeric": self.numeric or self.kind in ("quantity", "money", "integer"),
            "wide_only": self.wide_only,
        }


def quantity(key: str, label: str, **kwargs) -> Column:
    return Column(key, label, kind="quantity", numeric=True, **kwargs)


def money(key: str, label: str, **kwargs) -> Column:
    return Column(key, label, kind="money", numeric=True, **kwargs)


def integer(key: str, label: str, **kwargs) -> Column:
    return Column(key, label, kind="integer", numeric=True, **kwargs)


def text(key: str, label: str, **kwargs) -> Column:
    return Column(key, label, **kwargs)


def when(key: str, label: str, **kwargs) -> Column:
    return Column(key, label, kind="datetime", width=20, **kwargs)


def on_date(key: str, label: str, **kwargs) -> Column:
    return Column(key, label, kind="date", width=14, **kwargs)


def flag(key: str, label: str, **kwargs) -> Column:
    return Column(key, label, kind="boolean", width=10, **kwargs)


@dataclass(frozen=True)
class Filter:
    """A parameter a report accepts, described well enough to render (T7.7)."""

    key: str
    label: str
    #: text | date | datetime | choice | reference
    kind: str = "text"
    required: bool = False
    #: For ``choice``: the options. For ``reference``: the endpoint to look up.
    choices: tuple[tuple[str, str], ...] = ()
    resource: str = ""
    help_text: str = ""

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "required": self.required,
            "choices": [{"value": value, "label": label} for value, label in self.choices],
            "resource": self.resource,
            "help_text": self.help_text,
        }


class Report:
    """One report (§10, M1).

    Subclasses set ``slug``, ``title``, ``columns`` and implement ``rows``. Two
    optional extras earn their keep:

    * ``query`` — return a queryset when the report *is* one, so filtering and
      counting stay in SQL rather than in Python
    * ``totals`` — a footer row, because "and what does that add up to?" is the
      next question for half of these reports and computing it in three places
      is three chances to get it wrong
    """

    slug: str = ""
    title: str = ""
    description: str = ""
    #: Which requirement this exists for, shown on the report list so somebody
    #: can tell at a glance which report answers an auditor's question.
    requirement: str = ""
    columns: Sequence[Column] = ()
    filters: Sequence[Filter] = ()
    #: The permission needed. Reporting is normally `report.view_all`, but the
    #: technician-facing ones are deliberately narrower.
    required_permission: str = "report.view_all"
    #: Rough guide for the async threshold (T7.5). Reports that can only ever
    #: return a handful of rows say so and never queue.
    can_be_large: bool = True

    def rows(self, params: dict) -> Iterable[dict]:  # pragma: no cover - interface
        raise NotImplementedError

    def query(self, params: dict):
        """A queryset, when this report is one. ``None`` otherwise."""
        return None

    def totals(self, rows: Sequence[dict]) -> dict | None:
        """A footer, or ``None``. Summing every numeric column is the default."""
        numeric = [column for column in self.columns if column.numeric]
        if not numeric:
            return None

        summed: dict[str, Any] = {}
        for column in numeric:
            total = Decimal("0")
            seen = False
            for row in rows:
                value = row.get(column.key)
                if value in (None, ""):
                    continue
                try:
                    total += Decimal(str(value))
                    seen = True
                except (ArithmeticError, ValueError):
                    # A numeric-looking column that is not summable (a count of
                    # mixed units, say). Better to omit it than to print a
                    # nonsense total.
                    seen = False
                    break
            if seen:
                summed[column.key] = total
        return summed or None

    # -- helpers for subclasses ---------------------------------------------

    def as_dict(self) -> dict:
        """What the report list and the filter panel are built from (T7.7)."""
        return {
            "slug": self.slug,
            "title": self.title,
            "description": self.description,
            "requirement": self.requirement,
            "columns": [column.as_dict() for column in self.columns],
            "filters": [filter_.as_dict() for filter_ in self.filters],
            "can_be_large": self.can_be_large,
        }

    def render(self, params: dict) -> dict:
        """Rows, formatted values and totals — the payload every consumer uses.

        Both the raw value and its formatted string travel together. The screen
        and the PDF want the string; a spreadsheet wants the number so the
        recipient can sum a column themselves (M2). Formatting once here is what
        keeps the two identical.
        """
        rows = list(self.rows(params))
        return {
            "slug": self.slug,
            "title": self.title,
            "columns": [column.as_dict() for column in self.columns],
            "rows": [self._render_row(row) for row in rows],
            "totals": self._render_totals(rows),
            "row_count": len(rows),
            "params": {key: str(value) for key, value in params.items() if value not in (None, "")},
        }

    def _render_row(self, row: dict) -> dict:
        return {column.key: column.format(row.get(column.key)) for column in self.columns}

    def _render_totals(self, rows: Sequence[dict]) -> dict | None:
        totals = self.totals(rows)
        if not totals:
            return None
        return {
            column.key: column.format(totals[column.key])
            for column in self.columns
            if column.key in totals
        }

    def raw_rows(self, params: dict) -> list[dict]:
        """Unformatted rows, for the Excel writer's typed cells (M2)."""
        return list(self.rows(params))

    def table(self, params: dict) -> dict:
        """Rows as ordered cells, for the printed page.

        The PDF template iterates columns and needs each row in the same order;
        a dict would force a template filter to look values up by key. Same
        formatting as everything else — it comes from the columns.
        """
        rows = list(self.rows(params))
        totals = self.totals(rows)
        return {
            "columns": list(self.columns),
            "rows": [
                [column.format(row.get(column.key)) for column in self.columns]
                for row in rows
            ],
            "totals": (
                [
                    column.format(totals[column.key]) if column.key in totals else ""
                    for column in self.columns
                ]
                if totals
                else None
            ),
            "row_count": len(rows),
        }


# --------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------

_REGISTRY: dict[str, Report] = {}


def register(report_class: type[Report]) -> type[Report]:
    """Register a report. Usable as a decorator.

    The registry is what makes T7.1's criterion true: the API view, the Excel
    writer and the PDF renderer look reports up here and read their columns, so
    a new report is a new class and nothing else.
    """
    if not report_class.slug:
        raise ReportError(f"{report_class.__name__} needs a slug.")
    if not report_class.columns:
        raise ReportError(f"{report_class.__name__} needs columns.")
    if report_class.slug in _REGISTRY:
        raise ReportError(
            f"Two reports both call themselves {report_class.slug!r}. A slug is "
            f"in URLs and in saved exports, so it has to be unique."
        )
    _REGISTRY[report_class.slug] = report_class()
    return report_class


def get_report(slug: str) -> Report:
    # Loading here as well as in `all_reports`: a request for one report by slug
    # is the commonest entry point, and it has to work whether or not anything
    # has listed the catalogue first.
    _load_reports()
    report = _REGISTRY.get(slug)
    if report is None:
        raise ReportError(f"There is no report called {slug!r}.")
    return report


def all_reports() -> list[Report]:
    """Every registered report, in title order."""
    _load_reports()
    return sorted(_REGISTRY.values(), key=lambda report: report.title)


_LOADED = False


def _load_reports() -> None:
    """Import the report modules so their registrations run.

    Imported lazily rather than in ``apps.ready`` because the reports import
    querysets from half the project, and doing that at startup would make the
    import graph a knot for no gain. Guarded by a flag rather than relying on the
    module cache, so the common path is a boolean check.
    """
    global _LOADED
    if _LOADED:
        return
    from reporting import reports  # noqa: F401

    _LOADED = True


# --------------------------------------------------------------------------
# Parameter parsing, shared by the view and the export task
# --------------------------------------------------------------------------


def parse_params(report: Report, raw: dict) -> dict:
    """Validate and coerce a report's parameters.

    Shared so an async export and the interactive view cannot interpret the same
    query string differently — which would make the file disagree with the screen
    it was exported from (M2).
    """
    params: dict[str, Any] = {}
    for filter_ in report.filters:
        value = raw.get(filter_.key)
        if value in (None, ""):
            if filter_.required:
                raise ReportError(f"{report.title} needs {filter_.label.lower()}.")
            continue
        params[filter_.key] = _coerce(filter_, value)
    return params


def _coerce(filter_: Filter, value: Any) -> Any:
    if filter_.kind in ("date", "datetime"):
        return _parse_moment(value, filter_)
    if filter_.kind == "reference":
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ReportError(f"{filter_.label} should be an id.") from exc
    if filter_.kind == "choice" and filter_.choices:
        allowed = {choice for choice, _label in filter_.choices}
        if str(value) not in allowed:
            raise ReportError(
                f"{filter_.label} should be one of {', '.join(sorted(allowed))}."
            )
    return value


def _parse_moment(value: Any, filter_: Filter) -> datetime | date:
    from django.utils.dateparse import parse_date, parse_datetime

    if isinstance(value, (datetime, date)):
        return value

    text_value = str(value)
    if filter_.kind == "date":
        parsed_date = parse_date(text_value[:10])
        if parsed_date is None:
            raise ReportError(f"{filter_.label} should look like 2026-08-22.")
        return parsed_date

    parsed = parse_datetime(text_value) or parse_date(text_value[:10])
    if parsed is None:
        raise ReportError(f"{filter_.label} should be a date or a timestamp.")
    return parsed


# --------------------------------------------------------------------------
# Small helpers the reports share
# --------------------------------------------------------------------------


@dataclass
class Grouped:
    """Accumulate rows by key, for the reports that aggregate in Python.

    Used where the aggregation cannot be pushed into SQL — reconciliation, for
    instance, walks the ledger once and derives four figures from it (§10).
    """

    key: Callable[[dict], tuple]
    rows: dict[tuple, dict] = field(default_factory=dict)

    def add(self, row: dict, sum_keys: Sequence[str]) -> dict:
        identity = self.key(row)
        existing = self.rows.get(identity)
        if existing is None:
            self.rows[identity] = dict(row)
            return self.rows[identity]
        for key in sum_keys:
            existing[key] = Decimal(str(existing.get(key) or 0)) + Decimal(
                str(row.get(key) or 0)
            )
        return existing

    def values(self) -> list[dict]:
        return list(self.rows.values())
