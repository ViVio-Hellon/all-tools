"""休憩の時刻(作業停止で休憩を入れた行の「11:30〜12:30」)。

作業停止の休憩(記号 ``0`` ── 停止理由マスタの「休憩食事」)は、**分だけ**を打ちます。何時から
何時まで休んだのかは残っていませんでした。標準時間が溜まってくると、標準より大きく長い作業を
見つけられるようになりますが、そのとき休憩が本当にその行の中だったのか・分が合っているのかが
分からないと、長い理由を休憩と取り違えます。そこで、休憩を入れた行は**時刻**も聞きます
(「その他」の理由と同じ聞き方。v4.37.0)。

- 書き方は ``11:30〜12:30``。``11:30~12:30`` ``11:30-12:30`` ``1130-1230`` ``11時30分〜12時30分``
  全角の数字も受け付けて、``11:30〜12:30`` にそろえます
- 日をまたぐ休憩(``23:30〜0:30``)は 60 分
- **保存は止めません**(理由と同じ)。合わないところは画面で知らせるだけです:
  打った分と時刻の長さが違う・その行の作業時間(開始〜終了)の外にはみ出す

時刻はこの端末の中と CSV にだけ残します(共有の日報管理へは送りません ── 紙にも Access にも
欄がありません)。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Mapping, Optional

from .. import constants

#: 休憩の記号。停止理由マスタの「休憩食事」(`logic/save_checks.REST_STOP_CODE` と同じ)
REST_STOP_CODE = "0"

#: 行の値に持つ欄の名前(`presenters/entry.EXTRA_FAMILIES`・`daily_detail.break_range`)
ROW_FAMILY = "break_range"

#: そろえた書き方の区切り
SEPARATOR = "〜"

_TIME = r"(\d{1,2})\s*(?::|時)\s*(\d{1,2})\s*分?|(\d{3,4})"
_RANGE = re.compile(rf"^\s*(?:{_TIME})\s*(?:〜|~|-|ー|－|から|－)\s*(?:{_TIME})\s*$")


@dataclass(frozen=True)
class BreakRange:
    """読めた休憩の時刻。分は 0 時からの分(終わりが始まりより前なら日をまたぐ)。"""

    start: int
    end: int

    @property
    def minutes(self) -> int:
        span = self.end - self.start
        return span if span > 0 else span + 24 * 60

    def text(self) -> str:
        return f"{_clock(self.start)}{SEPARATOR}{_clock(self.end)}"


def _clock(minutes: int) -> str:
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


def parse(text: str) -> Optional[BreakRange]:
    """``11:30〜12:30`` などを読む。読めなければ None(空も None)。"""
    raw = unicodedata.normalize("NFKC", str(text or "")).strip()
    if not raw:
        return None
    found = _RANGE.match(raw)
    if not found:
        return None
    start = _minute_of(*found.group(1, 2, 3))
    end = _minute_of(*found.group(4, 5, 6))
    if start is None or end is None or start == end:
        return None
    return BreakRange(start, end)


def normalize(text: str) -> str:
    """そろえた書き方。読めなければ打ったまま(黙って消さない)。"""
    found = parse(text)
    return found.text() if found else str(text or "").strip()


def break_minutes(values: Mapping[str, str]) -> float:
    """その行で休憩(記号 0)に打った分の合計(作業停止①〜⑤)。"""
    total = 0.0
    for code_family, minute_family in constants.STOP_SLOTS:
        if str(values.get(code_family, "") or "").strip() != REST_STOP_CODE:
            continue
        try:
            total += float(unicodedata.normalize("NFKC", str(values.get(minute_family, "") or "")).strip() or 0)
        except ValueError:
            continue
    return total


def has_break(values: Mapping[str, str]) -> bool:
    """その行の作業停止に休憩(記号 0)があるか。"""
    return any(str(values.get(code, "") or "").strip() == REST_STOP_CODE
               for code, _minutes in constants.STOP_SLOTS)


def _row_span(values: Mapping[str, str]) -> Optional[tuple[int, int]]:
    """その行の作業時間(開始 KZ:KH 〜 終了 SZ:SH)を 0 時からの分で。日をまたげば終わりに 24 時間足す。"""
    try:
        start = int(str(values.get("KZ", "")).strip()) * 60 + int(str(values.get("KH", "")).strip())
        end = int(str(values.get("SZ", "")).strip()) * 60 + int(str(values.get("SH", "")).strip())
    except ValueError:
        return None
    if end <= start:
        end += 24 * 60
    return start, end


def problems(values: Mapping[str, str]) -> list[str]:
    """休憩の時刻について、知らせること(止めない)。合っていれば空。"""
    text = str(values.get(ROW_FAMILY, "") or "").strip()
    if not text:
        return []
    found = parse(text)
    if found is None:
        return [f"「{text}」は時刻として読めません。11:30〜12:30 の形で書いてください"]
    out = []
    minutes = break_minutes(values)
    if minutes and abs(found.minutes - minutes) >= 1:
        out.append(f"時刻は {found.minutes} 分、作業停止の休憩は {minutes:g} 分です")
    span = _row_span(values)
    if span is not None:
        start, end = span
        # 休憩の始まりを、その日の時刻と翌日の時刻の両方で当ててみる(日をまたぐ行のため)
        inside = any(start <= b and b + found.minutes <= end
                     for b in (found.start, found.start + 24 * 60))
        if not inside:
            out.append(f"この行の作業時間({_clock(start % 1440)}〜{_clock(end % 1440)})の外にはみ出しています")
    return out
