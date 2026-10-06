"""集計を値で残す (`services/summary.py` / packing_report の3表)

【印刷フォーマットと集計フォーマット】
VBA には2つのシートがありました ── 打った内容そのもの(印刷)と、
`Agg_OutPut` が並べ直したもの(集計)。Python版も2つのままですが、
後者は**シートではなく表**です。

    daily_header / daily_detail   打つところ
             ↓ 保存のたびに作り直す
    packing_report                1作業日1ライン1直
    packing_report_detail         1ロット1行
    packing_stop_detail           1停止1行

ここで守るのは4つです。

    ・**打った日報と、投影した集計の数が必ず一致すること**
      (「計算が合わない」がいちばん困る)
    ・明細を直したら、集計も作り直されること
    ・名前や分類は**保存したときのもの**が残ること
    ・読むほうが明細を数え直さないこと
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.connection import connect
from nippou.db.models import DetailRecord, HeaderRecord
from nippou.db.repository import NippouRepository
from nippou.db.schema import ensure_schema
from nippou.logic import packing_agg
from nippou.services import summary as summary_service
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase

DAY = "2026年8月31日"
NEXT = "2026年9月1日"
LINE = "L-1"


def header(shift: str = "1直", page: int = 1, **values) -> HeaderRecord:
    base = dict(report_date=DAY, line=LINE, shift=shift, page=page,
                worker="山田", reason="")
    base.update(values)
    return HeaderRecord(**base)


def detail(row_no: int = 1, **values) -> DetailRecord:
    base = dict(report_date=DAY, line=LINE, shift="1直", page=1, row_no=row_no,
                lot="A", con="100", wei="2000", tim="400", keisu="1.5")
    base.update(values)
    return DetailRecord(**base)


class SummaryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        conn = connect(self.tmp / "t.sqlite3")
        ensure_schema(conn)
        self.addCleanup(conn.close)
        self.repo = NippouRepository(conn)

    def save(self, head: HeaderRecord, details: list[DetailRecord]) -> None:
        self.repo.save(head, details)

    def refresh(self, shift: str = "1直", labels=None, work_date: str = DAY):
        # **マスタを読ませない。** 停止の内訳名は要点ではないうえ、
        # 参照先が無い環境では読むたびに記録が出る
        return summary_service.refresh_shift(self.repo, work_date, LINE, shift,
                                             labels=labels or {})


# ======================================================================
# Agg_OutPut / Aggre_Calcul ── **座標は捨て、内容と計算は残す**
# ======================================================================
class BuildTests(SummaryTestCase):
    def test_明細を足して出す(self) -> None:
        report = summary_service.build(
            [(header(), [detail()])], DAY, LINE, "1直")[0]
        self.assertEqual(report.total_quantity, 100.0)
        self.assertEqual(report.total_weight, 2000.0)
        self.assertEqual(report.work_time, 400.0)

    def test_ページをまたいだら足す(self) -> None:
        pages = [(header(page=1), [detail()]),
                 (header(page=2), [detail(page=2, tim="200")])]
        report = summary_service.build(pages, DAY, LINE, "1直")[0]
        self.assertEqual(report.total_quantity, 200.0)
        self.assertEqual(report.total_weight, 4000.0)
        self.assertEqual(report.work_time, 600.0)
        self.assertEqual(report.pages, 2)

    def test_ロット数は重複を除く(self) -> None:
        """同じロットが何行に分かれていても1つ。"""
        pages = [(header(), [detail(row_no=1, lot="A"),
                             detail(row_no=2, lot="A"),
                             detail(row_no=3, lot="B")])]
        report = summary_service.build(pages, DAY, LINE, "1直")[0]
        self.assertEqual(report.total_lot_count, 2)

    def test_番号の無い行はロットに数えない(self) -> None:
        """番号を打たずに時間だけ入れた行を1ロットと数えない。"""
        pages = [(header(), [detail(lot=""), detail(row_no=2, lot="B")])]
        self.assertEqual(
            summary_service.build(pages, DAY, LINE, "1直")[0].total_lot_count, 1)

    def test_係数処理ロット数は足す(self) -> None:
        pages = [(header(), [detail(row_no=1, keisu="1.5"),
                             detail(row_no=2, keisu="3")])]
        report = summary_service.build(pages, DAY, LINE, "1直")[0]
        self.assertEqual(report.coefficient_lot_count, 4.5)

    def test_停止は文字種で3つに分かれる(self) -> None:
        """VBA `Stop_Distr` の振り分けそのまま。"""
        rows = [detail(s="1", th="30"),               # 数値 → 管理ロス
                detail(row_no=2, s="ｲ", th="20"),      # カタカナ → 突発
                detail(row_no=3, s="A", th="10")]      # 英字 → ﾊﾝﾄﾞﾘﾝｸﾞ
        report = summary_service.build(
            [(header(), rows)], DAY, LINE, "1直")[0]
        self.assertEqual(report.equipment_stop_total, 30.0)
        self.assertEqual(report.setup_stop_total, 20.0)
        self.assertEqual(report.handling_stop_total, 10.0)
        self.assertEqual(report.operating_time, 1380.0)     # 1440 - 60
        self.assertEqual(report.operation_time, 1410.0)     # 1440 - 30

    def test_停止は縦に持つ(self) -> None:
        """シートでは①②③が横に並んでいた。DBは1停止1行。"""
        rows = [detail(s="1", th="30", ss="ｲ", ths="20")]
        details = summary_service.build(
            [(header(), rows)], DAY, LINE, "1直", {"1": "TPM活動"})[1]
        self.assertEqual(len(details), 1)
        stops = details[0].stops
        self.assertEqual([s.stop_no for s in stops], [1, 2])
        self.assertEqual(stops[0].stop_code, "1")
        self.assertEqual(stops[0].stop_reason, "TPM活動")
        self.assertEqual(stops[0].stop_kind, "管理ロス停止")
        self.assertEqual(stops[1].stop_kind, "突発停止")

    def test_記号の無い停止は行にしない(self) -> None:
        """時間だけ入っていても、どの分類にも入れようがない。"""
        details = summary_service.build(
            [(header(), [detail(s="", th="30")])], DAY, LINE, "1直")[1]
        self.assertEqual(details[0].stops, [])

    def test_空の行は並べない(self) -> None:
        """12行のうち打ったのが1行なら、残りは紙の余白と同じ。"""
        rows = [detail(row_no=1)]
        rows += [DetailRecord(report_date=DAY, line=LINE, shift="1直", page=1,
                              row_no=n) for n in range(2, 13)]
        details = summary_service.build(
            [(header(), rows)], DAY, LINE, "1直")[1]
        self.assertEqual(len(details), 1)

    def test_紙に載らない欄も残る(self) -> None:
        """用途コード・納入先などは**集計だけが残す場所**。"""
        rows = [detail(others1="H176", others2="ｼﾔ-ｼ", others3="納入先A",
                       others4="1P0001", others5="縦", others6="横")]
        d = summary_service.build([(header(), rows)], DAY, LINE, "1直")[1][0]
        self.assertEqual(d.purpose_code, "H176")
        self.assertEqual(d.purpose_name, "ｼﾔ-ｼ")
        self.assertEqual(d.delivery_destination, "納入先A")
        self.assertEqual(d.packing_spec_no, "1P0001")
        self.assertEqual(d.coil_vertical_split, "縦")
        self.assertEqual(d.coil_horizontal_split, "横")

    def test_ページが無くても形は返す(self) -> None:
        report, details = summary_service.build([], DAY, LINE, "1直")
        self.assertEqual(report.key(), (DAY, LINE, "1直"))
        self.assertEqual(report.pages, 0)
        self.assertEqual(details, [])


class MatchesLegacyTests(unittest.TestCase):
    """**現行の集計と同じ値になること。**

    `aggregate_shift` は明細から直接、`packing_agg.totals` は並べた
    ロット行から。VBA が「シートへ並べてから、その表を読んで合計する」
    順序だったので後者に合わせてありますが、**両方とも同じ値**でないと、
    ロット別の表と直の合計が食い違います。
    """

    def _pair(self, details):
        from nippou.logic.aggregation import aggregate_shift

        head = header()
        rows = packing_agg.build_rows(head, details)
        return aggregate_shift(head, details), packing_agg.totals(rows)

    def _same(self, a, b) -> None:
        for name in ("sheet_count", "weight_kg", "work_minutes",
                     "management_loss_minutes", "unplanned_stop_minutes",
                     "handling_stop_minutes", "operating_minutes",
                     "operating_rate_pct", "productivity_t_per_h"):
            self.assertAlmostEqual(getattr(a, name), getattr(b, name),
                                   msg=f"{name} が食い違います")

    def test_ふつうの直(self) -> None:
        a, b = self._pair([detail(row_no=1, s="1", th="30"),
                           detail(row_no=2, con="50", wei="900", tim="100",
                                  ss="ｲ", ths="20")])
        self._same(a, b)

    def test_停止3組そろった行(self) -> None:
        a, b = self._pair([detail(s="1", th="10", ss="ｲ", ths="20",
                                  sth="A", tht="30")])
        self._same(a, b)

    def test_分類できない記号は数えない(self) -> None:
        """数字とカタカナの混在などは、VBAも集計から外していた。"""
        a, b = self._pair([detail(s="1A", th="20")])
        self._same(a, b)
        self.assertEqual(b.total_stop_minutes, 0)

    def test_空の行が混じっても同じ(self) -> None:
        blank = DetailRecord(report_date=DAY, line=LINE, shift="1直", page=1,
                             row_no=2)
        a, b = self._pair([detail(row_no=1), blank])
        self._same(a, b)


# ======================================================================
# 残すほう ── 3階層を1トランザクションで
# ======================================================================
class SaveTests(SummaryTestCase):
    def test_保存して読み直せる(self) -> None:
        self.save(header(), [detail()])
        self.refresh()
        found = self.repo.load_packing_report(DAY, LINE, "1直")
        self.assertIsNotNone(found)
        self.assertEqual(found.total_quantity, 100.0)
        self.assertTrue(found.created_at, "登録日時が入っていません")
        self.assertTrue(found.updated_at, "更新日時が入っていません")

    def test_3階層がつながる(self) -> None:
        self.save(header(), [detail(s="1", th="30")])
        report = self.refresh()
        details = self.repo.packing_details(report.id)
        self.assertEqual(len(details), 1)
        self.assertEqual(details[0].report_id, report.id)
        self.assertEqual(len(details[0].stops), 1)
        self.assertEqual(details[0].stops[0].detail_id, details[0].id)

    def test_入れ直しても古い行が残らない(self) -> None:
        """**足し込みではなく置き換え。** 明細を直したら作り直す。"""
        self.save(header(), [detail(row_no=1, s="1", th="30"),
                             detail(row_no=2, lot="B")])
        self.refresh()
        self.save(header(), [detail(row_no=1, con="50", wei="500")])
        report = self.refresh()
        self.assertEqual(report.total_quantity, 50.0)
        details = self.repo.packing_details(report.id)
        self.assertEqual(len(details), 1)
        self.assertEqual(details[0].stops, [])

    def test_入れ直しても登録日時は変わらない(self) -> None:
        self.save(header(), [detail()])
        first = self.refresh()
        second = self.refresh()
        self.assertEqual(first.created_at, second.created_at)

    def test_子も一緒に消える(self) -> None:
        """外部キーの `ON DELETE CASCADE`。孤児の行を残さない。"""
        self.save(header(), [detail(s="1", th="30")])
        self.refresh()
        self.repo.conn.execute("DELETE FROM packing_report")
        self.repo.conn.commit()
        self.assertEqual(self.repo.conn.execute(
            "SELECT COUNT(*) FROM packing_report_detail").fetchone()[0], 0)
        self.assertEqual(self.repo.conn.execute(
            "SELECT COUNT(*) FROM packing_stop_detail").fetchone()[0], 0)

    def test_同じ作業日ラインでも直が違えば別(self) -> None:
        self.save(header(shift="1直"), [detail()])
        self.save(header(shift="2直"), [detail(shift="2直")])
        self.refresh("1直")
        self.refresh("2直")
        self.assertEqual(len(self.repo.packing_report_keys()), 2)

    def test_ページが1つも無ければ作らない(self) -> None:
        self.assertIsNone(self.refresh())
        self.assertIsNone(self.repo.load_packing_report(DAY, LINE, "1直"))

    def test_期間で取れる(self) -> None:
        self.save(header(), [detail()])
        self.save(header(report_date=NEXT), [detail(report_date=NEXT)])
        self.refresh()
        self.refresh(work_date=NEXT)
        found = self.repo.packing_reports_between(date(2026, 8, 1),
                                                  date(2026, 8, 31))
        self.assertEqual([r.work_date for r in found], [DAY])

    def test_ラインで絞れる(self) -> None:
        self.save(header(), [detail()])
        self.save(header(line="L2"), [detail(line="L2")])
        self.refresh()
        summary_service.refresh_shift(self.repo, DAY, "L2", "1直", labels={})
        found = self.repo.packing_reports_between(
            date(2026, 8, 1), date(2026, 8, 31), line="L2")
        self.assertEqual([r.line_name for r in found], ["L2"])


# ======================================================================
# 読むほう ── **明細を数え直さない**
# ======================================================================
class ReadTests(SummaryTestCase):
    def test_残した値から直ごとの行になる(self) -> None:
        self.save(header(), [detail(s="1", th="60")])
        self.refresh()
        rows = summary_service.day_rows(self.repo, DAY, LINE)
        self.assertEqual([r.shift for r in rows], ["1直"])
        self.assertEqual(rows[0].weight_ton, 2.0)
        self.assertEqual(rows[0].management_loss_minutes, 60.0)

    def test_並びは紙の順(self) -> None:
        """五十音でも時刻順でもない。**紙に出る順**。"""
        for shift in ("日勤", "3直", "1直", "2直"):
            self.save(header(shift=shift), [detail(shift=shift)])
            self.refresh(shift)
        rows = summary_service.day_rows(self.repo, DAY, LINE)
        self.assertEqual([r.shift for r in rows], ["1直", "2直", "3直", "日勤"])

    def test_期間は日ごとにまとまる(self) -> None:
        self.save(header(shift="1直"), [detail()])
        self.save(header(shift="2直"), [detail(shift="2直")])
        self.refresh("1直")
        self.refresh("2直")
        rows = summary_service.period_rows(
            self.repo, date(2026, 8, 1), date(2026, 8, 31), LINE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].shift, "全直")
        self.assertEqual(rows[0].sheet_count, 200.0)

    def test_停止は直をまたいで足す(self) -> None:
        self.save(header(shift="1直"), [detail(s="1", th="30")])
        self.save(header(shift="2直"), [detail(shift="2直", s="1", th="20")])
        self.refresh("1直", {"1": "TPM活動"})
        self.refresh("2直", {"1": "TPM活動"})
        items = summary_service.day_stop_items(self.repo, DAY, LINE)
        self.assertEqual(len(items), 1)
        stop, times = items[0]
        self.assertEqual(stop.stop_minutes, 50.0)
        self.assertEqual(times, 2)
        self.assertEqual(stop.text, "1 TPM活動")

    def test_停止は長い順(self) -> None:
        self.save(header(), [detail(s="1", th="10"),
                             detail(row_no=2, s="ｲ", th="90")])
        self.refresh()
        items = summary_service.day_stop_items(self.repo, DAY, LINE)
        self.assertEqual([s.stop_code for s, _ in items], ["ｲ", "1"])

    def test_名前も分類も保存したときのまま(self) -> None:
        """マスタが差し替わっても、**当時のグラフは当時の名前**で出る。"""
        self.save(header(), [detail(s="1", th="30")])
        self.refresh(labels={"1": "むかしの名前"})
        stop, _ = summary_service.day_stop_items(self.repo, DAY, LINE)[0]
        self.assertEqual(stop.stop_reason, "むかしの名前")
        self.assertEqual(stop.stop_kind, "管理ロス停止")

    def test_稼働率は直の平均ではない(self) -> None:
        """1直だけ動いた日と、3直とも動いた日が同じ数字になってはいけない。"""
        self.save(header(shift="1直"), [detail(s="1", th="144")])
        self.save(header(shift="2直"), [detail(shift="2直")])
        self.refresh("1直")
        self.refresh("2直")
        rows = summary_service.day_rows(self.repo, DAY, LINE)
        # 停止144分 / 1440分×2直 → 95%
        self.assertAlmostEqual(summary_service.day_rate_pct(rows), 95.0)

    def test_1日ぶんを1行にまとめられる(self) -> None:
        self.save(header(shift="1直"), [detail()])
        self.save(header(shift="2直"), [detail(shift="2直")])
        self.refresh("1直")
        self.refresh("2直")
        total = summary_service.day_total(
            summary_service.day_rows(self.repo, DAY, LINE))
        self.assertEqual(total.sheet_count, 200.0)
        self.assertEqual(total.weight_kg, 4000.0)

    def test_ロット行が読み戻せる(self) -> None:
        self.save(header(), [detail(others3="納入先A", s="1", th="30")])
        self.refresh(labels={"1": "TPM活動"})
        rows = summary_service.day_lot_rows(self.repo, DAY, LINE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].delivery_destination, "納入先A")
        self.assertEqual(rows[0].stops[0].stop_reason, "TPM活動")


# ======================================================================
# 日ごとの指標 ── **累積枚数**まで
# ======================================================================
class DayPointTests(SummaryTestCase):
    def test_累積が積み上がる(self) -> None:
        for day, count in ((DAY, "100"), (NEXT, "50")):
            self.save(header(report_date=day),
                      [detail(report_date=day, con=count)])
            self.refresh(work_date=day)
        points = summary_service.day_points(
            self.repo, date(2026, 8, 1), date(2026, 9, 30), LINE)
        self.assertEqual([p.quantity for p in points], [100.0, 50.0])
        self.assertEqual([p.cumulative_quantity for p in points],
                         [100.0, 150.0])

    def test_直の数を数える(self) -> None:
        self.save(header(shift="1直"), [detail()])
        self.save(header(shift="2直"), [detail(shift="2直")])
        self.refresh("1直")
        self.refresh("2直")
        point = summary_service.day_points(
            self.repo, date(2026, 8, 1), date(2026, 8, 31), LINE)[0]
        self.assertEqual(point.shifts, 2)
        self.assertEqual(point.quantity, 200.0)

    def test_日毎の7項目が出る(self) -> None:
        self.save(header(), [detail(s="1", th="60")])
        self.refresh()
        point = summary_service.day_points(
            self.repo, date(2026, 8, 1), date(2026, 8, 31), LINE)[0]
        self.assertEqual(point.weight_ton, 2.0)         # 直重量(t)
        self.assertEqual(point.quantity, 100.0)         # 直枚数(枚)
        self.assertEqual(point.stop_minutes, 60.0)      # 停止(分)
        self.assertEqual(point.operating_minutes, 1380.0)   # 稼働(分)
        self.assertAlmostEqual(point.operating_rate_pct, 1380 / 1440 * 100)
        self.assertGreater(point.productivity_t_per_h, 0)
        self.assertEqual(point.cumulative_quantity, 100.0)


# ======================================================================
# ロット・用途・納入先ごと ── **直またぎ**
# ======================================================================
class ByKeyTests(SummaryTestCase):
    def _carry(self) -> None:
        """1直でLOTAを始め、2直で終える。"""
        self.save(header(shift="1直"),
                  [detail(lot="A", others1="H176", others3="納入先A",
                          con="40", wei="800", tim="300", tut="2",
                          s="1", th="30")])
        self.save(header(shift="2直"),
                  [detail(shift="2直", lot="A", others1="H176",
                          others3="納入先A", con="60", wei="1200", tim="200",
                          tut="3", s="ｲ", th="20")])
        self.refresh("1直")
        self.refresh("2直")

    def test_直をまたいでも1行になる(self) -> None:
        self._carry()
        totals = summary_service.by_key(
            self.repo, date(2026, 8, 1), date(2026, 8, 31), LINE)
        self.assertEqual(len(totals), 1)
        self.assertTrue(totals[0].carried, "またぎと分かる印がありません")

    def test_開始側の時間を終わった側に足す(self) -> None:
        """**このロットに何分かかったか**が1つの数で出ること。"""
        self._carry()
        total = summary_service.by_key(
            self.repo, date(2026, 8, 1), date(2026, 8, 31), LINE)[0]
        self.assertEqual(total.work_time, 500.0)       # 300 + 200
        self.assertEqual(total.stop_minutes, 50.0)     # 30 + 20
        self.assertEqual(total.quantity, 100.0)
        self.assertEqual(total.package_count, 5.0)     # 2 + 3

    def test_行は終わった側の直に付く(self) -> None:
        self._carry()
        total = summary_service.by_key(
            self.repo, date(2026, 8, 1), date(2026, 8, 31), LINE)[0]
        self.assertEqual(total.shift, "2直")
        self.assertEqual(total.shifts, [f"{DAY} 1直", f"{DAY} 2直"])

    def test_鍵が違えば別の行(self) -> None:
        """納入先が違えば、同じロット番号でも別に数える。"""
        self.save(header(), [detail(row_no=1, lot="A", others3="納入先A"),
                             detail(row_no=2, lot="A", others3="納入先B")])
        self.refresh()
        totals = summary_service.by_key(
            self.repo, date(2026, 8, 1), date(2026, 8, 31), LINE)
        self.assertEqual(len(totals), 2)

    def test_またいでいなければ印は付かない(self) -> None:
        self.save(header(), [detail()])
        self.refresh()
        total = summary_service.by_key(
            self.repo, date(2026, 8, 1), date(2026, 8, 31), LINE)[0]
        self.assertFalse(total.carried)
        self.assertEqual(len(total.shifts), 1)

    def test_停止の内訳が行に出る(self) -> None:
        self._carry()
        total = summary_service.by_key(
            self.repo, date(2026, 8, 1), date(2026, 8, 31), LINE)[0]
        self.assertIn("30分", total.stop_text)
        self.assertIn("20分", total.stop_text)

    def test_期間の手前も見てから畳む(self) -> None:
        """月初の行が、前日から続いた荷でも割れないこと。"""
        self.save(header(report_date="2026年8月31日", shift="3直"),
                  [detail(lot="A", tim="100")])
        self.save(header(report_date=NEXT, shift="1直"),
                  [detail(report_date=NEXT, lot="A", tim="200")])
        self.refresh("3直")
        self.refresh("1直", work_date=NEXT)
        totals = summary_service.by_key(
            self.repo, date(2026, 9, 1), date(2026, 9, 30), LINE)
        self.assertEqual(len(totals), 1)
        self.assertEqual(totals[0].work_time, 300.0)
        self.assertEqual(totals[0].work_date, NEXT)


# ======================================================================
# 取りこぼしを埋める / 作り直す
# ======================================================================
class RebuildTests(SummaryTestCase):
    def test_集計の無い直はその場で作る(self) -> None:
        """この仕組みが入る前に保存されたぶんを、開いた人が気にせずに済む。"""
        self.save(header(), [detail()])
        self.assertEqual(self.repo.packing_report_keys(), [])
        made = summary_service.fill_missing(
            self.repo, date(2026, 8, 1), date(2026, 8, 31), LINE)
        self.assertEqual(made, 1)
        self.assertIsNotNone(self.repo.load_packing_report(DAY, LINE, "1直"))

    def test_そろっていれば何もしない(self) -> None:
        self.save(header(), [detail()])
        self.refresh()
        self.assertEqual(summary_service.fill_missing(
            self.repo, date(2026, 8, 1), date(2026, 8, 31), LINE), 0)

    def test_期間ぶんを作り直す(self) -> None:
        self.save(header(shift="1直"), [detail()])
        self.save(header(shift="2直"), [detail(shift="2直")])
        result = summary_service.rebuild(
            self.repo, date(2026, 8, 1), date(2026, 8, 31), line=LINE)
        self.assertEqual(result.shifts, 2)
        self.assertIn("2直", str(self.repo.packing_report_keys()))

    def test_無ければ無いと言う(self) -> None:
        result = summary_service.rebuild(
            self.repo, date(2026, 8, 1), date(2026, 8, 31), line=LINE)
        self.assertEqual(result.shifts, 0)
        self.assertIn("ありません", result.message)

    def test_直の鍵だけなら明細を読まない(self) -> None:
        """グラフを開くたびに通る道。**「無いものを探す」だけで済ませる。**"""
        self.save(header(page=1), [detail()])
        self.save(header(page=2), [detail(page=2)])
        keys = self.repo.shift_keys_between(date(2026, 8, 1), date(2026, 8, 31))
        self.assertEqual(keys, [(DAY, LINE, "1直")])     # ページ2つで1件

    def test_置き換えた表はもう無い(self) -> None:
        """`daily_summary` は `packing_report` に置き換わりました。

        同じ数字を2か所に置くと、片方だけ古くなったときにどちらが正か
        決められません。
        """
        names = {r[0] for r in self.repo.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("daily_summary", names)
        self.assertNotIn("daily_stop_summary", names)
        self.assertIn("packing_report", names)
        self.assertIn("packing_report_detail", names)
        self.assertIn("packing_stop_detail", names)


# ======================================================================
# 共有へ ── **ツールが無くても読める表**
# ======================================================================
class PushTests(SummaryTestCase):
    def _shared(self) -> Path:
        shared = self.tmp / "日報データ.sqlite3"
        sqlite3.connect(str(shared)).close()
        return shared

    def test_共有に3つの表として残る(self) -> None:
        from nippou.access_bridge import pusher

        self.save(header(), [detail(others3="納入先A", s="1", th="30")])
        self.refresh(labels={"1": "TPM活動"})

        shared = self._shared()
        self.assertEqual(pusher._push_summaries(self.repo, shared), 1)

        conn = sqlite3.connect(str(shared))
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT * FROM "T_日報集計_L-1"').fetchone()
        self.assertEqual(row["枚数"], 100.0)
        self.assertEqual(row["Lot数"], 1)
        lot = conn.execute('SELECT * FROM "T_日報集計明細_L-1"').fetchone()
        self.assertEqual(lot["ロット番号"], "A")
        self.assertEqual(lot["納入先"], "納入先A")
        stop = conn.execute('SELECT * FROM "T_日報停止明細_L-1"').fetchone()
        self.assertEqual(stop["記号"], "1")
        self.assertEqual(stop["内訳"], "TPM活動")
        self.assertEqual(stop["分類"], "管理ロス停止")

    def test_送ったら送り済みになる(self) -> None:
        from nippou.access_bridge import pusher

        self.save(header(), [detail()])
        self.refresh()
        pusher._push_summaries(self.repo, self._shared())
        self.assertEqual(self.repo.pending_packing_reports(), [])

    def test_同じ中身なら作り直しても送らない(self) -> None:
        """**差分がなければ送り直しません**(v3.65.0)。

            共有へ保存 をしてから 保存(確定) を行うと 共有へ未送信 になる
            差分がなければやはり 共有へ保存 を何度も求める必要がないのでは

        集計は保存のたびに作り直されるので、作り直したこと自体を
        「送るもの」と数えていました。
        """
        from nippou.access_bridge import pusher

        self.save(header(), [detail()])
        self.refresh()
        pusher._push_summaries(self.repo, self._shared())
        self.refresh()
        self.assertEqual(self.repo.pending_packing_reports(), [])

    def test_中身が変われば送る(self) -> None:
        from nippou.access_bridge import pusher

        self.save(header(), [detail()])
        self.refresh()
        pusher._push_summaries(self.repo, self._shared())
        self.save(header(), [detail(mai="20")])
        self.refresh()
        self.assertEqual(len(self.repo.pending_packing_reports()), 1)

    def test_送り直しても増えない(self) -> None:
        """同じキーは入れ替え。二重に積まない。"""
        from nippou.access_bridge import pusher

        self.save(header(), [detail(s="1", th="30")])
        shared = self._shared()
        for _ in range(2):
            self.refresh()
            pusher._push_summaries(self.repo, shared)
        conn = sqlite3.connect(str(shared))
        self.addCleanup(conn.close)
        for table in ("T_日報集計_L-1", "T_日報集計明細_L-1", "T_日報停止明細_L-1"):
            self.assertEqual(conn.execute(
                f'SELECT COUNT(*) FROM "{table}"').fetchone()[0], 1, table)

    def test_Accessには表を増やさない(self) -> None:
        from nippou.access_bridge import pusher

        self.save(header(), [detail()])
        self.refresh()
        self.assertEqual(
            pusher._push_summaries(self.repo, self.tmp / "日報データ.accdb"), 0)
        self.assertEqual(len(self.repo.pending_packing_reports()), 1)

    def test_表の名前はラインごと(self) -> None:
        from nippou.access_bridge import pusher

        self.assertEqual(pusher.summary_table_name("機側"), "T_日報集計_機側")
        self.assertEqual(pusher.agg_detail_table_name("機側"), "T_日報集計明細_機側")
        self.assertEqual(pusher.stop_detail_table_name("機側"),
                         "T_日報停止明細_機側")


# ======================================================================
# 紙の頭に載る
# ======================================================================
class PrintTests(SummaryTestCase):
    def test_集計が紙に載る(self) -> None:
        from nippou.reporting.print_format import build_print_html

        self.save(header(), [detail(s="1", th="30")])
        report = self.refresh(labels={"1": "TPM活動"})
        html = build_print_html(
            header(), [detail()], summary=report,
            stops=summary_service.day_stop_items(self.repo, DAY, LINE))
        self.assertIn("1直 の集計", html)
        self.assertIn("稼働率(%)", html)
        self.assertIn("TPM活動", html)

    def test_渡さなければ載らない(self) -> None:
        """VBAの印刷フォーマットと同じ姿。**明細だけの紙も出せる。**"""
        from nippou.reporting.print_format import build_print_html

        self.assertNotIn('<div class="agg">',
                         build_print_html(header(), [detail()]))

    def test_ページ数は複数のときだけ言う(self) -> None:
        from nippou.reporting.print_format import build_print_html

        self.save(header(page=1), [detail()])
        self.save(header(page=2), [detail(page=2)])
        report = self.refresh()
        html = build_print_html(header(), [detail()], summary=report)
        self.assertIn("2ページぶん", html)


# ======================================================================
# 画面から
# ======================================================================
@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class SummaryScreenTests(WebTestCase):
    def _key(self) -> tuple[str, str, str]:
        from nippou import work_context

        from app.routes.entry import build_shift_calculator

        ctx = work_context.get_context()
        return ctx.current_key(
            build_shift_calculator(self.repo().get_shift_times()))

    def _save_page(self) -> tuple[str, str, str]:
        day, line, shift = self._key()
        from nippou.db.models import DetailRecord as D
        from nippou.db.models import HeaderRecord as H

        self.repo().save(
            H(report_date=day, line=line, shift=shift, page=1),
            [D(report_date=day, line=line, shift=shift, page=1, row_no=1,
               lot="A", con="120", wei="3000", s="1", th="45", tim="400",
               others3="納入先A")])
        return day, line, shift

    def test_日報を保存すると集計も残る(self) -> None:
        """**押すのは「保存」だけ。** 集計を作るボタンは無い。

        **行の形は画面と同じにします**(`{行番号: {欄: 値}}`)。以前は
        DBの列名を並べた別の形で送っていて、中身が1つも入らないまま
        通っていました ── 空の紙を作らなくなった(v3.61.2)ので、
        いまは何も保存されません。
        """
        res = self.post("/api/entry/save", {
            "header": {"worker": "山田"}, "checks": {},
            "rows": {"1": {"LOT": "N7131T0", "KZ": "08", "KH": "00",
                           "SZ": "10", "SH": "00", "MAI": "8", "TUT": "9",
                           "CON": "100", "WEI": "1000", "S": "1", "TH": "45"}},
        })
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        day, line, shift = self._key()
        found = self.repo().load_packing_report(day, line, shift)
        self.assertIsNotNone(found, "保存しても集計が残っていません")

    def test_作り直せる(self) -> None:
        day, line, shift = self._save_page()
        res = self.post("/api/settings/rebuild-summary",
                        {"start": "2020-01-01", "end": "2099-12-31"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["shifts"], 1)
        self.assertIsNotNone(self.repo().load_packing_report(day, line, shift))

    def test_日付の形が違えば400(self) -> None:
        res = self.post("/api/settings/rebuild-summary",
                        {"start": "x", "end": "y"})
        self.assertEqual(res.status_code, 400)

    def test_逆さの期間は422(self) -> None:
        res = self.post("/api/settings/rebuild-summary",
                        {"start": "2026-12-31", "end": "2026-01-01"})
        self.assertEqual(res.status_code, 422)

    def test_紙に集計が載る(self) -> None:
        day, line, shift = self._save_page()
        body = self.get(f"/report/nippou?report_date={day}&line={line}"
                        f"&shift={shift}&page=1").get_data(as_text=True)
        self.assertIn('<div class="agg">', body)
        self.assertIn("稼働率(%)", body)

    def test_画面にも入口がある(self) -> None:
        body = self.get("/settings").get_data(as_text=True)
        self.assertIn('id="rebuild-summary"', body)
        self.assertIn("集計を作り直す", body)


if __name__ == "__main__":
    unittest.main()
