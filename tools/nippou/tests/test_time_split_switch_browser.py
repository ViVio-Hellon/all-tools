"""設定・管理者で「時間を割り振る」「LOTで時間を割り振る」をそれぞれ入り切りする(本物のブラウザ)

    時間を割り振る / LOTで時間を割り振る: 共にパスワードでオンオフ出来るようにし、配布設定できるようにしてください

鍵を開けるまでは押せない。開けて「LOTで時間を割り振る」だけ「使わない」にすると、その欄の表示が変わり、
日報入力には「時間を割り振る」だけが出る。待ち合わせは時間ではなく状態で待つ。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class TimeSplitSwitchBrowserTests(WebTestCase):
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

    def test_鍵を開けてそれぞれ入り切りすると日報入力のボタンが変わる(self) -> None:
        from app import current

        page = self.open("/settings?tab=terminal")
        page.wait_for_selector("#time-split-choices-lot [data-time-split='0']")
        self.assertTrue(page.is_disabled("#time-split-choices-lot [data-time-split='0']"), "鍵を開けずに押せる")
        self.assertEqual(page.get_attribute("#time-split-choices-lot [data-time-split='0']", "title"),
                         "上の鍵を開けると変えられます")

        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        page = self.open("/settings?tab=terminal")
        page.wait_for_selector("#time-split-choices-shift [data-time-split='1']:not([disabled])")
        page.click("#time-split-choices-shift [data-time-split='1']")
        page.wait_for_function(
            "document.querySelector('#time-split-choices-shift .time-split-now').textContent === '使う'")
        page.click("#time-split-choices-lot [data-time-split='0']")
        page.wait_for_selector(".toast:has-text('「LOTで時間を割り振る」を出さないようにしました')")
        self.assertEqual(page.inner_text("#time-split-choices-lot .time-split-now"), "使わない")
        self.assertIn("btn--primary", page.get_attribute("#time-split-choices-lot [data-time-split='0']", "class"))
        self.assertNotIn("btn--primary", page.get_attribute("#time-split-choices-lot [data-time-split='1']", "class"))
        self.assertEqual((current.time_split_button("shift"), current.time_split_button("lot")), (True, False))

        entry = self.open("/")
        entry.wait_for_selector("#grid-body")
        self.assertEqual(entry.locator("#time-split").count(), 1)
        self.assertEqual(entry.locator("#time-split-lot").count(), 0, "使わないボタンは出さない")
        self.assertIn("その時刻の休憩をもう打ってあれば足しません", entry.get_attribute("#time-split", "data-hint"))
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
