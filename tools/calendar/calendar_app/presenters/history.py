"""履歴表示のビューモデル ── 過去の月を、編集できない形で見せる

【何のためのものか】
カレンダー画面でも前月へ戻れば過去の月は見えますが、そこは**登録と
削除ができる画面**です。昔の月を調べている途中で押し間違えると、
過去の記録が変わってしまいます。そこで過去の月だけを見る画面を分け、
**書き込む道そのものを持たせません**(画面にも API にも無い)。

【どの月を選べるか】
**一覧に無いものは選べない。** 選べるのは、

* 手元に登録がある最も古い月から
* **先月**まで(今月はまだ動いているので、カレンダー画面で見る)

のあいだの月です。途中の登録が無い月も選べます(「その月は誰も
休んでいない」も記録なので、見られないと困る)。

セルの中身・色・祝日はカレンダー画面と同じ組み立て(``calendar.month_view``)
を使います ── 2か所で組むと、片方だけ直したときに見え方が食い違います。
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from dataclasses import dataclass
from typing import Any, Optional

from ..repository import Repository
from . import calendar as cal

#: 断りの種類。**文言から推し量らない**(設計 §1 の規則4)
REFUSE_NOT_LISTED = "not_listed"


@dataclass
class Choice:
    """月選びの1マス。"""

    year: int
    month: int
    count: int
    selectable: bool

    @property
    def key(self) -> str:
        return f"{self.year:04d}/{self.month:02d}"


def _last_month(today: _dt.date) -> tuple[int, int]:
    return cal.shift_month(today.year, today.month, -1)


def _index(year: int, month: int) -> int:
    return year * 12 + (month - 1)


def choices(conn: sqlite3.Connection,
            today: Optional[_dt.date] = None) -> dict[str, Any]:
    """選べる月の一覧。年ごとに12マスで返す(画面は写すだけ)。"""
    today = today or _dt.date.today()
    counts = Repository(conn).month_counts(today.replace(day=1))
    last = _last_month(today)
    if not counts:
        return {"years": [], "latest": None, "earliest": None,
                "message": "過去の月の登録がありません。"}
    first_key = min(counts)
    first = (int(first_key[:4]), int(first_key[5:7]))
    lo, hi = _index(*first), _index(*last)

    years: list[dict[str, Any]] = []
    for year in range(first[0], last[0] + 1):
        months = []
        for month in range(1, 13):
            index = _index(year, month)
            choice = Choice(year, month,
                            counts.get(f"{year:04d}/{month:02d}", 0),
                            lo <= index <= hi)
            months.append({"month": month, "label": f"{month}月",
                           "count": choice.count,
                           "selectable": choice.selectable})
        years.append({"year": year, "months": months,
                      "total": sum(m["count"] for m in months)})
    return {"years": years,
            "latest": {"year": last[0], "month": last[1]},
            "earliest": {"year": first[0], "month": first[1]},
            "message": ""}


def is_selectable(conn: sqlite3.Connection, year: int, month: int,
                  today: Optional[_dt.date] = None) -> bool:
    """その月を履歴として見せてよいか。**判断はここだけ。**"""
    listed = choices(conn, today)
    if listed["earliest"] is None:
        return False
    first = listed["earliest"]
    last = listed["latest"]
    return (_index(first["year"], first["month"]) <= _index(year, month)
            <= _index(last["year"], last["month"]))


def month_dict(conn: sqlite3.Connection, year: int, month: int,
               today: Optional[_dt.date] = None) -> dict[str, Any]:
    """過去の月1画面。カレンダー画面と同じ組み立てに、**閲覧のみ**の印を添える。"""
    today = today or _dt.date.today()
    view = cal.month_view(conn, year, month, today=today)
    payload = cal.month_dict(view)
    listed = choices(conn, today)
    first, last = listed["earliest"], listed["latest"]
    index = _index(view.year, view.month)
    payload.update(
        readonly=True,
        # 行き先の無い月へ進ませない(選べる範囲の中だけを行き来する)
        can_prev=first is not None and index > _index(first["year"], first["month"]),
        can_next=last is not None and index < _index(last["year"], last["month"]),
        count=sum(cell.count for cell in view.cells if cell.in_month),
        # 前後月を押しても何も起きない。今日の枠も過去の月には出ない
        notice="",
    )
    return payload


def day_dict(conn: sqlite3.Connection, day: _dt.date) -> dict[str, Any]:
    """1日の登録内容(読むだけ)。**削除に使う印(ID・中身の印)は返さない。**"""
    view = cal.day_view(conn, day)
    return {"date": view.date, "title": view.title,
            "detail": view.detail, "count": len(view.items)}
