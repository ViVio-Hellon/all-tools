"""4直・5直: **「どちらの直ですか」を本物のブラウザで選ぶ** (v4.49.0)

重なる時間に日報入力を開くと、作業者を選ぶ前に直を選ぶボタンが大きく出て、作業者の
カードも表も止まっている。押すとその直の紙が開き、作業者を選べるようになる。
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, time as clock_time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic.shift_rows import ShiftRow  # noqa: E402
from tests._entry_browser import ORIGIN, EntryBrowserCase  # noqa: E402
from tests.test_scenario_shift_flow import SHIFT_TIMES, _Now  # noqa: E402


class ShiftPickTests(EntryBrowserCase):
    def setUp(self) -> None:
        super().setUp()
        _Now.value = datetime.combine(date.today(), clock_time(12, 0))
        self.repo().replace_shift_rows(
            [ShiftRow(k, s, e) for k, (s, e) in SHIFT_TIMES.items()]
            + [ShiftRow("4", "11:00", "19:25", ("L-1",), 120, 120)])

    def test_直を選んでから作業者を選ぶ(self) -> None:
        page = self.watch(self.ctx.new_page())
        page.goto(f"{ORIGIN}/", wait_until="load")
        page.wait_for_selector("#shift-pick:not([hidden])")
        self.assertIn("どちらの直ですか", page.text_content("#shift-pick-why"))
        buttons = page.locator("#shift-pick-choices button")
        self.assertEqual(buttons.all_text_contents(), ["1直", "4直"])
        self.assertTrue(page.is_hidden("#start-pick-staff"))
        self.assertTrue(page.is_disabled("#open-staff"), "直を選ぶ前に作業者を選べてしまう")
        page.click("#shift-pick-choices button:has-text('4直')")
        page.wait_for_function("document.querySelector('#shift-pick')?.hidden === true",
                               timeout=8000)
        page.wait_for_selector("#start-pick-staff:not([hidden])")
        self.assertIn("4直", page.text_content(".start-shift__now"))
        self.assertFalse(page.is_disabled("#open-staff"))
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
