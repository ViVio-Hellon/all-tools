"""倉庫参照 + 現場のライン1つを、本物のブラウザで(看板の画面と設定画面)。

    - 開くと担当ライン(現場)のタブが先頭で選ばれ、サイズ(発注)が押せる・一括確認がある・
      「参照専用」の帯は出ない。押すと送られて赤になる
    - 倉庫参照のライン(LVC)のタブへ移ると、どのボタンも押せず、一括確認は消え、帯が出る
    - 設定 →「この端末」で「現場モードのライン1つも使う」を入れると、担当ラインの欄が出る

無い環境では飛ばします(CI では飛ばしません: ``ALLTOOLS_REQUIRE_BROWSER=1``)。
"""
from __future__ import annotations

from kanban import config
from tests._kanban_browser import KanbanBrowserCase

SIZE_2 = '#board .row[data-no="2"] .cell[data-kind="size"]'


class ViewWithSiteBoardBrowserTest(KanbanBrowserCase):
    MODE = config.MODE_WAREHOUSE_VIEW
    LINE = "LS"

    def setUp(self) -> None:
        super().setUp()
        self.app.config["VIEW_SITE"] = True
        self.store.set_view_site(True)

    def test_担当ラインは現場の操作ができ_ほかのラインは見るだけ(self) -> None:
        page = self.open("/board")
        page.wait_for_selector('#board[data-line="LS"] .row[data-no="2"]')
        tab = page.locator('#line-tabs .tab[data-line="LS"]')
        self.assertEqual(tab.get_attribute("aria-selected"), "true")
        self.assertIn("(現場)", tab.inner_text())
        self.assertTrue(page.locator(SIZE_2).is_enabled(), "担当ラインのサイズ(発注)が押せない")
        self.assertTrue(self.visible(page, "#batch-reset"), "一括確認が無い")
        self.assertFalse(self.visible(page, "#read-only-note"), "担当ラインなのに「参照専用」が出ている")

        # 2 番は発注中。押すと取り消し(確かめの窓は決める)
        page.click(SIZE_2)
        page.wait_for_selector(f'{SIZE_2}[data-state="neutral"]')
        orders = self.posted("/api/board/order")
        self.assertEqual(orders[-1]["line"], "LS", orders)

        # 倉庫参照のラインへ
        page.click('#line-tabs .tab[data-line="LVC"]')
        page.wait_for_selector('#board[data-line="LVC"]:not([aria-busy]) .row[data-no="2"]')
        page.wait_for_selector("#batch-reset", state="hidden")
        self.assertTrue(self.visible(page, "#read-only-note"), "倉庫参照のラインで「参照専用」が出ない")
        enabled = page.evaluate(
            "() => [...document.querySelectorAll('#board .cell[data-kind]')]"
            ".filter((b) => ['size', 'ship', 'hold'].includes(b.dataset.kind) && !b.disabled).length")
        self.assertEqual(enabled, 0, "倉庫参照のラインで押せるボタンがある")
        self.assertEqual(self.errors, [])


class ViewSiteSettingsBrowserTest(KanbanBrowserCase):
    MODE = config.MODE_WAREHOUSE_VIEW
    LINE = "LS"

    def test_この端末で併用を入れると担当ラインの欄が出る(self) -> None:
        page = self.open_settings("device")
        self.assertFalse(page.locator("#view-site").is_checked())
        self.assertEqual(page.locator("#line").count(), 0, "併用していないのに担当ラインの欄がある")
        page.check("#view-site")
        self.wait_until(page, lambda: bool(self.posted("/api/view-site")), "併用を送った")
        self.assertEqual(self.posted("/api/view-site")[-1], {"on": True})
        page.wait_for_selector("#line", timeout=15000)          # 読み直すと出る
        self.assertTrue(page.locator("#view-site").is_checked())
        self.assertTrue(self.store.get_view_site())
        self.assertEqual(self.errors, [])
