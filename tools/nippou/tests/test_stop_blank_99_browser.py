"""名前の無い 99(分類1)を**本物のブラウザ**で: 日報入力の選択欄・全停入力の窓・設定の一覧 (VER4.44.12 から)

`test_stop_blank_99` の画面の側。名前が空のまま出すと、選択欄は「99 」、全停入力の窓は字の無い
丸だけ(押しても「停止理由を選んでください」)、設定の一覧は記号だけになっていました。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._other_screen_browser import ScreenBrowserCase  # noqa: E402
from tests.test_stop_blank_99 import CSV_TEXT, USER_LINE, make_transmission  # noqa: E402


class BlankNinetyNineBrowserTests(ScreenBrowserCase):
    def setUp(self) -> None:
        super().setUp()
        make_transmission(self.tmp / "ref" / "伝送用ファイル.sqlite3")
        self.csv = self.tmp / "ref" / "停止内訳.csv"
        self.csv.write_text(CSV_TEXT.format(line99=USER_LINE), encoding="utf-8-sig")
        self.settle_review()

    def test_日報入力の選択欄で99を選べる(self) -> None:
        page = self.open_entry()
        text = page.eval_on_selector(
            '#S1 optgroup[label="設備停止"] option[value="99"]', "el => el.textContent.trim()")
        self.assertEqual(text, "99(名前なし)")
        page.select_option("#S1", "99")
        page.dispatch_event("#S1", "change")
        page.wait_for_function("document.getElementById('S1').value === '99'", timeout=8000)
        self.assertEqual(page.eval_on_selector_all(
            "#S1 option", "els => els.filter(e => e.textContent.includes('マスタに無い')).length"), 0)
        self.assertEqual(self.errors, [])

    def test_全停入力の窓で99を選んで入れられる(self) -> None:
        page = self.open_entry()
        page.click("#open-formstop")
        radio = 'input[name="formstop-reason"][value="99"]'
        page.wait_for_selector(f"#formstop-modal:not([hidden]) {radio}")
        label = page.eval_on_selector(radio, "el => el.closest('label').textContent.trim()")
        self.assertEqual(label, "99(名前なし)")
        page.check(radio)
        page.wait_for_function(
            "document.getElementById('formstop-selected').textContent.includes('99(名前なし)')",
            timeout=8000)
        page.click("#formstop-run")
        self.wait_until(page, lambda: bool(self.posts("/api/formstop/execute")), "全停入力の要求")
        self.assertEqual(self.posts("/api/formstop/execute")[-1]["code"], "99")
        self.wait_until(page, lambda: bool(self.repo().list_keys()), "全停のページの保存")
        key = self.repo().list_keys()[0]
        self.assertEqual(self.repo().load(*key)[1][0].s, "99")
        self.assertTrue(any("99(名前なし)" in d for d in self.dialogs), self.dialogs)
        self.assertEqual(self.errors, [])

    def test_設定の一覧に名前なしで出て_読み直しても同じ(self) -> None:
        page = self.open_screen("/settings?tab=master", "#tab-master-stops")
        page.click("#tab-master-stops")
        page.wait_for_selector("#panel-master-stops:not([hidden])")

        def row99() -> str:
            return page.eval_on_selector_all(
                "#stop-reason-body div",
                "els => els.map(e => e.textContent.trim()).filter(t => t.startsWith('99')).join('|')")

        self.assertEqual(row99(), "99 (名前なし)")
        self.assertTrue(page.eval_on_selector("#stop-reason-problems", "el => el.hidden"))
        before = len(self.posts("/api/settings/stop-reasons/reload"))
        page.click("#stop-reason-reload")                 # JS が描き直すほう
        self.wait_until(page, lambda: len(self.posts("/api/settings/stop-reasons/reload")) > before,
                        "読み直しの要求")
        page.wait_for_function(
            "[...document.querySelectorAll('#stop-reason-body div')]"
            ".some(e => e.textContent.trim() === '99 (名前なし)' && e.querySelector('span.lead'))",
            timeout=8000)
        self.assertTrue(page.eval_on_selector("#stop-reason-problems", "el => el.hidden"))
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    import unittest
    unittest.main()
