"""Box chains for the gate-out read sides (design §4.15.8).

Both the gate-out detail and the offline releasable bundle carry, per line, the
box it was picked from and, per named unit, the chain of boxes the unit is in
**now**. The storekeeper scans a pallet on the shelf, so the live chain is what
has to match, not where the unit sat when the pass was raised.

Boxes nest at most three deep (P10), so ``box__parent__parent`` reaches the
whole chain and a prefetch of these paths costs a constant number of queries.
"""

from __future__ import annotations

#: Prefetch paths that make `box_path` free on a pass's lines and units.
LINE_BOX_PREFETCH = ("lines__box__parent__parent",)
UNIT_BOX_PREFETCH = ("lines__serials__serial_unit__box__parent__parent",)
BOX_PREFETCH = LINE_BOX_PREFETCH + UNIT_BOX_PREFETCH


def box_path(box) -> list[str]:  # type: ignore[no-untyped-def]
    """Box codes from the outermost box down to ``box`` (empty when loose)."""
    codes: list[str] = []
    seen: set[int] = set()
    while box is not None and box.pk not in seen:
        seen.add(box.pk)
        codes.append(box.code)
        box = box.parent
    codes.reverse()
    return codes
