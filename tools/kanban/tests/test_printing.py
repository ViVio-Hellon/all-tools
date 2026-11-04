"""帳票生成のテスト。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from kanban.domain import printing
from kanban.domain.models import KanbanItem


def item(no: str, material: str, size: str, **kwargs) -> KanbanItem:
    return KanbanItem(line="LVC", mgmt_no=no, material=material, size=size, **kwargs)


class SiteDocumentTest(unittest.TestCase):
    def test_only_ordered_and_unshipped(self):
        items = [
            item("1", "外装紙", "A", want="〇", ordered_at="2026/07/14 09:05:00"),
            item("2", "外装紙", "B", want="〇", shipped="〇"),  # 発送済みは印刷しない
            item("3", "アングル", "C"),  # 未発注も印刷しない
        ]
        doc = printing.build_site_document("LVC", items)
        self.assertEqual(doc.data_row_count, 1)
        self.assertEqual(doc.rows[0].cells, ["外装紙", "A", "注文", "2026/07/14 09:05:00"])

    def test_separator_between_materials(self):
        items = [
            item("1", "外装紙", "A", want="〇"),
            item("2", "アングル", "B", want="〇"),
        ]
        doc = printing.build_site_document("LVC", items)
        self.assertEqual([r.separator for r in doc.rows], [False, True, False])

    def test_title_uses_display_name(self):
        doc = printing.build_site_document("LS", [])
        self.assertEqual(doc.title, "機側")

    def test_held_row_is_excluded(self):
        """注文中(倉庫対応中)の行は現場の注文票にも印刷しない
        (VBA の MaterialForm.btnPrint_Click の HoldValue <> "〇" 条件と同じ)。"""
        items = [item("1", "外装紙", "A", want="〇", hold="〇")]
        doc = printing.build_site_document("LVC", items)
        self.assertEqual(doc.data_row_count, 0)


class WarehouseDocumentTest(unittest.TestCase):
    def test_only_ordered_and_shipped(self):
        items = [
            item("1", "外装紙", "2200 × 50M : 3本", want="〇", shipped="〇"),
            item("2", "外装紙", "1900 × 50M : 5本", want="〇"),
        ]
        doc = printing.build_warehouse_document("LVC", items)
        self.assertEqual(doc.data_row_count, 1)
        self.assertEqual(doc.rows[0].cells, ["外装紙", "2200 × 50M", "3本", "発送"])

    def test_size_without_colon_goes_to_quantity(self):
        items = [item("1", "テープ", "1箱", want="〇", shipped="〇")]
        doc = printing.build_warehouse_document("LVC", items)
        self.assertEqual(doc.rows[0].cells, ["テープ", "", "1箱", "発送"])

    def test_non_permanent_document_ignores_shipping_state(self):
        items = [
            item("1", "ハードボード", "660 × 1050 : 100枚", want="〇", permanent="×"),
            item("2", "ハードボード", "750 × 1130 : 100枚", want="〇", permanent="〇"),
            item("3", "ハードボード", "510 × 770 : 100枚", permanent="×"),
        ]
        doc = printing.build_non_permanent_document("大板小板", items)
        self.assertEqual(doc.data_row_count, 1)
        self.assertEqual(doc.rows[0].cells, ["ハードボード", "660 × 1050", "100枚", "受付"])
        self.assertTrue(doc.highlight_title)

    def test_held_row_is_excluded_even_if_shipped(self):
        """保留中は通常リスト(発送=〇)にも一切載せない(VBA PrintLine と同じ)。

        保留単体の操作は発送を強制解除するが、一括発送は保留を見ずに
        対象を選ぶため、保留中のまま発送済みになった行が実在しうる。
        """
        items = [
            item(
                "1", "外装紙", "2200 × 50M : 3本",
                want="〇", shipped="〇", hold="〇",
            ),
            item("2", "外装紙", "1900 × 50M : 5本", want="〇", shipped="〇"),
        ]
        doc = printing.build_warehouse_document("LVC", items)
        self.assertEqual(doc.data_row_count, 1)
        self.assertEqual(doc.rows[0].cells[0], "外装紙")
        self.assertEqual(doc.rows[0].cells[1], "1900 × 50M")

    def test_non_permanent_document_shows_the_state(self):
        """非常設品リストは状態を出す: 赤だけ=受付・緑=発送・黄=注文中(注文中も載せる)。"""
        items = [
            item("1", "ハードボード", "660 × 1050 : 100枚", want="〇", permanent="×", hold="〇"),
            item("2", "ハードボード", "750 × 1130 : 100枚", want="〇", permanent="×"),
            item("3", "ハードボード", "1030 × 1520 : 100枚", want="〇", permanent="×", shipped="〇"),
            item("4", "ハードボード", "510 × 770 : 100枚", permanent="×", shipped="〇"),
        ]
        doc = printing.build_non_permanent_document("大板小板", items)
        self.assertEqual(doc.headers, ["資材", "サイズ", "数", "状態"])
        self.assertEqual([(r.cells[1], r.cells[3]) for r in doc.rows if r.cells],
                         [("660 × 1050", "注文中"), ("750 × 1130", "受付"), ("1030 × 1520", "発送")])

    def test_cancelled_shipping_goes_back_to_reception(self):
        """発送を付けて消した(緑→消灯)非常設品は、発注が残っているので「受付」で載る。"""
        items = [item("1", "ハードボード", "1030 × 1520 : 100枚", want="〇", permanent="×")]
        doc = printing.build_non_permanent_document("大板小板", items)
        self.assertEqual(doc.rows[0].cells[3], "受付")


class RenderTest(unittest.TestCase):
    def test_html_contains_rows_and_escapes(self):
        items = [item("1", "外装紙<script>", "A", want="〇")]
        doc = printing.build_site_document("LVC", items)
        html = printing.render_html(doc)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>外装", html)
        self.assertIn('<table class="report', html)

    def test_empty_document_renders_message(self):
        doc = printing.build_site_document("LVC", [])
        html = printing.render_html(doc)
        self.assertIn("対象の明細はありません", html)

    def test_auto_print_opens_the_dialog_on_load(self):
        """``auto_print`` は**開いた直後**に出す設定。

        「印刷する」ボタン自体も ``window.print()`` を呼ぶので、
        文字列の有無ではなく**読み込み時に呼ぶかどうか**で見る。
        """
        doc = printing.build_site_document("LVC", [item("1", "外装紙", "A", want="〇")])
        on_load = "addEventListener('load',()=>window.print())"
        self.assertIn(on_load, printing.render_html(doc, auto_print=True))
        self.assertNotIn(on_load, printing.render_html(doc, auto_print=False))


class EditableReportTest(unittest.TestCase):
    """**出す前に直せる。**

    帳票は人へ渡す紙なので、渡す直前に「この数だけ違う」と気付くことが
    ある。Excel 版はその場で打ち替えられた。直せないと、紙を直したいだけ
    なのに看板そのものを触ることになる。
    """

    def page(self, **kwargs) -> str:
        doc = printing.build_site_document(
            "LVC",
            [
                item("1", "外装紙", "2200 × 50M : 3本", want="〇"),
                item("2", "アングル", "2510 : 3束", want="〇"),
            ],
        )
        return printing.render_html(doc, **kwargs)

    def test_cells_can_be_typed_into(self):
        page = self.page()
        self.assertIn('contenteditable="true"', page)
        self.assertIn("🖨 印刷する", page)

    def test_original_values_are_kept_for_undo(self):
        """「元に戻す」はサーバへ訊き直さない。

        直している最中に共有DBが変わっても、手元の紙が勝手に
        書き換わらないようにするため。
        """
        self.assertIn('data-orig="外装紙"', self.page())

    def test_rows_can_be_left_out(self):
        page = self.page()
        self.assertIn('class="pick"', page)
        self.assertIn("tr.dropped { display: none !important; }", page)

    def test_edits_never_reach_the_shared_db(self):
        """**ここで直すのは紙だけ。** 通信は 1 度も起きない。"""
        page = self.page()
        for forbidden in ("fetch(", "XMLHttpRequest", "<form", "sendBeacon"):
            self.assertNotIn(forbidden, page, f"{forbidden} が入っている")

    def test_the_marks_do_not_reach_the_paper(self):
        """直した跡・チェック欄は紙に出さない。

        受け取った側が「これは何の印か」と迷う。
        """
        page = self.page()
        self.assertIn(".tools, th.pick, td.pick { display: none !important; }", page)
        self.assertIn("background: transparent !important", page)

    def test_plain_report_has_no_editor(self):
        page = self.page(editable=False)
        self.assertNotIn("contenteditable", page)
        self.assertNotIn("印刷する行", page)
        self.assertIn('<table class="report">', page)

    def test_empty_report_has_no_editor(self):
        """直すものが無いなら、道具立ても出さない。"""
        page = printing.render_html(printing.build_site_document("LVC", []))
        self.assertNotIn("contenteditable", page)
        self.assertIn("対象の明細はありません", page)

    def test_separator_spans_the_checkbox_column_too(self):
        """資材の区切り(空行)が、チェック欄のぶんもまたぐこと。"""
        page = self.page()
        self.assertIn('<tr class="separator"><td colspan="5"></td></tr>', page)

    def test_warehouse_report_is_editable_too(self):
        """**現場・倉庫ともに。** 倉庫の発送明細でも数を直せる。"""
        doc = printing.build_warehouse_document(
            "LVC", [item("1", "外装紙", "2200 × 50M : 3本", want="〇", shipped="〇")]
        )
        page = printing.render_html(doc)
        self.assertIn('data-orig="3本"', page)

    def test_non_permanent_report_is_editable_too(self):
        doc = printing.build_non_permanent_document(
            "LVC",
            [item("1", "外装紙", "2200 × 50M : 3本", want="〇", permanent="×")],
        )
        self.assertIn('contenteditable="true"', printing.render_html(doc))

    def test_write_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = printing.build_site_document("LS", [item("1", "外装紙", "A", want="〇")])
            path = printing.write_document(doc, base_dir=tmp)
            self.assertTrue(Path(path).is_file())
            self.assertIn("機側", Path(path).read_text(encoding="utf-8"))


class PrintableAreaTest(unittest.TestCase):
    """**文字・罫線・色の帯を、紙の端から 5mm 以上内側に置く。**

    プリンターは紙の縁から 4mm ほどには印刷できない。以前は左右と下の余白が
    0 で、しかもマスが折り返さないので、長い資材名があると右端が 0mm まで
    出ていた(Chromium で PDF にして測った)。画面のプレビューは紙の端まで
    描くので、収まって見えるだけだった。
    """

    def html(self) -> str:
        items = [KanbanItem(line="LVC", mgmt_no="1", material="資材", size="1 : 2本",
                            want="〇", unwant="", ordered_at="2026/09/25 09:00:00")]
        return printing.render_html(printing.build_site_document("LVC", items))

    def test_every_side_of_the_page_keeps_5mm(self):
        import re

        match = re.search(r"@page \{ size: A4 portrait; margin: ([^;]+); \}", self.html())
        self.assertIsNotNone(match, "@page の余白が見つからない")
        sides = [float(v.rstrip("m")) for v in match.group(1).split()]
        self.assertEqual(len(sides), 4, "上・右・下・左を全部書く(省略すると 0 の辺が出る)")
        for side in sides:
            self.assertGreaterEqual(side, 5.0, match.group(1))

    def test_the_body_adds_no_margin_on_paper(self):
        """余白は @page だけで取る。本文の余白は 1 ページ目の上と最後の下にしか効かない。"""
        html = self.html()
        self.assertIn("margin: 0;", html)
        self.assertIn("@media screen", html, "画面では同じだけ空けて見せる")

    def test_every_cell_stays_on_one_line(self):
        """どのマスも折り返さない(VER3.6.2)。入りきらないものは文字を小さくして収める。

        以前は資材・サイズを折り返していたので「1030 × 1520」「ハードボード(大板棚)」が
        2 行に割れた(現場の声)。紙で本当に 1 行になるかは ``test_report_print_browser``
        が Chromium の印刷の描画で確かめる。
        """
        html = self.html()
        self.assertIn("th, td { white-space: nowrap;", html)
        self.assertNotIn('class="wrap"', html)
        self.assertIn("window.fitCells", html, "入りきらないマスの文字を小さくするスクリプトが無い")
        self.assertIn("beforeprint", html, "印刷の直前にも測り直す")


class ColumnWidthTest(unittest.TestCase):
    """列の幅は中身に合わせて配る。足すと表の幅(紙の印刷できる幅より 1mm 狭い)ちょうど。"""

    def doc(self):
        items = [
            item("1", "ハードボード(大板棚)", "1030 × 1520 : 100枚", want="〇", permanent="×"),
            item("2", "ハードボード(大板棚)", "1000 × 1400 : 100枚", want="〇", permanent="×",
                 hold="〇"),
            item("3", "外装紙", "2200 × 50M : 3本", want="〇", permanent="×", shipped="〇"),
        ]
        return printing.build_non_permanent_document("L1", items)

    def test_widths_fill_the_printable_width_exactly(self):
        for build in (printing.build_site_document, printing.build_warehouse_document,
                      printing.build_non_permanent_document):
            with self.subTest(build=build.__name__):
                items = [item("1", "ハードボード(大板棚)", "1030 × 1520 : 100枚", want="〇",
                              shipped="〇", permanent="×")]
                widths = printing.column_widths(build("L1", items))
                self.assertAlmostEqual(sum(widths), printing.TABLE_WIDTH_MM, places=1)
                self.assertLess(printing.TABLE_WIDTH_MM, printing.PRINTABLE_WIDTH_MM)
                self.assertTrue(all(w > 0 for w in widths), widths)

    def test_material_and_size_are_wide_and_count_and_state_narrow(self):
        material, size, qty, state = printing.column_widths(self.doc())
        self.assertGreater(material, qty * 2)
        self.assertGreater(size, state)
        self.assertLess(qty, 40)
        self.assertLess(state, 40)

    def test_widths_are_written_on_the_header(self):
        html = printing.render_html(self.doc())
        for w in printing.column_widths(self.doc()):
            self.assertIn(f"width:{printing._mm(w)}mm", html)
        self.assertIn("table-layout: fixed;", html)
        self.assertIn(f"width: {printing._mm(printing.TABLE_WIDTH_MM)}mm;", html)

    def test_full_width_counts_more_than_half_width(self):
        self.assertGreater(printing.text_width_em("大板棚"), printing.text_width_em("100"))

if __name__ == "__main__":
    unittest.main()
