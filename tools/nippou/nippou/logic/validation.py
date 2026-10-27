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


def duplicate_lot_rows(lot_values: list[str]) -> dict[str, list[int]]:
    """重複しているロット№ → その行番号(1から)。**決まりは `find_duplicate_lots` そのもの**です。

    入力画面が、保存を押す前に重複した行へ印を付けるためのもの(v4.44.10)。保存の
    「LOTが重複しています」と同じものを数えます ── 空の行(ロット№を空けて上の行の
    続きを打った行)は数えず、前後の空白は落として比べます。決まりを2つ書かないよう、
    ここは `find_duplicate_lots` の答えに行番号を添えるだけです。
    """
    found = find_duplicate_lots(lot_values)
    return {lot: [n for n, v in enumerate(lot_values, start=1) if v.strip() == lot]
            for lot in found}
