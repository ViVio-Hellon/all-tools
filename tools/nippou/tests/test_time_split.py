"""時間の自動割り振り (v4.30.0)

    日報入力: 開始行と終了行の時間は一切入力せず 作業者の任意のタイミングで発火させると
    開始行と終了行までの時間を割り当てる機能
    1ページ 1〜12、2ページ 1〜10 → 22行を 480分(1直 07:00〜15:00)で割り振る
    07:00 07:20 / 07:20 07:45 / 07:45 08:05 と自動で振り分けていく
    この機能のオンオフは設定に置く パスワード要

【約束】
    ・直の長さを打った行の数で等しく分け、区切りは5分刻み(積み上げを四捨五入。ずれを貯めない)
    ・停止の分はその行に先に渡す(作業時間をマイナスにしない)
    ・最初の行は直の開始、最後の行は直の終了ちょうど。日をまたぐ直(3直)は 0 時へ戻る
    ・この直の全ページ(ページ順・行順)。いまのページは画面の中身、ほかは保存した中身
    ・オン・オフは設定・管理者で、管理者モード(パスワード)のときだけ。既定はオフ
    ・停止を先に渡した残りは梱包数(枚数 × 包数。包数が空なら 1)の比で分ける。どの行にも無ければ
      均等、一部だけ無ければその行は梱包数がある行の平均(v4.34.0)。比が極端でも 5 分刻みのまま、
      どの行にも最低 1 刻み(5 分)。1 分刻みは均等でも 1 行 5 分取れないときだけ
"""
from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import time_split as ts  # noqa: E402
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402


def times(plan, start=420):
    return [(":".join(ts.clock(start, a)), ":".join(ts.clock(start, b))) for a, b in plan.slots]


class SplitTests(unittest.TestCase):
    def test_依頼の例_22行を480分(self) -> None:
        plan = ts.split([Decimal(0)] * 22, 480)
        self.assertTrue(plan.ok)
        got = times(plan)
        self.assertEqual(got[:3], [("07:00", "07:20"), ("07:20", "07:45"), ("07:45", "08:05")])
        self.assertEqual(got[-1][1], "15:00")
        self.assertEqual(len(got), 22)

    def test_つながっていて5分刻み(self) -> None:
        plan = ts.split([Decimal(0)] * 7, 480)
        for (a, b), (c, _d) in zip(plan.slots, plan.slots[1:]):
            self.assertEqual(b, c)
        self.assertTrue(all(b % 5 == 0 for _a, b in plan.slots))
        self.assertEqual(plan.slots[0][0], 0)
        self.assertEqual(plan.slots[-1][1], 480)

    def test_停止の分は先に渡す(self) -> None:
        plan = ts.split([Decimal(0), Decimal(30), Decimal(0)], 60)
        self.assertEqual(plan.slots, [(0, 10), (10, 50), (50, 60)])
        for (a, b), stop in zip(plan.slots, [0, 30, 0]):
            self.assertGreaterEqual(b - a, stop, "作業時間をマイナスにしない")

    def test_小数の停止もはみ出さない(self) -> None:
        plan = ts.split([Decimal("12.5")] * 4, 100)
        self.assertTrue(plan.ok)
        for a, b in plan.slots:
            self.assertGreaterEqual(b - a, Decimal("12.5"))

    def test_行が多ければ1分刻み(self) -> None:
        plan = ts.split([Decimal(0)] * 200, 480)
        self.assertTrue(plan.ok)
        self.assertEqual(plan.step, 1)
        self.assertTrue(all(b > a for a, b in plan.slots))

    def test_割り振れないときは理由を返す(self) -> None:
        self.assertIn("停止の合計", ts.split([Decimal(500)], 480).message)
        self.assertIn("1分も渡らない", ts.split([Decimal(0)] * 600, 480).message)
        self.assertIn("割り振る行がありません", ts.split([], 480).message)
        self.assertFalse(ts.split([Decimal(0)], 0).ok)

    def test_日をまたぐ直(self) -> None:
        start = ts.parse_hhmm("22:50")
        plan = ts.split([Decimal(0)] * 10, (ts.parse_hhmm("07:00") - start) % 1440)
        got = times(plan, start)
        self.assertEqual(got[0][0], "22:50")
        self.assertEqual(got[-1][1], "07:00")
        self.assertIn("00:", "".join(a + b for a, b in got))

    def test_打った行とみなすもの(self) -> None:
        self.assertTrue(ts.is_filled({"LOT": "N7131T0"}))
        self.assertTrue(ts.is_filled({"S": "ｲ", "TH": "30"}))
        self.assertFalse(ts.is_filled({"KZ": "07", "KH": "00"}), "時刻だけの行は数えない")
        self.assertFalse(ts.is_filled({}))
        self.assertEqual(ts.row_stop_minutes({"TH": "10", "THS": "2.5", "TH5": "x"}), Decimal("12.5"))


class RestTests(unittest.TestCase):
    """1直 12:00〜13:00 / 2直 18:00〜19:00 / 3直 02:00〜03:00 を飛ばす(v4.31.0)。"""

    def test_直ごとの休憩(self) -> None:
        self.assertEqual(ts.break_window("1直", ts.parse_hhmm("07:00"), 480), (300, 60))
        self.assertEqual(ts.break_window("2直", ts.parse_hhmm("15:00"), 470), (180, 60))
        self.assertEqual(ts.break_window("3直", ts.parse_hhmm("22:50"), 490), (190, 60))
        self.assertEqual(ts.break_window("日勤", ts.parse_hhmm("07:00"), 600), (300, 60))
        self.assertIsNone(ts.break_window("1直", ts.parse_hhmm("13:00"), 120), "直の外の休憩は飛ばさない")

    def test_休憩にかかる行は60分のばす_後ろはずらす(self) -> None:
        """例: 11:40 12:20 となっていた場合は 60分追加として 11:40 13:20 になる"""
        start = ts.parse_hhmm("07:00")
        plan = ts.split([Decimal(0)] * 22, 480, rest=ts.break_window("1直", start, 480))
        got = times(plan, start)
        crossing = got[plan.break_index]
        self.assertLess(crossing[0], "12:00")
        self.assertGreaterEqual(crossing[1], "13:00")
        self.assertEqual(got[-1][1], "15:00")
        for (a, b), (c, _d) in zip(got, got[1:]):
            self.assertEqual(b, c, "行はつながったまま")
        clock = [t for pair in got for t in pair]
        self.assertFalse([t for t in clock if "12:00" < t < "13:00"], "12:00〜13:00 の中に区切りを置かない")
        # 割り振るのは 420 分(480 − 60)
        spans = [b - a for a, b in plan.slots]
        self.assertEqual(sum(spans) - 60, 420)

    def test_例の形(self) -> None:
        """休憩をまたぐ行は 60 分のばす(例 11:40〜12:20 → 11:40〜13:20 と同じ決まり)。

        4行・休憩を除いて 420 分 → 1行 105 分。3行目は 10:30〜12:15 になるところが、
        12:00 をまたぐので 10:30〜13:15。4行目は 13:15〜15:00。
        """
        start = ts.parse_hhmm("07:00")
        plan = ts.split([Decimal(0)] * 4, 480, rest=ts.break_window("1直", start, 480))
        self.assertEqual(times(plan, start), [("07:00", "08:45"), ("08:45", "10:30"),
                                              ("10:30", "13:15"), ("13:15", "15:00")])
        self.assertEqual(plan.break_index, 2)

    def test_3直は2時から3時を飛ばす(self) -> None:
        start = ts.parse_hhmm("22:50")
        plan = ts.split([Decimal(0)] * 10, 490, rest=ts.break_window("3直", start, 490))
        got = times(plan, start)
        a, b = got[plan.break_index]
        self.assertTrue(a < "02:00" and b >= "03:00", (a, b))
        self.assertEqual(got[-1][1], "07:00")


class PackTests(unittest.TestCase):
    """梱包数に合わせて割り振る(v4.34.0)。

        梱包10包み1
        梱包5包み1 であれば梱包10包み1のほうが時間振りが多い
        梱包数の入力がなければ均等でいいです
    """

    def assertWhole(self, plan, total: int) -> None:
        """つながっていて、0 分の行が無く、最後は直の終わりちょうど。"""
        self.assertTrue(plan.ok, plan.message)
        self.assertEqual(plan.slots[0][0], 0)
        self.assertEqual(plan.slots[-1][1], total)
        for (_a, b), (c, _d) in zip(plan.slots, plan.slots[1:]):
            self.assertEqual(b, c, "行はつながったまま")
        self.assertTrue(all(b > a for a, b in plan.slots), plan.slots)

    def test_梱包数は枚数かける包数_包数が空なら1(self) -> None:
        self.assertEqual(ts.row_packs({"MAI": "10", "TUT": "1"}), 10)
        self.assertEqual(ts.row_packs({"MAI": "10", "TUT": "3"}), 30)
        self.assertEqual(ts.row_packs({"MAI": "10", "TUT": ""}), 10, "包数が空なら 1")
        self.assertEqual(ts.row_packs({"MAI": "10"}), 10)
        self.assertEqual(ts.row_packs({"MAI": "x", "TUT": "1"}), 0, "数字でない欄は 0(梱包数の確かめと同じ)")
        self.assertEqual(ts.row_packs({"MAI": "", "TUT": "1"}), 0)
        self.assertEqual(ts.row_packs({"LOT": "A", "S": "ｲ", "TH": "30"}), 0, "停止だけの行")
        self.assertEqual(ts.row_packs({"MAI": "10", "TUT": "0"}), 0)

    def test_梱包10と梱包5は2対1(self) -> None:
        weights, by_packs = ts.pack_weights([ts.row_packs({"MAI": "10", "TUT": "1"}),
                                             ts.row_packs({"MAI": "5", "TUT": "1"})])
        self.assertTrue(by_packs)
        plan = ts.split([Decimal(0)] * 2, 480, weights=weights)
        self.assertWhole(plan, 480)
        self.assertEqual(plan.slots, [(0, 320), (320, 480)])
        self.assertEqual(times(plan), [("07:00", "12:20"), ("12:20", "15:00")])

    def test_梱包数がどの行にも無ければ均等(self) -> None:
        weights, by_packs = ts.pack_weights([Decimal(0)] * 22)
        self.assertFalse(by_packs)
        self.assertEqual(weights, [Decimal(1)] * 22)
        plan = ts.split([Decimal(0)] * 22, 480, weights=weights)
        self.assertEqual(plan.slots, ts.split([Decimal(0)] * 22, 480).slots, "重みが全部同じ = いままでの均等")
        self.assertEqual(times(plan)[:3], [("07:00", "07:20"), ("07:20", "07:45"), ("07:45", "08:05")])
        same = ts.split([Decimal(0)] * 22, 480, weights=[Decimal(7)] * 22)
        self.assertEqual(same.slots, plan.slots, "梱包数が全部同じでも均等")

    def test_一部だけ梱包数が無い行は平均で数える(self) -> None:
        weights, by_packs = ts.pack_weights([Decimal(10), Decimal(0), Decimal(20)])
        self.assertTrue(by_packs)
        self.assertEqual(weights, [Decimal(10), Decimal(15), Decimal(20)])
        plan = ts.split([Decimal(0)] * 3, 450, weights=weights)
        self.assertWhole(plan, 450)
        self.assertEqual([b - a for a, b in plan.slots], [100, 150, 200])

    def test_停止は先に渡して残りを梱包数の比で(self) -> None:
        # 120 分・2行目に停止 30 分 → 残り 90 分を 10:5 で 60 分・30 分
        plan = ts.split([Decimal(0), Decimal(30)], 120, weights=[Decimal(10), Decimal(5)])
        self.assertWhole(plan, 120)
        self.assertEqual(plan.slots, [(0, 60), (60, 120)])
        # 停止の大きい行が梱包数の少ない行でも、作業時間はマイナスにしない
        plan = ts.split([Decimal(0), Decimal(0), Decimal(40)], 120,
                        weights=[Decimal(1000), Decimal(1000), Decimal(1)])
        self.assertWhole(plan, 120)
        a, b = plan.slots[2]
        self.assertGreaterEqual(b - a, 40)

    def test_休憩をまたいでも梱包数の比(self) -> None:
        # 1直: 休憩を除いた 420 分を 10:5 で 280 分・140 分。2行目が 12:00 をまたいで 60 分のびる
        start = ts.parse_hhmm("07:00")
        plan = ts.split([Decimal(0)] * 2, 480, rest=ts.break_window("1直", start, 480),
                        weights=[Decimal(10), Decimal(5)])
        self.assertWhole(plan, 480)
        self.assertEqual(times(plan, start), [("07:00", "11:40"), ("11:40", "15:00")])
        self.assertEqual(plan.break_index, 1)
        spans = [b - a for a, b in plan.slots]
        self.assertEqual((spans[0], spans[1] - 60), (280, 140))

    def test_比が極端でも0分の行は出ない(self) -> None:
        for weights in ([1, 1000], [1000, 1], [1000, 1, 1], [1, 1000, 1], [1, 1, 1000],
                        [1000, 1000, 1, 1000]):
            with self.subTest(weights=weights):
                w = [Decimal(x) for x in weights]
                plan = ts.split([Decimal(0)] * len(w), 480, weights=w)
                self.assertWhole(plan, 480)
                self.assertEqual(plan.step, 5, "比が極端でも 5 分刻みのまま")
                self.assertTrue(all(b - a >= 5 for a, b in plan.slots), plan.slots)
                start = ts.parse_hhmm("07:00")
                plan = ts.split([Decimal(0)] * len(w), 480, weights=w,
                                rest=ts.break_window("1直", start, 480))
                self.assertWhole(plan, 480)

    def test_比が極端でも5分刻みのまま短い行に5分(self) -> None:
        """1 と 1000 でも直の全部を 1 分刻みにはしない。短い行にも 1 刻み(5 分)。"""
        plan = ts.split([Decimal(0)] * 2, 480, weights=[Decimal(1), Decimal(1000)])
        self.assertEqual(plan.step, 5)
        self.assertEqual(plan.slots, [(0, 5), (5, 480)])
        plan = ts.split([Decimal(0)] * 3, 480, weights=[Decimal(1000), Decimal(1), Decimal(1)])
        self.assertEqual(plan.step, 5)
        self.assertEqual(plan.slots, [(0, 470), (470, 475), (475, 480)])
        plan = ts.split([Decimal(0)] * 22, 480, weights=[Decimal(1)] * 21 + [Decimal(1000)])
        self.assertEqual(plan.step, 5)
        self.assertTrue(all(b - a >= 5 and b % 5 == 0 for a, b in plan.slots), plan.slots)

    def test_停止がある行は停止を刻みに切り上げた分が下限(self) -> None:
        # 梱包数がいちばん少ない行に停止 12 分 → 15 分(5 分刻みに切り上げ)が下限
        plan = ts.split([Decimal(0), Decimal(12), Decimal(0)], 480,
                        weights=[Decimal(1000), Decimal(1), Decimal(1000)])
        self.assertWhole(plan, 480)
        self.assertEqual(plan.step, 5)
        a, b = plan.slots[1]
        self.assertEqual(b - a, 15)
        # 最後の行でも同じ(前の行が後ろのぶんを残す)
        plan = ts.split([Decimal(0), Decimal(12)], 480, weights=[Decimal(1000), Decimal(1)])
        self.assertEqual(plan.slots, [(0, 465), (465, 480)])

    def test_1分刻みに落とすのは均等でも5分取れないときだけ(self) -> None:
        # 96 行 × 5 分 = 480 分 → 収まるので、比が極端でも 5 分刻み
        w = [Decimal(1)] * 95 + [Decimal(1000)]
        plan = ts.split([Decimal(0)] * 96, 480, weights=w)
        self.assertEqual(plan.step, 5)
        self.assertWhole(plan, 480)
        self.assertTrue(all(b - a == 5 for a, b in plan.slots), "どの行も 5 分ちょうど")
        # 97 行は収まらない → いままでどおり 1 分刻み(重みがあってもなくても)
        self.assertEqual(ts.split([Decimal(0)] * 97, 480).step, 1)
        plan = ts.split([Decimal(0)] * 97, 480, weights=[Decimal(1)] * 96 + [Decimal(1000)])
        self.assertEqual(plan.step, 1)
        self.assertWhole(plan, 480)
        # 停止で残りが減って 1 行 5 分取れないときも 1 分刻み
        self.assertEqual(ts.split([Decimal(0), Decimal(0), Decimal(475)], 480,
                                  weights=[Decimal(10), Decimal(5), Decimal(1)]).step, 1)

    def test_合計は直の長さちょうど(self) -> None:
        for n, total in ((5, 480), (13, 470), (22, 490), (7, 600)):
            with self.subTest(n=n, total=total):
                w = [Decimal(x) for x in (10, 5, 30, 1, 7, 12, 3, 8, 50, 2, 9, 4, 6, 10, 5,
                                          30, 1, 7, 12, 3, 8, 50)[:n]]
                plan = ts.split([Decimal(0)] * n, total, weights=w)
                self.assertWhole(plan, total)
                self.assertEqual(sum(b - a for a, b in plan.slots), total)


def row(lot: str, **extra) -> dict:
    values = {"LOT": lot, "MAI": "10", "TUT": "1"}
    values.update(extra)
    return values


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class DistributeWebTests(WebTestCase):
    def key(self):
        from nippou import work_context
        from app.routes.entry import build_shift_calculator

        ctx = work_context.get_context()
        return ctx.current_key(build_shift_calculator(self.repo().get_shift_times()))

    def bounds(self, shift):
        from nippou.services import shift_check
        return shift_check.bounds_of(self.repo().get_shift_times(), shift)

    def enable(self) -> None:
        from nippou import user_settings
        from nippou.config import KEY_TIME_SPLIT
        user_settings.save(KEY_TIME_SPLIT, True)

    def save_page(self, page: int, lots: list[str]) -> None:
        from nippou.db.models import DetailRecord as D, HeaderRecord as H
        day, line, shift = self.key()
        at = dict(report_date=day, line=line, shift=shift, page=page)
        self.repo().save(H(**at, worker="山田"),
                         [D(**at, row_no=n, lot=lot, mai="10", tut="1") for n, lot in enumerate(lots, 1)])

    def test_既定はオフでボタンも出ない(self) -> None:
        self.assertNotIn('id="time-split"', self.get("/").get_data(as_text=True))
        res = self.post("/api/entry/distribute-times",
                        {"rows": {"1": row("A")}, "header": {"worker": "山田"}})
        self.assertEqual(res.status_code, 403)

    def test_オンにはパスワードが要る(self) -> None:
        self.assertEqual(self.post("/api/settings/time-split", {"enabled": True}).status_code, 403)
        page = self.get("/settings").get_data(as_text=True)
        self.assertIn('id="time-split-setting"', page)
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        body = self.post("/api/settings/time-split", {"enabled": True}).get_json()
        self.assertTrue(body["enabled"])
        self.assertIn('id="time-split"', self.get("/").get_data(as_text=True))
        body = self.post("/api/settings/time-split", {"enabled": False}).get_json()
        self.assertFalse(body["enabled"])

    def test_全ページの打った行へ直の開始から終了まで割り振る(self) -> None:
        self.enable()
        day, line, shift = self.key()
        start, end = self.bounds(shift)
        self.save_page(1, [f"P1L{n}" for n in range(1, 13)])     # 1ページ 12行
        self.save_page(2, [f"P2L{n}" for n in range(1, 11)])     # 2ページ 10行(いま開いているページ)
        screen = {"rows": {str(n): row(f"P2L{n}") for n in range(1, 11)},
                  "header": {"worker": "山田"}}

        plan = self.post("/api/entry/distribute-times", {**screen, "dry_run": True}).get_json()
        self.assertEqual(plan["count"], 22, plan)
        self.assertEqual(plan["pages"], [1, 2])
        self.assertEqual((plan["first"], plan["last"]), (start, end))
        # 何も書いていない
        self.assertEqual(self.repo().load(day, line, shift, 1)[1][0].kz, "")

        body = self.post("/api/entry/distribute-times", screen).get_json()
        self.assertIn("22行に割り振りました", body["message"])
        slots = body["distributed"]["rows"]
        for a, b in zip(slots, slots[1:]):
            self.assertEqual(a["end"], b["start"], "行の時刻はつながる")
        # ほかのページ(1ページ)は書けている
        details = self.repo().load(day, line, shift, 1)[1]
        self.assertEqual(f"{details[0].kz}:{details[0].kh}", start)
        self.assertEqual((details[11].sz + ":" + details[11].sh), slots[11]["end"])
        self.assertTrue(details[0].tim, "作業時間も計算し直す")
        # いまのページは画面へ返す(最後の行が直の終わり)
        rows = body["rows"]
        self.assertEqual(f'{rows["10"]["SZ"]}:{rows["10"]["SH"]}', end)
        self.assertEqual(f'{rows["1"]["KZ"]}:{rows["1"]["KH"]}', slots[12]["start"])

    def test_打っていない行と時刻だけの行は数えない(self) -> None:
        self.enable()
        screen = {"rows": {"1": row("A"), "2": {"KZ": "08", "KH": "00"}, "4": row("B")},
                  "header": {"worker": "山田"}}
        plan = self.post("/api/entry/distribute-times", {**screen, "dry_run": True}).get_json()
        self.assertEqual([r["row"] for r in plan["rows"]], [1, 4])

    def test_休憩をまたぐ行に休憩0を60分入れる(self) -> None:
        self.enable()
        day, line, shift = self.key()
        if shift not in ts.BREAKS:
            self.skipTest(f"{shift} には休憩の決まりが無い")
        screen = {"rows": {str(n): row(f"L{n}") for n in range(1, 13)}, "header": {"worker": "山田"}}
        plan = self.post("/api/entry/distribute-times", {**screen, "dry_run": True}).get_json()
        rest = plan["rest"]
        self.assertEqual((rest["start"], rest["end"], rest["minutes"]), (*ts.BREAKS[shift], 60))
        self.assertEqual(rest["slot"], ["S", "TH"])
        body = self.post("/api/entry/distribute-times", screen).get_json()
        crossing = body["rows"][str(rest["row"])]
        self.assertEqual((crossing["S"], crossing["TH"]), ("0", "60"))
        self.assertIn("休憩", body["message"])
        # 作業時間は休憩ぶん引かれる(12行の作業時間の合計が 直の長さ − 60)
        start, end = self.bounds(shift)
        total = (ts.parse_hhmm(end) - ts.parse_hhmm(start)) % 1440
        tim = sum(Decimal(body["rows"][str(n)]["TIM"] or 0) for n in range(1, 13))
        self.assertEqual(tim, total - 60)

    def test_入れた休憩には時刻も入れる(self) -> None:
        """v4.38.1: 割り振りが飛ばした時間(12:00〜13:00 など)を、休憩の時刻にも入れる。"""
        self.enable()
        _day, _line, shift = self.key()
        if shift not in ts.BREAKS:
            self.skipTest(f"{shift} には休憩の決まりが無い")
        screen = {"rows": {str(n): row(f"L{n}") for n in range(1, 5)}, "header": {"worker": "山田"}}
        body = self.post("/api/entry/distribute-times", screen).get_json()
        rest = body["distributed"]["rest"]
        from nippou.logic import break_time
        got = body["rows"][str(rest["row"])]
        self.assertEqual(got["break_range"], break_time.normalize("〜".join(ts.BREAKS[shift])))
        entry = next(e for e in body["break_entries"] if e["row"] == rest["row"])
        self.assertEqual(entry["problems"], [], "割り振った時刻と休憩の時刻が合っている")

    def test_ほかの行の休憩0はまたぐ行へ移して二重に数えない(self) -> None:
        """v4.38.1: 打ってある休憩「0」60分と、飛ばす休憩の時間で、休憩を2回数えていた。

            時間割り振り時にすでに停止が入力されていればその時間を加味して割り振りする
            休憩時間は 1直12:00-13:00 ... この時間に割り振る時は自動で0-60が入るようにする
        """
        self.enable()
        _day, _line, shift = self.key()
        if shift not in ts.BREAKS:
            self.skipTest(f"{shift} には休憩の決まりが無い")
        # 1行目は休憩をまたがない(4行なら、またぐのは 2 か 3 行目)
        screen = {"rows": {"1": row("A", S="0", TH="60", break_range="〜".join(ts.BREAKS[shift])), "2": row("B"),
                           "3": row("C"), "4": row("D")},
                  "header": {"worker": "山田"}}
        rest = self.post("/api/entry/distribute-times", {**screen, "dry_run": True}).get_json()["rest"]
        self.assertTrue(rest["slot"], rest)
        self.assertIn("1行目の休憩「0」(60分)", rest["note"])
        self.assertIn("1つにまとめます", rest["note"])
        body = self.post("/api/entry/distribute-times", screen).get_json()
        rows = body["rows"]
        crossing = rows[str(rest["row"])]
        code, minutes = rest["slot"]
        self.assertEqual((crossing[code], crossing[minutes]), ("0", "60"))
        from nippou.logic import break_time
        self.assertEqual(crossing["break_range"], break_time.normalize("〜".join(ts.BREAKS[shift])))
        self.assertNotEqual(rest["row"], 1)
        self.assertEqual((rows["1"]["S"], rows["1"]["TH"], rows["1"]["break_range"]), ("", "", ""),
                         "移した行の休憩は消える")
        # 休憩は1回だけ: 作業時間の合計は 直の長さ − 60、どの行の作業時間も大きく偏らない
        start, end = self.bounds(shift)
        total = (ts.parse_hhmm(end) - ts.parse_hhmm(start)) % 1440
        tims = [Decimal(rows[str(n)]["TIM"] or 0) for n in range(1, 5)]
        self.assertEqual(sum(tims), total - 60, tims)
        self.assertLessEqual(max(tims) - min(tims), 10, f"休憩を2回数えると1行だけ60分のびる: {tims}")
        rest_total = sum(Decimal(rows[str(n)][m] or 0) for n in range(1, 5)
                         for c, m in ts.STOP_SLOTS if rows[str(n)][c] == "0")
        self.assertEqual(rest_total, 60)
        entry = next(e for e in body["break_entries"] if e["row"] == rest["row"])
        self.assertEqual(entry["problems"], [])

    def test_休憩0が1分でも足りなければ60分を入れる(self) -> None:
        """休憩が1分でもはいっていると、60分で打ってくれない(v4.32.0)。v4.38.1 からは、
        その1分もまたぐ行へ移して 60分 1つにまとめる。"""
        self.enable()
        _day, _line, shift = self.key()
        if shift not in ts.BREAKS:
            self.skipTest(f"{shift} には休憩の決まりが無い")
        screen = {"rows": {str(n): row(f"L{n}") for n in range(1, 13)}, "header": {"worker": "山田"}}
        screen["rows"]["1"]["S"] = "0"
        screen["rows"]["1"]["TH"] = "1"
        plan = self.post("/api/entry/distribute-times", {**screen, "dry_run": True}).get_json()
        rest = plan["rest"]
        self.assertIsNotNone(rest["slot"], rest)
        self.assertIn("1行目の休憩「0」(1分)", rest["note"])
        body = self.post("/api/entry/distribute-times", screen).get_json()
        crossing = body["rows"][str(rest["row"])]
        code, minutes = rest["slot"]
        self.assertEqual((crossing[code], crossing[minutes]), ("0", "60"))
        self.assertEqual(body["rows"]["1"]["S"], "")

    def test_またぐ行の停止が埋まっていれば割り振らずに断る(self) -> None:
        """v4.38.2: 停止①〜⑤が埋まっていて休憩を入れられない行がまたぐときは、割り振らない。

        前は入れずに割り振り、その行の作業時間が60分のびていた(休憩を2回数えるのと同じ)。
        """
        self.enable()
        _day, _line, shift = self.key()
        if shift not in ts.BREAKS:
            self.skipTest(f"{shift} には休憩の決まりが無い")
        full = dict(S="イ", TH="1", SS="イ", THS="1", STH="イ", THT="1", S4="イ", TH4="1", S5="イ", TH5="1")
        screen = {"rows": {"1": row("A", **full)}, "header": {"worker": "山田"}}
        res = self.post("/api/entry/distribute-times", {**screen, "dry_run": True})
        self.assertEqual(res.status_code, 422)
        self.assertIn("停止①〜⑤が埋まっていて", res.get_json()["error"]["message"])
        res = self.post("/api/entry/distribute-times", screen)
        self.assertEqual(res.status_code, 422, "書くときも断る")

    def test_決まった休憩と別の時刻の休憩0は移さない(self) -> None:
        """v4.38.2: 時刻が決まった休憩と重ならない「0」(9:00〜9:10 など)は別の休憩としてそのまま。"""
        self.enable()
        _day, _line, shift = self.key()
        if shift not in ts.BREAKS:
            self.skipTest(f"{shift} には休憩の決まりが無い")
        start, end = self.bounds(shift)
        begin = ts.parse_hhmm(start)
        early = "".join(ts.clock(begin, 10)) + "-" + "".join(ts.clock(begin, 20))  # 直の始め +10〜+20分
        screen = {"rows": {"1": row("A", S="0", TH="10", break_range=early), "2": row("B"),
                           "3": row("C"), "4": row("D")},
                  "header": {"worker": "山田"}}
        rest = self.post("/api/entry/distribute-times", {**screen, "dry_run": True}).get_json()["rest"]
        self.assertIn("決まった休憩とは別なのでそのままにします", rest["note"])
        self.assertEqual(rest.get("clear"), [])
        body = self.post("/api/entry/distribute-times", screen).get_json()
        rows = body["rows"]
        self.assertEqual((rows["1"]["S"], rows["1"]["TH"]), ("0", "10"), "別の休憩は残る")
        crossing = rows[str(rest["row"])]
        code, minutes = rest["slot"]
        self.assertEqual((crossing[code], crossing[minutes]), ("0", "60"))
        # 休憩は 10 + 60 分。作業時間の合計は 直の長さ − 70
        total = (ts.parse_hhmm(end) - begin) % 1440
        tim = sum(Decimal(rows[str(n)]["TIM"] or 0) for n in range(1, 5))
        self.assertEqual(tim, total - 70)
        self.assertEqual([e["problems"] for e in body["break_entries"] if e["problems"]], [])

    def test_またぐ行にもう0があればその欄を60分にする(self) -> None:
        self.enable()
        _day, _line, shift = self.key()
        if shift not in ts.BREAKS:
            self.skipTest(f"{shift} には休憩の決まりが無い")
        screen = {"rows": {str(n): row(f"L{n}") for n in range(1, 13)}, "header": {"worker": "山田"}}
        crossing_row = self.post("/api/entry/distribute-times",
                                 {**screen, "dry_run": True}).get_json()["rest"]["row"]
        screen["rows"][str(crossing_row)].update(S="0", TH="5")
        rest = self.post("/api/entry/distribute-times", {**screen, "dry_run": True}).get_json()["rest"]
        if rest["row"] != crossing_row:
            self.skipTest("5分の停止でまたぐ行が動いた")
        self.assertEqual(rest["slot"], ["S", "TH"])
        body = self.post("/api/entry/distribute-times", screen).get_json()
        got = body["rows"][str(crossing_row)]
        self.assertEqual((got["S"], got["TH"], got.get("SS", "")), ("0", "60", ""))

    def test_昼稼働なら休憩を飛ばさない(self) -> None:
        self.enable()
        _day, _line, shift = self.key()
        start, end = self.bounds(shift)
        screen = {"rows": {"1": row("A"), "2": row("B")}, "header": {"worker": "山田"},
                  "day_shift": True}
        plan = self.post("/api/entry/distribute-times", {**screen, "dry_run": True}).get_json()
        total = (ts.parse_hhmm(end) - ts.parse_hhmm(start)) % 1440
        self.assertEqual(plan["rows"][0]["end"], ":".join(ts.clock(ts.parse_hhmm(start), total // 2 // 5 * 5
                                                                 if (total / 2) % 5 < 2.5 else (total // 2 // 5 + 1) * 5)))
        # 書くときも落ちない(前は休憩の時刻が無くて思わぬエラーになっていた)
        body = self.post("/api/entry/distribute-times", screen).get_json()
        self.assertIn("2行に割り振りました", body.get("message", ""), body)
        self.assertNotIn("休憩", body["message"])

    def test_梱包数に合わせて割り振る(self) -> None:
        """梱包10包み1 / 梱包5包み1 であれば梱包10包み1のほうが時間振りが多い"""
        self.enable()
        _day, _line, shift = self.key()
        start, end = self.bounds(shift)
        total = (ts.parse_hhmm(end) - ts.parse_hhmm(start)) % 1440
        screen = {"rows": {"1": row("A", MAI="10", TUT="1"), "2": row("B", MAI="5", TUT="1")},
                  "header": {"worker": "山田"}, "day_shift": True}
        plan = self.post("/api/entry/distribute-times", {**screen, "dry_run": True}).get_json()
        self.assertTrue(plan["by_packs"])
        self.assertEqual(plan["weight_note"], "梱包数に合わせて割り振ります(梱包数が無い行は平均で数えます)")
        want = ts.split([Decimal(0)] * 2, total, weights=[Decimal(10), Decimal(5)])
        self.assertEqual([(r["start"], r["end"]) for r in plan["rows"]], times(want, ts.parse_hhmm(start)))
        first, second = [(ts.parse_hhmm(r["end"]) - ts.parse_hhmm(r["start"])) % 1440 for r in plan["rows"]]
        self.assertGreater(first, second)
        self.assertEqual(first + second, total)
        # 書くときも同じ割り振り
        body = self.post("/api/entry/distribute-times", screen).get_json()
        self.assertEqual(body.get("distributed", {}).get("rows"), plan["rows"], body)

    def test_包数が空なら1と数える(self) -> None:
        self.enable()
        screen = {"rows": {"1": row("A", MAI="10", TUT=""), "2": row("B", MAI="5", TUT="2")},
                  "header": {"worker": "山田"}, "day_shift": True}
        plan = self.post("/api/entry/distribute-times", {**screen, "dry_run": True}).get_json()
        self.assertTrue(plan["by_packs"])
        spans = [(ts.parse_hhmm(r["end"]) - ts.parse_hhmm(r["start"])) % 1440 for r in plan["rows"]]
        self.assertLessEqual(abs(spans[0] - spans[1]), 5, "10×1 と 5×2 は同じ梱包数")

    def test_梱包数が無ければ均等と確かめの窓に出す(self) -> None:
        self.enable()
        screen = {"rows": {"1": {"LOT": "A"}, "2": {"LOT": "B", "S": "ｲ", "TH": "10"}},
                  "header": {"worker": "山田"}, "day_shift": True}
        plan = self.post("/api/entry/distribute-times", {**screen, "dry_run": True}).get_json()
        self.assertFalse(plan["by_packs"])
        self.assertEqual(plan["weight_note"], "梱包数が無いので均等に割り振ります")

    def test_一部だけ梱包数が無い行は平均(self) -> None:
        self.enable()
        _day, _line, shift = self.key()
        start, end = self.bounds(shift)
        total = (ts.parse_hhmm(end) - ts.parse_hhmm(start)) % 1440
        screen = {"rows": {"1": row("A", MAI="10"), "2": {"LOT": "B"}, "3": row("C", MAI="20")},
                  "header": {"worker": "山田"}, "day_shift": True}
        plan = self.post("/api/entry/distribute-times", {**screen, "dry_run": True}).get_json()
        self.assertTrue(plan["by_packs"])
        want = ts.split([Decimal(0)] * 3, total, weights=[Decimal(10), Decimal(15), Decimal(20)])
        self.assertEqual([(r["start"], r["end"]) for r in plan["rows"]], times(want, ts.parse_hhmm(start)))

    def test_割り振れなければ理由を返す(self) -> None:
        self.enable()
        res = self.post("/api/entry/distribute-times", {"rows": {}, "header": {"worker": "山田"}})
        self.assertEqual(res.status_code, 422)
        self.assertIn("割り振る行がありません", res.get_json()["error"]["message"])


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class DistributeBrowserTests(WebTestCase):
    """本物のブラウザで: ボタン → 確かめ(はい)→ 表の時刻が埋まる。"""

    @classmethod
    def setUpClass(cls) -> None:
        from tests._browser import chromium_or_skip
        cls.pw, cls.browser = chromium_or_skip()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.pw.stop()

    def setUp(self) -> None:
        super().setUp()
        from tests.test_seq_entry import BrowserTests
        self.ctx = self.browser.new_context(viewport={"width": 1400, "height": 900})
        self.addCleanup(self.ctx.close)
        self.ctx.route("http://app.test/**", lambda route: BrowserTests._forward(self, route))
        self.errors: list[str] = []
        self.dialogs: list[str] = []

    def test_ボタンで確かめてから埋める(self) -> None:
        from nippou import user_settings
        from nippou.config import KEY_TIME_SPLIT
        from nippou.services import shift_check
        user_settings.save(KEY_TIME_SPLIT, True)

        page = self.ctx.new_page()
        page.on("pageerror", lambda e: self.errors.append(str(e)))

        def answer(dialog):
            self.dialogs.append(dialog.message)
            dialog.accept()
        page.on("dialog", answer)
        page.goto("http://app.test/", wait_until="load")
        page.wait_for_selector("#grid-body")
        page.wait_for_timeout(600)
        if page.evaluate("document.body.classList.contains('needs-worker')"):
            page.fill("#hd-worker", "山田")
            page.dispatch_event("#hd-worker", "change")
            page.wait_for_function("!document.body.classList.contains('needs-worker')", timeout=5000)
        for n in (1, 2, 3):
            page.fill(f"#MAI{n}", "10")
            page.dispatch_event(f"#MAI{n}", "blur")
        page.wait_for_timeout(500)
        page.click("#time-split")
        page.wait_for_function("document.getElementById('KZ1').value !== ''", timeout=5000)
        self.assertTrue(self.dialogs and "3 行に割り振ります" in self.dialogs[0], self.dialogs)
        self.assertIn("梱包数に合わせて割り振ります", self.dialogs[0])
        shift = self.dialogs[0].split(" ")[0]
        start, end = shift_check.bounds_of(self.repo().get_shift_times(), shift)
        self.assertEqual(f'{page.input_value("#KZ1")}:{page.input_value("#KH1")}', start)
        self.assertEqual(f'{page.input_value("#SZ3")}:{page.input_value("#SH3")}', end)
        self.assertEqual(page.input_value("#KZ4"), "", "打っていない行は触らない")
        self.assertEqual(self.errors, [])
