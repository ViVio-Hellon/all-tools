"""**どの帳票(印刷プレビュー)にも「🖨 印刷する」ボタンがあり、押すと印刷ダイアログが出る**(本物のブラウザで。段階1)。

以前は帳票の HTML に ``id="go"``・``window.print()``・印刷用の CSS の字があるかを見ていた
(`test_web_app.PrintButtonTest`)。ここでは出せる帳票を全部開いて**ボタンを押し**、
印刷ダイアログを頼んだか(``window.print`` を差し替えて数える)と、**紙(印刷の見た目)に
ボタンとチェック欄が出ない**ことを見る。帳票を足したときに道具立てを付け忘れないように。
"""
from __future__ import annotations

import unittest

from kanban import config
from kanban.domain import models, printing
from kanban.domain.models import KanbanItem
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


def _long_items() -> list[KanbanItem]:
    """長い資材名・サイズ(現場の写真で 2 行に割れたもの「1030 × 1520」「1000 × 1400」
    「ハードボード(大板棚)」と、それより長いもの)。"""
    rows = [
        ("1", "ハードボード(大板棚)", "1030 × 1520 : 100枚", {"shipped": "〇"}),
        ("2", "ハードボード(大板棚)", "1000 × 1400 : 100枚", {"hold": "〇"}),
        ("3", "ハードボード(大板棚)", "660 × 1050 : 100枚", {}),
        ("4", "ストレッチフィルム(手巻き用)", "500 × 300M : 6本", {"shipped": "〇"}),
        ("5", "木枠材", "2000 × 90 × 18 : 10本", {"shipped": "〇"}),
        ("6", "PPバンド", "15.5mm × 2500M : 2巻", {"shipped": "〇"}),
        ("7", "外装紙", "2200 × 50M : 3本", {}),
    ]
    return [KanbanItem(line="L1", mgmt_no=no, material=m, size=s, want="〇", unwant="",
                       ordered_at="2026/09/25 09:00:00", permanent="×", **kw)
            for no, m, s, kw in rows]


#: 紙に出るマスの 1 つずつ: [文字, 何行か, 文字の幅, 欄の中の幅, 文字の大きさ(px)]
CELLS = """() => [...document.querySelectorAll('table.report th:not(.pick), table.report tbody tr:not(.separator) td:not(.pick)')]
  .map((td) => {
    const r = document.createRange(); r.selectNodeContents(td);
    const lines = new Set([...r.getClientRects()].filter((x) => x.width > 0).map((x) => Math.round(x.top))).size;
    const cs = getComputedStyle(td);
    const room = td.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
    return [td.textContent, lines, r.getBoundingClientRect().width, room, parseFloat(cs.fontSize)];
  })"""


class ReportOneLineTest(unittest.TestCase):
    """**帳票のどのマスも、紙(A4 縦)で 1 行に収まる**(VER3.6.2。本物のブラウザの印刷の描画で)。

    現場の声: 注文票・発送明細・非常設品リストの印刷で、文字数によって 2 行になる
    (非常設品リストの「1030 × 1520」「1000 × 1400」「ハードボード(大板棚)」)。
    いまは折り返さず、列の幅を中身に合わせて配り、それでも入らないマスは文字を
    小さくする。印刷の見た目(``print``)で、どのマスも 1 行・欄からはみ出さない・
    表が紙の印刷できる幅に収まる・文字が下限より小さくならないことを見る。
    PDF(Chromium の印刷そのもの)も作って、1 枚に収まることを見る。
    """

    @classmethod
    def setUpClass(cls) -> None:
        from tests._browser import chromium_or_skip

        cls.pw, cls.browser = chromium_or_skip()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.pw.stop()

    def setUp(self) -> None:
        self.page = self.browser.new_page(viewport={"width": 1000, "height": 900})
        self.addCleanup(self.page.close)
        self.errors: list[str] = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))

    def docs(self):
        items = _long_items()
        return {
            "注文票": printing.build_site_document("L1", items),
            "発送明細": printing.build_warehouse_document("L1", items),
            "非常設品リスト": printing.build_non_permanent_document("L1", items),
        }

    def assert_one_line(self, name: str) -> None:
        page = self.page
        page.emulate_media(media="print")
        page.evaluate("window.fitCells()")     # 印刷の直前(beforeprint)と同じ
        cells = page.evaluate(CELLS)
        self.assertTrue(cells, name)
        min_px = printing.MIN_FONT_PT * 96 / 72
        for text, lines, width, room, size in cells:
            with self.subTest(report=name, cell=text):
                self.assertEqual(lines, 1, f"{name}: 「{text}」が {lines} 行になる")
                self.assertLessEqual(width, room + 0.5, f"{name}: 「{text}」が欄からはみ出す")
                self.assertGreaterEqual(size, min_px - 0.01, f"{name}: 「{text}」が小さすぎる")
        # 表は紙の印刷できる幅(196mm)に収まる(印刷の見た目では紙の余白の内側が 0)
        right = page.evaluate("document.querySelector('table.report').getBoundingClientRect().right")
        self.assertLessEqual(right, printing.PRINTABLE_WIDTH_MM / 25.4 * 96 + 1, name)

    def test_どの帳票も長い資材名とサイズが1行に収まる(self) -> None:
        for name, doc in self.docs().items():
            for editable in (True, False):
                with self.subTest(report=name, editable=editable):
                    self.page.emulate_media(media="screen")
                    self.page.set_content(printing.render_html(doc, editable=editable))
                    self.assert_one_line(name)
                    # Chromium の印刷そのもの(PDF)。表が紙の幅を越えると、印刷は縮めるか
                    # 2 枚目へ送る ── 1 枚に収まることを見る(「/Type /Page」の数 = 枚数)
                    pdf = self.page.pdf(format="A4", print_background=True)
                    pages = pdf.count(b"/Type /Page") - pdf.count(b"/Type /Pages")
                    self.assertEqual(pages, 1, f"{name}: 1 枚に収まらない")
        self.assertEqual(self.errors, [])

    def test_非常設品リストは長いものだけ文字を小さくし_短いものはそのまま(self) -> None:
        doc = self.docs()["非常設品リスト"]
        self.page.set_content(printing.render_html(doc))
        self.assert_one_line("非常設品リスト")
        sizes = dict((text, size) for text, _, _, _, size in self.page.evaluate(CELLS))
        base = doc.font_size_pt * 96 / 72
        self.assertAlmostEqual(sizes["発送"], base, delta=0.01, msg="短いマスまで小さくしている")
        self.assertLess(sizes["ストレッチフィルム(手巻き用)"], base, "長い資材名を小さくしていない")

    def test_打ち替えて長くなったマスも1行に収める(self) -> None:
        doc = self.docs()["発送明細"]
        self.page.set_content(printing.render_html(doc))
        cell = self.page.locator("tbody td[contenteditable]").nth(1)
        cell.click()
        self.page.keyboard.press("End")
        self.page.keyboard.type(" (至急・2便に分けて)")
        self.page.locator("h1").click()
        self.assert_one_line("発送明細(打ち替えたあと)")
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
