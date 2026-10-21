"""日報入力: 行を空にする(✕)の確かめは**押したボタンのすぐ下に出す** (本物のブラウザ。段階1)

前は字を探していました(`test_row_clear` の一部)。ここでは本当に押して確かめます:

    - 画面の上に出る確かめの窓(confirm)は使わない
    - ✕ のすぐ下に出て、「空にする」に焦点がある。どの行かを番号とロットで言う
    - 空にするを押すと行が空になる。やめる・Esc・外を押す・巻き戻す(スクロール)で閉じる
    - 空の行の ✕ では何も出さない(答えが1つしか無い確かめは出さない)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._entry_browser import EntryBrowserCase  # noqa: E402


class RowClearTests(EntryBrowserCase):
    def filled(self):
        page = self.open_entry()
        self.type_in(page, "LOT1", "N7131T0")
        self.type_in(page, "HIT1", "3")
        page.locator("#HIT1").press("Tab")
        page.wait_for_timeout(600)
        return page

    @staticmethod
    def press_clear(page, row: int) -> None:
        """✕ を押す。**先に見える所まで巻き戻して待つ**(人と同じ)。

        Tab で次の欄へ移ると表が横に流れ、✕ が見えなくなる。Playwright は押す前に巻き戻すが、
        その合図(scroll)が押したあとに届くと、巻き戻しで閉じる仕掛けが確かめを閉じてしまう。
        """
        button = page.locator(f'[data-clear-row="{row}"]')
        button.scroll_into_view_if_needed()
        page.wait_for_timeout(300)
        button.click()

    def ask(self, page, row: int = 1) -> None:
        self.press_clear(page, row)
        page.wait_for_selector("#row-ask:not([hidden])", timeout=3000)

    def test_ボタンのすぐ下に出て_空にするに焦点がある(self) -> None:
        page = self.filled()
        self.ask(page)
        button = page.locator('[data-clear-row="1"]').bounding_box()
        box = page.locator("#row-ask").bounding_box()
        self.assertAlmostEqual(box["y"], button["y"] + button["height"] + 4, delta=3)
        self.assertEqual(page.evaluate("document.activeElement.id"), "row-ask-yes")
        self.assertEqual(page.text_content("#row-ask-text").strip(), "1行目(N7131T0)を空にしますか?")
        self.assertEqual(self.dialogs, [], "画面の上に出る確かめの窓を使っています")

    def test_空にするを押すと行が空になる(self) -> None:
        page = self.filled()
        self.ask(page)
        page.click("#row-ask-yes")
        page.wait_for_selector("#row-ask", state="hidden", timeout=3000)
        page.wait_for_timeout(600)
        self.assertEqual(self.value(page, "LOT1"), "")
        self.assertEqual(self.value(page, "HIT1"), "")
        self.assertEqual(self.dialogs, [])
        self.assertEqual(self.errors, [])

    def test_やめる_Esc_外を押す_巻き戻すで閉じて行は残る(self) -> None:
        page = self.filled()
        for how in ("やめる", "Esc", "外", "スクロール"):
            with self.subTest(how=how):
                self.ask(page)
                if how == "やめる":
                    page.click("#row-ask-no")
                elif how == "Esc":
                    page.keyboard.press("Escape")
                elif how == "外":
                    page.mouse.click(5, 5)
                else:
                    page.evaluate("document.dispatchEvent(new Event('scroll'))")
                page.wait_for_selector("#row-ask", state="hidden", timeout=3000)
                self.assertEqual(self.value(page, "LOT1"), "N7131T0")

    def test_空の行では何も出さない(self) -> None:
        page = self.open_entry()
        self.press_clear(page, 2)
        page.wait_for_timeout(500)
        self.assertTrue(page.locator("#row-ask").is_hidden())
        self.assertEqual(self.dialogs, [])
