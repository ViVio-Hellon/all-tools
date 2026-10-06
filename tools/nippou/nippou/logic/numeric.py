"""Small numeric helpers shared by the calculation logic.

VBA's ``IsNumeric`` and ``Format(x, "0")`` / ``Format(x, "0.00")`` have
behaviour that Python's own coercion doesn't reproduce out of the box:
``IsNumeric`` accepts blanks-as-False but also accepts leading/trailing
whitespace and a leading ``+``/``-``; ``Format`` rounds half-away-from-zero
rather than Python's banker's rounding. Both are reimplemented here so the
ported calculations match the original tool's arithmetic exactly.
"""
from __future__ import annotations

import math


def is_numeric(value: object) -> bool:
    """Mirror VBA's ``IsNumeric`` for the plain strings/numbers this app
    ever stores in a textbox (empty string is not numeric)."""
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return True
    text = str(value).strip()
    if text == "":
        return False
    try:
        float(text)
        return True
    except ValueError:
        return False


def to_float(value: object) -> float:
    return float(str(value).strip())


def vba_round_half_away_from_zero(value: float, ndigits: int) -> float:
    """VBA's ``Format`` rounds half-away-from-zero, unlike Python's
    built-in ``round`` (banker's rounding)."""
    factor = 10 ** ndigits
    scaled = value * factor
    if scaled >= 0:
        rounded = math.floor(scaled + 0.5)
    else:
        rounded = math.ceil(scaled - 0.5)
    return rounded / factor


def format_fixed(value: float, ndigits: int) -> str:
    """Equivalent of VBA ``Format(value, "0")`` / ``Format(value, "0.00")``."""
    rounded = vba_round_half_away_from_zero(value, ndigits)
    return f"{rounded:.{ndigits}f}"
