"""Making a repeated create harmless (N2, §8.2).

The offline queue has always carried a ``client_uuid`` per captured document,
generated once and never regenerated, precisely so a replay cannot turn one
delivery into three. Nothing applied that to the ordinary online path — and a
storekeeper pressing "Save as draft" three times on a slow connection got three
drafts, which is the same defect arriving by a different road.

A button can be disabled while a request is in flight, and this one is, but that
is a race the client cannot win on its own: the second press can leave before
the first response arrives. The uuid settles it at the only place that knows —
the database, which already refuses a duplicate through a unique constraint per
organization.
"""

from __future__ import annotations

from typing import Any


def already_created(model: Any, validated_data: dict) -> Any | None:
    """The document this create would duplicate, if the client sent a uuid.

    Returns ``None`` when there is no uuid (nothing to match on) or nothing
    matches it yet — in which case the caller creates as normal.
    """
    client_uuid = validated_data.get("client_uuid")
    if not client_uuid:
        return None
    return model.objects.filter(client_uuid=client_uuid).first()
