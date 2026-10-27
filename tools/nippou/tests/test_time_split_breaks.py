"""時間を割り振る / LOTで時間を割り振る: 休憩をもう打ってあれば、その時刻には足さない

【利用者から】

    時間を割り振る / LOTで時間を割り振る: 共に休憩がすでに入っている場合は、12:00, 18:00, 02:00 に
    時間がまたいでいるところに休憩は足さないようにしてください。無い場合だけ自動で休憩を追加にしてください

決まり(`logic/time_split.plan_breaks`・`match_rests`):

    ・**休憩の時刻ごとに見る**。割り振る時間がまたぐ時刻(12:00・18:00・02:00)のそれぞれについて、
      この直にもう休憩「0」を打ってあればその時刻は足さず、無い時刻にだけ足す
    ・打ってある休憩をどの時刻のものと見るか: 時刻が重なればその時刻(ずらして 11:45〜12:45 でも 12:00)、
      重ならなければいちばん近い時刻。休憩の長さの半分より短いもの(10分休憩・打ちかけの 1分)は当てない。
      時刻の無い休憩は、行の順に、まだ当たっていない早い時刻へ
    ・打ってある休憩の行は消さない・動かさない(停止として先に渡し、その時刻は飛ばさない ── 二重に引かない)。
      時刻を書いた休憩の行は、その時刻をまたぐように合わせる(`split_pinned`)

止めた時計で: 2つまたぐ直(1直を 07:00〜19:30 にした日)、3直(02:00 が日をまたぐ)、休憩をまたがない
LOTで割り振る、ほかのロットの行に打ってある休憩、を通す。1直・2直・3直の通しは
`test_scenario_break_time_split`、ブラウザは `test_break_time_split_browser`。
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import break_time, time_split as ts  # noqa: E402
from tests import test_scenario_break_overrun as base  # noqa: E402
from tests._web import HAS_FLASK, SKIP_REASON  # noqa: E402

D = Decimal


def rest_row(index: int, minutes: int, clock: str = "") -> dict:
    values = {"LOT": f"L{index}", "S": "0", "TH": str(minutes)}
    if clock:
        values["break_range"] = clock
    return values


# ======================================================================
# 計算だけ
# ======================================================================
class BreakSlotTests(unittest.TestCase):
    def test_直がまたぐ休憩の時刻(self) -> None:
        self.assertEqual([(s.begin, s.at) for s in ts.break_slots(7 * 60, 480)], [("12:00", 300)])
        self.assertEqual([(s.begin, s.at) for s in ts.break_slots(15 * 60, 470)], [("18:00", 180)])
        # 3直は日をまたぐ(22:50 から 190 分目が 02:00)
        self.assertEqual([(s.begin, s.at) for s in ts.break_slots(22 * 60 + 50, 490)], [("02:00", 190)])
        # 2つまたぐ直(1直を 19:30 までにした日)
        self.assertEqual([s.begin for s in ts.break_slots(7 * 60, 750)], ["12:00", "18:00"])
        # またがない(11:00 まで)・終わりちょうどで終わる休憩は数えない・始まりちょうどは from_start のときだけ
        self.assertEqual(ts.break_slots(7 * 60, 240), [])
        self.assertEqual(ts.break_slots(7 * 60, 360), [])
        self.assertEqual(ts.break_slots(12 * 60, 120), [])
        self.assertEqual([s.at for s in ts.break_slots(12 * 60, 120, from_start=True)], [0])


class MatchTests(unittest.TestCase):
    """手で打った休憩を、どの休憩の時刻のものと見るか(`match_rests`)。"""

    def matched(self, rows: list[dict], start: int = 7 * 60, total: int = 750) -> list[tuple[int, str]]:
        rests = ts.typed_rests(rows, (start, start + total))
        ts.match_rests(rests, ts.break_slots(start, total, from_start=True), start)
        return [(r.index, r.slot) for r in rests]

    def test_時刻が重なればその時刻_ずらしてあっても(self) -> None:
        self.assertEqual(self.matched([rest_row(1, 60, "11:45〜12:45")]), [(0, "12:00")])
        self.assertEqual(self.matched([rest_row(1, 60, "18:30〜19:30")]), [(0, "18:00")])

    def test_重ならなければいちばん近い時刻(self) -> None:
        # 11:00〜11:50(早めに取った昼)→ 12:00。16:30〜17:20 → 18:00(12:00 より近い)
        self.assertEqual(self.matched([rest_row(1, 50, "11:00〜11:50")]), [(0, "12:00")])
        self.assertEqual(self.matched([rest_row(1, 50, "16:30〜17:20")]), [(0, "18:00")])

    def test_短い休憩はどの時刻にも当てない(self) -> None:
        # 10分休憩(重ならない)・打ちかけの1分(時刻なし)は、その時刻の休憩ではない
        self.assertEqual(self.matched([rest_row(1, 10, "9:00〜9:10")]), [(0, "")])
        self.assertEqual(self.matched([rest_row(1, 1)]), [(0, "")])
        self.assertEqual(self.matched([rest_row(1, 29)]), [(0, "")])
        self.assertEqual(self.matched([rest_row(1, 30)]), [(0, "12:00")], "半分(30分)からは当てる")

    def test_時刻の無い休憩は行の順にまだ当たっていない早い時刻へ(self) -> None:
        self.assertEqual(self.matched([rest_row(1, 60), rest_row(2, 60)]), [(0, "12:00"), (1, "18:00")])
        # 時刻を書いた休憩が 12:00 に当たっていれば、時刻の無い休憩は 18:00
        self.assertEqual(self.matched([rest_row(1, 60), rest_row(2, 60, "12:00〜13:00")]),
                         [(0, "18:00"), (1, "12:00")])

    def test_直の外の時刻は時刻の無い休憩として数える(self) -> None:
        self.assertEqual(self.matched([rest_row(1, 60, "21:00〜22:00")]), [(0, "12:00")])

    def test_3直の02時は日をまたいで当てる(self) -> None:
        self.assertEqual(self.matched([rest_row(1, 60, "1:50〜2:50")], 22 * 60 + 50, 490), [(0, "02:00")])
        self.assertEqual(self.matched([rest_row(1, 60, "23:30〜0:30")], 22 * 60 + 50, 490), [(0, "02:00")])


class PlanTests(unittest.TestCase):
    def plan(self, rows: list[dict], start: int = 7 * 60, total: int = 750):
        return ts.plan_breaks(rows, range(len(rows)), shift_start=start, shift_total=total,
                              span_start=start, span_total=total, shift_window=(start, start + total))

    def test_12時を打ってあれば12時は足さず18時だけ足す(self) -> None:
        got = self.plan([{"LOT": "A"}, rest_row(2, 60, "12:00〜13:00"), {"LOT": "C"}])
        self.assertEqual([(s.begin, s.added) for s in got.slots], [("12:00", False), ("18:00", True)])
        self.assertEqual(got.skip, [(660, 60)], "飛ばすのは足す時刻だけ")
        self.assertEqual([p[:3] for p in got.pins], [(1, 300, 360)])

    def test_何も無ければ全部足す_またがなければ何もしない(self) -> None:
        got = self.plan([{"LOT": "A"}, {"LOT": "B"}])
        self.assertEqual([s.added for s in got.slots], [True, True])
        self.assertEqual(self.plan([{"LOT": "A"}], total=240).slots, [])

    def test_ほかのロットの行に打ってあっても足さない(self) -> None:
        # LOTで割り振るのは 3行目だけ。12:00 の休憩は 1行目(ほかのロット)に打ってある
        rows = [rest_row(1, 60, "12:00〜13:00"), {"LOT": "B"}, {"LOT": "C"}]
        got = ts.plan_breaks(rows, range(2, 3), shift_start=7 * 60, shift_total=480,
                             span_start=11 * 60, span_total=180, from_start=True)
        self.assertEqual([(s.begin, s.added) for s in got.slots], [("12:00", False)])
        self.assertEqual(got.pins, [], "割り振らない行は合わせない")


class SplitTests(unittest.TestCase):
    def test_休憩を2つ飛ばす(self) -> None:
        plan = ts.split([D(0)] * 4, 750, rests=[(300, 60), (660, 60)])
        self.assertEqual(plan.slots[0][0], 0)
        self.assertEqual(plan.slots[-1][1], 750)
        for (a, b), (c, _d) in zip(plan.slots, plan.slots[1:]):
            self.assertEqual(b, c)
        for at, index in zip((300, 660), plan.break_indexes):
            a, b = plan.slots[index]
            self.assertTrue(a <= at and at + 60 <= b, (at, plan.slots))

    def test_打ってある休憩の時刻をその行がまたぐように合わせる(self) -> None:
        # 1行目に 11:30〜12:30 の休憩(4行・1直)。ふつうに割り振ると 1行目は 7:00〜9:45 で、休憩の外
        plain = ts.split([D(60), D(0), D(0), D(0)], 480)
        self.assertLess(plain.slots[0][1], 330)
        plan = ts.split_pinned([D(60), D(0), D(0), D(0)], 480, pins=[(0, 270, 330)])
        self.assertEqual(plan.slots[0], (0, 330))
        self.assertEqual(plan.pinned, [0])
        self.assertEqual(plan.slots[-1][1], 480)
        # 後ろの行の休憩: その行の始まりを休憩の始まりに
        plan = ts.split_pinned([D(0), D(0), D(0), D(60)], 480, pins=[(3, 300, 360)])
        a, b = plan.slots[3]
        self.assertTrue(a <= 300 and 360 <= b, plan.slots)
        # もう含んでいれば変えない
        self.assertEqual(ts.split_pinned([D(0), D(60), D(0)], 480, pins=[(1, 200, 260)]).slots,
                         ts.split([D(0), D(60), D(0)], 480).slots)

    def test_合わせられなければそのまま(self) -> None:
        # 割り振る時間の外の休憩
        self.assertEqual(ts.split_pinned([D(60), D(0)], 120, pins=[(0, 200, 260)]).slots,
                         ts.split([D(60), D(0)], 120).slots)


# ======================================================================
# 画面の道(止めた時計)
# ======================================================================
@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class BreakWebTests(base.BreakOverrunScenarioTests):
    #: 1直を 19:30 までにした日(12:00 と 18:00 の2つをまたぐ)
    LONG = {"1": ("07:00", "19:30"), "2": ("19:30", "22:50"), "3": ("22:50", "07:00"), "昼": ("08:00", "17:00")}

    def open_shift(self, when: str, shift: str, extra: dict, count: int = 6, *, long: bool = False) -> dict:
        from nippou import user_settings
        from nippou.config import KEY_TIME_SPLIT
        user_settings.save(KEY_TIME_SPLIT, True)
        if long:
            self.repo().replace_shift_times(self.LONG)
        body = self.start_shift(when, base.WORKER)
        self.assertEqual(body["shift"], shift, body.get("message"))
        rows = self.typed({str(n): {"LOT": self.lots[n]} for n in range(1, count + 1)})
        for n, values in extra.items():
            rows[str(n)].update(values)
        return rows

    def split(self, rows: dict, url: str = "/api/entry/distribute-times", **extra):
        dry = self.post(url, self.sheet(rows, base.WORKER, dry_run=True, **extra))
        self.assertEqual(dry.status_code, 200, dry.get_json())
        res = self.post(url, self.sheet(rows, base.WORKER, **extra))
        self.assertEqual(res.status_code, 200, res.get_json())
        return dry.get_json(), res.get_json()

    @staticmethod
    def rests(rows: dict) -> dict[int, list[tuple[str, str]]]:
        out: dict[int, list[tuple[str, str]]] = {}
        for n, values in rows.items():
            for code, minutes in ts.STOP_SLOTS:
                if values.get(code) == "0":
                    out.setdefault(int(n), []).append((values[minutes], break_time.normalize(values["break_range"])))
        return out

    def tims(self, rows: dict) -> Decimal:
        return sum((Decimal(v["TIM"] or 0) for v in rows.values()), Decimal(0))

    def problems(self, body: dict) -> dict:
        return {e["row"]: e["problems"] for e in body["break_entries"] if e["problems"]}

    def test_2つまたぐ直で12時を打ってあれば18時だけ足す(self) -> None:
        rows = self.open_shift("2026-10-05 07:05", "1直", {2: {"S": "0", "TH": "60", "break_range": "12:00〜13:00"}},
                               long=True)
        self.at("2026-10-05 19:20")
        dry, body = self.split(rows)
        self.assertEqual([(b["time"], b["added"]) for b in dry["breaks"]], [("12:00", False), ("18:00", True)])
        notes = "\n".join(dry["break_notes"])
        self.assertIn("12:00 の休憩はもう入っているので足しません(2行目の休憩「0」60分 12:00〜13:00)", notes)
        self.assertIn("休憩 18:00〜19:00(60分)は飛ばします。", notes)
        self.assertIn("12:00 の休憩はもう入っているので足しませんでした", body["message"])
        self.assertIn("18:00 の休憩を足しました", body["message"])
        rests = self.rests(body["rows"])
        crossing = dry["rest"]["row"]
        self.assertEqual(rests, {2: [("60", "12:00〜13:00")], crossing: [("60", "18:00〜19:00")]})
        self.assertEqual(self.tims(body["rows"]), 750 - 120, "休憩は2つで120分だけ引く")
        self.assertEqual(self.problems(body), {})
        saved = self.save(body["rows"], base.WORKER)
        self.assertEqual(saved.status_code, 200, self.codes_of(saved))

    def test_2つまたぐ直で何も無ければ2つとも足す(self) -> None:
        rows = self.open_shift("2026-10-05 07:05", "1直", {}, long=True)
        self.at("2026-10-05 19:20")
        dry, body = self.split(rows)
        self.assertEqual([(b["time"], b["added"]) for b in dry["breaks"]], [("12:00", True), ("18:00", True)])
        got = sorted(r for values in self.rests(body["rows"]).values() for r in values)
        self.assertEqual(got, [("60", "12:00〜13:00"), ("60", "18:00〜19:00")])
        self.assertEqual(self.tims(body["rows"]), 750 - 120)
        self.assertEqual(self.problems(body), {})

    def test_3直は日をまたいだ02時の休憩を見る(self) -> None:
        # 2行目に 01:50〜02:50 と打ってある → 足さない。無ければ 02:00〜03:00 を足す
        rows = self.open_shift("2026-10-05 22:55", "3直",
                               {2: {"S": "0", "TH": "60", "break_range": "1:50〜2:50"}}, count=4)
        self.at("2026-10-06 06:50")
        dry, body = self.split(rows)
        self.assertEqual([(b["time"], b["added"]) for b in dry["breaks"]], [("02:00", False)])
        self.assertEqual(self.rests(body["rows"]), {2: [("60", "1:50〜2:50")]})
        self.assertEqual(self.tims(body["rows"]), 490 - 60)
        self.assertEqual(self.problems(body), {})

        rows = {n: {**v, "S": "", "TH": "", "break_range": ""} for n, v in body["rows"].items()}
        dry, body = self.split(rows)
        self.assertEqual([(b["time"], b["added"]) for b in dry["breaks"]], [("02:00", True)])
        [(row, got)] = self.rests(body["rows"]).items()
        self.assertEqual(got, [("60", "2:00〜3:00")])
        self.assertEqual(self.problems(body), {})

    def test_LOTで割り振る_休憩をまたがなければ何も足さない(self) -> None:
        rows = self.open_shift("2026-10-05 07:05", "1直", {}, count=3)
        rows["2"]["LOT"] = rows["3"]["LOT"] = ""      # 1ロット(続きの行はロット№を打たない)
        self.at("2026-10-05 10:00")
        dry, body = self.split(rows, "/api/entry/distribute-lot", focus_row=3)
        self.assertEqual(dry["breaks"], [])
        self.assertIsNone(dry["rest"])
        self.assertEqual(self.rests(body["rows"]), {})
        self.assertNotIn("休憩", body["message"])

    def test_LOTで割り振る_ロットの行に打ってあれば足さず動かさない(self) -> None:
        # 1行目(ロット1)は 7:00〜11:00 で済み。2〜4行目(ロット2)を 11:00 から割り振る。
        # 4行目に 12:00〜13:00 の休憩を打ってある → 足さない・4行目に残す・4行目が休憩をまたぐ
        lot2 = self.lots[2]
        rows = self.open_shift("2026-10-05 07:05", "1直", {}, count=4)
        rows["1"].update(KZ="07", KH="00", SZ="11", SH="00")
        for n in ("3", "4"):
            rows[n]["LOT"] = ""            # ロット2の続きの行(ロット№は打たない)
        rows["2"]["LOT"] = lot2
        rows["4"].update(S="0", TH="60", break_range="12:00〜13:00")
        self.at("2026-10-05 14:00")
        dry, body = self.split(rows, "/api/entry/distribute-lot", focus_row=4)
        self.assertEqual(dry["lot"]["lot"], lot2)
        self.assertEqual([(b["time"], b["added"]) for b in dry["breaks"]], [("12:00", False)])
        self.assertIn("12:00 の休憩はもう入っているので足しませんでした", body["message"])
        self.assertEqual(self.rests(body["rows"]), {4: [("60", "12:00〜13:00")]})
        got = body["rows"]["4"]
        self.assertLessEqual(f'{got["KZ"]}:{got["KH"]}', "12:00")
        self.assertEqual(f'{got["SZ"]}:{got["SH"]}', "14:00")
        self.assertEqual(self.problems(body), {})

    def test_LOTで割り振る_無ければ休憩をまたぐ行に足す(self) -> None:
        rows = self.open_shift("2026-10-05 07:05", "1直", {}, count=3)
        rows["1"].update(KZ="07", KH="00", SZ="11", SH="00")
        rows["3"]["LOT"] = ""                          # 2〜3行目がロット2
        self.at("2026-10-05 14:00")
        dry, body = self.split(rows, "/api/entry/distribute-lot", focus_row=3)
        self.assertEqual([(b["time"], b["added"]) for b in dry["breaks"]], [("12:00", True)])
        self.assertIn("12:00 の休憩を足しました", body["message"])
        self.assertEqual(list(self.rests(body["rows"]).values()), [[("60", "12:00〜13:00")]])


for _name in [n for n in dir(base.BreakOverrunScenarioTests) if n.startswith("test_")]:
    setattr(BreakWebTests, _name, None)


if __name__ == "__main__":
    unittest.main()
