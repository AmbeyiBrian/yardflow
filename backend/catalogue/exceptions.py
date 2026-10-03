"""Catalogue refusals (C10)."""

from core.exceptions import DomainError


class ItemTrackingLocked(DomainError):
    """C10: tracking mode and unit of measure cannot change once an item has moved,
    because every past movement of it was recorded under the old meaning."""

    code = "ITEM_TRACKING_LOCKED"
    status_code = 409
    default_message = "This item has stock history, so its tracking mode and unit cannot change."
