import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.models import DetailRecord, HeaderRecord
from nippou.reporting.print_format import (
    build_print_html,
    build_shift_html,
    default_output_path,
    write_print_html,
)


def make_header(**overrides) -> HeaderRecord:
    base = dict(
        report_date="2026年8月3日", line="L-1", shift="1直", page=1,
        worker="山田", count="12", weight_kg="1234.5",
    )
    base.update(overrides)
    return HeaderRecord(**base)


class BuildPrintHtmlTests(unittest.TestCase):
    def test_includes_header_info(self) -> None:
        header = make_header()
        out = build_print_html(header, [])
        self.assertIn("2026年8月3日", out)
        self.assertIn("L-1", out)                              # 紙は正規の呼び名(v4.12.5)
        self.assertIn("1直", out)
        self.assertIn("山田", out)
        self.assertIn("1234.5", out)

    def test_includes_detail_rows_sorted_by_row_no(self) -> None:
        header = make_header()
        details = [
            DetailRecord(report_date=header.report_date, line="L-1", shift="1直", page=1, row_no=2, lot="LOT2"),
            DetailRecord(report_date=header.report_date, line="L-1", shift="1直", page=1, row_no=1, lot="LOT1"),
        ]
        out = build_print_html(header, details)
        self.assertLess(out.index("LOT1"), out.index("LOT2"))

    def test_escapes_html_special_characters(self) -> None:
        header = make_header(worker="<script>alert(1)</script>")
        out = build_print_html(header, [])
        self.assertNotIn("<script>alert(1)</script>", out)
        self.assertIn("&lt;script&gt;", out)

    def test_empty_details_still_produces_valid_table(self) -> None:
        header = make_header()
        out = build_print_html(header, [])
        self.assertIn("<table>", out)
        self.assertIn("<thead>", out)

    def test_generated_at_included_when_given(self) -> None:
        header = make_header()
        out = build_print_html(header, [], generated_at="2026-08-08 09:00:00")
        self.assertIn("2026-08-08 09:00:00", out)


class SheetLayoutTests(unittest.TestCase):
    """**実物の Excel と見比べられる形にする。**

    「初めて見たけどちょっと違いすぎてるな」と言われたところです。
    実物(`2026年8月31日3直_HVC`)の A1:O3 と 21〜24行目に合わせて、
    頭は 左の3行・中央の題・右の枠、表の下は 負荷計算後 と
    直実績合計、という形にしました。
    """

    def test_頭は左の3行と中央の題と右の枠(self) -> None:
        out = build_print_html(make_header(worker="山田"), [])
        self.assertIn('<div class="head">', out)
        self.assertIn('<table class="head__keys">', out)
        for label in ("作業日・直", "梱包ライン", "作業者名"):
            self.assertIn(f"<th>{label}</th>", out)
        self.assertIn('<div class="head__title">', out)
        self.assertIn('<div class="head__right">', out)

    def test_作業日と直は実物どおり1つの欄に入る(self) -> None:
        """実物の B1 は `2026年8月31日3直` で、日付と直が1つのセル。"""
        out = build_print_html(
            make_header(report_date="2026年8月31日", shift="3直"), [])
        self.assertIn("<td>2026年8月31日3直</td>", out)

    def test_昼稼働は選ばれたほうだけ出す(self) -> None:
        """紙は「有・無」を丸で囲むが、刷ったものに手で丸は付けない。"""
        out = build_print_html(make_header(day_shift="有"), [])
        self.assertIn("<b>昼稼働</b>有", out)
        # 枠の中に「有・無」を両方出して、どちらか分からない紙にしない
        self.assertNotIn("<b>昼稼働</b>有・無", out)

    def test_昼稼働が空なら無と読む(self) -> None:
        out = build_print_html(make_header(day_shift=""), [])
        self.assertIn("<b>昼稼働</b>無", out)

    def test_表の下に負荷計算後と直実績合計が並ぶ(self) -> None:
        out = build_print_html(
            make_header(coefficient_lot_count="3.25", lot_count="4",
                        count="1200", weight_kg="4379.4"), [])
        self.assertIn('<div class="totals">', out)
        self.assertIn("<th>負荷計算後</th>", out)
        self.assertIn('<th colspan="3">直実績合計</th>', out)
        for value in ("3.25", "4", "1200"):
            self.assertIn(f"<td>{value}</td>", out)

    def test_重量はKgとTの2つを出す(self) -> None:
        """実物の W24 は1つのセルに改行で「4379.4 Kg」「4.38 T」。

        現場はトンのほうで話すので、Kg だけだと毎回 1000 で割ることに
        なります。
        """
        out = build_print_html(make_header(weight_kg="4379.4"), [])
        self.assertIn('<span class="kg">4379.4 Kg</span>', out)
        self.assertIn('<span class="ton">4.38 T</span>', out)

    def test_重量が数字でなければトンは出さない(self) -> None:
        """**添え物なので、黙って消えます。** 紙も空欄のことがあります。"""
        for weight in ("", "  ", "未計量"):
            with self.subTest(weight=weight):
                out = build_print_html(make_header(weight_kg=weight), [])
                self.assertNotIn('class="ton"', out)

    def test_合計は表の下で集計の上(self) -> None:
        """紙の並びは 表 → 合計。集計はこの道具の足し物なので、その下。"""
        from nippou.db.models import PackingReport

        report = PackingReport(work_date="2026年8月3日", line_name="L-1",
                               shift="1直", pages=1, total_quantity=20.0)
        out = build_print_html(make_header(), [], summary=report)
        self.assertLess(out.index("</table>"), out.index('class="totals"'))
        self.assertLess(out.index('class="totals"'), out.index("1直 の集計"))

    # ---- 合計欄が空のまま刷られていた -------------------------------
    def test_打っていないﾛｯﾄ数は集計から入れる(self) -> None:
        """**取り込んだ紙の合計欄が空でした。**

        ﾛｯﾄ数と係数Lot数は、コイルのライン以外では手入力です。CSVにも
        xlsxにもこの2つは無いので、取り込んだぶんは空のまま刷られて
        いました ── 同じ紙の下の集計には「Lot数 3」と出ているのに、
        合計欄だけ空、という紙になります。
        """
        from nippou.db.models import PackingReport

        report = PackingReport(work_date="2026年8月3日", line_name="L-1",
                               shift="1直", pages=1, total_lot_count=3,
                               coefficient_lot_count=1.5)
        out = build_print_html(make_header(lot_count="", coefficient_lot_count=""),
                               [], summary=report)
        totals = out[out.index('class="totals"'):out.index("</table>",
                                                           out.index('class="totals"'))]
        self.assertIn("<td>3</td>", totals)        # ﾛｯﾄ数
        self.assertIn("<td>1.50</td>", totals)     # 係数Lot数

    def test_打ってあれば打った値のまま(self) -> None:
        """**入れ直しません。** 手で打った値が正で、集計は控えです。"""
        from nippou.db.models import PackingReport

        report = PackingReport(work_date="2026年8月3日", line_name="L-1",
                               shift="1直", pages=1, total_lot_count=3)
        out = build_print_html(make_header(lot_count="9"), [], summary=report)
        totals = out[out.index('class="totals"'):out.index("</table>",
                                                           out.index('class="totals"'))]
        self.assertIn("<td>9</td>", totals)
        self.assertNotIn("<td>3</td>", totals)

    def test_枚数と重量も直の合計(self) -> None:
        """**2ページの直の2枚目が「ﾛｯﾄ数 13 / 枚数 10」だった。**

        ﾛｯﾄ数は直の集計から入るのに、枚数・重量はそのページの行の合計で、
        同じ「直実績合計」の行に直とページが混ざっていた。
        """
        from nippou.db.models import PackingReport

        report = PackingReport(work_date="2026年8月3日", line_name="L-1",
                               shift="1直", pages=2, total_lot_count=13,
                               total_quantity=630.0, total_weight=4379.4)
        out = build_print_html(make_header(lot_count="", count="10", weight_kg="70.0"),
                               [], summary=report)
        totals = out[out.index('class="totals"'):out.index("</table>",
                                                           out.index('class="totals"'))]
        self.assertIn("<td>13</td>", totals)
        self.assertIn("<td>630</td>", totals)
        self.assertIn("4379.4 Kg", totals)
        self.assertIn("4.38 T", totals)
        self.assertNotIn("<td>10</td>", totals)

    def test_集計が無ければ空のまま(self) -> None:
        """集計を渡さない紙(VBAと同じ姿)では、これまでどおり。"""
        out = build_print_html(make_header(lot_count=""), [])
        self.assertNotIn("<td>0</td>", out[out.index('class="totals"'):])

    def test_12行いつも出る(self) -> None:
        """**打っていなくても12行。**

        紙は12行の罫線が引かれた用紙で、打った数で行数は変わりません。
        打った行だけ出すと、3行しか打っていない直の紙が、表の途中で
        切れた別物に見えます。
        """
        out = build_print_html(make_header(), [])
        body = out[out.index("<tbody>"):out.index("</tbody>")]
        self.assertEqual(body.count("<tr>"), 12)
        for row_no in range(1, 13):
            with self.subTest(row=row_no):
                self.assertIn(f"<tr><td>{row_no}</td>", body)

    def test_打った行は行番号のところに出る(self) -> None:
        """歯抜け(2行目と5行目だけ)でも、紙の2行目・5行目に出ます。

        順に詰めると画面と紙で行番号がずれます ── 理由欄は
        「3行目: ○○」と行番号で書くので、ずれると指す行が変わります。
        """
        details = [
            DetailRecord(report_date="2026年8月3日", line="L-1", shift="1直",
                         page=1, row_no=5, lot="LOT5"),
            DetailRecord(report_date="2026年8月3日", line="L-1", shift="1直",
                         page=1, row_no=2, lot="LOT2"),
        ]
        out = build_print_html(make_header(), details)
        body = out[out.index("<tbody>"):out.index("</tbody>")]
        rows = body.split("<tr>")[1:]
        self.assertEqual(len(rows), 12)
        self.assertIn("LOT2", rows[1])
        self.assertIn("LOT5", rows[4])
        for i in (0, 2, 3, 5, 11):
            with self.subTest(row=i + 1):
                self.assertNotIn("LOT", rows[i])

    def test_12行に収まらない行も捨てない(self) -> None:
        """起きないはずですが、黙って消えるほうが困ります。"""
        details = [
            DetailRecord(report_date="2026年8月3日", line="L-1", shift="1直",
                         page=1, row_no=13, lot="LOT13"),
        ]
        out = build_print_html(make_header(), details)
        body = out[out.index("<tbody>"):out.index("</tbody>")]
        self.assertEqual(body.count("<tr>"), 13)
        self.assertIn("LOT13", body)

    def test_頭の表が幅いっぱいに伸びない(self) -> None:
        """`table{width:100%}` に引かれると、題が縦に潰れます。

        一度そうなりました ── 左の3行が紙いっぱいに広がって、中央の
        「梱包実績日報表」が1文字ずつ縦に折り返されていました。
        """
        out = build_print_html(make_header(), [])
        rule = out[out.index(".head__keys {"):]
        rule = rule[:rule.index("}")]
        self.assertIn("width: auto", rule)
        self.assertIn("flex: none", rule)


class BuildShiftHtmlTests(unittest.TestCase):
    """**1直ぶんを1つの窓に。** 3ページある直を3回開かせない。"""

    def pages(self, count: int):
        return [(make_header(page=n),
                 [DetailRecord(report_date="2026年8月3日", line="L-1",
                               shift="1直", page=n, row_no=1,
                               lot=f"LOT{n}")])
                for n in range(1, count + 1)]

    def test_ページぶんの紙が1つの文書に入る(self) -> None:
        out = build_shift_html(self.pages(3))
        self.assertEqual(out.count('<section class="sheet">'), 3)
        for n in (1, 2, 3):
            self.assertIn(f"LOT{n}", out)

    def test_html_は1つだけ(self) -> None:
        """`<html>` が入れ子になっていると、開くブラウザ次第で崩れる。"""
        out = build_shift_html(self.pages(3))
        self.assertEqual(out.count("<!doctype html>"), 1)
        self.assertEqual(out.count("<html lang=\"ja\">"), 1)
        self.assertEqual(out.count("</body>"), 1)

    def test_ページのあいだで改ページする(self) -> None:
        """1回の Ctrl+P で、紙はページの数だけ出る。"""
        out = build_shift_html(self.pages(2))
        self.assertIn("page-break-before: always", out)

    def test_表題に全ページ数が出る(self) -> None:
        out = build_shift_html(self.pages(3))
        self.assertIn("<title>日報 2026年8月3日 L-1 1直 全3ページ</title>", out)

    def test_1ページでも同じ道を通る(self) -> None:
        out = build_shift_html(self.pages(1))
        self.assertEqual(out.count('<section class="sheet">'), 1)
        self.assertIn("全1ページ", out)

    def test_1ページも無ければそう書く(self) -> None:
        out = build_shift_html([])
        self.assertIn("該当するデータがありません", out)
        self.assertEqual(out.count("<!doctype html>"), 1)

    def test_集計はどのページにも載る(self) -> None:
        """刷った紙がばらけても、1枚1枚がその直の数字を持つ。"""
        from nippou.db.models import PackingReport

        report = PackingReport(work_date="2026年8月3日", line_name="L-1",
                               shift="1直", pages=2, total_quantity=20.0)
        out = build_shift_html(self.pages(2), summary=report)
        self.assertEqual(out.count("1直 の集計"), 2)


class WritePrintHtmlTests(unittest.TestCase):
    def test_writes_file_and_creates_parent_dirs(self) -> None:
        header = make_header()
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = Path(tmpdir) / "nested" / "print.html"
            result = write_print_html(header, [], out_path)
            self.assertEqual(result, out_path)
            self.assertTrue(out_path.exists())
            content = out_path.read_text(encoding="utf-8")
            self.assertIn("L-1", content)


class DefaultOutputPathTests(unittest.TestCase):
    def test_builds_expected_filename(self) -> None:
        header = make_header(report_date="2026年8月3日", line="L-1", shift="1直", page=1)
        path = default_output_path(Path("/tmp/reports"), header)
        self.assertEqual(
            path,
            Path("/tmp/reports/L-1/印刷/2026.08/03/印刷_2026年8月3日_L-1_1直_1.html"))

    def test_集計CSVと同じ並べ方(self) -> None:
        """片方だけ別の場所に出ると、探す場所が2通りになります。

        ラインで分かれているので、自分のラインのフォルダを開けば
        紙も集計も同じ日付でそろいます。
        """
        from nippou.reporting.csv_export import KIND_PRINT, dated_dir

        header = make_header(report_date="2026年8月3日", line="L-1")
        path = default_output_path(Path("/tmp/reports"), header)
        self.assertEqual(
            path.parent,
            dated_dir(Path("/tmp/reports"), "2026年8月3日", "L-1", KIND_PRINT))

    def test_sanitizes_unsafe_line_characters(self) -> None:
        header = make_header(line="L/1:*")
        path = default_output_path(Path("/tmp/reports"), header)
        self.assertNotIn("/", path.name.split("_")[2])


if __name__ == "__main__":
    unittest.main()
