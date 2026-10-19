"""休憩の時刻(作業停止で休憩を入れた行の「11:30〜12:30」)と、その確かめ。

作業停止の休憩(記号 ``0`` ── 停止理由マスタの「休憩食事」)は、**分だけ**を打ちます。何時から
何時まで休んだのかは残っていませんでした。標準時間が溜まってくると、標準より大きく長い作業を
見つけられるようになりますが、そのとき休憩が本当にその行の中だったのか・分が合っているのかが
分からないと、長い理由を休憩と取り違えます。そこで、休憩を入れた行は**時刻**も聞きます
(「その他」の理由と同じ聞き方。v4.37.0)。

- 書き方は ``11:30〜12:30``。``11:30~12:30`` ``11:30-12:30`` ``1130-1230`` ``11時30分〜12時30分``
  全角の数字も受け付けて、``11:30〜12:30`` にそろえます
- 1行に休憩が2つあるとき(10分休憩と昼休み など)は ``10:00〜10:10、12:00〜12:50`` と「、」で並べます
  (「,」「/」「;」でも可)
- 日をまたぐ休憩(``23:30〜0:30``)は 60 分

【確かめること】(v4.37.1。標準時間の「大きく超過」の土台なので)

大きく超過を見るときの作業時間は「停止を全部引いた分」です。休憩の時刻がでたらめだと、
休憩の分もでたらめに引かれ、超過を見落としたり、超過していない人を超過に見せたりします。
そこで次を「直すところ」として扱い、**保存(確定)を止めます**(:data:`ISSUE_CODES`)::

    読めない            「昼」「25:00〜26:00」「11:30」など時刻として読めない
    休憩の記号が無い    作業停止に休憩(0)が無いのに時刻だけ書いてある
    分が合わない        時刻の長さと、作業停止に打った休憩の分が違う
    直の外              その直の時間(1直 07:00〜15:00 など)の外 ── ありえない時刻
    行の外              その行の作業時間(開始〜終了)の外にはみ出している
    休憩どうしが重なる  同じ行に並べた休憩どうし・ほかの行(ほかのページも)の休憩と重なっている
    ほかの停止と重なる  休憩の時間とほかの停止の分を足すと、その行の作業時間に入りきらない
                        (ほかの停止は分しか無いので、入りきるかで見る)

**空は止めません**(書いていない直 ── 前の版や取り込んだ過去の日報 ── を、全部「直すところ」に
しないため)。空の行は画面の窓と一覧で促します。

時刻はこの端末の中と CSV・標準作業時間の実績にだけ残します(共有の日報管理へは送りません ──
紙にも Access にも欄がありません)。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Sequence

from .. import constants

#: 休憩の記号。停止理由マスタの「休憩食事」(`logic/save_checks.REST_STOP_CODE` と同じ)
REST_STOP_CODE = "0"

#: 行の値に持つ欄の名前(`presenters/entry.EXTRA_FAMILIES`・`daily_detail.break_range`)
ROW_FAMILY = "break_range"

#: そろえた書き方の区切り(1つの休憩の始まりと終わり / 休憩と休憩)
SEPARATOR = "〜"
JOINER = "、"

DAY = 24 * 60

_TIME = r"(\d{1,2})\s*(?::|時)\s*(\d{1,2})\s*分?|(\d{3,4})"
_RANGE = re.compile(rf"^\s*(?:{_TIME})\s*(?:〜|~|-|ー|－|から|－)\s*(?:{_TIME})\s*$")
#: 休憩と休憩の区切り(NFKC のあと)
_SPLIT = re.compile(r"[、,;/\n]+")

# 確かめの種類(文言から推し量らない)
UNREADABLE = "unreadable"
NO_BREAK_CODE = "no_break_code"
LENGTH = "length"
OUTSIDE_SHIFT = "outside_shift"
OUTSIDE_ROW = "outside_row"
OVERLAP_SELF = "overlap_self"
OVERLAP_ROW = "overlap_row"
OTHER_STOPS = "other_stops"

ISSUE_CODES: tuple[str, ...] = (UNREADABLE, NO_BREAK_CODE, LENGTH, OUTSIDE_SHIFT, OUTSIDE_ROW,
                                OVERLAP_SELF, OVERLAP_ROW, OTHER_STOPS)


@dataclass(frozen=True)
class BreakRange:
    """読めた休憩の時刻。分は 0 時からの分(終わりが始まりより前なら日をまたぐ)。"""

    start: int
    end: int

    @property
    def minutes(self) -> int:
        span = self.end - self.start
        return span if span > 0 else span + DAY

    def text(self) -> str:
        return f"{clock(self.start)}{SEPARATOR}{clock(self.end)}"


@dataclass(frozen=True)
class Issue:
    """確かめで見つけたもの1件。"""

    code: str
    message: str


def clock(minutes: int) -> str:
    minutes %= DAY
    return f"{minutes // 60}:{minutes % 60:02d}"


def _minute_of(hour: Optional[str], minute: Optional[str], packed: Optional[str]) -> Optional[int]:
    if packed:
        hour, minute = packed[:-2], packed[-2:]
    try:
        h, m = int(hour or ""), int(minute or "")
    except ValueError:
        return None
    if not (0 <= h <= 24 and 0 <= m < 60) or (h == 24 and m != 0):
        return None
    return (h % 24) * 60 + m


def _parse_one(raw: str) -> Optional[BreakRange]:
    found = _RANGE.match(raw)
    if not found:
        return None
    start = _minute_of(*found.group(1, 2, 3))
    end = _minute_of(*found.group(4, 5, 6))
    if start is None or end is None or start == end:
        return None
    return BreakRange(start, end)


def parse_all(text: str) -> Optional[list[BreakRange]]:
    """``11:30〜12:30`` や ``10:00〜10:10、12:00〜12:50`` を読む。

    空なら空のリスト。**1つでも読めなければ None**(読めたぶんだけで数えると、分の確かめが狂う)。
    """
    raw = unicodedata.normalize("NFKC", str(text or "")).strip()
    if not raw:
        return []
    pieces = [p.strip() for p in _SPLIT.split(raw) if p.strip()]
    ranges = [_parse_one(p) for p in pieces]
    if not ranges or any(r is None for r in ranges):
        return None
    return ranges  # type: ignore[return-value]


def parse(text: str) -> Optional[BreakRange]:
    """休憩1つぶんを読む。2つ以上書いてある・読めない・空なら None。"""
    found = parse_all(text)
    return found[0] if found and len(found) == 1 else None


def normalize(text: str) -> str:
    """そろえた書き方。読めなければ打ったまま(黙って消さない)。"""
    found = parse_all(text)
    if found:
        return JOINER.join(r.text() for r in found)
    return str(text or "").strip()


def total_minutes(ranges: Iterable[BreakRange]) -> int:
    return sum(r.minutes for r in ranges)


def _number(text: object) -> float:
    try:
        return float(unicodedata.normalize("NFKC", str(text or "")).strip() or 0)
    except ValueError:
        return 0.0


def break_minutes(values: Mapping[str, str]) -> float:
    """その行で休憩(記号 0)に打った分の合計(作業停止①〜⑤)。"""
    return sum(_number(values.get(minute_family, ""))
               for code_family, minute_family in constants.STOP_SLOTS
               if str(values.get(code_family, "") or "").strip() == REST_STOP_CODE)


def other_stop_minutes(values: Mapping[str, str]) -> float:
    """その行の、休憩でない停止の分の合計。"""
    return sum(_number(values.get(minute_family, ""))
               for code_family, minute_family in constants.STOP_SLOTS
               if str(values.get(code_family, "") or "").strip() not in ("", REST_STOP_CODE))


def has_break(values: Mapping[str, str]) -> bool:
    """その行の作業停止に休憩(記号 0)があるか。"""
    return any(str(values.get(code, "") or "").strip() == REST_STOP_CODE
               for code, _minutes in constants.STOP_SLOTS)


def row_span(values: Mapping[str, str]) -> Optional[tuple[int, int]]:
    """その行の作業時間(開始 KZ:KH 〜 終了 SZ:SH)を 0 時からの分で。日をまたげば終わりに 24 時間足す。"""
    try:
        start = int(str(values.get("KZ", "")).strip()) * 60 + int(str(values.get("KH", "")).strip())
        end = int(str(values.get("SZ", "")).strip()) * 60 + int(str(values.get("SH", "")).strip())
    except ValueError:
        return None
    if end <= start:
        end += DAY
    return start, end


def shift_window(start: str, end: str) -> Optional[tuple[int, int]]:
    """直の ``("07:00", "15:00")`` → 0 時からの分。日をまたげば終わりに 24 時間足す。読めなければ None。"""
    def minute(text: str) -> Optional[int]:
        parts = unicodedata.normalize("NFKC", str(text or "")).strip().split(":")
        if len(parts) != 2:
            return None
        try:
            return int(parts[0]) * 60 + int(parts[1])
        except ValueError:
            return None

    s, e = minute(start), minute(end)
    if s is None or e is None or s == e:
        return None
    return (s, e if e > s else e + DAY)


def _inside(r: BreakRange, window: tuple[int, int]) -> bool:
    """休憩が窓(行・直)の中に収まるか。休憩の始まりをその日・翌日・前日で当ててみる。"""
    start, end = window
    return any(start <= b and b + r.minutes <= end
               for b in (r.start, r.start + DAY, r.start - DAY))


def overlaps(a: BreakRange, b: BreakRange) -> bool:
    """2つの休憩が重なるか(:func:`_overlap` の外向きの名前)。"""
    return _overlap(a, b)


def inside(r: BreakRange, window: tuple[int, int]) -> bool:
    """休憩が窓(行・直)の中に収まるか(:func:`_inside` の外向きの名前)。"""
    return _inside(r, window)


def _overlap(a: BreakRange, b: BreakRange) -> bool:
    """2つの休憩が重なるか(時計の上で。端が接するだけは重ならない)。"""
    a0, a1 = a.start, a.start + a.minutes
    for shift in (-DAY, 0, DAY):
        b0 = b.start + shift
        if max(a0, b0) < min(a1, b0 + b.minutes):
            return True
    return False


def issues(values: Mapping[str, str], *,
           window: Optional[tuple[int, int]] = None, shift: str = "",
           others: Sequence[tuple[str, Sequence[BreakRange]]] = ()) -> list[Issue]:
    """休憩の時刻について、直すところ。合っていれば空(書いていなければ空)。

    ``window`` はその直の時間(:func:`shift_window`)、``others`` はほかの行の
    ``(「2ページ 3行目」, 読めた休憩)``。どちらも無ければ、その行の中だけで見ます。
    """
    text = str(values.get(ROW_FAMILY, "") or "").strip()
    if not text:
        return []
    found = parse_all(text)
    if found is None:
        return [Issue(UNREADABLE, f"「{text}」は時刻として読めません。"
                                  "11:30〜12:30 の形で書いてください(2つあれば「、」で並べる)")]
    out: list[Issue] = []
    total = total_minutes(found)
    typed = break_minutes(values)
    if not has_break(values):
        out.append(Issue(NO_BREAK_CODE, "作業停止に休憩(0)が無いのに、休憩の時刻が書いてあります。"
                                        "休憩をやめたなら時刻を消してください"))
    elif abs(total - typed) >= 1:
        out.append(Issue(LENGTH, f"時刻は {total} 分、作業停止の休憩は {typed:g} 分です"))

    for i, a in enumerate(found):
        for b in found[i + 1:]:
            if _overlap(a, b):
                out.append(Issue(OVERLAP_SELF, f"{a.text()} と {b.text()} が重なっています"))

    span = row_span(values)
    # 直の外: 行の中に収まっていれば言わない(残業でその行が直を越えていることがあるため。
    # 行の時刻そのものは保存の確かめのほかの項目が見る)
    outside_shift = [r for r in found if window is not None and not _inside(r, window)
                     and (span is None or not _inside(r, span))]
    if outside_shift:
        span_text = f"{clock(window[0])}〜{clock(window[1])}" if window else ""
        out.append(Issue(OUTSIDE_SHIFT,
                         f"{'・'.join(r.text() for r in outside_shift)} は"
                         f"{shift or 'この直'}({span_text})の時間ではありません"))
    if span is not None:
        # 直の外と言ったものは重ねて言わない
        outside_row = [r for r in found if r not in outside_shift and not _inside(r, span)]
        if outside_row:
            out.append(Issue(OUTSIDE_ROW,
                             f"{'・'.join(r.text() for r in outside_row)} が、この行の作業時間"
                             f"({clock(span[0])}〜{clock(span[1])})の外にはみ出しています"))
        others_min = other_stop_minutes(values)
        length = span[1] - span[0]
        if others_min and total + others_min > length:
            out.append(Issue(OTHER_STOPS,
                             f"休憩 {total} 分とほかの停止 {others_min:g} 分を足すと {total + others_min:g} 分で、"
                             f"この行の作業時間 {length} 分に入りきりません。休憩とほかの停止が"
                             "重なっていないか、時刻と分を見直してください"))

    for label, theirs in others:
        for a in found:
            hit = next((b for b in theirs if _overlap(a, b)), None)
            if hit is not None:
                out.append(Issue(OVERLAP_ROW, f"{a.text()} が、{label}の休憩({hit.text()})と重なっています"))
    return out


def problems(values: Mapping[str, str], **kwargs) -> list[str]:
    """:func:`issues` の文だけ。"""
    return [i.message for i in issues(values, **kwargs)]


def shift_issues(rows: Sequence[tuple[object, str, Mapping[str, str]]], *,
                 window: Optional[tuple[int, int]] = None, shift: str = ""
                 ) -> dict[object, list[Issue]]:
    """直ぜんぶの行(全ページ)をまとめて確かめる。``rows`` は ``(鍵, 「2ページ 3行目」, 値)``。

    行どうしの重なりも見ます(ほかのページの休憩と重なっていても分かる)。
    """
    ranges = {key: (parse_all(values.get(ROW_FAMILY, "")) or []) for key, _label, values in rows}
    out: dict[object, list[Issue]] = {}
    for key, _label, values in rows:
        others = [(label, ranges[k]) for k, label, _v in rows if k != key and ranges[k]]
        found = issues(values, window=window, shift=shift, others=others)
        if found:
            out[key] = found
    return out


def values_of(record: object) -> dict[str, str]:
    """`DetailRecord`(小文字の属性)を、この層が読む欄の名前へ。"""
    names = ["KZ", "KH", "SZ", "SH", *(f for pair in constants.STOP_SLOTS for f in pair)]
    out = {name: str(getattr(record, name.lower(), "") or "") for name in names}
    out[ROW_FAMILY] = str(getattr(record, ROW_FAMILY, "") or "")
    return out
