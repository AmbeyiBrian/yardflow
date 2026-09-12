"""Pagination (design §6).

Cursor pagination rather than page numbers, so that deep pages stay cheap and a
row inserted mid-listing cannot shift results across pages — which matters for
movement history, where new rows arrive constantly (N-2).

DRF's default cursor ordering is ``-created``; every model here uses
``created_at`` (§4), so the default is set once rather than on each view.
"""

from rest_framework.pagination import CursorPagination


class TimestampCursorPagination(CursorPagination):
    """Newest first, by creation time."""

    ordering = "-created_at"
    page_size_query_param = "page_size"
    max_page_size = 200


class OccurrenceCursorPagination(CursorPagination):
    """For append-only records timestamped by when they happened, not written.

    Used by the audit trail and the stock ledger, where ``occurred_at`` is the
    meaningful order and there is no ``created_at`` (§3.2, §4.2).
    """

    ordering = "-occurred_at"
    page_size_query_param = "page_size"
    max_page_size = 200


class JoinedCursorPagination(CursorPagination):
    """For ``User``, which records ``date_joined`` rather than ``created_at``.

    ``User`` is not a ``TimeStampedModel`` — it predates this project's own base
    classes, being Django's own auth model — so the default cursor ordering
    resolves to nothing and every list of people would be a 500.
    """

    ordering = "-date_joined"
    page_size_query_param = "page_size"
    max_page_size = 200
