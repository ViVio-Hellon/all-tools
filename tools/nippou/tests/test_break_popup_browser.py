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
        page.fill("#break-text", "1130-1230")
        page.click("#break-apply")
        page.wait_for_selector("#break-modal", state="hidden")
        page.wait_for_function(
            "document.getElementById('break-1')?.value === '11:30〜12:30'", timeout=5000)
        self.assertEqual(page.locator("#break-rows .break__problem").count(), 0)
        self.assertEqual(self.errors, [])

    def test_合わないときは知らせるだけ(self) -> None:
        page = self.open_entry()
        self.fill_row(page, 2, "1100", "1300")
        page.select_option("#S2", "0")
        page.fill("#TH2", "60")
        page.dispatch_event("#TH2", "change")
        self.assertTrue(self.break_open(page))
        page.fill("#break-text", "12:30〜13:00")
        page.click("#break-apply")
        page.wait_for_selector("#break-modal", state="hidden")
        page.wait_for_selector("#break-rows .break__problem", timeout=5000)
        text = page.text_content("#break-rows")
        self.assertIn("時刻は 30 分、作業停止の休憩は 60 分です", text)

    def test_休憩でない停止では聞かない(self) -> None:
        page = self.open_entry()
        page.select_option("#S3", "イ")
        page.fill("#TH3", "10")
        page.dispatch_event("#TH3", "change")
        self.assertFalse(self.break_open(page))
        self.assertEqual(page.locator("#break-rows [data-break-row]").count(), 0)


if __name__ == "__main__":
    unittest.main()
