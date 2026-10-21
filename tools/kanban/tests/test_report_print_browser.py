"""**どの帳票(印刷プレビュー)にも「🖨 印刷する」ボタンがあり、押すと印刷ダイアログが出る**(本物のブラウザで。段階1)。

以前は帳票の HTML に ``id="go"``・``window.print()``・印刷用の CSS の字があるかを見ていた
(`test_web_app.PrintButtonTest`)。ここでは出せる帳票を全部開いて**ボタンを押し**、
印刷ダイアログを頼んだか(``window.print`` を差し替えて数える)と、**紙(印刷の見た目)に
ボタンとチェック欄が出ない**ことを見る。帳票を足したときに道具立てを付け忘れないように。
"""
from __future__ import annotations

import unittest

from kanban import config
from kanban.domain import models
from tests._kanban_browser import KanbanBrowserCase

#: 印刷ダイアログは本当には出さない。頼んだ回数だけ数える
PRINT_SPY = "window.__printed = 0; window.print = () => { window.__printed += 1; };"


class _PrintButton:
    """見え方ごとの試験の土台(`KanbanBrowserCase` と組んで使う)。"""

    def test_どの帳票も印刷するを押すと印刷ダイアログを頼み_紙には道具が出ない(self) -> None:
        from app.routes.report import MODE_KINDS

        # 明細のある状態にする(0 件だと「明細はありません」の画面になる)
        self.store.apply_transition("LVC", "6", models.order_button_changes, operation="order")
        if self.MODE == config.MODE_WAREHOUSE:
            self.store.apply_transition("LVC", "2", models.ship_button_changes, operation="ship")
        kinds = MODE_KINDS[self.MODE]
        self.assertTrue(kinds)
        self.ctx.add_init_script(PRINT_SPY)
        page = self.watch(self.ctx.new_page())
        for kind in kinds:
            with self.subTest(kind=kind):
                self.open(f"/report/{kind}?line=LVC", page)
                page.emulate_media(media="screen")
                button = page.get_by_role("button", name="🖨 印刷する")
                self.assertTrue(button.is_visible(), "印刷するボタンが無い")
                self.assertTrue(page.locator("td.pick").first.is_visible(), "試験の前提: 画面ではチェック欄が見える")
                before = page.evaluate("window.__printed")
                button.click()
                page.wait_for_function("(n) => window.__printed > n", arg=before)

                page.emulate_media(media="print")
                for part in (".tools", "th.pick", "td.pick"):
                    self.assertFalse(page.locator(part).first.is_visible(), f"{kind}: {part} が紙に出る")
                self.assertTrue(page.locator("tbody tr").first.is_visible(), "紙に明細が出ない")
        self.assertEqual(self.errors, [])


class SitePrintButtonTest(_PrintButton, KanbanBrowserCase):
    MODE = config.MODE_SITE


class WarehousePrintButtonTest(_PrintButton, KanbanBrowserCase):
    MODE = config.MODE_WAREHOUSE
    LINE = ""


if __name__ == "__main__":
    unittest.main()
