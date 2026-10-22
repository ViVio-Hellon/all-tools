"""LOTで時間を割り振る (v4.43.0)

    1直ページ1 LOTAが 1直初めてのロット
        5枚1包 / 5枚1包 / 2枚1包
    LOTで時間を割り振る → 7:00〜割り振り / 割り振り / 割り振り〜ﾀﾞﾌﾞﾙｸﾘｯｸの時間
    続いてLOTB(3枚1包 / 2枚1包 / 1枚1包)
    → LOTAの続き〜割り振り / 割り振り / 割り振り〜ﾀﾞﾌﾞﾙｸﾘｯｸの時間

    1ロット目であれば直開始から、2ロット目以降は前ロットの続きから、押したタイミングまで
    停止・休憩をまたぐ・EX・VC交換 は「時間を割り振る」と同じ処理

【約束】
    ・割り振るのは直の最後のロット(ロット№が変わった行から最後の行まで)。それより前の行は触らない
    ・終わりはダブルクリックで入れる時刻と同じ(分の1の位を 0 か 5 に寄せる)
    ・区切りは時計の5分刻み(前のロットの終わりが 9:37 でも 9:40・9:45 …)
    ・前のロットの最後の行に終了が無い・いまが直の外・過去の直を開いている → 断る
    ・いまが休憩の中なら休憩の始まりまで。休憩をまたげば休憩を飛ばし、またぐ行に休憩「0」
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import time_split as ts  # noqa: E402
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402

DAY = date(2026, 10, 21)


def rows_of(*pairs, lot="LOTA", start=1, **first) -> dict:
    """(枚, 包) の行。1行目にだけロット№(と first の欄)。"""
    out = {}
    for i, (mai, tut) in enumerate(pairs):
        values = {"MAI": str(mai), "TUT": str(tut)}
        if i == 0:
            values.update({"LOT": lot, **first})
        out[str(start + i)] = values
    return out


def span(r: dict) -> int:
    return (ts.parse_hhmm(r["end"]) - ts.parse_hhmm(r["start"])) % 1440


class AlignTests(unittest.TestCase):
    def test_始まりが5分刻みでなくても区切りは時計の5分刻み(self) -> None:
        plan = ts.split([Decimal(0)] * 3, 53, origin=9 * 60 + 37)
        clocks = [(ts.clock(9 * 60 + 37, a), ts.clock(9 * 60 + 37, b)) for a, b in plan.slots]
        self.assertEqual(clocks[0][0], ("09", "37"))
        self.assertEqual(clocks[-1][1], ("10", "30"))
        for (_, end), _next in zip(clocks[:-1], clocks[1:]):
            self.assertEqual(int(end[1]) % 5, 0, clocks)

    def test_休憩がちょうど始まりからなら最初の行がまたぐ(self) -> None:
        rest = ts.break_window("1直", 12 * 60, 150, from_start=True)
        self.assertEqual(rest, (0, 60))
        self.assertIsNone(ts.break_window("1直", 12 * 60, 150))
        plan = ts.split([Decimal(0)] * 2, 150, rest=rest)
        self.assertEqual(plan.break_index, 0)
        self.assertEqual(plan.slots[0][0], 0)
        self.assertEqual(plan.slots[-1][1], 150)


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class DistributeLotWebTests(WebTestCase):
    """1直(07:00〜15:00)で。いまの時刻は試験が決める。"""

    def setUp(self) -> None:
        super().setUp()
        from nippou import user_settings
        from nippou.config import KEY_TIME_SPLIT, KEY_TIME_SPLIT_FACTORS
        from tests.test_scenario_shift_flow import _Now, install_clock

        user_settings.save(KEY_TIME_SPLIT, True)
        # 割り振りの重みは梱包数だけで見る(EX・段取りの倍率は、その試験で入れる)
        user_settings.save(KEY_TIME_SPLIT_FACTORS, {"ex": 1, "setup": 1, "vc": 1})
        self._now = _Now
        self.at("08:12")
        install_clock(self)

    def at(self, hhmm: str) -> None:
        hour, minute = map(int, hhmm.split(":"))
        self._now.value = datetime.combine(DAY, time(hour, minute))

    def bounds(self):
        from nippou.services import shift_check
        return shift_check.bounds_of(self.repo().get_shift_times(), "1直")

    def lot(self, rows: dict, *, dry: bool = True, **extra):
        res = self.post("/api/entry/distribute-lot",
                        {"rows": rows, "header": {"worker": "山田"}, "dry_run": dry, **extra})
        return res

    def plan(self, rows: dict, **extra) -> dict:
        res = self.lot(rows, **extra)
        self.assertEqual(res.status_code, 200, res.get_json())
        return res.get_json()

    def test_1ロット目は直の開始から押した時刻まで(self) -> None:
        start, _end = self.bounds()
        self.at("08:12")                                   # ダブルクリックと同じく 08:10 に寄せる
        plan = self.plan(rows_of((5, 1), (5, 1), (2, 1)))
        self.assertEqual(plan["count"], 3)
        self.assertEqual(plan["rows"][0]["start"], start)
        self.assertEqual(plan["rows"][-1]["end"], "08:10")
        self.assertEqual(plan["lot"]["lot"], "LOTA")
        self.assertEqual(plan["lot"]["from"], "直の開始")
        for a, b in zip(plan["rows"], plan["rows"][1:]):
            self.assertEqual(a["end"], b["start"], "行の時刻はつながる")
        spans = [span(r) for r in plan["rows"]]
        self.assertGreater(spans[0], spans[2], "5枚の行は2枚の行より長い")

    def test_2ロット目は前のロットの続きから_前の行は触らない(self) -> None:
        """LOTA を 08:10 までに割り振ったあと、LOTB を 09:00 に押す。"""
        self.at("08:10")
        screen = rows_of((5, 1), (5, 1), (2, 1))
        body = self.plan(screen, dry=False)
        for key in screen:
            screen[key].update({f: body["rows"][key].get(f, "") for f in ("KZ", "KH", "SZ", "SH")})
        self.assertEqual(f'{screen["3"]["SZ"]}:{screen["3"]["SH"]}', "08:10")
        screen.update(rows_of((3, 1), (2, 1), (1, 1), lot="LOTB", start=4))
        self.at("09:02")
        plan = self.plan(screen)
        self.assertEqual([r["row"] for r in plan["rows"]], [4, 5, 6], "LOTB の行だけ")
        self.assertEqual(plan["rows"][0]["start"], "08:10", "LOTA の続きから")
        self.assertEqual(plan["rows"][-1]["end"], "09:00")
        self.assertEqual(plan["lot"]["from"], "前のロットの続き(3行目の終了)")
        spans = [span(r) for r in plan["rows"]]
        self.assertGreater(spans[0], spans[2])
        # 書くときも LOTA の行は触らない
        body = self.plan(screen, dry=False)
        self.assertEqual(f'{body["rows"]["1"]["KZ"]}:{body["rows"]["1"]["KH"]}', screen["1"]["KZ"] + ":"
                         + screen["1"]["KH"])
        self.assertEqual(f'{body["rows"]["3"]["SZ"]}:{body["rows"]["3"]["SH"]}', "08:10")
        self.assertEqual(f'{body["rows"]["6"]["SZ"]}:{body["rows"]["6"]["SH"]}', "09:00")
        self.assertIn("ロット LOTB の 3行に 08:10〜09:00", body["message"])

    def test_前のロットの終わりが5分刻みでなくても区切りは5分刻み(self) -> None:
        screen = rows_of((5, 1), lot="LOTA", KZ="07", KH="00", SZ="08", SH="07")
        screen.update(rows_of((5, 1), (5, 1), (5, 1), lot="LOTB", start=2))
        self.at("09:00")
        plan = self.plan(screen)
        self.assertEqual(plan["rows"][0]["start"], "08:07")
        for r in plan["rows"][:-1]:
            self.assertEqual(int(r["end"][-1]) % 5, 0, plan["rows"])

    def test_前のロットの最後の行に終了が無ければ断る(self) -> None:
        screen = rows_of((5, 1))
        screen.update(rows_of((5, 1), lot="LOTB", start=2))
        res = self.lot(screen)
        self.assertEqual(res.status_code, 422)
        self.assertIn("前のロットの最後の行(1行目)に終了の時刻がありません", res.get_json()["error"]["message"])

    def test_停止の分は先に渡す(self) -> None:
        self.at("08:00")
        plan = self.plan(rows_of((5, 1), (5, 1), S="ｲ", TH="30"))
        spans = [span(r) for r in plan["rows"]]
        self.assertGreaterEqual(spans[0], 30 + 5)
        self.assertEqual(sum(spans), 60)

    def test_休憩をまたげば飛ばして休憩0を入れる(self) -> None:
        screen = rows_of((5, 1), lot="LOTA", KZ="07", KH="00", SZ="11", SH="00")
        screen.update(rows_of((5, 1), (5, 1), (5, 1), lot="LOTB", start=2))
        self.at("14:00")
        plan = self.plan(screen)
        self.assertEqual(plan["rest"]["start"], "12:00")
        self.assertEqual(sum(span(r) for r in plan["rows"]), 180)
        body = self.plan(screen, dry=False)
        crossing = body["rows"][str(plan["rest"]["row"])]
        self.assertIn(("0", "60"), [(crossing.get(c), crossing.get(m)) for c, m in ts.STOP_SLOTS])

    def test_前のロットが休憩の始まりで終われば最初の行が休憩をまたぐ(self) -> None:
        screen = rows_of((5, 1), lot="LOTA", KZ="07", KH="00", SZ="12", SH="00")
        screen.update(rows_of((5, 1), (5, 1), lot="LOTB", start=2))
        self.at("14:00")
        plan = self.plan(screen)
        self.assertEqual(plan["rows"][0]["start"], "12:00")
        self.assertEqual(plan["rest"]["row"], 2)

    def test_いまが休憩の中なら休憩の始まりまで(self) -> None:
        screen = rows_of((5, 1), lot="LOTA", KZ="07", KH="00", SZ="11", SH="00")
        screen.update(rows_of((5, 1), (5, 1), lot="LOTB", start=2))
        self.at("12:20")
        plan = self.plan(screen)
        self.assertEqual(plan["rows"][-1]["end"], "12:00")
        self.assertIn("休憩の始まり 12:00 までを割り振ります", plan["lot"]["note"])

    def test_EXのロットとVC交換も同じ処理(self) -> None:
        from nippou import user_settings
        from nippou.config import KEY_TIME_SPLIT_FACTORS

        user_settings.save(KEY_TIME_SPLIT_FACTORS, {"ex": 1.2, "setup": 1.5, "vc": 1.5})
        screen = rows_of((5, 1), lot="LOTA", VC="両面", KZ="07", KH="00", SZ="08", SH="00")
        screen.update(rows_of((5, 1), (5, 1), lot="LOTB", start=2, VC="片面", ET="EX"))
        self.at("09:00")
        plan = self.plan(screen)
        notes = " ".join(plan["adjust_notes"])
        self.assertIn("EX のロットの 2 行は 1.2倍", notes)
        self.assertIn("ロットの変わり目 1 か所", notes)
        self.assertIn("VC が前のロットと違う 1 か所", notes)
        spans = [span(r) for r in plan["rows"]]
        self.assertGreater(spans[0], spans[1], "変わり目の1梱包目は長め")

    def test_いまが直の外なら断る(self) -> None:
        """開いている直(1直)の終わりを過ぎてから押した(画面は1直のまま)。"""
        from unittest import mock

        self.at("14:00")
        with mock.patch("app.routes.entry._clock_now", return_value=datetime.combine(DAY, time(16, 30))):
            res = self.lot(rows_of((5, 1)))
        self.assertEqual(res.status_code, 422)
        self.assertIn("1直の時間の外", res.get_json()["error"]["message"])

    def test_ロット番号が無ければ断る(self) -> None:
        res = self.lot({"1": {"MAI": "5", "TUT": "1"}})
        self.assertEqual(res.status_code, 422)
        self.assertIn("ロット№を打った行がありません", res.get_json()["error"]["message"])

    def test_設定でオンにした端末だけ(self) -> None:
        from nippou import user_settings
        from nippou.config import KEY_TIME_SPLIT

        self.assertIn('id="time-split-lot"', self.get("/").get_data(as_text=True))
        user_settings.save(KEY_TIME_SPLIT, False)
        self.assertNotIn('id="time-split-lot"', self.get("/").get_data(as_text=True))
        self.assertEqual(self.lot(rows_of((5, 1))).status_code, 403)


if __name__ == "__main__":
    unittest.main()
