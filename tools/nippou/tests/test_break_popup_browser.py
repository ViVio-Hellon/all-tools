"""日報入力: 作業停止で休憩(0)を選ぶと休憩の時刻を聞く (v4.37.0)

「その他」の理由と同じ聞き方 ── 選んだ瞬間に1度だけ窓が出て、入れた時刻は表の下の欄に残り、
保存して読み直しても残る。本物のブラウザで確かめる。無い環境では飛ばします。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HAS_FLASK, SKIP_REASON  # noqa: E402
from tests import test_reason_popup_browser as reason_popup  # noqa: E402


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class BreakPopupBrowserTests(reason_popup.ReasonPopupBrowserTests):
    # 親の試験はここでは流さない(親のファイルで流れる)
    test_やめたあとでもほかの行でその他を選べば聞く = None
    test_同じ行で選び直せばまた聞く = None

    def break_open(self, page) -> bool:
        try:
            page.wait_for_selector("#break-modal:not([hidden])", timeout=4000)
            return True
        except Exception:                                  # noqa: BLE001 - 出なかった
            return False

    def fill_row(self, page, row: int, start: str, end: str) -> None:
        for fam, value in (("KZ", start[:2]), ("KH", start[2:]), ("SZ", end[:2]), ("SH", end[2:])):
            page.fill(f"#{fam}{row}", value)
            page.dispatch_event(f"#{fam}{row}", "change")

    def test_休憩を選ぶと時刻を聞き_入れた時刻が欄に残る(self) -> None:
        page = self.open_entry()
        self.fill_row(page, 1, "1100", "1300")
        page.select_option("#S1", "0")
        page.fill("#TH1", "60")
        page.dispatch_event("#TH1", "change")
        self.assertTrue(self.break_open(page), "休憩を選んだら聞く")
        self.assertIn("1行目", page.text_content("#break-why"))
        page.fill("#break-minutes", "60")
        page.fill("#break-text", "1130-1230")
        page.click("#break-apply")
        page.wait_for_selector("#break-modal", state="hidden")
        page.wait_for_function(
            "document.getElementById('break-1')?.value === '11:30〜12:30'", timeout=5000)
        self.assertEqual(page.locator("#break-rows .break__problem").count(), 0)
        self.assertEqual(self.errors, [])

    def test_合わないときは赤で知らせる(self) -> None:
        page = self.open_entry()
        self.fill_row(page, 2, "1100", "1300")
        page.select_option("#S2", "0")
        page.fill("#TH2", "60")
        page.dispatch_event("#TH2", "change")
        self.assertTrue(self.break_open(page))
        page.fill("#break-minutes", "60")
        page.fill("#break-text", "12:30〜13:00")
        page.click("#break-apply")
        # 合わなければ窓がもう一度出る(v4.38.1)。下の一覧にも赤で残る
        page.wait_for_function(
            "!document.getElementById('break-modal').hidden"
            " && document.getElementById('break-problems').textContent.includes('時刻は 30 分')",
            timeout=5000)
        page.wait_for_selector("#break-rows .break__problem", timeout=5000)
        text = page.text_content("#break-rows")
        self.assertIn("時刻は 30 分、作業停止の休憩は 60 分です", text)
        self.assertEqual(page.locator("#break-rows .break__problem.msg--error").count(), 1)

    def test_ほかの行の休憩と重なれば出る(self) -> None:
        page = self.open_entry()
        for row, (start, end, text) in {1: ("1100", "1300", "11:30〜12:30"),
                                        2: ("1100", "1300", "12:00〜13:00")}.items():
            self.fill_row(page, row, start, end)
            page.select_option(f"#S{row}", "0")
            page.fill(f"#TH{row}", "60")
            page.dispatch_event(f"#TH{row}", "change")
            self.assertTrue(self.break_open(page))
            page.fill("#break-minutes", "60")
            page.fill("#break-text", text)
            page.click("#break-apply")
            if row == 1:
                page.wait_for_selector("#break-modal", state="hidden")
        # 重なった行は窓がもう一度出る(v4.38.1)
        page.wait_for_function(
            "!document.getElementById('break-modal').hidden"
            " && document.getElementById('break-problems').textContent.includes('重なっています')",
            timeout=5000)
        page.wait_for_function(
            "document.querySelector('#break-rows')?.textContent.includes('重なっています')", timeout=5000)
        self.assertIn("1行目の休憩(11:30〜12:30)と重なっています", page.text_content("#break-rows"))
        self.assertEqual(self.errors, [])

    def test_休憩でない停止では聞かない(self) -> None:
        page = self.open_entry()
        page.select_option("#S3", "イ")
        page.fill("#TH3", "10")
        page.dispatch_event("#TH3", "change")
        self.assertFalse(self.break_open(page))
        self.assertEqual(page.locator("#break-rows [data-break-row]").count(), 0)


    # ---- v4.38.1: 先に分、次に時刻。合わなければ窓をもう一度 ----------------------
    def test_分を先に聞く_分が無いと入れない(self) -> None:
        page = self.open_entry()
        self.fill_row(page, 4, "1100", "1300")
        page.select_option("#S4", "0")               # 分はまだ打っていない
        self.assertTrue(self.break_open(page))
        self.assertEqual(page.evaluate("document.activeElement.id"), "break-minutes", "分から聞く")
        self.assertIn("先に①休憩の分", page.text_content("#break-why"))
        page.fill("#break-text", "11:30〜12:30")
        page.click("#break-apply")                    # 分が空のまま
        self.assertTrue(page.is_visible("#break-modal"), "分が空なら閉じない")
        self.assertIn("① 休憩の分", page.text_content("#break-problems"))
        page.fill("#break-minutes", "60")
        page.click("#break-apply")
        page.wait_for_selector("#break-modal", state="hidden")
        page.wait_for_function("document.getElementById('break-4')?.value === '11:30〜12:30'", timeout=5000)
        self.assertEqual(page.input_value("#TH4"), "60", "窓で入れた分が表の欄に入る")
        page.wait_for_timeout(600)
        self.assertFalse(page.is_visible("#break-modal"), "合っていれば窓はもう出ない")
        self.assertEqual(self.errors, [])

    def test_窓で入れた分と時刻が合わなければもう一度聞く(self) -> None:
        page = self.open_entry()
        self.fill_row(page, 5, "1100", "1300")
        page.select_option("#S5", "0")
        self.assertTrue(self.break_open(page))
        page.fill("#break-minutes", "60")
        page.fill("#break-text", "11:30〜12:00")      # 30 分
        page.click("#break-apply")
        page.wait_for_function(
            "!document.getElementById('break-modal').hidden"
            " && document.getElementById('break-problems').textContent.includes('時刻は 30 分')",
            timeout=5000)
        self.assertEqual(page.input_value("#break-minutes"), "60")
        page.fill("#break-text", "11:30〜12:30")
        page.click("#break-apply")
        page.wait_for_selector("#break-modal", state="hidden")
        page.wait_for_timeout(800)
        self.assertFalse(page.is_visible("#break-modal"))

    def test_あとで表の分だけ打ち直して合わなくなっても窓で知らせる(self) -> None:
        page = self.open_entry()
        self.fill_row(page, 6, "1100", "1300")
        page.select_option("#S6", "0")
        self.assertTrue(self.break_open(page))
        page.fill("#break-minutes", "60")
        page.fill("#break-text", "11:30〜12:30")
        page.click("#break-apply")
        page.wait_for_selector("#break-modal", state="hidden")
        page.wait_for_timeout(800)
        page.fill("#TH6", "50")                        # 時刻は 60 分のまま、分だけ 50 に
        page.dispatch_event("#TH6", "change")
        page.locator("#TH6").blur()
        page.wait_for_function(
            "!document.getElementById('break-modal').hidden"
            " && document.getElementById('break-problems').textContent.includes('休憩は 50 分')",
            timeout=5000)
        self.assertEqual(page.input_value("#break-minutes"), "50")


if __name__ == "__main__":
    unittest.main()
