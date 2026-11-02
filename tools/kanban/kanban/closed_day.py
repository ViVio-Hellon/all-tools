"""発注のできない日(土曜・日曜・祝日)の案内。

現場の要望: 「土曜日曜祝日は現場倉庫ともに案内を出してほしい(簡易でいい)。発注のできない日だよ、
ぐらいが分かればいい」。看板の画面(現場・倉庫・倉庫参照)の上に帯で出す(``app/templates/board.html``)。
**案内だけ**で、発注そのものは止めない。

祝日はインターネットに頼らず計算する(:mod:`kanban.holiday`。カレンダーの祝日の計算の写し)。
日付はこの端末の時計で決める。画面を開いたままでも日付が変われば出し直す ── 画面は数秒おきに
``/api/status`` を読むので、そこに入れて渡す。
"""

from __future__ import annotations

import datetime as _dt

from . import holiday

_WEEKDAY_NAMES = ("月", "火", "水", "木", "金", "土", "日")


def today() -> _dt.date:
    """いまの日付(この端末の時計)。試験はここを差し替える。"""
    return _dt.date.today()


def notice(day: _dt.date | None = None) -> str:
    """その日が土曜・日曜・祝日なら案内の文。ふつうの日は空。

    祝日の名前を先に言う(日曜と重なっても「祝日(文化の日)」)。振替休日・国民の休日は
    その名前のまま言う。
    """
    day = day or today()
    if isinstance(day, _dt.datetime):
        day = day.date()
    name = holiday.holiday_name(day)
    if name in ("振替休日", "国民の休日"):
        what = name
    elif name:
        what = f"祝日({name})"
    elif day.weekday() >= 5:
        what = f"{_WEEKDAY_NAMES[day.weekday()]}曜日"
    else:
        return ""
    return f"今日は{what}です。発注のできない日です。"
