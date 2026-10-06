"""看板集計 (:mod:`kanban.presenters.stats`)

出した日 → 発送 → 届いた日(赤を消した日)を 1 回として組み立て、
届くまでの日数・次に出すまでの日数・週/月の回数・提出率を出す。
"""

from __future__ import annotations

import csv
import io
import sqlite3
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from kanban.db.shared import SharedDb
from kanban.domain import events as ev
from kanban.presenters import stats


def e(at, kind, no="1", line="LVC", ordered_at=None, material="中性紙", size="A"):
    return stats.Event(
        at=datetime.strptime(at, "%Y/%m/%d %H:%M"), line=line, mgmt_no=no, kind=kind,
        material=material, size=size,
        ordered_at=datetime.strptime(ordered_at, "%Y/%m/%d %H:%M") if ordered_at else None,
    )


class CycleTest(unittest.TestCase):
    def test_one_full_cycle_and_the_next(self):
        cycles = stats.build_cycles([
            e("2026/09/01 09:00", ev.ORDERED),
            e("2026/09/02 15:00", ev.SHIPPED),
            e("2026/09/03 08:00", ev.DELIVERED),
            e("2026/09/08 10:00", ev.ORDERED),
        ])
        self.assertEqual(len(cycles), 2)
        first = cycles[0]
        self.assertEqual(first.days_to_ship, 1)
        self.assertEqual(first.days_to_deliver, 2, "何日で届いたか(日付の差)")
        self.assertEqual(first.days_to_next, 5, "次に出すまで何日か(届いた日 → 次に出した日)")
        self.assertIsNone(cycles[1].delivered_at, "まだ届いていない回")

    def test_a_cancel_is_not_counted(self):
        """届く前に赤を消した(押し間違い)は、出した回数に数えない。"""
        cycles = stats.build_cycles([
            e("2026/09/01 09:00", ev.ORDERED),
            e("2026/09/01 09:01", ev.CANCELLED),
            e("2026/09/02 09:00", ev.ORDERED),
        ])
        self.assertEqual([c.ordered_at.day for c in cycles], [2])

    def test_an_order_from_before_recording_is_rebuilt(self):
        """記録を始める前に出していた看板も、届いた記録の「出した日時」で 1 回にする。"""
        cycles = stats.build_cycles([
            e("2026/09/05 08:00", ev.DELIVERED, ordered_at="2026/09/01 09:00"),
        ])
        self.assertEqual(cycles[0].days_to_deliver, 4)

    def test_unshipping_clears_the_ship_date(self):
        cycles = stats.build_cycles([
            e("2026/09/01 09:00", ev.ORDERED),
            e("2026/09/02 09:00", ev.SHIPPED),
            e("2026/09/02 09:05", ev.UNSHIPPED),
        ])
        self.assertIsNone(cycles[0].shipped_at)

    def test_kanbans_are_kept_apart(self):
        cycles = stats.build_cycles([
            e("2026/09/01 09:00", ev.ORDERED, no="1"),
            e("2026/09/01 09:00", ev.ORDERED, no="2"),
            e("2026/09/03 09:00", ev.DELIVERED, no="1"),
        ])
        by = {c.mgmt_no: c for c in cycles}
        self.assertEqual(by["1"].days_to_deliver, 2)
        self.assertIsNone(by["2"].delivered_at)


class RateTest(unittest.TestCase):
    def test_rate_is_kanbans_submitted_over_kanbans_in_the_line(self):
        """提出率 = その月に 1 回以上出した看板の枚数 ÷ そのラインの看板の枚数。
        同じ看板を 2 回出しても 1 枚と数える(回数は別に出す)。"""
        cycles = stats.build_cycles([
            e("2026/09/01 09:00", ev.ORDERED, no="1"),
            e("2026/09/02 09:00", ev.DELIVERED, no="1"),
            e("2026/09/10 09:00", ev.ORDERED, no="1"),
            e("2026/09/11 09:00", ev.ORDERED, no="2"),
            e("2026/10/01 09:00", ev.ORDERED, no="3"),
        ])
        table = stats.rate_table(cycles, {"LVC": 4}, ["2026-09", "2026-10"], ["LVC"])
        sep, octo = table["LVC"]
        self.assertEqual((sep["submitted"], sep["count"], sep["rate"]), (2, 3, 50.0))
        self.assertEqual((octo["submitted"], octo["rate"]), (1, 25.0))

    def test_no_kanbans_means_no_rate(self):
        table = stats.rate_table([], {"LVC": 0}, ["2026-09"], ["LVC"])
        self.assertIsNone(table["LVC"][0]["rate"])

    def test_months(self):
        self.assertEqual(stats.months_between("2025-11", "2026-02"),
                         ["2025-11", "2025-12", "2026-01", "2026-02"])
        self.assertEqual(stats.default_months(datetime(2026, 9, 25).date()), ("2025-10", "2026-09"))
        self.assertIsNone(stats.parse_month("あした"))

    def test_weeks_start_on_monday(self):
        self.assertEqual(stats.week_start(datetime(2026, 9, 27)).isoformat(), "2026-09-21")


class LeadTimeTest(unittest.TestCase):
    """出した日 → 届いた日 の日数と、その平均。"""

    def cycles(self):
        return stats.build_cycles([
            e("2026/09/01 09:00", ev.ORDERED, no="1"),
            e("2026/09/03 09:00", ev.DELIVERED, no="1"),      # 2 日
            e("2026/09/10 09:00", ev.ORDERED, no="1"),
            e("2026/09/15 09:00", ev.DELIVERED, no="1"),      # 5 日
            e("2026/09/02 09:00", ev.ORDERED, no="2"),
            e("2026/09/03 09:00", ev.DELIVERED, no="2"),      # 1 日
            e("2026/09/20 09:00", ev.ORDERED, no="3"),        # まだ届いていない
        ])

    def test_average_leaves_out_what_has_not_arrived(self):
        got = stats.lead_stats(self.cycles())
        self.assertEqual(got, {"delivered": 3, "avg_deliver_days": 2.7,
                               "min_deliver_days": 1, "max_deliver_days": 5})

    def test_per_kanban_slowest_first(self):
        rows = stats.per_kanban_lead(self.cycles())
        self.assertEqual([r["mgmt_no"] for r in rows], ["1", "2", "3"])
        first = rows[0]
        self.assertEqual((first["count"], first["delivered"], first["avg_deliver_days"],
                          first["min_deliver_days"], first["max_deliver_days"]), (2, 2, 3.5, 2, 5))
        self.assertIsNone(rows[2]["avg_deliver_days"], "届いていない看板に平均は無い")
        self.assertEqual(rows[2]["open"], 1)

    def test_monthly_average_per_line(self):
        table = stats.rate_table(self.cycles(), {"LVC": 3}, ["2026-09"], ["LVC"])
        self.assertEqual(table["LVC"][0]["avg_deliver_days"], 2.7)
        summary = stats.line_summary(self.cycles(), ["2026-09"], ["LVC"])[0]
        self.assertEqual((summary["avg_deliver_days"], summary["min_deliver_days"],
                          summary["max_deliver_days"]), (2.7, 1, 5))


class SeveralTerminalsTest(unittest.TestCase):
    """現場と倉庫の 2 台が書いた記録を、1 回ずつ正しくつなぐ(実機で見つかった件)。"""

    O = "2026/09/01 09:00"

    def test_cancel_after_the_warehouse_shipped_counts_as_delivered(self):
        """倉庫が発送したあと、現場がそれを取り込む前に赤を消した。

        現場の端末は発送を知らないので「取消」と記録する。以前はこの回が
        まるごと消えていた(出した回数にも届いた回数にも入らない)。
        """
        cycles = stats.build_cycles([
            e("2026/09/01 09:00", ev.ORDERED, ordered_at=self.O),
            e("2026/09/02 10:00", ev.SHIPPED, ordered_at=self.O),
            e("2026/09/04 08:00", ev.CANCELLED, ordered_at=self.O),
        ])
        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0].days_to_deliver, 3)

    def test_a_line_nobody_ships_counts_red_off_as_delivered(self):
        """NS1 は倉庫の画面に無く、現場も発送を押せない。赤を消すのは届いたとき。"""
        self.assertFalse(stats.line_ships("NS1"))
        self.assertTrue(stats.line_ships("LVC"))
        cycles = stats.build_cycles([
            e("2026/09/01 09:00", ev.ORDERED, line="NS1", ordered_at=self.O),
            e("2026/09/03 09:00", ev.CANCELLED, line="NS1", ordered_at=self.O),
        ])
        self.assertEqual(cycles[0].days_to_deliver, 2)

    def test_clock_skew_between_terminals_does_not_split_a_cycle(self):
        """倉庫の時計が遅れていて、「発送」が「出した」より前に並んでも 1 回。"""
        cycles = stats.build_cycles([
            e("2026/09/01 08:58", ev.SHIPPED, ordered_at=self.O),     # 倉庫(時計が遅れている)
            e("2026/09/01 09:00", ev.ORDERED, ordered_at=self.O),
            e("2026/09/02 09:00", ev.DELIVERED, ordered_at=self.O),
        ])
        self.assertEqual(len(cycles), 1)
        self.assertIsNotNone(cycles[0].shipped_at)
        self.assertEqual(cycles[0].days_to_deliver, 1)

    def test_a_real_cancel_is_still_left_out(self):
        cycles = stats.build_cycles([
            e("2026/09/01 09:00", ev.ORDERED, ordered_at=self.O),
            e("2026/09/01 09:01", ev.CANCELLED, ordered_at=self.O),
            e("2026/09/05 09:00", ev.ORDERED, ordered_at="2026/09/05 09:00"),
        ])
        self.assertEqual([c.ordered_at.day for c in cycles], [5])

    def test_unship_then_cancel_is_a_cancel(self):
        """倉庫が発送を取り消した(間違えて緑を点けた)あとで赤を消したのは、取消。"""
        cycles = stats.build_cycles([
            e("2026/09/01 09:00", ev.ORDERED, ordered_at=self.O),
            e("2026/09/01 10:00", ev.SHIPPED, ordered_at=self.O),
            e("2026/09/01 10:01", ev.UNSHIPPED, ordered_at=self.O),
            e("2026/09/01 11:00", ev.CANCELLED, ordered_at=self.O),
        ])
        self.assertEqual(cycles, [])


class LoadAndCsvTest(unittest.TestCase):
    """共有DBから読んで、CSV にする(Excel で開ける形)。"""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="kanban_stats_"))
        self.path = self.dir / "看板マスタ.sqlite3"
        c = sqlite3.connect(str(self.path))
        c.execute("CREATE TABLE [看板_LVC] ([管理番号] NUMERIC, [資材] TEXT, [サイズ] TEXT)")
        c.executemany("INSERT INTO [看板_LVC] VALUES (?,?,?)",
                      [(1, "中性紙", "A"), (2, "中性紙", "B"), (3, "", "")])
        cols = ", ".join(f"[{n}] TEXT" for n in
                         ("ID", "日時", "ライン", "管理番号", "出来事", "資材", "サイズ", "出した日時", "端末"))
        c.execute(f"CREATE TABLE [看板履歴] ({cols})")
        c.executemany("INSERT INTO [看板履歴] VALUES (?,?,?,?,?,?,?,?,?)", [
            ("a", "2026/09/01 09:00:00", "LVC", "1", "出した", "中性紙", "A", "2026/09/01 09:00:00", "PC"),
            ("b", "2026/09/03 08:00:00", "LVC", "1", "届いた", "中性紙", "A", "2026/09/01 09:00:00", "PC"),
            ("b", "2026/09/03 08:00:00", "LVC", "1", "届いた", "中性紙", "A", "2026/09/01 09:00:00", "PC"),
        ])
        c.commit()
        c.close()
        self.db = SharedDb(str(self.path), cache_dir=str(self.dir / "cache"))

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_load_counts_kanbans_shown_on_the_board(self):
        data = stats.load(self.db)
        self.assertTrue(data.available)
        self.assertEqual(data.totals["LVC"], 2, "資材・サイズの無い行まで数えている")
        self.assertEqual(len(data.events), 2, "同じ ID を二重に数えている")

    def test_csv_kinds(self):
        data = stats.load(self.db)
        months = ["2026-09"]
        rows = list(csv.reader(io.StringIO(stats.build_csv("cycles", data, months))))
        self.assertEqual(rows[0][:3], ["ライン", "管理番号", "資材"])
        self.assertEqual(rows[1][8], "2", "届くまでの日数")
        rate = list(csv.reader(io.StringIO(stats.build_csv("rate", data, months))))
        self.assertEqual(rate[1][2:5], ["2", "1", "50.0"])
        weekly = list(csv.reader(io.StringIO(stats.build_csv("weekly", data, months))))
        self.assertEqual(weekly[1][4:], ["2026/08/31", "1"])
        self.assertTrue(stats.build_csv("monthly", data, months).startswith("ライン"))
        self.assertEqual(rate[0][-1], "届くまでの平均日数")
        self.assertEqual(rate[1][-1], "2.0")
        lead = list(csv.reader(io.StringIO(stats.build_csv("lead", data, months))))
        self.assertEqual(lead[0][6], "届くまでの平均日数")
        self.assertEqual(lead[1][:2] + lead[1][4:9], ["LVC", "1", "1", "1", "2.0", "2", "2"])
        summary = list(csv.reader(io.StringIO(stats.build_csv("summary", data, months))))
        self.assertEqual(summary[1][:3], ["LVC", "2", "1"])
        self.assertEqual(summary[1][6], "2.0")

    def test_without_history_it_says_so(self):
        c = sqlite3.connect(str(self.path))
        c.execute("DROP TABLE [看板履歴]")
        c.commit()
        c.close()
        data = stats.load(SharedDb(str(self.path), cache_dir=str(self.dir / "cache2")))
        self.assertTrue(data.available)
        self.assertIn("まだ記録がありません", data.why)


if __name__ == "__main__":
    unittest.main()
