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
        shift = self.dialogs[0].split(" ")[0]
        start, end = shift_check.bounds_of(self.repo().get_shift_times(), shift)
        self.assertEqual(f'{page.input_value("#KZ1")}:{page.input_value("#KH1")}', start)
        self.assertEqual(f'{page.input_value("#SZ3")}:{page.input_value("#SH3")}', end)
        self.assertEqual(page.input_value("#KZ4"), "", "打っていない行は触らない")
        self.assertEqual(self.errors, [])
