"""Item type search ranking (C9), shared by the catalogue list and Find stock (E8)."""

from __future__ import annotations

from django.db.models import Case, IntegerField, Q, Value, When


def search_rank(text: str) -> Case:
    """Best matches first: name starts with the text, name contains it, code contains it."""
    return Case(
        When(name__istartswith=text, then=Value(0)),
        When(name__icontains=text, then=Value(1)),
        When(code__icontains=text, then=Value(2)),
        default=Value(3),
        output_field=IntegerField(),
    )


def search_match(text: str) -> Q:
    """The fields a search looks in, as ``ItemTypeViewSet.search_fields`` names them."""
    return Q(name__icontains=text) | Q(code__icontains=text) | Q(description__icontains=text)
