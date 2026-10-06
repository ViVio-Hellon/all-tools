"""集計を**値で残す** (VBA の「集計シート」に当たるもの)

【印刷フォーマットと集計フォーマット】
VBA には2つのシートがありました。

    印刷フォーマット  打った内容そのもの。紙になる
    集計フォーマット  `Agg_OutPut` が並べ直したもの。合計を出す土台

Python版も2つのままです。ただし後者は**シートではなく表**です。

    daily_header / daily_detail     打つところ (印刷フォーマット)
             ↓ 保存のたびに作り直す
    packing_report                  1作業日1ライン1直
    packing_report_detail           1ロット1行
    packing_stop_detail             1停止1行 (横持ち→縦持ち)

**打つ場所は1つだけです。** 下の3つは投影なので、数が食い違うことは
ありません ── 食い違ったら作り直せば必ず揃います(`rebuild`)。

【なぜ投影を残すのか】
明細から何度でも計算できる値ですが、あえて表にしておく理由が3つ
あります。

    ・そのとき使った負荷係数・停止の分類で**固定して残る**
      (`用途名負荷係数算出` が差し替わっても、当時の集計は当時のまま)
    ・期間のグラフが、何百ページぶんの明細を読み直さずに出せる
    ・**ツールを通さずに読める表**になる(共有DBにも同じ形で置きます)

【いつ走るか】
    日報の保存(確定・自動・新しいページ)  … その直を作り直す
    設定の「集計を作り直す」            … 期間をまとめて作り直す
    読むとき、まだ無ければ              … その場で作ってから読む
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional

from ..db.models import (
    DetailRecord,
    HeaderRecord,
    PackingDetail,
    PackingReport,
    PackingStop,
)
from ..db.repository import NippouRepository
from ..logging_setup import get_logger
from ..logic import packing_agg
from ..logic.aggregation import ShiftAggregate, day_rate_pct, shift_sort_key
from ..logic.packing_agg import DayPoint, KeyTotals, LotRow, StopRow
from ..logic.shift import parse_business_date

log = get_logger("services.summary")

#: 直をまたいだロットを1つにまとめるとき、期間の**手前**を何日ぶん
#: 見に行くか。1直で始めて2直で終わる形なので1日あれば足りますが、
#: 3直(00:00〜)から翌日へ続く形も拾えるように2日みます
LOOK_BACK_DAYS = 2


def _shift_pages(repo: NippouRepository, report_date: str, line: str,
                 shift: str) -> list[tuple[HeaderRecord, list[DetailRecord]]]:
    found = []
    for page in repo.saved_pages(report_date, line, shift):
        loaded = repo.load(report_date, line, shift, page)
        if loaded is not None:
            found.append(loaded)
    return found


def stop_labels() -> dict[str, str]:
    """停止の記号 → 内訳名。読めなければ空(記号だけで残す)。"""
    from .month_rollover import stop_labels as read

    return read()


# ======================================================================
# 作るほう ── 日報 → 集計フォーマット
# ======================================================================
def build(pages: list[tuple[HeaderRecord, list[DetailRecord]]],
          work_date: str, line: str, shift: str,
          labels: Optional[dict[str, str]] = None
          ) -> tuple[PackingReport, list[PackingDetail]]:
    """ページの並びから、集計1行とロット行を作る。**DBに触りません。**

    `Agg_OutPut` でロット行に並べ直し、`Aggre_Calcul` でその並びを
    足す ── VBA と同じ順序です。合計を明細と別に出さないので、
    ロット別の表と直の合計が食い違いません。
    """
    rows = packing_agg.build_shift_rows(pages, labels)
    agg = packing_agg.totals(rows, work_date=work_date, line=line, shift=shift)
    first = pages[0][0] if pages else HeaderRecord(work_date, line, shift)

    report = PackingReport(
        work_date=work_date, line_name=line, shift=shift, pages=len(pages),
        worker_name=first.worker, daytime_operation=first.day_shift,
        reason=first.reason,
        operation_time=agg.operational_minutes,
        work_time=agg.work_minutes,
        operating_time=agg.operating_minutes,
        operating_rate=agg.operating_rate_pct,
        equipment_stop_total=agg.management_loss_minutes,
        setup_stop_total=agg.unplanned_stop_minutes,
        handling_stop_total=agg.handling_stop_minutes,
        total_lot_count=packing_agg.lot_count(rows),
        coefficient_lot_count=packing_agg.coefficient_lot_count(rows),
        total_quantity=agg.sheet_count,
        total_weight=agg.weight_kg,
        productivity=agg.productivity_t_per_h)
    return report, [_as_detail(row) for row in rows]


def _as_detail(row: LotRow) -> PackingDetail:
    """ロット行 → 保存する形。**名前で写します**(座標ではなく)。"""
    return PackingDetail(
        page=row.page, row_no=row.row_no, lot_no=row.lot_no,
        material_condition=row.material_condition, dimension=row.dimension,
        incoming_quantity=row.incoming_quantity,
        start_hour=row.start_hour, start_minute=row.start_minute,
        end_hour=row.end_hour, end_minute=row.end_minute,
        worker_count=row.worker_count, interleaf=row.interleaf,
        packing_quantity=row.packing_quantity,
        packing_package_count=row.packing_package_count,
        vc_type=row.vc_type, actual_quantity=row.actual_quantity,
        actual_weight=row.actual_weight, work_time=row.work_time,
        unit_weight=row.unit_weight,
        coefficient_lot_count=row.coefficient_lot_count,
        purpose_code=row.purpose_code, purpose_name=row.purpose_name,
        delivery_destination=row.delivery_destination,
        packing_spec_no=row.packing_spec_no, etc=row.etc,
        coil_vertical_split=row.coil_vertical_split,
        coil_horizontal_split=row.coil_horizontal_split,
        hiki_no=row.hiki_no,
        stops=[PackingStop(
            stop_no=s.stop_no, stop_code=s.stop_code,
            stop_reason=s.stop_reason, stop_kind=s.stop_kind,
            stop_minutes=s.stop_minutes) for s in row.stops])


def refresh_shift(repo: NippouRepository, work_date: str, line: str,
                  shift: str, labels: Optional[dict[str, str]] = None
                  ) -> Optional[PackingReport]:
    """1直ぶんを作り直して残す。ページが1つも無ければ何もしません。"""
    pages = _shift_pages(repo, work_date, line, shift)
    if not pages:
        return None
    report, details = build(pages, work_date, line, shift,
                            labels if labels is not None else stop_labels())
    repo.save_packing_report(report, details)
    return report


@dataclass
class Rebuilt:
    """作り直した結果。**どこまでやったかを画面に出す。**"""

    shifts: int = 0
    start: str = ""
    end: str = ""

    @property
    def message(self) -> str:
        if not self.shifts:
            return "作り直す集計はありませんでした"
        return f"{self.start} 〜 {self.end} の集計を {self.shifts}直ぶん作り直しました"

    def as_dict(self) -> dict:
        return {"shifts": self.shifts, "start": self.start, "end": self.end,
                "message": self.message}


def rebuild(repo: NippouRepository, start: date, end: date,
            line: Optional[str] = None) -> Rebuilt:
    """期間ぶんを明細から作り直す。

    **古いデータや、表を直に書き換えたあと**のための入口です。ふだんは
    保存のたびに走るので押す必要はありません。
    """
    labels = stop_labels()
    keys = repo.shift_keys_between(start, end, line=line)
    for key in keys:
        refresh_shift(repo, *key, labels=labels)
    if keys:
        log.info("集計を作り直しました %s〜%s: %d直", start, end, len(keys))
    return Rebuilt(shifts=len(keys), start=start.isoformat(),
                   end=end.isoformat())


def fill_missing(repo: NippouRepository, start: date, end: date,
                 line: Optional[str] = None) -> int:
    """**まだ集計の無い直**だけを作る。画面が読む前に1度通す。

    保存のたびに作っているので、ふつうは0件です。0件でないのは、
    この仕組みが入る前に保存されたぶんと、表を直に書き換えたぶん。

    **0件のときは明細を1行も読みません。** グラフを開くたびに通る道
    なので、「無いものを探す」だけで済ませます。
    """
    have = set(repo.packing_report_keys())
    made = 0
    labels: Optional[dict[str, str]] = None
    for key in repo.shift_keys_between(start, end, line=line):
        if key in have:
            continue
        if labels is None:                        # 要るときだけマスタを読む
            labels = stop_labels()
        refresh_shift(repo, *key, labels=labels)
        have.add(key)
        made += 1
    return made


# ======================================================================
# 読むほう ── **残した値から**組み直す
# ======================================================================
def for_period(repo: NippouRepository, start: date, end: date,
               line: Optional[str] = None) -> list[PackingReport]:
    """期間ぶんの直の合計。**無いぶんはその場で作ってから返します。**"""
    fill_missing(repo, start, end, line)
    return repo.packing_reports_between(start, end, line=line)


def for_date(repo: NippouRepository, work_date: str, line: str
             ) -> list[PackingReport]:
    """その日ぶん(直の並び)。グラフの「本日」と印刷の頭で使う。"""
    parsed = parse_business_date(work_date)
    if parsed is None:
        return []
    return for_period(repo, parsed, parsed, line)


def as_aggregate(report: PackingReport) -> ShiftAggregate:
    """残した集計1行を `ShiftAggregate` に戻す。

    稼働時間・稼働率・生産性は `ShiftAggregate` が停止時間から出し直す
    ので、ここでは**元になる4つの時間**だけ戻します。保存してある
    `operating_rate` などと食い違うことはありません(同じ式で出した値を
    書いてあるだけなので)。
    """
    return ShiftAggregate(
        report_date=report.work_date, line=report.line_name,
        shift=report.shift,
        sheet_count=report.total_quantity, weight_kg=report.total_weight,
        work_minutes=report.work_time,
        management_loss_minutes=report.equipment_stop_total,
        unplanned_stop_minutes=report.setup_stop_total,
        handling_stop_minutes=report.handling_stop_total)


def day_rows(repo: NippouRepository, work_date: str, line: str
             ) -> list[ShiftAggregate]:
    """その日の直ごと。`aggregate_day` と**同じ形・同じ並び**で返す。"""
    rows = [as_aggregate(r) for r in for_date(repo, work_date, line)]
    rows.sort(key=lambda r: shift_sort_key(r.shift))
    return rows


def shift_rows(repo: NippouRepository, start: date, end: date,
               line: Optional[str] = None) -> list[ShiftAggregate]:
    """期間ぶんの**直ごと**(日別にまとめる前のもの)。"""
    return [as_aggregate(r) for r in for_period(repo, start, end, line)]


def period_rows(repo: NippouRepository, start: date, end: date,
                line: Optional[str] = None) -> list[ShiftAggregate]:
    """期間の**日ごと**(その日の全直を足したもの)。`aggregate_by_date` 相当。"""
    totals: dict[str, ShiftAggregate] = {}
    order: list[str] = []
    for row in shift_rows(repo, start, end, line):
        acc = totals.get(row.report_date)
        if acc is None:
            acc = ShiftAggregate(report_date=row.report_date, line=row.line,
                                 shift="全直")
            totals[row.report_date] = acc
            order.append(row.report_date)
        acc.sheet_count += row.sheet_count
        acc.weight_kg += row.weight_kg
        acc.work_minutes += row.work_minutes
        acc.management_loss_minutes += row.management_loss_minutes
        acc.unplanned_stop_minutes += row.unplanned_stop_minutes
        acc.handling_stop_minutes += row.handling_stop_minutes
    # `for_period` が日付順に返すので、出てきた順がそのまま日付順
    return [totals[d] for d in order]


def day_points(repo: NippouRepository, start: date, end: date,
               line: Optional[str] = None) -> list[DayPoint]:
    """日ごとの指標(**累積枚数つき**)。集計・グラフ画面の数字の出どころ。"""
    return packing_agg.day_points(shift_rows(repo, start, end, line))


# ----------------------------------------------------------------------
# ロット行 (`Agg_OutPut` が並べていたもの)
# ----------------------------------------------------------------------
def _to_lot_row(report: PackingReport, detail: PackingDetail) -> LotRow:
    return LotRow(
        work_date=report.work_date, line_name=report.line_name,
        shift=report.shift, page=detail.page, row_no=detail.row_no,
        lot_no=detail.lot_no, material_condition=detail.material_condition,
        dimension=detail.dimension,
        incoming_quantity=detail.incoming_quantity,
        start_hour=detail.start_hour, start_minute=detail.start_minute,
        end_hour=detail.end_hour, end_minute=detail.end_minute,
        worker_count=detail.worker_count, interleaf=detail.interleaf,
        packing_quantity=detail.packing_quantity,
        packing_package_count=detail.packing_package_count,
        vc_type=detail.vc_type, etc=detail.etc,
        actual_quantity=detail.actual_quantity,
        actual_weight=detail.actual_weight, work_time=detail.work_time,
        unit_weight=detail.unit_weight,
        coefficient_lot_count=detail.coefficient_lot_count,
        purpose_code=detail.purpose_code, purpose_name=detail.purpose_name,
        delivery_destination=detail.delivery_destination,
        packing_spec_no=detail.packing_spec_no,
        coil_vertical_split=detail.coil_vertical_split,
        coil_horizontal_split=detail.coil_horizontal_split,
        hiki_no=detail.hiki_no,
        stops=[StopRow(stop_no=s.stop_no, stop_code=s.stop_code,
                       stop_reason=s.stop_reason, stop_kind=s.stop_kind,
                       stop_minutes=s.stop_minutes) for s in detail.stops])


def shift_lot_rows(repo: NippouRepository, report: PackingReport
                   ) -> list[LotRow]:
    """1直ぶんのロット行。紙の頭に載せる停止を組むのに使う。"""
    return [_to_lot_row(report, d) for d in repo.packing_details(report.id)]


def lot_rows(repo: NippouRepository, start: date, end: date,
             line: Optional[str] = None) -> list[LotRow]:
    """期間ぶんのロット行。並びは 日付 → 直 → ページ → 行。"""
    fill_missing(repo, start, end, line)
    out: list[LotRow] = []
    for report, details in repo.packing_details_between(start, end, line=line):
        out.extend(_to_lot_row(report, d) for d in details)
    out.sort(key=lambda r: r.sort_key)
    return out


def day_lot_rows(repo: NippouRepository, work_date: str, line: str
                 ) -> list[LotRow]:
    parsed = parse_business_date(work_date)
    if parsed is None:
        return []
    return lot_rows(repo, parsed, parsed, line)


def stop_items(rows: Iterable[LotRow]) -> list[tuple[StopRow, int]]:
    """記号ごとの停止(長い順)。円グラフと表が読む。"""
    return packing_agg.stop_rollup(rows)


def day_stop_items(repo: NippouRepository, work_date: str, line: str
                   ) -> list[tuple[StopRow, int]]:
    return stop_items(day_lot_rows(repo, work_date, line))


def period_stop_items(repo: NippouRepository, start: date, end: date,
                      line: Optional[str] = None
                      ) -> list[tuple[StopRow, int]]:
    """**期間ぶんの**記号ごとの停止(長い順)。

        停止累計の円グラフ が出ないですね
        日集計でしか停止は出せないんですか？ それでは困ります

    出どころは日のものと同じロット行です ── 期間ぶんを読んで、同じ
    畳み方(`stop_rollup`)をするだけ。**別の数え方を作りません。**
    """
    return stop_items(lot_rows(repo, start, end, line))


def by_shift(rows: list[ShiftAggregate]) -> list[ShiftAggregate]:
    """期間ぶんを**直ごとに1行**へ畳む(1直・2直・3直・日勤)。

    日別の表と同じ形なので、画面は同じ部品で描けます ── 「この1か月、
    どの直がいちばん止まっているか」は、日ごとに見ていては出ません。
    """
    totals: dict[str, ShiftAggregate] = {}
    for row in rows:
        acc = totals.get(row.shift)
        if acc is None:
            acc = ShiftAggregate(report_date="", line=row.line,
                                 shift=row.shift)
            totals[row.shift] = acc
        acc.sheet_count += row.sheet_count
        acc.weight_kg += row.weight_kg
        acc.work_minutes += row.work_minutes
        acc.management_loss_minutes += row.management_loss_minutes
        acc.unplanned_stop_minutes += row.unplanned_stop_minutes
        acc.handling_stop_minutes += row.handling_stop_minutes
    return sorted(totals.values(), key=lambda r: shift_sort_key(r.shift))


def by_key(repo: NippouRepository, start: date, end: date,
           line: Optional[str] = None) -> list[KeyTotals]:
    """ロット・用途・納入先ごとの集計。**直をまたいだぶんを1つにします。**

    期間の**手前 `LOOK_BACK_DAYS` 日ぶん**も読んでから畳みます ──
    月初の行に「前月の3直から続いていたぶん」が落ちないように。
    畳んだあと、終わった直が期間の中にあるものだけを返します。
    """
    from datetime import timedelta

    wide = lot_rows(repo, start - timedelta(days=LOOK_BACK_DAYS), end, line)
    kept = []
    for total in packing_agg.by_key(wide):
        parsed = parse_business_date(total.work_date)
        if parsed is not None and start <= parsed <= end:
            kept.append(total)
    return kept


# ----------------------------------------------------------------------
# その日ぜんぶを1行に
# ----------------------------------------------------------------------
def day_total(rows: list[ShiftAggregate]) -> ShiftAggregate:
    """その日ぜんぶを1行に。印刷の頭に載せる「本日計」。"""
    total = ShiftAggregate(
        report_date=rows[0].report_date if rows else "",
        line=rows[0].line if rows else "", shift="合計")
    for row in rows:
        total.sheet_count += row.sheet_count
        total.weight_kg += row.weight_kg
        total.work_minutes += row.work_minutes
        total.management_loss_minutes += row.management_loss_minutes
        total.unplanned_stop_minutes += row.unplanned_stop_minutes
        total.handling_stop_minutes += row.handling_stop_minutes
    return total


#: その日の稼働率(%)。式は `logic/aggregation.py` に1つだけ置いてある
day_rate_pct = day_rate_pct
