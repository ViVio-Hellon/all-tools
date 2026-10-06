"""看板集計の追加分: 注文中(在庫切れ)・使用量(数量)・倉庫の画面に出るまで、と押し間違いの扱い。"""

from __future__ import annotations

import csv
import io
import shutil
import tempfile
import unittest
from pathlib import Path

from kanban import config
from kanban.db.store import DEFAULT_COLUMN_MAP, Store
from kanban.domain import events as ev
from kanban.domain import models
from kanban.domain.models import KanbanItem
from kanban.presenters import stats

from test_stats import e


def item(**kw) -> KanbanItem:
    base = dict(line="LVC", mgmt_no="1", material="外装紙", size="2200 : 3本", want="〇", unwant="",
                ordered_at="2026/09/01 09:00:00")
    base.update(kw)
    return KanbanItem(**base)


class ClassifyHoldTest(unittest.TestCase):
    def test_hold_and_unhold_are_recorded(self):
        ordered = item()
        self.assertEqual(ev.classify(ordered, models.hold_button_changes(ordered)), [ev.HOLD])
        held = item(hold="〇")
        self.assertEqual(ev.classify(held, models.hold_button_changes(held)), [ev.UNHOLD])

    def test_shipping_ends_the_hold(self):
        held = item(hold="〇")
        self.assertEqual(ev.classify(held, models.ship_button_changes(held)), [ev.SHIPPED, ev.UNHOLD])


class HoldCycleTest(unittest.TestCase):
    def test_a_hold_until_shipping(self):
        c = stats.build_cycles([
            e("2026/09/01 09:00", ev.ORDERED),
            e("2026/09/01 10:00", ev.HOLD, ordered_at="2026/09/01 09:00"),
            e("2026/09/04 11:00", ev.SHIPPED, ordered_at="2026/09/01 09:00"),
            e("2026/09/04 11:00", ev.UNHOLD, ordered_at="2026/09/01 09:00"),
            e("2026/09/05 08:00", ev.DELIVERED, ordered_at="2026/09/01 09:00"),
        ])[0]
        self.assertEqual(len(c.holds), 1)
        self.assertEqual(c.hold_days, 3)

    def test_a_hold_taken_off_right_away_is_a_mistake(self):
        """付けて 5 分以内に外した注文中は数えない(押し間違い)。"""
        c = stats.build_cycles([
            e("2026/09/01 09:00", ev.ORDERED),
            e("2026/09/01 10:00", ev.HOLD, ordered_at="2026/09/01 09:00"),
            e("2026/09/01 10:02", ev.UNHOLD, ordered_at="2026/09/01 09:00"),
        ])[0]
        self.assertEqual(c.holds, [])
        c = stats.build_cycles([
            e("2026/09/01 09:00", ev.ORDERED),
            e("2026/09/01 10:00", ev.HOLD, ordered_at="2026/09/01 09:00"),
            e("2026/09/01 10:05", ev.UNHOLD, ordered_at="2026/09/01 09:00"),
        ])[0]
        self.assertEqual(len(c.holds), 1, "5 分ちょうどは数える")

    def test_a_hold_still_on(self):
        c = stats.build_cycles([
            e("2026/09/01 09:00", ev.ORDERED),
            e("2026/09/02 10:00", ev.HOLD, ordered_at="2026/09/01 09:00"),
        ])[0]
        self.assertEqual(c.holds[0][1], None)
        self.assertIsNone(c.hold_days)
        rows = stats.per_kanban_holds([c])
        self.assertEqual((rows[0]["holds"], rows[0]["holds_open"], rows[0]["open_since"]), (1, 1, "2026/09/02"))

    def test_most_held_kanban_comes_first(self):
        evts = []
        for no, n in (("1", 1), ("2", 3)):
            for i in range(n):
                at = f"2026/09/{i * 7 + 1:02d} 09:00"
                evts += [e(at, ev.ORDERED, no=no),
                         e(f"2026/09/{i * 7 + 1:02d} 10:00", ev.HOLD, no=no, ordered_at=at),
                         e(f"2026/09/{i * 7 + 3:02d} 10:00", ev.SHIPPED, no=no, ordered_at=at)]
        rows = stats.per_kanban_holds(stats.build_cycles(evts))
        self.assertEqual([(r["mgmt_no"], r["holds"]) for r in rows], [("2", 3), ("1", 1)])
        self.assertEqual(rows[0]["avg_hold_days"], 2.0)


class MistakeTest(unittest.TestCase):
    def test_on_a_line_without_shipping_a_quick_cancel_is_a_mistake(self):
        """発送の無いライン(NS1)は、赤を消せば「届いた」。すぐ消したものだけは押し間違い。"""
        quick = stats.build_cycles([
            e("2026/09/01 09:00", ev.ORDERED, line="NS1"),
            e("2026/09/01 09:01", ev.CANCELLED, line="NS1", ordered_at="2026/09/01 09:00"),
        ])
        self.assertEqual(quick, [])
        slow = stats.build_cycles([
            e("2026/09/01 09:00", ev.ORDERED, line="NS1"),
            e("2026/09/03 09:00", ev.CANCELLED, line="NS1", ordered_at="2026/09/01 09:00"),
        ])
        self.assertEqual(slow[0].days_to_deliver, 2)

    def test_on_a_shipping_line_a_cancel_is_a_mistake_whatever_the_time(self):
        cycles = stats.build_cycles([
            e("2026/09/01 09:00", ev.ORDERED),
            e("2026/09/03 09:00", ev.CANCELLED, ordered_at="2026/09/01 09:00"),
        ])
        self.assertEqual(cycles, [])

    def test_a_ship_taken_back_keeps_the_last_ship(self):
        c = stats.build_cycles([
            e("2026/09/01 09:00", ev.ORDERED),
            e("2026/09/01 10:00", ev.SHIPPED, ordered_at="2026/09/01 09:00"),
            e("2026/09/01 10:01", ev.UNSHIPPED, ordered_at="2026/09/01 09:00"),
            e("2026/09/02 10:00", ev.SHIPPED, ordered_at="2026/09/01 09:00"),
        ])[0]
        self.assertEqual(c.days_to_ship, 1)


class QuantityTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(stats.parse_quantity("2200 × 50M : 3本"), (3.0, "本"))
        self.assertEqual(stats.parse_quantity("660 × 1050 : 100枚"), (100.0, "枚"))
        self.assertEqual(stats.parse_quantity("５０M：２束"), (2.0, "束"), "全角も読む")
        self.assertEqual(stats.parse_quantity("2510"), (None, ""))
        self.assertEqual(stats.parse_quantity("A : 少し"), (None, ""))

    def test_usage_per_month(self):
        evts = [e(f"2026/09/{d:02d} 09:00", ev.ORDERED, size="2200 : 3本") for d in (1, 8, 15)]
        evts += [e("2026/10/01 09:00", ev.ORDERED, no="2", size="2510")]
        rows = stats.per_kanban_usage(stats.build_cycles(evts), ["2026-09", "2026-10"])
        first = rows[0]
        self.assertEqual((first["mgmt_no"], first["per_order"], first["unit"], first["count"], first["total"],
                          first["per_month"]), ("1", 3, "本", 3, 9, 4.5))
        self.assertEqual((rows[1]["per_order"], rows[1]["total"], rows[1]["unreadable"]), (None, None, 1))

    def test_csv(self):
        evts = [e("2026/09/01 09:00", ev.ORDERED, size="2200 : 3本"),
                e("2026/09/01 10:00", ev.HOLD, ordered_at="2026/09/01 09:00"),
                e("2026/09/03 10:00", ev.UNHOLD, ordered_at="2026/09/01 09:00")]
        data = stats.Dataset(available=True, events=evts, totals={"LVC": 3})
        usage = list(csv.reader(io.StringIO(stats.build_csv("usage", data, ["2026-09"]))))
        self.assertEqual(usage[0][:8], ["ライン", "管理番号", "資材", "サイズ", "月", "出した回数", "数量の合計", "単位"])
        self.assertEqual(usage[1][4:8], ["2026-09", "1", "3", "本"])
        holds = list(csv.reader(io.StringIO(stats.build_csv("holds", data, ["2026-09"]))))
        self.assertEqual(holds[1][-1], "2")
        cycles = list(csv.reader(io.StringIO(stats.build_csv("cycles", data, ["2026-09"]))))
        head = cycles[0]
        self.assertEqual(cycles[1][head.index("注文中の回数")], "1")
        self.assertEqual(cycles[1][head.index("数量")], "3")


class SeenTest(unittest.TestCase):
    """倉庫の端末に初めて出た時刻(取り込みで書く)。"""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="kanban_seen_"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.store = Store(str(self.dir / "wh.sqlite3"), host_name="PC-WH")
        self.store.ensure_schema()
        self.addCleanup(self.store.close)

    def rows(self, want="", ordered_at=""):
        return [{"mgmt_no": "1", "material": "外装紙", "size": "2200 : 3本", "want": want,
                 "unwant": "" if want else "〇", "ordered_at": ordered_at, "shipped": "", "confirmed_at": "",
                 "permanent": "〇", "hold": "", "hold_at": ""}]

    def imp(self, rows):
        self.store.import_line(line="LVC", table_name="看板_LVC", key_column="管理番号", key_category="TEXT",
                               column_map=dict(DEFAULT_COLUMN_MAP),
                               categories={n: "TEXT" for n in DEFAULT_COLUMN_MAP.values()},
                               rows=rows, source_path="x")

    def seen(self):
        return [r for r in self.store.unsent_events() if r["kind"] == ev.SEEN]

    def test_recorded_when_an_order_first_arrives_at_the_warehouse(self):
        self.store.records_seen = True
        self.imp(self.rows())                         # はじめての取り込み(何も書かない)
        self.imp(self.rows("〇", "2026/09/01 09:00:00"))
        got = self.seen()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["ordered_at"], "2026/09/01 09:00:00")
        self.imp(self.rows("〇", "2026/09/01 09:00:00"))
        self.assertEqual(len(self.seen()), 1, "同じ回で 2 度書いた")

    def test_not_on_the_first_import_or_on_other_terminals(self):
        self.store.records_seen = True
        self.imp(self.rows("〇", "2026/09/01 09:00:00"))
        self.assertEqual(self.seen(), [], "いつから出ていたか分からない")
        other = Store(str(self.dir / "site.sqlite3"), host_name="PC-SITE")
        other.ensure_schema()
        self.addCleanup(other.close)
        self.store = other                            # 現場の端末(records_seen = False)
        self.imp(self.rows())
        self.imp(self.rows("〇", "2026/09/01 09:00:00"))
        self.assertEqual(self.seen(), [])

    def test_hours_to_seen_and_to_ship(self):
        c = stats.build_cycles([
            e("2026/09/01 09:00", ev.ORDERED),
            e("2026/09/01 12:30", ev.SEEN, ordered_at="2026/09/01 09:00"),
            e("2026/09/01 11:00", ev.SEEN, ordered_at="2026/09/01 09:00"),   # 倉庫のもう 1 台(こちらが早い)
            e("2026/09/02 11:00", ev.SHIPPED, ordered_at="2026/09/01 09:00"),
        ])[0]
        self.assertEqual((c.hours_to_seen, c.hours_seen_to_ship), (2.0, 24.0))
        self.assertEqual(stats.warehouse_stats([c])["avg_seen_hours"], 2.0)


if __name__ == "__main__":
    unittest.main()
