"""集計管理: 内訳番号99 の時間 (**管理者だけ**)

【何のための面か】
分類1・内訳番号99(名前は空白)は、**超えた休憩を打つための記号**として使い
始めました(`logic/aggregation.OTHER_STOP_CODES` の説明)。超えた分をほかの
停止や作業時間に足して打つと日報の意味がなくなるので、まずは 99 で打って
もらって様子を見る ── その様子を見る場所です。

    ライン別     読めるラインごとの合計(1行1ライン。混ぜずに並べるだけ)
    直別         選んだラインの 1直・2直・3直・日勤ごと
    作業者別     選んだラインの作業者ごと
    月ごと       選んだラインの月ごとの移り変わり(前の月との差つき)

使い始めた日(最初に 99 が打たれた日)と、その日からの日数も添えます ──
始めてから増えているのか減っているのかを読むため。

【誰に出すか】
**管理者(管理者モード・アクセス権限の表で Administrator の PC。`ctx.editor`)
だけ**です。面も、サーバの口(`/api/agg/code99`)も、ほかの端末には出しません・
答えません(`app/routes/agg.py`)。作業者の目に触れる所に出さないので、面の
名前は「内訳番号99 の時間」と中立にしてあります。

【何を数えるか】
集計管理の「その他」(その他停止)と**同じもの**です。打った日報(見出しと明細)
を、集計を作るときと同じ並べ方(`packing_agg.build_rows`)で並べ、種類が
「その他停止」の停止を足します ── 別の数え方を作りません。

    回数       99 を書いた回数(時間が空でも1回。停止の項目ごとの数え方と同じ)
    合計(分)   99 に書いた時間の合計
    打った直   99 が1つでもあった直の数
    保存のある直  その期間に日報を保存した直の数(99 の無い直も入る)
    1直あたり(分) 合計 ÷ 保存のある直(直の数が違う月どうしを比べるため)

**作業者は日報の作業者の欄**から取ります(ページごと。複数人の欄は名前ごとに
分けて、その全員に同じ分を数えます ── 標準作業時間の「作業者ごと」と同じ
数え方。`logic/standard_time.names_of`)。そのため作業者別の合計は、ライン
全体の合計より多くなることがあります。

【どこから読むか】
この端末のラインは手元、ほかのラインは控え(LocalBackup)。集計管理のライン
の欄と同じ決まりです(`services/line_view`)。控えには書きません。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Optional

from ..db.repository import NippouRepository
from ..logic import line_names, packing_agg
from ..logic.aggregation import OTHER_STOP_KIND, shift_sort_key
from ..logic.numeric import format_fixed
from ..logic.shift import parse_business_date
from ..logic.standard_time import names_of
from .agg_admin import Table

#: 面の名前(作業者の目に触れても差し支えないよう中立に)
TITLE = "内訳番号99 の時間"

#: 表の鍵(CSVのファイル名は `csv_export.agg_output_path`)
TABLE_KEYS: tuple[str, ...] = ("code99_lines", "code99_shifts", "code99_workers",
                               "code99_months")

#: 作業者の欄が空のページ
NO_WORKER = "(作業者の欄が空)"

#: 読むときの最初の日(「最初に打った日」を探すため、期間の前も読みます)
_EARLIEST = date(1900, 1, 1)


@dataclass(frozen=True)
class Page99:
    """99 のあった1ページぶん。"""

    day: date
    shift: str
    worker: str
    count: int
    minutes: float


@dataclass
class LineData:
    """1ラインぶんの読んだもの(期間で絞る前)。"""

    line: str
    pages: list[Page99] = field(default_factory=list)
    #: 保存のある直 (日, 直)
    saved: set[tuple[date, str]] = field(default_factory=set)

    @property
    def first_date(self) -> Optional[date]:
        """最初に 99 が打たれた日(読めた範囲で)。"""
        return min((p.day for p in self.pages if p.count), default=None)


def read_line(repo: NippouRepository, line: str, end: date) -> LineData:
    """そのラインの日報を `end` まで読む。**最初に打った日を探すので、期間の前も読みます。**"""
    found = LineData(line=line)
    for header, details in repo.list_headers_between(_EARLIEST, end, line=line):
        day = parse_business_date(header.report_date)
        if day is None:
            continue
        found.saved.add((day, header.shift))
        count, minutes = 0, 0.0
        for row in packing_agg.build_rows(header, details):
            for stop in row.stops:
                if stop.stop_kind == OTHER_STOP_KIND:
                    count += 1
                    minutes += stop.stop_minutes
        if count:
            found.pages.append(Page99(day=day, shift=header.shift,
                                      worker=header.worker or "", count=count,
                                      minutes=minutes))
    return found


@dataclass
class _Sum:
    count: int = 0
    minutes: float = 0.0
    shifts: set[tuple[date, str]] = field(default_factory=set)

    def add(self, page: Page99) -> None:
        self.count += page.count
        self.minutes += page.minutes
        self.shifts.add((page.day, page.shift))


def _in(day: date, start: date, end: date) -> bool:
    return start <= day <= end


def _per_shift(minutes: float, saved: int) -> str:
    return format_fixed(minutes / saved, 1) if saved else ""


def _date_text(day: Optional[date]) -> str:
    return day.isoformat() if day else ""


def _months(start: date, end: date) -> list[tuple[int, int]]:
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def line_table(data: Iterable[LineData], start: date, end: date, today: date,
               note: str) -> Table:
    rows = []
    for d in data:
        total = _Sum()
        for p in d.pages:
            if _in(p.day, start, end):
                total.add(p)
        saved = sum(1 for day, _ in d.saved if _in(day, start, end))
        first = d.first_date
        rows.append([line_names.label(d.line), str(total.count),
                     format_fixed(total.minutes, 0), str(len(total.shifts)), str(saved),
                     _per_shift(total.minutes, saved), _date_text(first),
                     str((today - first).days) if first else ""])
    return Table(
        key="code99_lines", title="ライン別", note=note,
        columns=["ライン", "回数", "合計(分)", "打った直", "保存のある直",
                 "1直あたり(分)", "最初に打った日", "その日から(日)"],
        numeric=[False, True, True, True, True, True, False, True],
        rows=rows, empty="読めるラインがありません")


def shift_table(data: Optional[LineData], start: date, end: date, note: str) -> Table:
    sums: dict[str, _Sum] = {}
    saved: dict[str, int] = {}
    if data is not None:
        for day, shift in data.saved:
            if _in(day, start, end):
                saved[shift] = saved.get(shift, 0) + 1
        for p in data.pages:
            if _in(p.day, start, end):
                sums.setdefault(p.shift, _Sum()).add(p)
    rows = []
    for shift in sorted(set(saved) | set(sums), key=shift_sort_key):
        s = sums.get(shift, _Sum())
        rows.append([shift, str(s.count), format_fixed(s.minutes, 0), str(len(s.shifts)),
                     str(saved.get(shift, 0)), _per_shift(s.minutes, saved.get(shift, 0))])
    return Table(
        key="code99_shifts", title="直別", note=note,
        columns=["直", "回数", "合計(分)", "打った直", "保存のある直", "1直あたり(分)"],
        numeric=[False, True, True, True, True, True],
        rows=rows, empty="この期間に保存された直はありません")


def worker_table(data: Optional[LineData], start: date, end: date, note: str) -> Table:
    sums: dict[str, _Sum] = {}
    if data is not None:
        for p in data.pages:
            if not _in(p.day, start, end):
                continue
            for name in names_of(p.worker) or [NO_WORKER]:
                sums.setdefault(name, _Sum()).add(p)
    ordered = sorted(sums.items(), key=lambda kv: (-kv[1].minutes, kv[0]))
    return Table(
        key="code99_workers", title="作業者別", note=note,
        columns=["作業者", "回数", "合計(分)", "打った直"],
        numeric=[False, True, True, True],
        rows=[[name, str(s.count), format_fixed(s.minutes, 0), str(len(s.shifts))]
              for name, s in ordered],
        empty="この期間に内訳番号99 はありません")


def month_table(data: Optional[LineData], start: date, end: date, note: str) -> Table:
    rows = []
    before: Optional[float] = None
    for y, m in _months(start, end):
        s = _Sum()
        saved = 0
        if data is not None:
            for p in data.pages:
                if (p.day.year, p.day.month) == (y, m) and _in(p.day, start, end):
                    s.add(p)
            saved = sum(1 for day, _ in data.saved
                        if (day.year, day.month) == (y, m) and _in(day, start, end))
        diff = "" if before is None else format_fixed(s.minutes - before, 0)
        if diff and not diff.startswith("-") and diff != "0":
            diff = "+" + diff
        rows.append([f"{y}-{m:02d}", str(s.count), format_fixed(s.minutes, 0),
                     str(len(s.shifts)), str(saved), _per_shift(s.minutes, saved), diff])
        before = s.minutes
    return Table(
        key="code99_months", title="月ごとの移り変わり", note=note,
        columns=["月", "回数", "合計(分)", "打った直", "保存のある直", "1直あたり(分)",
                 "前の月との差(分)"],
        numeric=[False, True, True, True, True, True, True],
        rows=rows, empty="期間がありません")


@dataclass
class Code99View:
    """1面ぶん。"""

    line: str
    start: str
    end: str
    tables: list[Table]
    #: 選んだラインで最初に 99 が打たれた日と、その日から今日までの日数
    first_date: str = ""
    days_since: Optional[int] = None
    #: 読めなかったライン(控えが無い・開けない)と理由
    unread: list[str] = field(default_factory=list)
    generated_at: str = ""

    @property
    def first_text(self) -> str:
        if not self.first_date:
            return f"{line_names.label(self.line)} ではまだ内訳番号99 が打たれていません"
        return (f"{line_names.label(self.line)} で最初に内訳番号99 が打たれた日: "
                f"{self.first_date}(今日まで {self.days_since}日)")

    def as_dict(self) -> dict[str, Any]:
        return {"title": TITLE, "line": self.line, "start": self.start, "end": self.end,
                "tables": [t.as_dict() for t in self.tables],
                "first_date": self.first_date, "days_since": self.days_since,
                "first_text": self.first_text, "unread": self.unread,
                "generated_at": self.generated_at}


def default_start(data: Optional[LineData], end: date) -> date:
    """期間を選んでいないときの始まり。**使い始めた月の1日**(無ければその月の1日)。

    始めてからの増減を読むための面なので、既定は使い始めたところからにします。
    """
    first = data.first_date if data is not None else None
    if first is None or first > end:
        return end.replace(day=1)
    return first.replace(day=1)


def build(lines: list[LineData], *, line: str, start: Optional[date], end: date,
          today: date, unread: Optional[list[str]] = None) -> Code99View:
    """1面ぶんを組み立てる。**DBは読みません**(読むのは `read_line`)。

    `lines` は読めたラインぜんぶ(ライン別の表)、`line` は直別・作業者別・月ごと
    に出すライン(集計管理のラインの欄で選んだもの)。
    """
    picked = next((d for d in lines if d.line == line), None)
    start = start or default_start(picked, end)
    note = f"{start.isoformat()} 〜 {end.isoformat()}"
    line_note = f"{note} / {line_names.label(line)}"
    first = picked.first_date if picked is not None else None
    return Code99View(
        line=line, start=start.isoformat(), end=end.isoformat(),
        first_date=_date_text(first),
        days_since=(today - first).days if first else None,
        unread=list(unread or []),
        generated_at=datetime.now().strftime("%H:%M"),
        tables=[line_table(lines, start, end, today, note),
                shift_table(picked, start, end, line_note),
                worker_table(picked, start, end, line_note),
                month_table(picked, start, end, line_note)])
