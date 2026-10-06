"""共有保存の履歴を、表とグラフにする (`services/push_history`)

    各直で共有保存を押したタイミングと担当者 … CSV出力とグラフ出力

【表】押した1回 × 送った直 = 1行。**CSV はこの表をそのまま書きます**
(画面と CSV で列や並びが違うと、突き合わせるたびに読み替えることになる)。

【グラフ】**直の終わりに対して、いつ押したか**を、日ごと・直ごとに点で
並べます。**ふつうは直内(線より下)です。**

    共有は普通直内に押すはずです　直内に打ち終える⇒保存⇒共有保存
    このツールは直時間をすぎる前提でもよいのですか？

そのとおりで、ツールも直内に押す前提です(残り15分で催促、残り5分で
自動確定、終わった直は見るだけ)。直が終わってから押すのは、次の直が
前の直の残りを送ったときなどの**例外**です。グラフは線(直の終わり)を
境に、下を「直内」、上を「直が終わってから」として読みます ── 以前は
題を「終わりから何分後」にしていて、過ぎてから押すのが当たり前のように
読めました。

    縦   直の終わりを 0。下が「◯分前(直内)」、上が「◯分後(直が終わってから)」
    横   報告日。1日の幅の中で 1直・2直・3直を左から並べる
    色と形  直(1直 ●  2直 ■  3直 ◆  日勤 ▲)── 色だけに頼らない
    塗り    塗り = 押したあと共有に出ている(送れた・送るもの無し)
            白抜き = 出ていない(関門で止めた・送れなかった)
    載せる  担当者(その直の作業者)・押した人(押したときの作業者)・時刻・結果

時刻そのもの(14:52)ではなく「終わりに対して何分」にしたのは、直ごとに
終わる時刻が違うからです ── 1直の 14:52 と 2直の 22:42 は、どちらも
「終わる少し前」で、並べて比べたいのはそちらです。直の時刻は
時間マスタ(`shift_config`)から読みます。

**判断はここだけ**で、画面(`chart.js` の `paintTimingChart`)は受け取った
点を座標へ写すだけです。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Optional

from ..logic.shift import parse_business_date
from ..services import push_history
from ..services.shift_check import bounds_of
from .agg_admin import Table

#: 直の並び・色・形。**色は直に付く**(並べ替えや絞り込みで塗り替えない)
SHIFT_MARKS: tuple[tuple[str, str, str], ...] = (
    ("1直", "cat-1", "circle"),
    ("2直", "cat-2", "square"),
    ("3直", "cat-3", "diamond"),
    ("日勤", "cat-4", "triangle"),
)

COLUMNS: tuple[tuple[str, str, bool], ...] = (
    # (鍵, 見出し, 数字の列か)
    ("pressed_at", "押した日時", False),
    ("report_date", "報告日", False),
    ("line", "ライン", False),
    ("shift", "直", False),
    ("worker", "担当者", False),
    ("result", "結果", False),
    ("pages", "ページ数", True),
    ("failed_pages", "送れなかったページ", True),
    ("timing", "直内か", False),
    ("after_end", "直の終わりとの差(分・マイナスは直内)", True),
    ("pressed_key", "押したときの直", False),
    ("pressed_by", "押した人", False),
    ("via", "通し方", False),
    ("detail", "理由", False),
    ("terminal", "端末", False),
)

#: グラフの縦の範囲(分)。**これより外の点は端に置き、本当の値を横に書く。**
#: 1回だけ翌朝に押した直があると、範囲がそれに引っ張られて、ふだんの
#: 「終わる数分〜数十分前」が0の線に潰れて読めなくなるため。
#: ふつうは直内なので、**下(直内)を広めに**取ります
Y_FLOOR, Y_CEIL = -240, 120

IN_SHIFT = "直内"
AFTER_SHIFT = "直の後"


def timing_of(minutes: Optional[int]) -> str:
    """直内に押したか。**終わった瞬間(0分)までは直内。**"""
    if minutes is None:
        return ""
    return IN_SHIFT if minutes <= 0 else AFTER_SHIFT


def shift_end(report_date: date, shift: str,
              shift_times: dict[str, tuple[str, str]]) -> Optional[datetime]:
    """その直が終わる日時。**終わりが始まり以前なら翌日**(3直 22:50〜07:00)。"""
    start, end = bounds_of(shift_times, shift)
    try:
        s_h, s_m = (int(x) for x in start.split(":"))
        e_h, e_m = (int(x) for x in end.split(":"))
    except (ValueError, AttributeError):
        return None
    ends = datetime.combine(report_date, datetime.min.time()).replace(hour=e_h, minute=e_m)
    if (e_h, e_m) <= (s_h, s_m):
        ends += timedelta(days=1)
    return ends


def minutes_after_end(row: dict, shift_times: dict[str, tuple[str, str]]
                      ) -> Optional[int]:
    """直の終わりから何分後に押したか(終わる前はマイナス)。読めなければ None。"""
    day = parse_business_date(str(row.get("report_date", "")))
    if day is None:
        return None
    ends = shift_end(day, str(row.get("shift", "")), shift_times)
    try:
        pressed = datetime.fromisoformat(str(row.get("pressed_at", "")))
    except ValueError:
        return None
    if ends is None:
        return None
    return round((pressed - ends).total_seconds() / 60)


def _span_text(minutes: int) -> str:
    """「17時間後」「3時間前」「45分後」(端に寄せた点に添える)。"""
    side = "後" if minutes > 0 else "前"
    minutes = abs(minutes)
    if minutes >= 60:
        hours = minutes / 60
        return f"{hours:.0f}時間{side}" if hours >= 10 else f"{hours:.1f}時間{side}"
    return f"{minutes}分{side}"


def _duration(minutes: int) -> str:
    """「12分」「1時間5分」「17時間」。"""
    if minutes < 60:
        return f"{minutes}分"
    hours, rest = divmod(minutes, 60)
    return f"{hours}時間{rest}分" if rest and hours < 10 else f"{round(minutes / 60)}時間"


def when_text(minutes: int) -> str:
    """点に載せたときの一言。「直の終わりの12分前(直内)」「直が終わって5分後」。"""
    if minutes == 0:
        return "直の終わりちょうど(直内)"
    if minutes < 0:
        return f"直の終わりの{_duration(-minutes)}前(直内)"
    return f"直が終わって{_duration(minutes)}後"


def _display_time(value: str) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value


@dataclass
class PushLogView:
    table: Table
    chart: dict[str, Any]
    source: str
    warning: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    line: str = ""
    start: str = ""
    end: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"table": self.table.as_dict(), "chart": self.chart,
                "source": self.source, "warning": self.warning,
                # **並びごと渡す**(JSON の辞書は鍵の順に並べ替えられる)
                "counts": [[label, n] for label, n in self.counts.items()],
                "line": self.line,
                "start": self.start, "end": self.end}


def period_presets(today: date) -> list[dict[str, str]]:
    """期間のすぐ選べる形(報告日)。**並びと中身はここで決める**(画面は写すだけ)。

        共有保存の履歴：期間も選べるようにしてください

    日付を2つ打たなくても、よく見る幅は1回押せば済むように。
    """
    first = today.replace(day=1)
    last_month_end = first - timedelta(days=1)
    spans = (
        ("this_month", "今月", first, today),
        ("last_month", "先月", last_month_end.replace(day=1), last_month_end),
        ("last_7", "直近7日", today - timedelta(days=6), today),
        ("last_30", "直近30日", today - timedelta(days=29), today),
    )
    return [{"key": key, "label": label, "start": s.isoformat(), "end": e.isoformat()}
            for key, label, s, e in spans]


def _in_scope(row: dict, line: Optional[str], start: date, end: date) -> Optional[date]:
    day = parse_business_date(str(row.get("report_date", "")))
    if day is None or not (start <= day <= end):
        return None
    if line and row.get("line") != line:
        return None
    return day


def _chart(rows: list[tuple[date, dict, Optional[int]]], start: date, end: date,
           line: Optional[str]) -> dict[str, Any]:
    days = []
    day = start
    while day <= end:
        days.append(day)
        day += timedelta(days=1)
    index = {d: i for i, d in enumerate(days)}
    marks = {name: (mark, shape) for name, mark, shape in SHIFT_MARKS}
    points = []
    values = [m for _, _, m in rows if m is not None]
    for day, row, minutes in rows:
        if minutes is None:
            continue
        mark, shape = marks.get(str(row["shift"]), ("cat-10", "circle"))
        clipped = minutes < Y_FLOOR or minutes > Y_CEIL
        points.append({
            "day": index[day], "shift": row["shift"], "mark": mark, "shape": shape,
            "minutes": max(Y_FLOOR, min(Y_CEIL, minutes)), "actual": minutes,
            "clipped": clipped, "reached": row["result"] in push_history.REACHED,
            # 端に寄せた点に添える本当の値(「+17時間」)
            "clip_label": _span_text(minutes) if clipped else "",
            "tip": "\n".join([
                f"{row['report_date']} {row['line']} {row['shift']} / {row['result']}",
                f"押した人 {row.get('pressed_by') or '(不明)'}"
                f" / その直の担当者 {row['worker'] or '(未入力)'}",
                f"押した {_display_time(str(row['pressed_at']))} ・ {when_text(minutes)}",
            ]),
            "pressed_by": row.get("pressed_by") or "",
        })
    # 押した点が無くても、直内(下)と直の後(上)の両方が見える広さは取る
    low = max(Y_FLOOR, min([-60] + values))
    high = min(Y_CEIL, max([30] + values))
    y_min = (low // 30) * 30
    y_max = -(-high // 30) * 30
    used = {p["shift"] for p in points}
    return {
        "days": [f"{d.month}/{d.day}" for d in days],
        "points": points,
        "y_min": y_min, "y_max": y_max,
        "shifts": [{"name": n, "mark": m, "shape": s} for n, m, s in SHIFT_MARKS
                   if n in used],
        "title": f"直の終わりに対して、いつ押したか({line or '全ライン'})",
        # 線(直の終わり)の上下の読み方。**ふつうは下(直内)**
        "below": "直内に押した(ふつう)",
        "above": "直が終わってから押した",
        "empty": "この期間に押した記録はありません",
    }


def build(repo, *, line: Optional[str], start: date, end: date,
          db_path=None) -> PushLogView:
    """期間(報告日)とラインで絞った履歴。`line=None` なら全ライン。"""
    readout = push_history.read(repo, db_path)
    shift_times = repo.get_shift_times()
    picked: list[tuple[date, dict, Optional[int]]] = []
    for row in readout.rows:
        day = _in_scope(row, line, start, end)
        if day is None:
            continue
        picked.append((day, row, minutes_after_end(row, shift_times)))

    table_rows = []
    for _, row, minutes in picked:
        cells = []
        for key, _, _ in COLUMNS:
            if key == "after_end":
                cells.append("" if minutes is None else f"{minutes:+d}")
            elif key == "timing":
                cells.append(timing_of(minutes))
            elif key == "pressed_at":
                cells.append(_display_time(str(row.get(key, ""))))
            else:
                value = row.get(key, "")
                cells.append("" if value is None else str(value))
        table_rows.append(cells)

    # 並びは決めておく: 押した回数 → 直内 → 直の後 → 結果
    counts: dict[str, int] = {"押した": len({r["pressed_at"] for _, r, _ in picked}),
                              IN_SHIFT: 0, AFTER_SHIFT: 0}
    for _, row, minutes in picked:
        counts[row["result"]] = counts.get(row["result"], 0) + 1
        when = timing_of(minutes)
        if when:
            counts[when] = counts.get(when, 0) + 1

    table = Table(
        key="push_log", title="共有保存の履歴",
        note=f"{start.isoformat()} 〜 {end.isoformat()} / {line or '全ライン'}",
        columns=[label for _, label, _ in COLUMNS],
        numeric=[numeric for _, _, numeric in COLUMNS],
        rows=table_rows,
        empty="この期間に「共有へ保存」を押した記録はありません")
    return PushLogView(table=table, chart=_chart(picked, start, end, line),
                       source=readout.source, warning=readout.warning,
                       counts=counts, line=line or "全ライン",
                       start=start.isoformat(), end=end.isoformat())
