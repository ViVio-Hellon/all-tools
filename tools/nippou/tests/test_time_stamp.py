"""開始・終了の時刻をダブルクリックで入れる (v4.2.0)

    入力画面の開始終了の時間テキストボックスをダブルクリックすると
    そのタイミングの時間を入力するようにしてください
    時間 分 両方入れてください
    またそれに伴い その分の単位の1の位だけを四捨五入
    1,2,3,4は切り捨て 5はそのまま 6,7,8,9は切り上げ
    普通に入力も可能だが、ダブルクリックで2つのテキストボックスを簡単に
    埋めれる という風にしたい
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import input_rules, work_time  # noqa: E402
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402


class RoundingTests(unittest.TestCase):
    """分の1の位: 1〜4 切り捨て / 5 そのまま / 6〜9 切り上げ。"""

    def test_1の位ごと(self) -> None:
        want = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0, 5: 5, 6: 10, 7: 10, 8: 10, 9: 10}
        for ones, rounded in want.items():
            for tens in (0, 20, 50):
                with self.subTest(minute=tens + ones):
                    self.assertEqual(work_time.round_minute(tens + ones),
                                     tens + rounded)

    def test_時と分の2桁で返す(self) -> None:
        self.assertEqual(work_time.stamp(datetime(2026, 10, 1, 8, 3)), ("08", "00"))
        self.assertEqual(work_time.stamp(datetime(2026, 10, 1, 13, 5)), ("13", "05"))
        self.assertEqual(work_time.stamp(datetime(2026, 10, 1, 13, 7)), ("13", "10"))

    def test_切り上げで60分になれば時を進める(self) -> None:
        self.assertEqual(work_time.stamp(datetime(2026, 10, 1, 13, 57)), ("14", "00"))
        # 3直は日をまたぐ。**0時に戻る**(24時にはしない ── 時は 0〜23)
        self.assertEqual(work_time.stamp(datetime(2026, 10, 1, 23, 58)), ("00", "00"))
        self.assertEqual(work_time.stamp(datetime(2026, 10, 1, 23, 55)), ("23", "55"))

    def test_埋めるのは時と分の2欄(self) -> None:
        self.assertEqual(work_time.STAMP_FIELDS,
                         {"start": ("KZ", "KH"), "end": ("SZ", "SH")})

    def test_欄の属性でどの欄かを渡す(self) -> None:
        from nippou.logic import input_shortcuts

        for family, which in (("KZ", "start"), ("KH", "start"),
                              ("SZ", "end"), ("SH", "end")):
            with self.subTest(family=family):
                attrs = input_rules.as_attributes(family)
                self.assertEqual(attrs["data-stamp"], which)
                # 説明はマウスを乗せると出る簡易説明へ(v4.7.0)。`title` と
                # 両方あると、ブラウザの説明と2つ重なって出る
                self.assertNotIn("title", attrs)
                self.assertIn("ダブルクリック", input_shortcuts.hint(family))
                self.assertIn("0 か 5 に丸め", input_shortcuts.hint(family))
        self.assertNotIn("data-stamp", input_rules.as_attributes("LOT"))
        self.assertNotIn("data-stamp", input_rules.as_attributes("TH"))


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class StampWebTests(WebTestCase):
    """`/api/entry/state` に `stamp` を添える(打ったときと同じ道)。"""

    def stamp(self, which: str, row: int = 1, at=datetime(2026, 10, 1, 10, 7),
              rows=None) -> dict:
        from app.routes import entry as entry_routes

        body = {"rows": rows or {"1": {"LOT": "N7131T0"}},
                "header": {"worker": "山田"}, "checks": {},
                "row": row, "changed": "KH" if which == "start" else "SH",
                "stamp": {"row": row, "which": which}}
        with patch.object(entry_routes, "_clock_now", return_value=at):
            res = self.post("/api/entry/state", body)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:300])
        return res.get_json()

    def test_開始をダブルクリックすると時と分の両方に入る(self) -> None:
        body = self.stamp("start", at=datetime(2026, 10, 1, 9, 3))
        self.assertEqual((body["rows"]["1"]["KZ"], body["rows"]["1"]["KH"]), ("09", "00"))
        self.assertEqual(body["stamp"], {"ok": True, "row": 1, "which": "start",
                                         "text": "09:00", "message": ""})

    def test_終了も_作業時間まで出る(self) -> None:
        rows = {"1": {"LOT": "N7131T0", "KZ": "09", "KH": "00"}}
        body = self.stamp("end", rows=rows, at=datetime(2026, 10, 1, 10, 7))
        one = body["rows"]["1"]
        self.assertEqual((one["SZ"], one["SH"]), ("10", "10"))
        self.assertEqual(one["TIM"], "70")          # 9:00 → 10:10 は70分

    def test_ほかの行は触らない(self) -> None:
        rows = {"1": {"KZ": "08", "KH": "00"}, "3": {"KZ": "11", "KH": "30"}}
        body = self.stamp("start", row=3, rows=rows, at=datetime(2026, 10, 1, 12, 4))
        self.assertEqual((body["rows"]["3"]["KZ"], body["rows"]["3"]["KH"]), ("12", "00"))
        self.assertEqual((body["rows"]["1"]["KZ"], body["rows"]["1"]["KH"]), ("08", "00"))

    def test_過去の直を開いているときは入れない(self) -> None:
        """いまの時刻は、その直の時刻ではない(後から作った前日の直など)。"""
        from nippou import work_context
        from nippou.services.nippou_service import RecallState

        work_context.get_context().recall = RecallState(
            active=True, report_date="2026年9月30日", line="L-1", shift="1直", page=1)
        body = self.stamp("start")
        self.assertFalse(body["stamp"]["ok"])
        self.assertIn("過去の直", body["stamp"]["message"])
        self.assertEqual(body["rows"]["1"]["KZ"], "")

    def test_知らない欄や行は無視する(self) -> None:
        from app.routes import entry as entry_routes

        for stamp in ({"row": 1, "which": "middle"}, {"row": 99, "which": "start"},
                      {"row": "x", "which": "start"}, "start"):
            with self.subTest(stamp=stamp):
                with patch.object(entry_routes, "_clock_now",
                                  return_value=datetime(2026, 10, 1, 9, 0)):
                    body = self.post("/api/entry/state",
                                     {"rows": {}, "stamp": stamp}).get_json()
                self.assertNotIn("stamp", body)
                self.assertEqual(body["rows"]["1"]["KZ"], "")

    def test_画面に印と案内とダブルクリックの口(self) -> None:
        html = self.get("/").get_data(as_text=True)
        self.assertIn('data-stamp="start"', html)
        self.assertIn('data-stamp="end"', html)
        self.assertIn('id="stamp-tip"', html)
        js = (Path(__file__).resolve().parent.parent / "app" / "static" / "js"
              / "views" / "entry.js").read_text(encoding="utf-8")
        self.assertIn('addEventListener("dblclick"', js)
        self.assertIn("stamp: { row, which }", js)


if __name__ == "__main__":
    unittest.main()
