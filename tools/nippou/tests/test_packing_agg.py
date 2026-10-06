"""Agg_OutPut / Aggre_Calcul の中身 (`logic/packing_agg.py`)

VBA の `Agg_OutPut` は、印刷シートのセルを読んで集計シートの決まった
座標へ書いていました。**その座標は持ち込みません** ── 列の意味は
名前で決まります。持ち込んだのは、そこで何を出していたかと、どう
計算していたかです。

ここで守るのは:

    ・出していた項目が1つも減っていないこと(紙に載らない6つを含む)
    ・空の行を並べないこと(`Tim_BlankCount` と同じ)
    ・停止が横持ちから縦持ちになっても、分類と時間が変わらないこと
    ・`aggregate_shift` と**同じ値**になること(`tests/test_summary.py`)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.models import DetailRecord, HeaderRecord
from nippou.logic import packing_agg
from nippou.logic.aggregation import ShiftAggregate

DAY = "2026年8月31日"


def header(**values) -> HeaderRecord:
    base = dict(report_date=DAY, line="L-1", shift="1直", page=1)
    base.update(values)
    return HeaderRecord(**base)


def detail(row_no: int = 1, **values) -> DetailRecord:
    base = dict(report_date=DAY, line="L-1", shift="1直", page=1, row_no=row_no)
    base.update(values)
    return DetailRecord(**base)


class BuildRowTests(unittest.TestCase):
    def test_紙の欄が名前で移る(self) -> None:
        d = detail(lot="A", zai="F52S", siz="130x1529x3054", ken="9",
                   kz="8", kh="30", sz="10", sh="45", hit="2", ai="有",
                   mai="3", tut="2", vc="両面", et="反転",
                   con="6", wei="1200.5", tim="135", uni="200.08")
        row = packing_agg.build_row(header(), d)
        self.assertEqual(row.lot_no, "A")
        self.assertEqual(row.material_condition, "F52S")
        self.assertEqual(row.dimension, "130x1529x3054")
        self.assertEqual(row.incoming_quantity, 9.0)
        self.assertEqual((row.start_hour, row.start_minute), (8, 30))
        self.assertEqual((row.end_hour, row.end_minute), (10, 45))
        self.assertEqual(row.worker_count, 2.0)
        self.assertEqual(row.interleaf, "有")
        self.assertEqual(row.packing_quantity, 3.0)
        self.assertEqual(row.packing_package_count, 2.0)
        self.assertEqual(row.vc_type, "両面")
        self.assertEqual(row.etc, "反転")
        self.assertEqual(row.actual_quantity, 6.0)
        self.assertEqual(row.actual_weight, 1200.5)
        self.assertEqual(row.work_time, 135.0)
        self.assertEqual(row.unit_weight, 200.08)

    def test_紙に載らない6つも移る(self) -> None:
        """**集計だけが残す場所**だった欄。1つも落とさない。"""
        d = detail(others1="H176", others2="ｼﾔ-ｼ", others3="納入先A",
                   others4="1P0001", others5="縦", others6="横", keisu="1.5")
        row = packing_agg.build_row(header(), d)
        self.assertEqual(row.purpose_code, "H176")
        self.assertEqual(row.purpose_name, "ｼﾔ-ｼ")
        self.assertEqual(row.delivery_destination, "納入先A")
        self.assertEqual(row.packing_spec_no, "1P0001")
        self.assertEqual(row.coil_vertical_split, "縦")
        self.assertEqual(row.coil_horizontal_split, "横")
        self.assertEqual(row.coefficient_lot_count, 1.5)

    def test_打っていない時刻は空のまま(self) -> None:
        """**0時0分と区別する。** 0時は打った値、空は打っていない。"""
        row = packing_agg.build_row(header(), detail(kz="", kh=""))
        self.assertIsNone(row.start_hour)
        self.assertIsNone(row.start_minute)
        row = packing_agg.build_row(header(), detail(kz="0", kh="0"))
        self.assertEqual((row.start_hour, row.start_minute), (0, 0))

    def test_数でない値は0として足す(self) -> None:
        self.assertEqual(packing_agg.num("abc"), 0.0)
        self.assertEqual(packing_agg.num(None), 0.0)
        self.assertEqual(packing_agg.num("12.5"), 12.5)

    def test_どこにも打っていない行は並べない(self) -> None:
        rows = packing_agg.build_rows(
            header(), [detail(1, lot="A"), detail(2), detail(3, tim="30")])
        self.assertEqual([r.row_no for r in rows], [1, 3])

    def test_行番号の順に並ぶ(self) -> None:
        rows = packing_agg.build_rows(
            header(), [detail(3, lot="C"), detail(1, lot="A"),
                       detail(2, lot="B")])
        self.assertEqual([r.lot_no for r in rows], ["A", "B", "C"])


class StopTests(unittest.TestCase):
    def test_3組が縦になる(self) -> None:
        stops = packing_agg.build_stops(
            detail(s="1", th="30", ss="ｲ", ths="20", sth="A", tht="10"))
        self.assertEqual([s.stop_no for s in stops], [1, 2, 3])
        self.assertEqual([s.stop_kind for s in stops],
                         ["管理ロス停止", "突発停止", "ハンドリング停止"])
        self.assertEqual([s.stop_minutes for s in stops], [30.0, 20.0, 10.0])

    def test_記号が空なら作らない(self) -> None:
        self.assertEqual(packing_agg.build_stops(detail(s="", th="30")), [])

    def test_時間が空でも1回は1回(self) -> None:
        """書いたのに回数に出ない、を避ける。"""
        stops = packing_agg.build_stops(detail(s="1", th=""))
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0].stop_minutes, 0.0)

    def test_分類できない記号はその他(self) -> None:
        stops = packing_agg.build_stops(detail(s="1A", th="20"))
        self.assertEqual(stops[0].stop_kind, "その他")

    def test_内訳名が付く(self) -> None:
        stops = packing_agg.build_stops(detail(s="1", th="30"),
                                        {"1": "TPM活動"})
        self.assertEqual(stops[0].stop_reason, "TPM活動")
        self.assertEqual(stops[0].text, "1 TPM活動")

    def test_名前が無ければ記号だけ(self) -> None:
        stops = packing_agg.build_stops(detail(s="1", th="30"))
        self.assertEqual(stops[0].text, "1")

    def test_記号ごとにまとめる(self) -> None:
        rows = packing_agg.build_rows(header(), [
            detail(1, s="1", th="30"), detail(2, s="1", th="20"),
            detail(3, s="ｲ", th="90")])
        rollup = packing_agg.stop_rollup(rows)
        self.assertEqual([s.stop_code for s, _ in rollup], ["ｲ", "1"])  # 長い順
        self.assertEqual(rollup[1][0].stop_minutes, 50.0)
        self.assertEqual(rollup[1][1], 2)                                # 回数


class TotalsTests(unittest.TestCase):
    def test_その他は集計に入れない(self) -> None:
        """VBA と同じく、文字種で分けられない記号は外す。"""
        rows = packing_agg.build_rows(header(), [detail(s="1A", th="20")])
        total = packing_agg.totals(rows)
        self.assertEqual(total.total_stop_minutes, 0)

    def test_稼働時間と稼働率(self) -> None:
        rows = packing_agg.build_rows(header(), [detail(s="1", th="144")])
        total = packing_agg.totals(rows)
        self.assertEqual(total.operating_minutes, 1440 - 144)
        self.assertAlmostEqual(total.operating_rate_pct, (1440 - 144) / 1440 * 100)
        self.assertEqual(total.operational_minutes, 1440 - 144)

    def test_行が無くても鍵は入る(self) -> None:
        total = packing_agg.totals([], work_date=DAY, line="L-1", shift="2直")
        self.assertEqual((total.report_date, total.line, total.shift),
                         (DAY, "L-1", "2直"))


class LotCountTests(unittest.TestCase):
    def test_重複を除く(self) -> None:
        rows = packing_agg.build_rows(header(), [
            detail(1, lot="A"), detail(2, lot="A"), detail(3, lot="B")])
        self.assertEqual(packing_agg.lot_count(rows), 2)

    def test_番号の無い行は数えない(self) -> None:
        rows = packing_agg.build_rows(header(), [
            detail(1, lot=""), detail(2, tim="30")])
        self.assertEqual(packing_agg.lot_count(rows), 0)

    def test_係数は足す(self) -> None:
        rows = packing_agg.build_rows(header(), [
            detail(1, keisu="1.5"), detail(2, keisu="3")])
        self.assertEqual(packing_agg.coefficient_lot_count(rows), 4.5)


class ByKeyTests(unittest.TestCase):
    def _rows(self):
        first = packing_agg.build_rows(
            header(shift="1直"),
            [detail(1, lot="A", others1="H176", others3="納入先A",
                    tut="2", con="40", wei="800", tim="300", s="1", th="30")])
        second = packing_agg.build_rows(
            header(shift="2直"),
            [detail(1, lot="A", others1="H176", others3="納入先A",
                    tut="3", con="60", wei="1200", tim="200", s="ｲ", th="20")])
        return first + second

    def test_直をまたいで1行になる(self) -> None:
        totals = packing_agg.by_key(self._rows())
        self.assertEqual(len(totals), 1)
        self.assertTrue(totals[0].carried)

    def test_終わった側の直に付く(self) -> None:
        total = packing_agg.by_key(self._rows())[0]
        self.assertEqual(total.shift, "2直")
        self.assertEqual(total.shifts, [f"{DAY} 1直", f"{DAY} 2直"])

    def test_開始側の時間を足す(self) -> None:
        total = packing_agg.by_key(self._rows())[0]
        self.assertEqual(total.work_time, 500.0)
        self.assertEqual(total.stop_minutes, 50.0)
        self.assertEqual(total.package_count, 5.0)
        self.assertEqual(total.quantity, 100.0)
        self.assertEqual(total.weight_kg, 2000.0)

    def test_停止は1行で読める(self) -> None:
        text = packing_agg.by_key(self._rows())[0].stop_text
        self.assertIn("1 30分×1", text)
        self.assertIn("ｲ 20分×1", text)

    def test_鍵が1つでも違えば別(self) -> None:
        rows = packing_agg.build_rows(header(), [
            detail(1, lot="A", others3="納入先A"),
            detail(2, lot="A", others3="納入先B")])
        self.assertEqual(len(packing_agg.by_key(rows)), 2)

    def test_またいでいなければ印は付かない(self) -> None:
        rows = packing_agg.build_rows(header(), [detail(1, lot="A")])
        self.assertFalse(packing_agg.by_key(rows)[0].carried)


class DayPointTests(unittest.TestCase):
    def _agg(self, day: str, **values) -> ShiftAggregate:
        base = dict(report_date=day, line="L-1", shift="1直")
        base.update(values)
        return ShiftAggregate(**base)

    def test_日ごとにまとまる(self) -> None:
        points = packing_agg.day_points([
            self._agg(DAY, sheet_count=100, weight_kg=2000),
            self._agg(DAY, sheet_count=50, weight_kg=1000),
        ])
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].quantity, 150.0)
        self.assertEqual(points[0].shifts, 2)

    def test_累積が積み上がる(self) -> None:
        points = packing_agg.day_points([
            self._agg("2026年8月1日", sheet_count=100),
            self._agg("2026年8月2日", sheet_count=50),
            self._agg("2026年8月3日", sheet_count=25),
        ])
        self.assertEqual([p.cumulative_quantity for p in points],
                         [100.0, 150.0, 175.0])

    def test_暦の順に並ぶ(self) -> None:
        """ゼロ埋めしない日付なので、文字の並びでは正しく並ばない。"""
        points = packing_agg.day_points([
            self._agg("2026年8月30日"), self._agg("2026年8月3日")])
        self.assertEqual([p.work_date for p in points],
                         ["2026年8月3日", "2026年8月30日"])

    def test_稼働率は直の数で割る(self) -> None:
        points = packing_agg.day_points([
            self._agg(DAY, management_loss_minutes=144),
            self._agg(DAY, shift="2直")])
        self.assertAlmostEqual(points[0].operating_rate_pct, 95.0)

    def test_空なら空(self) -> None:
        self.assertEqual(packing_agg.day_points([]), [])


if __name__ == "__main__":
    unittest.main()
