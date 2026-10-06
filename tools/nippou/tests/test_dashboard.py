"""集計のダッシュボード (`presenters/dashboard.py`)

VBA `graphF` は `MultiPage1` のタブで**1枚ずつ**切り替えていた。
Web版は数字・棒・ドーナツ・表を並べたものを主表示にし、切り替えは
「1つずつ大きく見る」に残す。

ここで守るのは**何を出すかの判断**のほう ── 割合の丸め、「その他」への
まとめ、稼働率の出し方、データが無いときに何と言うか。
"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.models import DetailRecord, HeaderRecord
from nippou.logic.aggregation import ShiftAggregate
from nippou.presenters import dashboard as dash
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase

DAY = "2026年8月31日"


def agg(shift: str, **values) -> ShiftAggregate:
    base = dict(report_date=DAY, line="L-1", shift=shift)
    base.update(values)
    return ShiftAggregate(**base)


class NumberTileTests(unittest.TestCase):
    def test_今日の3つ(self) -> None:
        rows = [agg("1直", weight_kg=4000, sheet_count=100),
                agg("2直", weight_kg=2000, sheet_count=50)]
        tiles = {t.key: t for t in dash.today_tiles(rows, DAY)}
        self.assertEqual(tiles["today_weight"].value, "6.00")
        self.assertEqual(tiles["today_weight"].unit, "t")
        self.assertEqual(tiles["today_count"].value, "150")

    def test_稼働率は合計から出し直す(self) -> None:
        """直ごとの平均にすると、1直だけの日と3直の日が同じ数字になる。"""
        rows = [agg("1直", management_loss_minutes=144),   # 1440分の10%
                agg("2直")]
        tiles = {t.key: t for t in dash.today_tiles(rows, DAY)}
        # 止まっていたのは 2880分中の144分 → 95.0%
        self.assertEqual(tiles["today_rate"].value, "95.0")

    def test_保存が無ければ0で出す(self) -> None:
        tiles = {t.key: t for t in dash.today_tiles([], DAY)}
        self.assertEqual(tiles["today_weight"].value, "0.00")
        self.assertEqual(tiles["today_rate"].value, "0.0")

    def test_未保存は0でも出す(self) -> None:
        """消えると「押し忘れか、そもそも無いのか」が分からない。"""
        tile = dash.pending_tile(0)
        self.assertEqual(tile.value, "0")
        self.assertTrue(tile.has_data)
        self.assertEqual(tile.link, "/settings")


class DonutTests(unittest.TestCase):
    def test_割合まで決めるのはサーバ(self) -> None:
        rows = [agg("1直", management_loss_minutes=60,
                    unplanned_stop_minutes=30, handling_stop_minutes=10)]
        tile = dash.stop_kind_tile(rows)
        self.assertEqual([s["label"] for s in tile.slices],
                         ["管理ロス設備停止", "段取り・突発停止",
                          "ハンドリング停止"])
        self.assertEqual([s["pct"] for s in tile.slices], [60.0, 30.0, 10.0])
        self.assertEqual(tile.total, "100")

    def test_0のものは出さない(self) -> None:
        rows = [agg("1直", management_loss_minutes=60)]
        tile = dash.stop_kind_tile(rows)
        self.assertEqual([s["label"] for s in tile.slices], ["管理ロス設備停止"])

    def test_多いぶんはその他へまとめる(self) -> None:
        """**多すぎると読めない。** 上限を超えたら1つにまとめる。"""
        pairs = [(f"項目{n}", float(10 - n)) for n in range(9)]
        slices, total = dash._donut_slices(pairs)
        self.assertEqual(len(slices), dash.DONUT_LIMIT + 1)
        self.assertEqual(slices[-1]["label"], "その他")
        self.assertEqual(total, sum(v for _, v in pairs))

    def test_長い順に並ぶ(self) -> None:
        slices, _ = dash._donut_slices([("小", 1.0), ("大", 9.0)])
        self.assertEqual([s["label"] for s in slices], ["大", "小"])

    def test_停止が無ければそう言う(self) -> None:
        tile = dash.stop_kind_tile([agg("1直")])
        self.assertFalse(tile.has_data)
        self.assertIn("停止入力はありません", tile.empty)


class TableTileTests(unittest.TestCase):
    def test_直別の表(self) -> None:
        tile = dash.shift_table_tile([agg("1直", sheet_count=10, weight_kg=1000)])
        self.assertEqual(tile.columns[0], "直")
        self.assertEqual(tile.rows[0][0], "1直")
        self.assertEqual(tile.rows[0][2], "1.00")     # 重量(t)

    def test_行が無ければ空だと分かる(self) -> None:
        tile = dash.shift_table_tile([])
        self.assertFalse(tile.has_data)


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        from nippou.db.connection import connect
        from nippou.db.repository import NippouRepository
        from nippou.db.schema import ensure_schema

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        conn = connect(Path(self._tmp.name) / "t.sqlite3")
        ensure_schema(conn)
        self.addCleanup(conn.close)
        self.repo = NippouRepository(conn)

    def _save(self, shift: str = "1直") -> None:
        header = HeaderRecord(report_date=DAY, line="L-1", shift=shift, page=1,
                              count="100", weight_kg="1000")
        self.repo.save(header, [DetailRecord(
            report_date=DAY, line="L-1", shift=shift, page=1, row_no=1,
            lot="A", con="100", wei="1000", s="0", th="30", tim="400")])

    def _build(self):
        return dash.build(self.repo, report_date=DAY, line="L-1",
                          start=date(2026, 8, 1), end=date(2026, 8, 31),
                          pending=2)

    def test_タイルがそろう(self) -> None:
        self._save()
        keys = [t.key for t in self._build().tiles]
        self.assertEqual(keys, [
            # 集計シートの帯が出していた7項目
            "today_weight", "today_count", "today_rate", "today_stop",
            "today_operating", "today_productivity", "cumulative_count",
            "pending",
            "shift_weight", "shift_stop", "stop_kinds", "stop_items",
            "trend_cumulative",
            "trend_weight", "trend_rate", "trend_count",
            "shift_table", "stop_table"])

    def test_切り替えの選択肢は絵だけ(self) -> None:
        """数字や表を「大きく見る」に並べても、出せるものがない。"""
        self._save()
        keys = [c["key"] for c in self._build().choices]
        self.assertNotIn("today_weight", keys)
        self.assertNotIn("shift_table", keys)
        self.assertIn("stop_kinds", keys)

    def test_辞書にすると画面が読める形になる(self) -> None:
        self._save()
        body = self._build().as_dict()
        self.assertEqual(body["line"], "L-1")
        self.assertIn("choices", body)
        self.assertTrue(all("has_data" in t for t in body["tiles"]))

    def test_保存が無くてもタイルは出る(self) -> None:
        """**題だけでも出す。** 空白の画面は「壊れた」に見える。"""
        tiles = self._build().tiles
        self.assertEqual(len(tiles), 18)
        self.assertFalse(any(t.has_data for t in tiles if t.kind == "donut"))


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class DashboardScreenTests(WebTestCase):
    def _save(self) -> None:
        from nippou import work_context

        from app.routes.entry import build_shift_calculator

        ctx = work_context.get_context()
        day, line, shift = ctx.current_key(
            build_shift_calculator(self.repo().get_shift_times()))
        self.repo().save(
            HeaderRecord(report_date=day, line=line, shift=shift, page=1,
                         count="120", weight_kg="3000"),
            [DetailRecord(report_date=day, line=line, shift=shift, page=1,
                          row_no=1, lot="A", con="120", wei="3000",
                          s="0", th="45", tim="400")])

    def test_タイルがサーバ側で描かれている(self) -> None:
        """**JSが動かなくても数字と表は読める。**"""
        self._save()
        body = self.get("/graph").get_data(as_text=True)
        self.assertIn("本日の合計重量", body)
        self.assertIn('data-tile="today_weight"', body)
        self.assertIn("3.00", body)              # 3000kg → 3.00t
        self.assertIn("本日の直別集計", body)

    def test_切り替えも残っている(self) -> None:
        body = self.get("/graph").get_data(as_text=True)
        self.assertIn("1つずつ大きく見る", body)
        self.assertIn('id="focus-pick"', body)
        self.assertIn("大きく見る", body)

    def test_期間を変えて並べ直せる(self) -> None:
        self._save()
        res = self.post("/api/graph/dashboard",
                        {"start": "2026-01-01", "end": "2030-12-31"})
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        keys = [t["key"] for t in body["tiles"]]
        self.assertIn("trend_weight", keys)

    def test_日付の形が違えば400(self) -> None:
        res = self.post("/api/graph/dashboard", {"start": "x", "end": "y"})
        self.assertEqual(res.status_code, 400)

    def test_逆さの期間は422(self) -> None:
        res = self.post("/api/graph/dashboard",
                        {"start": "2026-12-31", "end": "2026-01-01"})
        self.assertEqual(res.status_code, 422)

    def test_未保存の件数が出る(self) -> None:
        self._save()
        body = self.get("/graph").get_data(as_text=True)
        self.assertIn("共有へ未保存", body)


if __name__ == "__main__":
    unittest.main()
