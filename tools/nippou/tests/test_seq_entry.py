"""日報入力: 行番号のダブルクリックで「順に打つ」(v4.29.0)

    日報を入力:VBAに合った機能なのだが作業時間の開始から順にmsgboxに入力していく
    モードがありました(マウスによる操作を減らす)同じ機能である必要はないが、
    作業開始 0815 enter 作業終了 0911 enter 作業人数 3 enter ~梱包数まで
    入力とエンターだけでも いけるモードはつくれないか 行の数字ダブルクリックで発火させる

【約束】
    ・行番号をダブルクリックすると、その行のすぐ下に聞く窓が出る(✕ は「空にする」のまま)
    ・作業開始 → 作業終了 → 作業人数 → 合紙 → 個装単位 枚数 → 梱包単位 包数 の順
    ・Enter で表の欄へ入れて次へ。空のまま Enter は今の値のまま
    ・形が違えば止まって理由を出す(時刻は 4 桁の時分)
    ・Shift+Enter で前へ。Esc でやめる(入れたぶんは残る)
    ・打ち終えたら次の行のロット№へ

本物のブラウザ(Playwright)で通します。無い環境では飛ばします。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._browser import chromium_or_skip  # noqa: E402
from tests._web import HAS_FLASK, HEADERS, SKIP_REASON, WebTestCase  # noqa: E402


class MarkupTests(WebTestCase):
    """画面に窓と近道の説明があるか(ブラウザが無くても見られるところ)。"""

    def test_窓と説明がある(self) -> None:
        if not HAS_FLASK:
            self.skipTest(SKIP_REASON)
        page = self.get("/").get_data(as_text=True)
        self.assertIn('id="seq-entry"', page)
        self.assertIn('id="seq-entry-input"', page)
        self.assertIn("行の番号をダブルクリック", page)

    # 聞く順(作業開始 → 作業終了 → 作業人数 → 合紙 → 枚数 → 包数)は、下の BrowserTests の
    # 「入力とEnterだけで1行打てる」で**聞いた見出しの順に**確かめます(並べ替えると落ちる)


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class BrowserTests(WebTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.pw, cls.browser = chromium_or_skip()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.pw.stop()

    def setUp(self) -> None:
        super().setUp()
        self.ctx = self.browser.new_context(viewport={"width": 1400, "height": 900})
        self.addCleanup(self.ctx.close)
        self.ctx.route("http://app.test/**", self._forward)
        self.errors: list[str] = []

    def _forward(self, route) -> None:
        req = route.request
        path = req.url.split("http://app.test", 1)[1]
        headers = dict(HEADERS)
        if req.headers.get("x-tab"):
            headers["X-Tab"] = req.headers["x-tab"]
        res = self.client.open(path, method=req.method, headers=headers,
                               data=req.post_data_buffer,
                               content_type=req.headers.get("content-type"))
        try:
            route.fulfill(status=res.status_code, body=res.get_data(),
                          headers={"content-type": res.headers.get("Content-Type", "text/plain")})
        finally:
            res.close()

    def open_entry(self):
        page = self.ctx.new_page()
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.on("dialog", lambda d: d.accept())
        page.goto("http://app.test/", wait_until="load")
        page.wait_for_selector("#grid-body")
        page.wait_for_timeout(600)
        # 作業者が決まっていないと表は伏せてある。名前を入れて開ける
        if page.evaluate("document.body.classList.contains('needs-worker')"):
            page.fill("#hd-worker", "山田")
            page.dispatch_event("#hd-worker", "change")
            page.wait_for_function("!document.body.classList.contains('needs-worker')",
                                   timeout=5000)
        return page

    def open_seq(self, page, row: int) -> None:
        page.dblclick(f'#grid-body tr[data-row="{row}"] .rowno__n')
        page.wait_for_selector("#seq-entry:not([hidden])")

    def answer(self, page, text: str, *, back: bool = False) -> None:
        box = page.locator("#seq-entry-input")
        box.fill(text)
        box.press("Shift+Enter" if back else "Enter")

    def label(self, page) -> str:
        return page.text_content("#seq-entry-label") or ""

    def test_入力とEnterだけで1行打てる(self) -> None:
        page = self.open_entry()
        self.open_seq(page, 2)
        self.assertEqual(self.label(page), "作業開始")
        self.assertIn("2行目", page.text_content("#seq-entry-title"))
        # 聞く欄に焦点がある(マウスを使わずに打ち始められる)
        self.assertEqual(page.evaluate("document.activeElement.id"), "seq-entry-input")
        self.answer(page, "0815")
        self.assertEqual(self.label(page), "作業終了")
        self.answer(page, "0911")
        self.assertEqual(self.label(page), "作業人数")
        self.answer(page, "3")
        self.assertEqual(self.label(page), "合紙")
        self.answer(page, "1")                       # 1 = 有
        self.assertEqual(self.label(page), "個装単位 枚数")
        self.answer(page, "50")
        self.assertIn("梱包単位 包数", self.label(page))
        self.answer(page, "2")
        page.wait_for_selector("#seq-entry", state="hidden")
        got = {f: page.input_value(f"#{f}2") for f in ("KZ", "KH", "SZ", "SH", "HIT", "MAI", "TUT")}
        self.assertEqual(got, {"KZ": "08", "KH": "15", "SZ": "09", "SH": "11",
                               "HIT": "3", "MAI": "50", "TUT": "2"})
        self.assertEqual(page.eval_on_selector("#AI2", "el => el.value"), "有")
        # 打ち終えたら次の行のロット№へ
        self.assertEqual(page.evaluate("document.activeElement.id"), "LOT3")
        # 欄を離れたときと同じくサーバへ送り、作業時間(分)が出る
        page.wait_for_function("document.getElementById('TIM2')?.value === '56'", timeout=5000)
        self.assertEqual(self.errors, [])

    def test_形が違えば止まって理由を出す_空のままは今の値(self) -> None:
        page = self.open_entry()
        page.fill("#HIT1", "4")
        self.open_seq(page, 1)
        self.answer(page, "8:5")                     # 3 桁にならない
        self.assertEqual(self.label(page), "作業開始")
        self.assertFalse(page.is_hidden("#seq-entry-error"))
        self.answer(page, "0875")                    # 分が 75
        self.assertIn("分", page.text_content("#seq-entry-error"))
        self.answer(page, "815")                     # 3 桁は 08:15
        self.answer(page, "")                        # 作業終了は空のまま
        self.assertEqual(self.label(page), "作業人数")
        # 今の値(4)が入っていて、そのまま Enter で通る
        self.assertEqual(page.input_value("#seq-entry-input"), "4")
        page.locator("#seq-entry-input").press("Enter")
        self.assertEqual(self.label(page), "合紙")
        self.assertEqual((page.input_value("#KZ1"), page.input_value("#KH1")), ("08", "15"))
        self.assertEqual(page.input_value("#SZ1"), "")
        self.assertEqual(page.input_value("#HIT1"), "4")

    def test_前へ戻れて_Escでやめても入れたぶんは残る(self) -> None:
        page = self.open_entry()
        self.open_seq(page, 3)
        self.answer(page, "1300")
        self.answer(page, "1345")
        self.assertEqual(self.label(page), "作業人数")
        page.locator("#seq-entry-input").press("Shift+Enter")
        self.assertEqual(self.label(page), "作業終了")
        self.assertEqual(page.input_value("#seq-entry-input"), "1345")
        self.answer(page, "1350")
        page.locator("#seq-entry-input").press("Escape")
        page.wait_for_selector("#seq-entry", state="hidden")
        self.assertEqual((page.input_value("#KZ3"), page.input_value("#KH3"),
                          page.input_value("#SZ3"), page.input_value("#SH3")),
                         ("13", "00", "13", "50"))

    def test_空にするのバツでは開かない(self) -> None:
        page = self.open_entry()
        page.hover('#grid-body tr[data-row="4"] td.rowno')
        page.dblclick('#grid-body tr[data-row="4"] [data-clear-row]')
        page.wait_for_timeout(200)
        self.assertTrue(page.is_hidden("#seq-entry"))


if __name__ == "__main__":
    unittest.main()
