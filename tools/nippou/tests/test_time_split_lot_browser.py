"""時間を割り振る: EX・段取り・VC交換を本物のブラウザで (v4.42.0)

    - 確かめの窓に「EX のロット…」「ロットの変わり目…1梱包目…」が出て、はいで EX の行が長く埋まる
    - 設定・管理者で倍率を変えて保存できる(管理者モードでないと押せない)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import time_split as ts  # noqa: E402
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class TimeSplitLotBrowserTests(WebTestCase):
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

    def open(self, path: str):
        page = self.ctx.new_page()
        page.on("pageerror", lambda e: self.errors.append(str(e)))

        def answer(dialog):
            self.dialogs.append(dialog.message)
            dialog.accept()
        page.on("dialog", answer)
        page.goto(f"http://app.test{path}", wait_until="load")
        return page

    def span(self, page, n: int) -> int:
        """その行の時間から停止の分を引いたもの(休憩をまたぐ行には休憩「0」60分が入る)。"""
        a = ts.parse_hhmm(f'{page.input_value(f"#KZ{n}")}:{page.input_value(f"#KH{n}")}')
        b = ts.parse_hhmm(f'{page.input_value(f"#SZ{n}")}:{page.input_value(f"#SH{n}")}')
        stops = sum(int(page.input_value(f"#{f}{n}") or 0) for f in ("TH", "THS", "THT", "TH4", "TH5"))
        return (b - a) % 1440 - stops

    def test_確かめの窓に出してEXの行を長く埋める(self) -> None:
        from nippou import user_settings
        from nippou.config import KEY_TIME_SPLIT
        user_settings.save(KEY_TIME_SPLIT, True)

        page = self.open("/")
        page.wait_for_selector("#grid-body")
        page.wait_for_timeout(600)
        if page.evaluate("document.body.classList.contains('needs-worker')"):
            page.fill("#hd-worker", "山田")
            page.dispatch_event("#hd-worker", "change")
            page.wait_for_function("!document.body.classList.contains('needs-worker')", timeout=5000)
        # 1〜2行目: EX、3〜4行目: EX でない。どれも 5枚×1包(ロット№は打たない ── 引きに行かせない)
        for n in (1, 2, 3, 4):
            page.fill(f"#MAI{n}", "5")
            page.dispatch_event(f"#MAI{n}", "blur")
        for n in (1, 2):
            page.fill(f"#ET{n}", "EX")
            page.dispatch_event(f"#ET{n}", "change")
        page.wait_for_timeout(500)
        page.click("#time-split")
        page.wait_for_function("document.getElementById('KZ1').value !== ''", timeout=5000)
        self.assertTrue(self.dialogs, "確かめの窓が出ない")
        self.assertIn("・EX のロットの 2 行は 1.2倍で数えます", self.dialogs[0])
        self.assertGreater(self.span(page, 1) + self.span(page, 2),
                           self.span(page, 3) + self.span(page, 4))
        self.assertEqual(self.errors, [])

    def test_設定で倍率を変える(self) -> None:
        page = self.open("/settings?tab=terminal")
        page.wait_for_selector("#factor-save")
        self.assertTrue(page.is_disabled("#factor-save"), "管理者モードでないのに押せる")

        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        page = self.open("/settings?tab=terminal")
        page.wait_for_selector("#factor-save:not([disabled])")
        page.fill("#factor-ex", "1.4")
        page.fill("#factor-vc", "２")
        page.click("#factor-save")
        page.wait_for_selector(".toast:has-text('割り振りの倍率を変えました')")
        self.assertEqual(page.input_value("#factor-vc"), "2")
        got = ts.Factors.read(self._saved())
        self.assertEqual((float(got.ex), float(got.setup), float(got.vc)), (1.4, 1.5, 2.0))

        # 読めない値は断り、理由を出す(黙って既定に戻さない)
        page.fill("#factor-setup", "9")
        page.click("#factor-save")
        page.wait_for_selector("#time-split-note:not([hidden])")
        self.assertIn("段取り(ロットの変わり目)の倍率は", page.inner_text("#time-split-note"))
        self.assertEqual(float(ts.Factors.read(self._saved()).setup), 1.5)
        self.assertEqual(self.errors, [])

    @staticmethod
    def _saved():
        from nippou import user_settings
        from nippou.config import KEY_TIME_SPLIT_FACTORS
        return user_settings.get(KEY_TIME_SPLIT_FACTORS, None)


if __name__ == "__main__":
    unittest.main()
