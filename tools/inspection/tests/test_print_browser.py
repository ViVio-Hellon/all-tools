"""印刷の進み具合を**本物のブラウザ**で ── Word の点検表なら「Word の…」と言う (段階1)

前は ``printing.js`` の字(``${job.app || "Excel"} の空き待ち...``・``item.notice``)を探していた。
ここでは見本の Word の点検表を選んで「印刷」を押し、

- Excel・Word の手が空くのを待っているあいだは「Word の空き待ち...」と出す(Excel と言わない)
- 刷れた点検表にも、サーバの知らせ(ほかの人が開いたまま など)を出す
"""
from __future__ import annotations

import json
import unittest

from tests._page_browser import PageBrowserCase

NOTICE = "山田さんが「作業手順の確認」を開いたままです(印刷はできます)"


class PrintBrowserTests(PageBrowserCase):
    def test_Wordの点検表はWordの空き待ちと言い_刷れたものにも知らせを出す(self) -> None:
        word = self.item("作業手順の確認")
        backend = self.biz.excel.backend
        original = backend.print_files

        def with_notice(files, copies, on_event, should_continue):
            outcomes = original(files, copies, on_event, should_continue)
            for outcome, path in zip(outcomes, files):
                if path.endswith(".docx"):
                    outcome.notice = NOTICE
            return outcomes

        backend.print_files = with_notice
        ctx = self.new_context()
        ctx.add_init_script(f"sessionStorage.setItem('isp.selection', {json.dumps(json.dumps([word['id']]))});")
        page = self.open("/", ctx)
        page.wait_for_selector("#sel-list li")

        # Excel・Word の手を塞いでおく(プレビューを作っている途中と同じ)
        busy = self.biz.excel.acquire("試験(プレビューの途中)", timeout=5)
        busy.__enter__()
        try:
            page.click("#btn-print")
            page.wait_for_selector("#confirm-dialog[open]")
            page.click("#confirm-yes")
            page.wait_for_selector("#print-dialog[open]")
            page.wait_for_function("document.getElementById('print-title').textContent === 'Word の空き待ち...'")
            self.assertEqual(page.inner_text("#print-current"), "プレビューの作成が終わるのを待っています")
        finally:
            busy.__exit__(None, None, None)

        page.wait_for_function("document.getElementById('print-title').textContent === '印刷完了'",
                               timeout=15000)
        self.assertIn(NOTICE, page.inner_text("#print-items"))
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
