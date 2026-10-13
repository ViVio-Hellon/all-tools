"""日報入力: 「その他」の理由を、やめたあとでも聞き直す (v4.32.0)

    停止理由でその他を選ぶとテキスト入力できるようになるが、1回でもやめるとその後はその他を選んでも
    テキスト入力が出てこなくなる(上に入力欄はあるがその場での聞きがなくなる)

前は「空のいちばん上の行」1つだけを覚えていたので、やめた行が空のまま居座り、ほかの行で「その他」を
選んでも聞かなかった。行ごとに覚え、「その他」でなくなった行は忘れる。本物のブラウザで確かめる。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class ReasonPopupBrowserTests(WebTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from tests._browser import chromium_or_skip
        cls.pw, cls.browser = chromium_or_skip()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.pw.stop()

    def open_entry(self):
        from tests.test_seq_entry import BrowserTests
        from tests.test_web_entry_sheet import ReasonPopupTests
        ReasonPopupTests._make_master(self)
        ctx = self.browser.new_context(viewport={"width": 1400, "height": 900})
        self.addCleanup(ctx.close)
        ctx.route("http://app.test/**", lambda route: BrowserTests._forward(self, route))
        page = ctx.new_page()
        self.errors: list[str] = []
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.on("dialog", lambda d: d.accept())
        page.goto("http://app.test/", wait_until="load")
        page.wait_for_selector("#grid-body")
        page.wait_for_timeout(600)
        if page.evaluate("document.body.classList.contains('needs-worker')"):
            page.fill("#hd-worker", "山田")
            page.dispatch_event("#hd-worker", "change")
            page.wait_for_function("!document.body.classList.contains('needs-worker')", timeout=5000)
        return page

    def modal_open(self, page) -> bool:
        try:
            page.wait_for_selector("#reason-modal:not([hidden])", timeout=4000)
            return True
        except Exception:                                  # noqa: BLE001 - 出なかった
            return False

    def close_modal(self, page) -> None:
        page.click('#reason-modal .modal__foot [data-close="reason-modal"], '
                   '#reason-modal [data-close="reason-modal"] >> nth=-1')
        page.wait_for_selector("#reason-modal", state="hidden")

    def test_やめたあとでもほかの行でその他を選べば聞く(self) -> None:
        page = self.open_entry()
        page.select_option("#S1", "ヨ")
        self.assertTrue(self.modal_open(page), "1行目で聞く")
        self.close_modal(page)                       # やめる(1行目の理由は空のまま)
        page.select_option("#S3", "ヨ")
        self.assertTrue(self.modal_open(page), "やめたあとでも、3行目で選べば聞く")
        self.assertIn("3行目", page.text_content("#reason-why"))
        self.close_modal(page)
        self.assertEqual(self.errors, [])

    def test_同じ行で選び直せばまた聞く(self) -> None:
        page = self.open_entry()
        page.select_option("#S2", "ヨ")
        self.assertTrue(self.modal_open(page))
        self.close_modal(page)
        page.select_option("#S2", "")                 # その他をやめた
        page.wait_for_timeout(800)
        page.select_option("#S2", "ヨ")
        self.assertTrue(self.modal_open(page), "選び直せばまた聞く")
        self.close_modal(page)


if __name__ == "__main__":
    unittest.main()
