"""時間用の1行 ── **対象ライン・早出・残業つき**(v4.49.0)

    テーブル：時間用に対象ラインの列を追加して管理できるようにしたい
    ただし1，2，3直の概念はなくならないので設定がなければ1，2，3直で見るままでよい

時間用(伝送用ファイル)の列:

    直         1・2・3・昼 と、4・5 … (決まったラインだけの直)
    開始・終了  07:00 の形
    対象ライン  空 = 全ライン共通(いままでどおり)。書けばそのラインだけ。
               複数は「、」で区切る(NS1、機側)
    早出・残業  分。4直・5直…だけが使う(入力できる幅。`logic/shift.ExtraShift`)

**列が無い表でも読みます**(全部の行が全ライン共通・早出残業 0 ── いままでと同じ)。
列の名前は「早出(分)」「残業(分)」でも読みます。

あるラインで使う直(:func:`for_line`):

    1. 対象ラインが空の行(全ライン共通)
    2. 対象ラインにそのラインがある行。**同じ直なら共通の行より勝つ**
       (決まったラインだけ 2直の時刻を変える、もできる)

この層は**純粋**。DB もファイルも触りません。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

from . import line_names
from .shift import BASE_KEYS, ExtraShift, ShiftCalculator, shift_name, times_from_master, usable_pair

KEY_COLUMN = "直"
START_COLUMN = "開始"
END_COLUMN = "終了"
LINES_COLUMN = "対象ライン"
#: 早出・残業の列。**前の名前が先**(マスタで足すときの名前)。括弧つきでも読む
EARLY_COLUMNS: tuple[str, ...] = ("早出", "早出(分)", "早出（分）")
OVERTIME_COLUMNS: tuple[str, ...] = ("残業", "残業(分)", "残業（分）")

#: 対象ラインの区切り(読点・カンマ・スラッシュ・空白)
_SPLIT = re.compile(r"[、,，/／\s]+")


@dataclass(frozen=True)
class ShiftRow:
    """時間用の1行。`lines` が空なら全ライン共通。"""

    key: str
    start: str
    end: str
    lines: tuple[str, ...] = ()
    early: int = 0
    overtime: int = 0

    @property
    def name(self) -> str:
        return shift_name(self.key)

    @property
    def stored_key(self) -> str:
        """手元の表(`shift_config`)での鍵。共通は「4」、ラインつきは「4@NS1、機側」。"""
        return f"{self.key}@{'、'.join(self.lines)}" if self.lines else self.key

    def applies(self, line: str) -> bool:
        """`line` で使う行か。"""
        return not self.lines or _line(line) in self.lines


def _norm(text: object) -> str:
    return unicodedata.normalize("NFKC", "" if text is None else str(text)).strip()


def _line(text: object) -> str:
    """ラインの比べる形(前の名前 `LS` も正規へ。中板3 は 中板 ── 番号は見ない)。"""
    value = _norm(text)
    return line_names.to_code(value) or line_names.upgrade(value)


def parse_lines(text: object) -> tuple[str, ...]:
    """「NS1、機側」→ ("NS1", "機側")。空なら ()(全ライン共通)。"""
    out: list[str] = []
    for part in _SPLIT.split(_norm(text)):
        name = _line(part)
        if name and name not in out:
            out.append(name)
    return tuple(out)


def parse_minutes(text: object) -> Optional[int]:
    """「120」「１２０」→ 120。空は 0。**読めなければ None**(マスタ管理が断る)。"""
    value = _norm(text)
    if not value:
        return 0
    if value.endswith("分"):
        value = value[:-1].strip()
    try:
        number = float(value)
    except ValueError:
        return None
    if number != number or number < 0 or not number.is_integer() or number > 24 * 60:
        return None
    return int(number)


def _pick(row: Mapping[str, object], names: Iterable[str]) -> object:
    for name in names:
        if name in row and _norm(row.get(name)):
            return row.get(name)
    return ""


def from_table(rows: Iterable[Mapping[str, object]]) -> list[ShiftRow]:
    """時間用の行(列名 → 値)を読む。直が空の行は飛ばす。早出・残業が読めなければ 0。"""
    out: list[ShiftRow] = []
    for row in rows:
        key = _norm(row.get(KEY_COLUMN, ""))
        if not key:
            continue
        if key.endswith("直") and len(key) > 1:
            key = key[:-1]                       # 「4直」と書いても「4」
        out.append(ShiftRow(
            key=key,
            start=_norm(row.get(START_COLUMN, "")),
            end=_norm(row.get(END_COLUMN, "")),
            lines=parse_lines(row.get(LINES_COLUMN, "")),
            early=parse_minutes(_pick(row, EARLY_COLUMNS)) or 0,
            overtime=parse_minutes(_pick(row, OVERTIME_COLUMNS)) or 0))
    return out


def common(rows: Iterable[ShiftRow]) -> dict[str, tuple[str, str]]:
    """全ライン共通の行だけ ``{鍵: (開始, 終了)}``(いままでの `get_shift_times` の形)。"""
    return {r.key: (r.start, r.end) for r in rows if not r.lines}


def for_line(rows: Iterable[ShiftRow], line: str
             ) -> tuple[dict[str, tuple[str, str]], tuple[ExtraShift, ...]]:
    """`line` で使う直。``({鍵: (開始, 終了)}, 4直・5直…)``。

    鍵と時刻の辞書には 4直・5直…も入れます(`bounds_text` で名指しできるように)。
    **ラインの行は共通の行に勝つ。** 同じ直のラインの行が2つあれば後の行。
    """
    rows = list(rows)
    times: dict[str, tuple[str, str]] = {}
    meta: dict[str, ShiftRow] = {}
    for preferred in (False, True):             # 共通の行 → ラインの行(勝つ)
        for row in rows:
            if bool(row.lines) != preferred or not row.applies(line):
                continue
            times[row.key] = (row.start, row.end)
            meta[row.key] = row
    extras = []
    for key, row in meta.items():
        if key in BASE_KEYS:
            continue
        pair = usable_pair(times[key])
        if pair is None:
            continue
        extras.append(ExtraShift(row.name, pair[0], pair[1], row.early, row.overtime))
    return times, tuple(sorted(extras, key=lambda e: _order(e.name)))


def _order(name: str) -> tuple[int, str]:
    digits = "".join(c for c in name if c.isdigit())
    return (int(digits) if digits else 999, name)


def calculator_for_line(rows: Iterable[ShiftRow], line: str) -> ShiftCalculator:
    """そのラインの直の計算機(4直・5直…を含む)。ラインの行が無ければいままでと同じ。"""
    times, extras = for_line(rows, line)
    return ShiftCalculator(times_from_master(times, extras))


def line_shift_names(rows: Iterable[ShiftRow], line: str) -> list[str]:
    """そのラインの 4直・5直…の名前(並べる順)。無ければ空。"""
    return [e.name for e in for_line(rows, line)[1]]


def all_extra_names(rows: Iterable[ShiftRow]) -> list[str]:
    """どれかのラインの 4直・5直…の名前(並べる順)。集計の並びに使う。"""
    names = {shift_name(r.key) for r in rows if r.key not in BASE_KEYS}
    return sorted(names, key=_order)
