"""期間で見る ── 本日系の数字と停止集計を、期間で数え直す

    この期間の集計CSVを出力で集計後 グラフ表示にて
    停止累計の円グラフ が出ないですね
    日集計でしか停止は出せないんですか？ それでは困ります

    期間集計してしまうと累計枚数しか表示しないのがさみしい
    期間表示は本日系の表示を切り替えてでも
    期間でしか見れないデータを見せるべき

期間を選んでも、上の数字は「本日の…」のままで、期間のものは累積枚数
1つだけでした ── 1か月を選んでも、**1か月ぶんの稼働率も停止時間も
出ていません**。停止の輪もその日のぶんだけです。

数え方は日のものと**1つも変えません**。変えると、同じ「稼働率」が日と
期間で別のものになります。違うのは数える範囲だけです。
"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.models import DetailRecord, HeaderRecord
from nippou.logic import formula
from nippou.logic.aggregation import ShiftAggregate
from nippou.presenters import dashboard as dash
from nippou.services import summary as summary_service

try:
    from tests._web import WebTestCase
    HAS_FLASK = True
except Exception:                                 # noqa: BLE001
    HAS_FLASK = False
    WebTestCase = object                          # type: ignore
SKIP = "Flask が入っていません"


def agg(day: str, shift: str, **values) -> ShiftAggregate:
    base = dict(report_date=day, line="L-1", shift=shift)
    base.update(values)
    return ShiftAggregate(**base)


class PeriodTileTests(unittest.TestCase):
    """**期間の6項目。** 式は日のものと同じ形。"""

    def rows(self) -> list[ShiftAggregate]:
        # 2日 × 2直 = 4直。停止は合わせて 288分
        return [
            agg("2026年8月30日", "1直", weight_kg=4000, sheet_count=100,
                management_loss_minutes=144),
            agg("2026年8月30日", "2直", weight_kg=2000, sheet_count=50),
            agg("2026年8月31日", "1直", weight_kg=3000, sheet_count=70,
                unplanned_stop_minutes=144),
            agg("2026年8月31日", "2直", weight_kg=1000, sheet_count=30),
        ]

    def tiles(self) -> dict[str, object]:
        return {t.key: t for t in dash.period_tiles(self.rows(), "8月")}

    def test_期間の合計重量(self) -> None:
        self.assertEqual(self.tiles()["period_weight"].value, "10.00")

    def test_期間の合計枚数(self) -> None:
        """累積枚数を**正式な期間の項目**として出す。"""
        self.assertEqual(self.tiles()["period_count"].value, "250")

    def test_期間の停止時間(self) -> None:
        self.assertEqual(self.tiles()["period_stop"].value, "288")

    def test_期間の稼働時間(self) -> None:
        """予定総時間(1440×4直=5760) − 停止288 = 5472分。"""
        self.assertEqual(self.tiles()["period_operating"].value, "5472")

    def test_期間の稼働率(self) -> None:
        """稼働時間 ÷ 予定総時間 = 5472 / 5760 = 95.0%。"""
        self.assertEqual(self.tiles()["period_rate"].value, "95.0")

    def test_期間の生産性(self) -> None:
        """期間合計重量 ÷ 期間稼働時間(h) = 10t ÷ 91.2h。"""
        self.assertEqual(self.tiles()["period_productivity"].value, "0.11")

    def test_保存が無くても0で出す(self) -> None:
        tiles = {t.key: t for t in dash.period_tiles([], "8月")}
        self.assertEqual(tiles["period_weight"].value, "0.00")
        self.assertEqual(tiles["period_rate"].value, "0.0")

    def test_割り算には途中式を添える(self) -> None:
        rate = self.tiles()["period_rate"]
        keys = [s["key"] for s in rate.steps]
        self.assertIn("period_whole", keys)
        self.assertIn("period_rate", keys)

    def test_期間と書いてある(self) -> None:
        """「本日の…」のままでは、何を見ているのか分かりません。"""
        for tile in self.tiles().values():
            self.assertTrue(tile.title.startswith("期間の"), tile.title)
            self.assertEqual(tile.note, "8月")


class PeriodFormulaTests(unittest.TestCase):
    """途中式 ── **日数ではなく直数で割る。**"""

    def steps(self):
        rows = [agg("2026年8月30日", "1直", management_loss_minutes=144),
                agg("2026年8月30日", "2直"),
                agg("2026年8月31日", "1直")]
        return {s.key: s for s in formula.period_steps(rows, "8月30日〜31日")}

    def test_予定総時間は直数で伸ばす(self) -> None:
        step = self.steps()["period_whole"]
        self.assertIn("1440", step.substituted)
        self.assertIn("× 3", step.substituted)
        self.assertIn("4320", step.result)

    def test_日数も添える(self) -> None:
        self.assertIn("2日ぶん", self.steps()["period_shifts"].substituted)

    def test_稼働率の式は割り算のまま(self) -> None:
        step = self.steps()["period_rate"]
        self.assertIn("稼働時間 ÷ 予定総時間", step.formula)
        self.assertIn("平均ではありません", step.note)

    def test_飾りを画面に出さない(self) -> None:
        for step in self.steps().values():
            self.assertNotIn("**", step.formula + step.result + step.note)


class ByShiftTests(unittest.TestCase):
    """期間ぶんを直ごとに畳む ── 「どの直がいちばん止まっているか」。"""

    def test_同じ直をまとめる(self) -> None:
        rows = summary_service.by_shift([
            agg("2026年8月30日", "1直", weight_kg=1000,
                management_loss_minutes=30),
            agg("2026年8月31日", "1直", weight_kg=2000,
                management_loss_minutes=20),
            agg("2026年8月31日", "2直", weight_kg=500),
        ])
        self.assertEqual([r.shift for r in rows], ["1直", "2直"])
        self.assertEqual(rows[0].weight_kg, 3000)
        self.assertEqual(rows[0].management_loss_minutes, 50)

    def test_並びは紙と同じ(self) -> None:
        rows = summary_service.by_shift([
            agg("2026年8月31日", "3直"), agg("2026年8月31日", "1直"),
            agg("2026年8月31日", "日勤"), agg("2026年8月31日", "2直")])
        self.assertEqual([r.shift for r in rows], ["1直", "2直", "3直", "日勤"])


@unittest.skipUnless(HAS_FLASK, SKIP)
class ScreenTests(WebTestCase):
    """画面 ── **切り替えると、停止の輪も期間になる。**"""

    def setUp(self) -> None:
        super().setUp()
        self.post("/api/entry/line", {"line": "L-1"})

    def save(self, day: str, shift: str, *, stop_minutes: str = "60",
             mai: str = "10") -> None:
        key = dict(report_date=day, line="L-1", shift=shift, page=1)
        self.repo().save(
            HeaderRecord(**key, worker="山田"),
            [DetailRecord(**key, row_no=1, lot="A1", ken="10", mai=mai,
                          tut="1", kz="08", kh="00", sz="09", sh="00",
                          wei="1000", tim="60", s="1", th=stop_minutes,
                          con="10")])

    def dashboard(self, mode: str) -> dict:
        return self.post("/api/graph/dashboard", {
            "start": "2026-08-01", "end": "2026-08-31",
            "mode": mode}).get_json()

    def tiles(self, mode: str) -> dict:
        return {t["key"]: t for t in self.dashboard(mode)["tiles"]}

    def test_その日が既定(self) -> None:
        self.save("2026年8月30日", "1直")
        tiles = self.tiles("day")
        self.assertIn("today_weight", tiles)
        self.assertNotIn("period_weight", tiles)

    def test_期間に切り替えると期間の数字になる(self) -> None:
        self.save("2026年8月30日", "1直")
        self.save("2026年8月31日", "1直")
        tiles = self.tiles("period")
        self.assertIn("period_weight", tiles)
        self.assertNotIn("today_weight", tiles)
        self.assertEqual(tiles["period_count"]["value"], "20")

    def test_停止の輪が期間ぶん出る(self) -> None:
        """**ここが本題。** 日集計でしか停止が出ませんでした。"""
        self.save("2026年8月30日", "1直", stop_minutes="30")
        self.save("2026年8月31日", "1直", stop_minutes="45")
        tiles = self.tiles("period")
        self.assertEqual(tiles["stop_items"]["total"], "75")
        self.assertTrue(tiles["stop_items"]["slices"])
        self.assertEqual(tiles["stop_kinds"]["total"], "75")
        self.assertIn("期間", tiles["stop_items"]["note"])

    def test_その日のままならその日の停止(self) -> None:
        self.save("2026年8月30日", "1直", stop_minutes="30")
        self.save("2026年8月31日", "1直", stop_minutes="45")
        tiles = self.tiles("day")
        # いまの報告日(今日)には保存が無いので、その日の停止は0
        self.assertEqual(tiles["stop_items"]["total"], "0")
        self.assertIn("本日", tiles["stop_items"]["note"])

    def test_停止の表も期間になる(self) -> None:
        self.save("2026年8月30日", "1直", stop_minutes="30")
        self.save("2026年8月31日", "1直", stop_minutes="45")
        table = self.tiles("period")["stop_table"]
        self.assertTrue(table["rows"])
        self.assertIn("期間", table["note"])

    def test_直別の棒にも期間と書く(self) -> None:
        """棒は期間ぶんなのに札が「本日」だと、読み違えます。"""
        self.save("2026年8月30日", "1直")
        tiles = self.tiles("period")
        self.assertEqual(tiles["shift_stop"]["note"], "期間")
        self.assertEqual(tiles["shift_weight"]["note"], "期間")

    def test_直別も期間で畳む(self) -> None:
        self.save("2026年8月30日", "1直")
        self.save("2026年8月31日", "1直")
        table = self.tiles("period")["shift_table"]
        self.assertEqual(len(table["rows"]), 1, "1直が2行に出ています")
        self.assertEqual(table["title"], "期間の直別集計")

    def test_知らない見せ方はその日に倒す(self) -> None:
        """画面が古くても動くこと。"""
        self.save("2026年8月30日", "1直")
        body = self.post("/api/graph/dashboard", {
            "start": "2026-08-01", "end": "2026-08-31",
            "mode": "なにこれ"}).get_json()
        self.assertEqual(body["mode"], "day")

    def test_画面に切り替えがある(self) -> None:
        html = self.get("/graph").get_data(as_text=True)
        self.assertIn('id="mode"', html)
        self.assertIn("期間(まとめて見る)", html)


if __name__ == "__main__":
    unittest.main()
