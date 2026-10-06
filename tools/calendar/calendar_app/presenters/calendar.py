"""カレンダー画面のビューモデル (tkinter 版 ``CalendarWindow.show_month`` の移植)

42セル(7列×6行)を組み立てる。**どの日をどう塗るか・何を書くかは
すべてここで決める** ── JS は返ってきたものを写すだけで、月の割り出しも
祝日の判定も色の選択も持たない(``docs/設計.md`` §1)。

tkinter 版との対応:

| tkinter                          | ここ                       |
|----------------------------------|----------------------------|
| ``DayCell.set_colors``           | ``Cell.tone``              |
| ``DayCell.set_today``            | ``Cell.today``             |
| ``cell.label.config(text=...)``  | ``Cell.day`` / ``Cell.body`` |
| ``cell.tooltip.set_text(...)``   | ``Cell.tip``               |
"""

from __future__ import annotations

import calendar as _cal
import datetime as _dt
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Optional

from .. import config, settings
from ..holiday import holiday_name
from ..repository import (
    DayRecord,
    Repository,
    Worker,
    build_day_detail,
    build_day_text,
    build_day_tooltip,
    shift_neighbours,
)

#: 1画面に出す日数。VBA から変わらない(7列 × 6行)
CELL_COUNT = 42
COLUMNS = 7
ROWS = 6

#: 表示できる範囲。**行き先の無い月へ進ませない**ための上下限で、
#: 「前月」「翌月」を押せるかどうかの判断にも使う
MIN_YEAR = 2000
MAX_YEAR = 2100

#: セルの調子。色そのものではなく**意味**を返す。
#: 実際の色は ``tokens.css`` ただ1つが決める(設計指針: 色の出どころは1つ)
TONE_OUT = "out"          # 前後月
TONE_HOLIDAY = "holiday"  # 日曜・祝日
TONE_SATURDAY = "sat"     # 土曜
TONE_WEEKDAY = "weekday"  # 平日


@dataclass
class Cell:
    """日付セル1つ分 (VBA: clsDayCell)。"""

    date: str                  # ``yyyy/mm/dd``。押したときにサーバへ返す鍵
    day: int                   # セルに出す日
    tone: str = TONE_WEEKDAY
    in_month: bool = True
    today: bool = False
    holiday: str = ""          # 祝日名(無ければ空)
    body: str = ""             # ライン設定で絞ったあとの本文
    tip: str = ""              # 残業繋ぎ・早出繋ぎ(tkinter 版の ControlTipText)
    count: int = 0             # その日の登録件数(絞り込み前)


@dataclass
class MonthView:
    """カレンダー1画面ぶん。"""

    year: int
    month: int
    title: str
    weekdays: list[dict[str, str]] = field(default_factory=list)
    cells: list[Cell] = field(default_factory=list)
    my_line: str = ""
    can_prev: bool = True
    can_next: bool = True
    #: 班員名簿が空のときの案内。空なら問題なし
    notice: str = ""


# ---------------------------------------------------------------------------
# 月の割り出し
# ---------------------------------------------------------------------------
def clamp(year: int, month: int) -> tuple[int, int]:
    """表示できる範囲へ収める。**範囲外の月を組み立てない。**"""
    if month < 1:
        year, month = year - 1, 12
    elif month > 12:
        year, month = year + 1, 1
    year = max(MIN_YEAR, min(MAX_YEAR, year))
    return year, month


def grid_start(year: int, month: int) -> _dt.date:
    """カレンダーの左上の日(その月の1日を含む週の日曜)。

    VBA: ``firstDay - (Weekday(firstDay) - 1)``。
    Python の ``weekday()`` は月曜=0 なので ``(weekday() + 1) % 7`` で
    日曜からの日数になる。
    """
    first = _dt.date(year, month, 1)
    return first - _dt.timedelta(days=(first.weekday() + 1) % 7)


def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    """``delta`` か月ずらす。範囲外へは出ない。"""
    index = (year * 12 + (month - 1)) + delta
    return clamp(index // 12, index % 12 + 1)


# ---------------------------------------------------------------------------
# 組み立て
# ---------------------------------------------------------------------------
def month_view(conn: sqlite3.Connection, year: int, month: int,
               *, today: Optional[_dt.date] = None) -> MonthView:
    """指定した年月のカレンダーを組み立てる (VBA: ShowMonth)。"""
    year, month = clamp(year, month)
    repo = Repository(conn)
    today = today or _dt.date.today()
    my_line = settings.get_my_line()
    start = grid_start(year, month)

    # 42日分をまとめて1回で取得する(セルごとにDBを叩くと遅いため)
    records = repo.get_month_records(start, start + _dt.timedelta(days=CELL_COUNT - 1))

    cells: list[Cell] = []
    for index in range(CELL_COUNT):
        current = start + _dt.timedelta(days=index)
        key = current.strftime(config.DATE_KEY_FORMAT)
        day_records = records.get(key, [])
        in_month = current.month == month and current.year == year
        holiday = holiday_name(current)

        cells.append(Cell(
            date=key,
            day=current.day,
            tone=_tone(current, in_month, holiday),
            in_month=in_month,
            today=in_month and current == today,
            holiday=holiday,
            # **前後月は中身を出さない**(VBA と同じ)。押せもしないので、
            # 出すと「押せそうなのに反応しない」ことになる
            body=build_day_text(day_records, my_line) if in_month else "",
            tip=build_day_tooltip(day_records) if in_month else "",
            count=len(day_records) if in_month else 0,
        ))

    return MonthView(
        year=year,
        month=month,
        title=f"{year}年 {month}月",
        weekdays=_weekdays(),
        cells=cells,
        my_line=my_line,
        can_prev=(year, month) > (MIN_YEAR, 1),
        can_next=(year, month) < (MAX_YEAR, 12),
        notice="" if repo.has_workers() else
               "班員名簿が取り込まれていません。設定画面から取り込んでください。",
    )


def _tone(current: _dt.date, in_month: bool, holiday: str) -> str:
    """セルの調子。VBA の色分けと同じ規則。"""
    if not in_month:
        return TONE_OUT
    if holiday or current.weekday() == 6:      # 日曜・祝日
        return TONE_HOLIDAY
    if current.weekday() == 5:                 # 土曜
        return TONE_SATURDAY
    return TONE_WEEKDAY


def _weekdays() -> list[dict[str, str]]:
    """曜日見出し。日曜は赤系、土曜は青系(VBA と同じ)。"""
    tones = {0: "sun", 6: "sat"}
    return [{"label": name, "tone": tones.get(index, "")}
            for index, name in enumerate(config.WEEKDAY_LABELS)]


# ---------------------------------------------------------------------------
# 1日ぶん
# ---------------------------------------------------------------------------
@dataclass
class DayView:
    """1日の登録内容 (tkinter 版の ViewerDialog / DeletePickerDialog)。"""

    date: str
    title: str
    detail: str = ""                                  # 閲覧用の整形済み本文
    items: list[dict[str, Any]] = field(default_factory=list)  # 削除の選択肢


def day_view(conn: sqlite3.Connection, day: _dt.date) -> DayView:
    """その日の登録内容。閲覧と削除の両方がこれを読む。

    **1つのビューモデルで両方まかなう。** 閲覧と削除で別々に組むと、
    片方だけ絞り込みを直したときに食い違う。
    """
    records = Repository(conn).get_day_records(day)
    weekday = config.WEEKDAY_LABELS[(day.weekday() + 1) % 7]
    return DayView(
        date=day.strftime(config.DATE_KEY_FORMAT),
        title=f"{day.year}年{day.month}月{day.day}日 ({weekday})",
        detail=build_day_detail(records),
        items=[_delete_item(record) for record in records],
    )


def record_mark(record: DayRecord) -> str:
    """その行の**中身**を指す印。ID とは別に添える。

    【なぜ ID だけでは足りないか】
    画面は開いたままにされる。削除のダイアログを開いて放置しているあいだに
    背景の取り込み直しが走ると、同じ ID が別の行になっていることがある ──
    取り込み元の ``ID`` は ``INTEGER PRIMARY KEY``(オートインクリメント
    ではない)ので、**消えた番号は再利用されうる**。

    ID だけで消すと、最悪「別の人の休みを消して、履歴にもその人が残る」。
    そこで画面が見ていた中身をここで印にし、押した時点で合わなければ
    「先に変わっていた」として断る(``app/routes/calendar.py``)。
    """
    return "\x1f".join([record.kubun, record.naiyou, record.code])


def _delete_item(record: DayRecord) -> dict[str, Any]:
    """削除の選択肢1行 (tkinter 版 DeletePickerDialog の見出しと同じ形)。"""
    if record.is_rest:
        caption = f"[休み] {record.naiyou}"
        if record.shift:
            caption += f" ({record.shift}直)"
    else:
        caption = f"[連絡] {record.naiyou}"
    return {
        "id": record.id,
        # 画面はこれを**そのまま返すだけ**。中身の意味は見ない
        "mark": record_mark(record),
        "caption": caption,
        "kind": "rest" if record.is_rest else "comment",
        "line": record.line,
        "group": record.group,
    }


# ---------------------------------------------------------------------------
# 休みの登録に使う選択肢
# ---------------------------------------------------------------------------
def worker_options(conn: sqlite3.Connection) -> dict[str, Any]:
    """休みにする作業者の選択肢 (tkinter 版 WorkerPickerDialog)。

    **ライン見出しごとに班で束ねて返す。** 並び順・見出しの作り方は
    ``Repository.get_workers`` が VBA から引き継いでいるので、ここでは
    その順番を崩さずに入れ子へ組み替えるだけ。
    """
    workers = Repository(conn).get_workers(settings.get_my_line())
    lines: list[dict[str, Any]] = []
    index: dict[str, dict[str, Any]] = {}
    for worker in workers:
        line = index.get(worker.line)
        if line is None:
            line = {"line": worker.line, "groups": [], "_groups": {}}
            index[worker.line] = line
            lines.append(line)
        group = line["_groups"].get(worker.group)
        if group is None:
            group = {"group": worker.group, "workers": []}
            line["_groups"][worker.group] = group
            line["groups"].append(group)
        group["workers"].append(_worker_dict(worker))
    for line in lines:
        line.pop("_groups", None)
    return {"lines": lines, "my_line": settings.get_my_line(),
            "total": len(workers), "shift_links": _shift_links()}


def _shift_links() -> dict[str, dict[str, str]]:
    """どの直が繋ぐのか(直ごとの前直・次直)。

    **休んだ直の前が残業で、次が早出で繋ぎます。** 画面はこれを見て
    「1直の残業を繋ぐ人」「3直の早出を繋ぐ人」と聞きます ── 繋ぎを
    続けて2回聞くので、**どちらを聞かれているのか**が見出しで分かる
    ようにするためです(見出しが似ていると、選んだのに効いていないと
    思われます)。

    求め方は ``repository.shift_neighbours`` の1つだけ。閲覧・
    ツールチップと違う言い方になると、同じ登録が2通りに見えます。
    """
    links: dict[str, dict[str, str]] = {}
    for shift in ("1", "2", "3"):
        neighbours = shift_neighbours(shift)
        if neighbours is None:              # 起きないが、黙って形を崩さない
            continue
        prev, nxt = neighbours
        links[shift] = {"overtime": str(prev), "early": str(nxt)}
    return links


def _worker_dict(worker: Worker) -> dict[str, Any]:
    return {
        "code": worker.code,
        "name": worker.name,
        "group": worker.group,
        "line": worker.line,
        # **直を聞くかどうかはサーバが決める。** A/B/C/D 以外(昼勤・日勤)は
        # 3交替に属さないので、直と繋ぎの確認をしない(VBA と同じ)
        "needs_shift": not worker.is_day_shift,
    }


def comment_targets(conn: sqlite3.Connection) -> dict[str, Any]:
    """コメント・連絡の宛先 (tkinter 版 ComboPickerDialog)。

    ライン → 班 の2段。1つ目を選ぶと2つ目の候補が
    **実在する組み合わせだけ**に絞られるよう、対応表をそのまま渡す。
    """
    pairs = Repository(conn).get_line_group_pairs()
    lines: list[str] = []
    groups: dict[str, list[str]] = {}
    for line, group in pairs:
        if line not in groups:
            groups[line] = []
            lines.append(line)
        if group not in groups[line]:
            groups[line].append(group)
    return {"lines": lines, "groups": groups, "my_line": settings.get_my_line()}


# ---------------------------------------------------------------------------
# 辞書へ (JSON で返す形)
# ---------------------------------------------------------------------------
def month_dict(view: MonthView) -> dict[str, Any]:
    return {
        "year": view.year,
        "month": view.month,
        "title": view.title,
        "weekdays": view.weekdays,
        "cells": [vars(cell) for cell in view.cells],
        "my_line": view.my_line,
        "can_prev": view.can_prev,
        "can_next": view.can_next,
        "notice": view.notice,
    }


def day_dict(view: DayView) -> dict[str, Any]:
    return {"date": view.date, "title": view.title,
            "detail": view.detail, "items": view.items}


def parse_date(text: str) -> _dt.date:
    """``yyyy/mm/dd`` を日付にする。形が違えば ``ValueError``。

    受け口(``app/routes``)が**入力の形だけ**を見るための入口。
    実在しない日(2月30日)もここで弾かれる。
    """
    return _dt.datetime.strptime(text.strip(), config.DATE_KEY_FORMAT).date()


def month_days(year: int, month: int) -> int:
    """その月の日数。テストと入力検査から使う。"""
    return _cal.monthrange(year, month)[1]
