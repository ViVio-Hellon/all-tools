"""集計管理の画面ぶん (VBA の「集計シート」を人が読む形にしたもの)

【何のための画面か】
VBA では、`Agg_OutPut` が並べた集計シートそのものが**見る場所**でした。
シートを開けば、その日のロットが1行ずつ並んでいて、上の帯に直ごとの
合計が出ていました。

その「見る場所」をここに移します。出すものは4つです。

    直別       その日の直ごとの合計 (`Aggre_Calcul`)
    日別       期間の日ごと + **累積枚数** (集計シートの推移)
    ロット一覧 1ロット1行 (`Agg_OutPut` が並べていたもの)
    ロット別   **鍵でまとめたもの**(VBAには無い。今回の追加)

【ロット別だけが新しい】
ロット№・用途コード・用途名・納入先・包装仕様NO を鍵にして、梱包数・
作業時間・停止をまとめます。**直をまたいだぶんは、終わった直に足します**
── 1直で始めて2直で終わったロットが2行に割れると、「この荷に何分
かかったか」がどちらを見ても出てきません。

【丸めるのはここだけ】
保存してある値は数のままです(`packing_report` / `packing_report_detail`)。
早く丸めると、足し合わせたときに端数が積もって「計算が合わない」に
なります。画面に出す直前のここで丸めます。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

from ..db.repository import NippouRepository
from ..logic.numeric import format_fixed
from ..logic.packing_agg import KEY_LABELS, DayPoint, KeyTotals, LotRow
from ..logic import line_names

#: ロット一覧の列。**紙に載らない欄もここに出ます** ── 集計だけが
#: 残す場所だったので、見る場所にも出ていないと意味がありません
LOT_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("work_date", "作業日", 0), ("shift", "直", 0),
    ("page", "ページ", -1), ("row_no", "行", -1),
    ("lot_no", "ロット№", 0), ("material_condition", "材・調質", 0),
    ("dimension", "厚×幅×丈", 0),
    ("incoming_quantity", "検入枚数", 0),
    ("start", "開始", 0), ("end", "終了", 0),
    ("worker_count", "作業人数", 0), ("interleaf", "合紙", 0),
    ("packing_quantity", "個装枚数", 0),
    ("packing_package_count", "梱包包数", 0),
    ("vc_type", "ＶＣ種別", 0), ("etc", "反転・EX etc", 0),
    ("actual_quantity", "実績枚数", 0), ("actual_weight", "実績重量", 1),
    ("work_time", "作業(分)", 0), ("unit_weight", "単重", 3),
    ("coefficient_lot_count", "係数ﾛｯﾄ数", 2),
    ("purpose_code", "用途コード", 0), ("purpose_name", "用途名", 0),
    ("delivery_destination", "納入先", 0),
    ("packing_spec_no", "包装仕様NO", 0),
    ("coil_vertical_split", "コイル縦割", 0),
    ("coil_horizontal_split", "コイル横縦割", 0),
    ("stops", "作業停止", 0),
)


def _hhmm(hour: Optional[int], minute: Optional[int]) -> str:
    """時・分。**打っていなければ空**(0時0分と区別する)。"""
    if hour is None and minute is None:
        return ""
    return f"{hour if hour is not None else 0}:{(minute or 0):02d}"


def _cell(row: LotRow, name: str, digits: int) -> str:
    if name == "start":
        return _hhmm(row.start_hour, row.start_minute)
    if name == "end":
        return _hhmm(row.end_hour, row.end_minute)
    if name == "stops":
        return " / ".join(f"{s.text} {format_fixed(s.stop_minutes, 0)}分"
                          for s in row.stops)
    value = getattr(row, name, "")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return format_fixed(float(value), max(0, digits))
    return str(value)


@dataclass
class Table:
    """表1つ。**画面はこれを写すだけ。**"""

    key: str
    title: str
    note: str = ""
    columns: list[str] = field(default_factory=list)
    #: 右寄せにする列(数字の列)。目で桁を揃えるため
    numeric: list[bool] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    empty: str = "この期間のデータはありません"

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "title": self.title, "note": self.note,
                "columns": self.columns, "numeric": self.numeric,
                "rows": self.rows, "empty": self.empty,
                "count": len(self.rows)}


def lot_table(rows: list[LotRow], note: str) -> Table:
    """ロット一覧(`Agg_OutPut` が並べていたもの)。"""
    return Table(
        key="lots", title="ロット一覧", note=note,
        columns=[label for _, label, _ in LOT_COLUMNS],
        numeric=[digits >= 0 and name not in (
            "work_date", "shift", "lot_no", "material_condition", "dimension",
            "start", "end", "interleaf", "vc_type", "etc", "purpose_code",
            "purpose_name", "delivery_destination", "packing_spec_no",
            "coil_vertical_split", "coil_horizontal_split", "stops")
            for name, _, digits in LOT_COLUMNS],
        rows=[[_cell(row, name, digits) for name, _, digits in LOT_COLUMNS]
              for row in rows],
        empty="この期間に保存された行はありません")


def key_table(totals: list[KeyTotals], note: str) -> Table:
    """ロット・用途・納入先ごと。**直をまたいだぶんは1行にまとまります。**

    「直」の欄は**終わった側**です。またいだときは「1直→2直」のように
    出します ── 何直ぶんを足した数なのかが行から読めるように。
    """
    rows = []
    for total in totals:
        shifts = "→".join(s.split(" ", 1)[-1] for s in total.shifts)
        rows.append([
            total.work_date, shifts + ("(またぎ)" if total.carried else ""),
            *[getattr(total, name) for name, _ in KEY_LABELS],
            format_fixed(total.package_count, 0),
            format_fixed(total.quantity, 0),
            format_fixed(total.weight_kg / 1000.0, 3),
            format_fixed(total.work_time, 0),
            format_fixed(total.stop_minutes, 0),
            total.stop_text,
        ])
    return Table(
        key="by_key", title="ロット・用途・納入先ごと", note=note,
        columns=["作業日", "直", *[label for _, label in KEY_LABELS],
                 "梱包数(包)", "枚数", "重量(t)", "作業(分)", "停止(分)",
                 "停止の内訳"],
        numeric=[False, False, *[False for _ in KEY_LABELS],
                 True, True, True, True, True, False],
        rows=rows,
        empty="この期間に保存された行はありません")


def shift_table(reports, note: str) -> Table:
    """直別(`Aggre_Calcul` が帯に出していたもの)。"""
    return Table(
        key="shifts", title="直別の集計", note=note,
        columns=["作業日", "直", "作業者", "昼稼働", "ページ", "ﾛｯﾄ数",
                 "係数ﾛｯﾄ数", "枚数", "重量(t)", "作業(分)", "管理ロス",
                 "突発", "ﾊﾝﾄﾞﾘﾝｸﾞ", "稼働(分)", "操業(分)", "稼働率(%)",
                 "生産性(t/h)"],
        numeric=[False, False, False, False, True, True, True, True, True,
                 True, True, True, True, True, True, True, True],
        rows=[[r.work_date, r.shift, r.worker_name, r.daytime_operation or "無",
               str(r.pages), str(r.total_lot_count),
               format_fixed(r.coefficient_lot_count, 2),
               format_fixed(r.total_quantity, 0),
               format_fixed(r.total_weight_ton, 3),
               format_fixed(r.work_time, 0),
               format_fixed(r.equipment_stop_total, 0),
               format_fixed(r.setup_stop_total, 0),
               format_fixed(r.handling_stop_total, 0),
               format_fixed(r.operating_time, 0),
               format_fixed(r.operation_time, 0),
               format_fixed(r.operating_rate, 1),
               format_fixed(r.productivity, 2)] for r in reports],
        empty="この期間に保存された直はありません")


def day_table(points: list[DayPoint], note: str) -> Table:
    """日別。**累積枚数まで出す** ── 月の途中で「いま何枚まで来たか」。"""
    return Table(
        key="days", title="日別の集計", note=note,
        columns=["作業日", "直数", "直重量(t)", "直枚数(枚)", "停止(分)",
                 "稼働時間(分)", "生産性(t/h)", "稼働率(%)", "累積枚数(枚)"],
        numeric=[False, True, True, True, True, True, True, True, True],
        rows=[[p.work_date, str(p.shifts),
               format_fixed(p.weight_ton, 3),
               format_fixed(p.quantity, 0),
               format_fixed(p.stop_minutes, 0),
               format_fixed(p.operating_minutes, 0),
               format_fixed(p.productivity_t_per_h, 2),
               format_fixed(p.operating_rate_pct, 1),
               format_fixed(p.cumulative_quantity, 0)] for p in points],
        empty="この期間に保存された日はありません")


@dataclass
class AggAdmin:
    """1画面ぶん。"""

    line: str
    start: str
    end: str
    tables: list[Table]
    #: この表を引いた時刻("HH:MM")。**開きっぱなしにできるので要る**
    #: (理由は `presenters/dashboard.Dashboard.generated_at` と同じ)
    generated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"line": self.line, "start": self.start, "end": self.end,
                "tables": [t.as_dict() for t in self.tables],
                "generated_at": self.generated_at}

    def table(self, key: str) -> Optional[Table]:
        return next((t for t in self.tables if t.key == key), None)


def build(repo: NippouRepository, *, line: str, start: date, end: date
          ) -> AggAdmin:
    """1画面ぶんを組み立てる。**DBを読むのはここだけ。**

    並びは、**粗いほうから細かいほうへ** ── 日別 → 直別 → ロット別 →
    ロット一覧。まず「今月どうなっているか」を見て、気になったところへ
    降りていく順です。
    """
    from ..services import summary as summary_service

    note = f"{start.isoformat()} 〜 {end.isoformat()} / {line_names.label(line)}"
    rows = summary_service.lot_rows(repo, start, end, line)
    return AggAdmin(
        line=line, start=start.isoformat(), end=end.isoformat(),
        generated_at=datetime.now().strftime("%H:%M"),
        tables=[
            day_table(summary_service.day_points(repo, start, end, line), note),
            shift_table(summary_service.for_period(repo, start, end, line), note),
            key_table(summary_service.by_key(repo, start, end, line), note),
            lot_table(rows, note),
        ])
