"""早見表の表示内容 (VBA `UFquick` の置き換え)

**長さは計算画面と同じ式(`vc_calc.vc_length`)で出す。**

    π × ((肉厚×2 + 内径)² − 内径²) ÷ (4 × VC厚) ÷ 1000

(ブック「自動計算 肉厚から長さ」の式から、転記ミスの `−肉厚` を取り除いたもの)。VC厚 は枠の「計算品種」の
VC品種 から引くので、VC厚 を直せば早見表も変わる(同じ事実を2か所に持たない)。
整数への丸めは アプリ設定「早見表の丸め」(既定 切り捨て)。

紙の早見表(R.1.10/1)は `−肉厚` の入った式から手で整数にしたもので、正しい式の
切り捨てとは 122 セル中 13 セルが 1m 違う(docs/vc/VBA解析.md §12)。

計算品種の無い枠(R575B。ブックにも VBA にも無い)は、早見表値.長さ をそのまま出す。
列の見出し = その枠に入っている肉厚の和集合(小さい順)、行 = 内径(小さい順)。
値の無いセルは空欄(紙の 2008系 内径95 の 47 と同じ)。
"""
from __future__ import annotations

from typing import Any, Optional

from .calc import vc_length
from .masters import QuickBlock, Snapshot
from .vba_compat import excel_to_int

ROUNDINGS = ("切り捨て", "四捨五入")


def number_text(value: float) -> str:
    """見出しの数。整数なら小数点を付けない(5 / 87)。"""
    return str(int(value)) if float(value).is_integer() else format(value, "g")


def rounding_of(snapshot: Snapshot) -> str:
    mode = snapshot.setting("早見表の丸め", "切り捨て").strip()
    return mode if mode in ROUNDINGS else "切り捨て"


def cell_value(block: QuickBlock, inside: float, thickness: float,
               fixed: Optional[int], mode: str) -> Optional[int]:
    """1セルの長さ(m)。式で出せなければ None。"""
    if block.product:
        if block.vcatu is None or block.vcatu <= 0:
            return None
        return excel_to_int(vc_length(thickness, block.vcatu, inside), mode)
    return fixed


def build(snapshot: Snapshot) -> dict[str, Any]:
    mode = rounding_of(snapshot)
    blocks = []
    for block in snapshot.quick:
        heads = sorted({t for row in block.rows for t in row.cells})
        problem = ""
        if block.product and block.vcatu is None:
            problem = (f"計算品種「{block.product}」が VC品種 にありません。"
                       "マスタ管理 → 早見表ブロック で直してください。")
        rows = []
        for row in block.rows:
            values = []
            for t in heads:
                if t not in row.cells:
                    values.append("")
                    continue
                v = cell_value(block, row.inside, t, row.cells[t], mode)
                values.append("" if v is None else str(v))
            rows.append({"inside": number_text(row.inside),
                         "label": f"内径{number_text(row.inside)}mm", "cells": values})
        blocks.append({
            "name": block.name,
            "vendor": block.vendor,
            "tone": block.tone,
            "source": "式" if block.product else "固定値",
            "vcatu": None if block.vcatu is None else format(block.vcatu, "g"),
            "problem": problem,
            "headers": [number_text(t) for t in heads],
            "rows": rows,
        })
    width = max((len(b["headers"]) for b in blocks), default=0)
    return {
        "title": snapshot.setting("早見表タイトル", "VCフィルム長さ早見表"),
        "revision_label": snapshot.setting("早見表改訂", ""),
        "rounding": mode,
        "columns": width,
        "blocks": blocks,
    }
