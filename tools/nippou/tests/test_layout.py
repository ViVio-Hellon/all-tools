"""列の並びと見出し ── 画面と紙が食い違わないこと

指摘は「日報入力を写真1(梱包実績日報表)のレイアウトに寄せてほしい」
でした。入力画面と印刷用HTMLが別々に列を持っていたので、同じ欄が
画面では「材質」、紙では「材・調質」と呼ばれていました。

`nippou/layout.py` に1つだけ置き、両方がそこを読むようにしています。
ここが守っているのは**両方が本当に同じものを読んでいること**です。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou import layout                                       # noqa: E402
from nippou.db.models import DetailRecord, HeaderRecord         # noqa: E402
from nippou.presenters import entry as presenter                # noqa: E402
from nippou.reporting import print_format                       # noqa: E402


def _blank_detail() -> DetailRecord:
    return DetailRecord(report_date="2026年8月31日", line="HVC",
                        shift="3直", page=1, row_no=1)


class PaperOrderTests(unittest.TestCase):
    """紙(梱包実績日報表)の並びどおりか。"""

    def test_紙の並び(self) -> None:
        self.assertEqual(
            [c.family for c in layout.COLUMNS],
            ["LOT", "ZAI", "SIZ", "KEN", "KZ", "KH", "SZ", "SH", "HIT", "AI",
             "MAI", "TUT", "VC", "ET", "S", "TH", "SS", "THS", "STH", "THT",
             "CON", "WEI", "TIM", "UNI"])

    def test_合紙が入っている(self) -> None:
        """K列(合紙)。**DBには前からあったのに、どこにも出ていなかった。**"""
        found = next(c for c in layout.COLUMNS if c.family == "AI")
        self.assertEqual(found.label, "合紙")
        self.assertEqual(found.kind, "select")
        self.assertFalse(found.outside_print)
        # 保存する先もある
        self.assertTrue(hasattr(_blank_detail(), "ai"))

    def test_見出しは紙の言葉(self) -> None:
        labels = {c.family: c.label for c in layout.COLUMNS}
        self.assertEqual(labels["ZAI"], "材・調質")
        self.assertEqual(labels["LOT"], "ロット№")
        # **紙とCSVはここを読みます。** 画面だけ別の名前を持てる
        self.assertEqual(layout.COLUMNS[0].screen_name, "LOTNO")
        self.assertEqual(labels["KEN"], "検入枚数")
        self.assertEqual(labels["HIT"], "作業人数")
        self.assertEqual(labels["MAI"], "個装単位 枚数")
        self.assertEqual(labels["TUT"], "梱包単位 包数")

    def test_2段の見出し(self) -> None:
        groups = dict(g for g in layout.column_groups() if g[0])
        self.assertEqual(groups["梱包作業時間"], 4)     # 開始 時分 / 終了 時分
        self.assertEqual(groups["梱包数量"], 2)         # 個装単位 / 梱包単位
        self.assertEqual(groups["作業停止①"], 2)        # 記号 / 時間(分)
        self.assertEqual(groups["実績合計"], 2)         # 枚数 / 重量

    def test_結合しない列は幅1のまま(self) -> None:
        """見出しの無い列が隣り合っても、勝手にくっつかない。"""
        # ロット№ / 材・調質 は隣同士だが別々の列
        self.assertEqual(layout.column_groups()[:2], (("", 1), ("", 1)))


class OutsidePrintTests(unittest.TestCase):
    """紙に載らない欄(印刷範囲の外)。"""

    def test_作業時間と単重は紙に載らない(self) -> None:
        outside = [c.family for c in layout.COLUMNS if c.outside_print]
        self.assertEqual(outside, ["TIM", "UNI"])

    def test_印刷用の列からは外れている(self) -> None:
        families = [c.family for c in layout.PRINT_COLUMNS]
        self.assertNotIn("TIM", families)
        self.assertNotIn("UNI", families)
        self.assertIn("WEI", families)

    def test_印刷HTMLに出てこない(self) -> None:
        header = HeaderRecord(report_date="2026年8月31日", line="HVC",
                              shift="3直", page=1, worker="近藤雅幹")
        detail = DetailRecord(report_date="2026年8月31日", line="HVC",
                              shift="3直", page=1, row_no=1,
                              lot="H5422S0", tim="80", uni="250.04")
        html = print_format.build_print_html(header, [detail])
        self.assertIn("H5422S0", html)
        self.assertNotIn("250.04", html)      # 単重は紙に載らない
        self.assertNotIn("作業時間(分)", html)


class SharedSourceTests(unittest.TestCase):
    """画面と紙が**同じもの**を読んでいるか。"""

    def test_画面はlayoutを読んでいる(self) -> None:
        self.assertIs(presenter.COLUMNS, layout.COLUMNS)

    def test_紙もlayoutを読んでいる(self) -> None:
        """印刷用HTMLに、紙の見出しがそのまま出る。"""
        header = HeaderRecord(report_date="2026年8月31日", line="HVC",
                              shift="3直", page=1)
        html = print_format.build_print_html(header, [])
        for label in ("ロット№", "材・調質", "梱包作業時間", "梱包数量",
                      "作業停止①", "実績合計", "合紙"):
            with self.subTest(label=label):
                self.assertIn(label, html)

    def test_紙の注意書きも出る(self) -> None:
        header = HeaderRecord(report_date="2026年8月31日", line="HVC",
                              shift="3直", page=1)
        html = print_format.build_print_html(header, [])
        self.assertIn(layout.STOP_TIME_NOTE, html)
        self.assertIn(layout.SHEET_TITLE, html)

    def test_全ファミリが保存先を持っている(self) -> None:
        """列を足したのに保存できない、を作らない。"""
        record = _blank_detail()
        for column in layout.COLUMNS:
            with self.subTest(family=column.family):
                self.assertTrue(hasattr(record, column.field),
                                f"DetailRecord に {column.field} がありません")

    def test_全ファミリが本当に書き出される(self) -> None:
        """**属性があるだけでは足りない。** 実際に値が入るところまで見る。

        合紙(AI)がこれで漏れていました ── DBに `ai` 列があり、
        `DetailRecord` に `ai` があるのに、`to_records` が使っていた
        並び(`ROW_FIELD_FAMILIES`)にだけ入っておらず、何を入れても
        保存されませんでした。
        """
        state = presenter.empty_state()
        for i, column in enumerate(layout.COLUMNS, start=1):
            state.set(1, column.family, f"v{i}")
        _header, details = presenter.to_records(
            state, "2026年8月31日", "HVC", "3直", 1)
        first = details[0]
        for i, column in enumerate(layout.COLUMNS, start=1):
            with self.subTest(family=column.family):
                self.assertEqual(getattr(first, column.field), f"v{i}",
                                 f"{column.family} が書き出されていません")


class InterleafTests(unittest.TestCase):
    def test_合紙は有無の2択(self) -> None:
        self.assertEqual(layout.INTERLEAF_CHOICES, ("", "有", "無"))


if __name__ == "__main__":
    unittest.main()


class ColumnWidthTests(unittest.TestCase):
    """LOTNO は最大7桁なのでぎりぎりまで狭く、寸法は見切れない幅に。"""

    def width(self, family: str) -> int:
        return next(c.width for c in layout.COLUMNS if c.family == family)

    def test_LOTNOは7桁ぶん(self) -> None:
        # 12px の等幅でない字で7文字 ≒ 55px + 左右の余白 8px。96px では広すぎた
        self.assertLessEqual(self.width("LOT"), 72)
        self.assertGreaterEqual(self.width("LOT"), 64)

    def test_寸法は丈5桁まで見切れない(self) -> None:
        # 「20.000×1528.0×13053.0」≒ 154px + 余白 8px。132px では切れていた
        self.assertGreaterEqual(self.width("SIZ"), 164)
