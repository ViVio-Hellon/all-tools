"""Input validation helpers referenced by ``NippouDB_Save`` (e.g.
``CheckDuplicateLots``)."""
from __future__ import annotations

from collections import Counter


def find_duplicate_lots(lot_values: list[str]) -> list[str]:
    """Returns the distinct LOT numbers that appear more than once among
    the non-blank entries in ``lot_values`` (one row's worth of LOT
    textboxes). An empty result means the save may proceed."""
    counts = Counter(v.strip() for v in lot_values if v.strip())
    return sorted(v for v, n in counts.items() if n > 1)
