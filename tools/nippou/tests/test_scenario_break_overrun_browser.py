"""画面で: 休憩なしの直を「設備移動のため保存」で通せるか / 休憩の時刻の誤りは通せないか (v4.37.1)

`test_scenario_break_overrun` の画面版です。止めた時計(1直の終わり 14:57)で、本物のブラウザから
「共有へ保存」を押し、断られたら下の欄に「設備移動のため保存」と打って「このまま共有へ保存」を押します。

    休憩なし(大きく超過した行あり)  … 断られる → 一文を打つと通る(休憩の確かめは飛ばせる)
    休憩の時刻が合わない             … 断られる → 欄は打てなくなり、直すところに休憩の時刻が並ぶ
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HAS_FLASK, SKIP_REASON  # noqa: E402
from tests import test_scenario_break_overrun as scenario  # noqa: E402
from tests.test_scenario_shift_flow import SKIP  # noqa: E402


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class SkipPhraseBrowserTests(scenario.BreakOverrunScenarioTests):
    # 親の通しの試験はここでは流さない(親のファイルで流れる)
    test_9直つづけて_すべての停止_大きく超過したりしなかったり = None

    @classmethod
    def setUpClass(cls) -> None:
        from tests._browser import chromium_or_skip
        cls.pw, cls.browser = chromium_or_skip()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.pw.stop()

    def open_page(self):
        from tests.test_seq_entry import BrowserTests
        ctx = self.browser.new_context(viewport={"width": 1400, "height": 900})
        self.addCleanup(ctx.close)
        ctx.route("http://app.test/**", lambda route: BrowserTests._forward(self, route))
        # 直の終わりの確認画面へ連れて行く見張りは止める(この試験は共有へ保存の画面)
        ctx.route("http://app.test/api/graph/review", lambda route: route.fulfill(
            status=200, body='{"due": false}', headers={"content-type": "application/json"}))
        page = ctx.new_page()
        self.errors: list[str] = []
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.on("dialog", lambda d: d.accept())
        page.goto("http://app.test/", wait_until="load")
        page.wait_for_selector("#grid-body")
        page.wait_for_timeout(500)
        return page

    def one_shift(self, *, rest: str) -> dict:
        """10/7 1直を打って保存しておく(画面に出る中身)。休憩は ``rest`` の通り。"""
        p = self.plan("1直")
        p.add(scenario.NORMAL, 1)
        p.add(scenario.OVER, 2, rest=rest)
        p.add(scenario.NORMAL, 1)
        p.fill()
        rows = self.run_shift("2026-10-07", "1直", p, first=True)
        return rows

    def press_push(self, page) -> None:
        page.click("#push-shared")
        page.wait_for_selector("#entry-skip:not([hidden])", timeout=8000)

    def test_休憩なしは一文を打つと画面から通せる(self) -> None:
        rows = self.one_shift(rest="")
        self.assertEqual(self.save(rows, scenario.WORKER).status_code, 200)
        page = self.open_page()
        self.press_push(page)
        self.assertTrue(page.is_hidden("#entry-skip-blocked"), "通せないものは混ざっていない")
        self.assertIn("休憩", page.text_content("#check-panel"))
        self.assertNotIn(("2026年10月7日", "1直", 1), self.shared_keys())
        page.fill("#entry-skip-phrase", SKIP)
        page.click("#entry-skip-push")
        page.wait_for_function("document.getElementById('entry-skip').hidden", timeout=8000)
        self.assertIn(("2026年10月7日", "1直", 1), self.shared_keys())
        self.assertEqual(self.errors, [])

    def test_休憩の時刻の誤りは一文を打てない(self) -> None:
        rows = self.one_shift(rest="wrong")
        self.assertEqual(self.save(rows, scenario.WORKER).status_code, 422, "保存(確定)で断る")
        self.assertEqual(self.autosave(rows, scenario.WORKER).status_code, 200, "打ちかけは手元に残る")
        page = self.open_page()
        self.press_push(page)
        self.assertFalse(page.is_hidden("#entry-skip-blocked"))
        self.assertIn("休憩の時刻", page.text_content("#entry-skip-blockers"))
        self.assertTrue(page.is_disabled("#entry-skip-phrase"))
        self.assertTrue(page.is_disabled("#entry-skip-push"))
        self.assertNotIn(("2026年10月7日", "1直", 1), self.shared_keys())
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
