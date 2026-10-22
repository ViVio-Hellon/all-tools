"""GW計算の記録の形式を、設定・管理者で選ぶ(本物のブラウザ。v4.44.1)

    .jsonl で出さないとダメなんですか? 選択制か CSV で出してもらわないと .jsonl は普通は見れないですよね

    - 既定は CSV。管理者モードでないと選べない
    - 押すと「いま」と押したボタンが変わり、次の計算からその形式で残る
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class GwRecordFormatBrowserTests(WebTestCase):
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

    def open(self, path: str):
        page = self.ctx.new_page()
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.goto(f"http://app.test{path}", wait_until="load")
        return page

    def test_既定はCSVで_管理者モードで選ぶ(self) -> None:
        from nippou.services import gw_record

        page = self.open("/settings?tab=output")
        page.wait_for_selector("#gw-record-choices", state="attached")
        self.assertIn("CSV", page.inner_text("#gw-record-now"))
        self.assertTrue(page.is_disabled("[data-gw-record-format='jsonl']"), "管理者モードでないのに押せる")

        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        page = self.open("/settings?tab=output")
        page.wait_for_selector("[data-gw-record-format='both']:not([disabled])")
        page.click("[data-gw-record-format='both']")
        page.wait_for_function("document.getElementById('gw-record-now').textContent.includes('両方')",
                               timeout=5000)
        self.assertIn("btn--primary", page.get_attribute("[data-gw-record-format='both']", "class"))
        self.assertNotIn("btn--primary", page.get_attribute("[data-gw-record-format='csv']", "class") or "")
        self.assertEqual(gw_record.record_format(), "both")
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
