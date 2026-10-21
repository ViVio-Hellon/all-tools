"""共有へ保存: **押す前に下見を見る・進み具合を出して終われば消す** (本物のブラウザ。段階1)

前は字を探していました(`test_save_progress` の `test_画面は押す前に下見を見る`・
`ScreenWiringTests`)。ここでは共有へ保存のボタンを本当に押し、サーバの答え(下見・
進み具合・送った結果)は試験が決めて、**画面に出たもの**を見ます:

    下見で数が多いと言われたら、押す前に訊く(数・期間を言う)。「いいえ」なら送らない
    送っているあいだは画面の上に進み具合を出す(0.3 秒より早く終わるものには出さない)
    中身はサーバの字のまま。中止を押せば止めてと頼む。終われば必ず消す

押すボタンは3か所(日報入力・設定・直の実績)。どれも同じ道を通ることを確かめます。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._entry_browser import ORIGIN  # noqa: E402
from tests.test_entry_shell_browser import ShellCase  # noqa: E402

MANY = {"confirm_needed": True, "pages": 120, "shifts": 30, "minutes": 6,
        "first_day": "2026年9月1日", "last_day": "2026年9月30日"}
RUNNING = {"running": True, "title": "共有へ保存しています",
           "headline": "[2/5] 共有へ送っています 3/4ページ (75%)",
           "note": "", "done": 3, "total": 4, "percent": 75, "can_stop": True}

#: 押すボタンのある画面(道, 描き終えた印, 共有へ保存のボタン)
SCREENS = (("日報入力", "/", "#grid-body", "#push-shared"),
           ("設定", "/settings", "#push", "#push"),
           ("直の実績", "/graph/review", "#review-act", "#review-act"))


class PushCase(ShellCase):
    def setUp(self) -> None:
        super().setUp()
        # 直の実績の締めくくりを「共有へ保存して終わる」にする(このボタンが共有へ送る)
        from nippou.logic import shift_closing
        patcher = mock.patch.object(shift_closing.Closing, "action",
                                    new=property(lambda self: shift_closing.ACT_PUSH))
        patcher.start()
        self.addCleanup(patcher.stop)

    def open_screen(self, path: str, ready: str):
        if path == "/":
            return self.open_entry()
        page = self.watch(self.ctx.new_page())
        page.goto(f"{ORIGIN}{path}", wait_until="load")
        page.wait_for_selector(ready)
        page.wait_for_timeout(500)
        return page

    @staticmethod
    def press(page, button: str) -> None:
        # 設定の「共有へ保存」は面(タブ)の中にあるので、見えていなくても押す
        page.dispatch_event(button, "click")

    @staticmethod
    def on_top(page) -> bool:
        """進み具合の枠が**いちばん上に見えている**か(真ん中を指したとき、枠の中に当たる)。"""
        return page.evaluate("""() => {
            const b = document.getElementById("job-float");
            const r = b.getBoundingClientRect();
            return b.contains(document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2));
        }""")


class ConfirmPushTests(PushCase):
    def test_数が多ければ押す前に訊き_いいえなら送らない(self) -> None:
        self.answers["/api/settings/push/plan"] = (200, MANY)
        self.answers["/api/settings/push"] = (200, {"message": "共有へ保存しました"})
        for name, path, ready, button in SCREENS:
            with self.subTest(name):
                page = self.open_screen(path, ready)
                # いいえ
                self.confirm_answer = False
                self.dialogs.clear()
                mark = len(self.sent)
                self.press(page, button)
                self.wait_until(page, lambda: bool(self.dialogs), "下見の確かめ")
                page.wait_for_timeout(300)
                plan = [p for p in self.paths(mark) if p.startswith("/api/settings/push/plan")]
                self.assertTrue(plan and "scope=" in plan[0], plan)   # 押したボタンの範囲ぶん
                self.assertIn("120ページ(30直)", self.dialogs[0])
                self.assertIn("2026年9月1日 〜 2026年9月30日", self.dialogs[0])
                self.assertNotIn("/api/settings/push", self.paths(mark), "いいえなのに送りました")
                # はい
                self.confirm_answer = True
                mark = len(self.sent)
                self.press(page, button)
                self.wait_sent(page, mark, "/api/settings/push")
                page.close()

    def test_数が少なければ訊かずに送る(self) -> None:
        self.answers["/api/settings/push/plan"] = (200, {"confirm_needed": False})
        self.answers["/api/settings/push"] = (200, {"message": "共有へ保存しました"})
        page = self.open_entry()
        mark = len(self.sent)
        self.press(page, "#push-shared")
        self.wait_sent(page, mark, "/api/settings/push")
        self.assertEqual(self.dialogs, [])


class ProgressTests(PushCase):
    def test_送っているあいだ進み具合を出し_終われば消す(self) -> None:
        self.answers["/api/settings/push/plan"] = (200, {"confirm_needed": False})
        self.answers["/api/progress/stop"] = (200, {"ok": True})
        for name, path, ready, button in SCREENS:
            with self.subTest(name):
                self._drain()                           # 前の画面で止めたままの要求を片付ける
                page = self.open_screen(path, ready)
                self.answers["/api/progress"] = (200, {"running": False})
                self.hold = ("/api/settings/push",)
                self.press(page, button)
                self.wait_held(page, 1)
                # まだサーバの字が無いうちは、押した画面が言う仕事の名前
                page.wait_for_function(
                    "(() => { const b = document.getElementById('job-float'); return b && !b.hidden; })()",
                    timeout=3000)
                self.assertEqual(page.text_content("#job-float .job__head"), "共有へ保存しています")
                # サーバの進み具合を、そのまま出す
                self.answers["/api/progress"] = (200, RUNNING)
                page.wait_for_function(
                    "document.querySelector('#job-float .job__head').textContent.includes('[2/5]')",
                    timeout=3000)
                self.assertEqual(page.text_content("#job-float .job__head"), RUNNING["headline"])
                self.assertEqual(page.evaluate("document.querySelector('#job-float progress').value"), 75)
                # 中止は「止めて」と頼むだけ(送った束は共有に入る)
                mark = len(self.sent)
                # 直の実績では、前は枠が画面いっぱいの覆い(z-index 200)の下に隠れて押せなかった
                # (段階1で見つけた不具合。v4.40.2 で枠を覆いの上へ)
                self.assertTrue(self.on_top(page), "進み具合の枠がほかの物の下に隠れています")
                page.click("#job-float .job__stop")
                self.wait_sent(page, mark, "/api/progress/stop")
                # 終われば必ず消す
                self.hold = ()
                self.fulfill(page, {"message": "共有へ保存しました"})
                page.wait_for_function(
                    "document.getElementById('job-float').hidden", timeout=3000)
                page.close()

    def test_すぐ終わるものには出さない(self) -> None:
        """0.3 秒より早く終わるものに出すと、ちらつくだけ。"""
        self.answers["/api/settings/push/plan"] = (200, {"confirm_needed": False})
        self.answers["/api/settings/push"] = (200, {"message": "共有へ保存しました"})
        self.answers["/api/progress"] = (200, RUNNING)
        page = self.open_entry()
        page.evaluate("""() => {
            window.__shown = false;
            new MutationObserver(() => {
              const b = document.getElementById("job-float");
              if (b && !b.hidden) window.__shown = true;
            }).observe(document.body, { subtree: true, childList: true,
                                        attributes: true, attributeFilter: ["hidden"] });
        }""")
        mark = len(self.sent)
        self.press(page, "#push-shared")
        self.wait_sent(page, mark, "/api/settings/push")
        page.wait_for_timeout(800)
        self.assertFalse(page.evaluate("window.__shown"), "一瞬で終わったのに進み具合を出しました")


if __name__ == "__main__":
    unittest.main()
