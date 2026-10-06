"""考え方と計算 ── 標準作業時間と梱包力を、読んで分かる専用の面 (v4.11.0)

    標準時間 梱包力 概念と計算方法は参照できるようにしてください(専用タブ)
    ちなみに計算方法に根拠はあるんですか？

【約束】
    ・「考え方と計算」の面がある(読むだけ。標準作業時間の画面の面の1つ)
    ・6つの節: とは / 標準の出し方 / 梱包力とは / 計算の例 / 根拠 / 気をつけること
    ・**例の数は本物の計算に通したもの**(計算を直したら例も変わる)
    ・根拠は生産管理(IE)の言葉と対応させ、このツールで決めたことは分けて書く
    ・梱包力の「しくみ」・標準の面から、この面の該当の節へ飛べる
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import standard_time as logic  # noqa: E402
from nippou.presenters import standard_time as view  # noqa: E402
from tests._web import WebTestCase  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class GuideNumbersTests(unittest.TestCase):
    """例の数が、計算そのものと合っている。"""

    def setUp(self) -> None:
        self.g = view.guide()

    def test_標準は中央値_トラブルの日に引っ張られない(self) -> None:
        g = self.g
        self.assertEqual([w["per_package"] for w in g["works"]], ["30.0", "35.0", "40.0", "50.0"])
        self.assertEqual((g["median"], g["mean"]), ("37.5", "38.75"))
        self.assertEqual(g["trouble_median"], g["median"])              # 中央値は動かない
        self.assertEqual(g["trouble_mean"], "63.75")                     # 平均は跳ねる

    def test_標準作業時間と標準との差(self) -> None:
        g = self.g
        self.assertEqual(g["estimate"], "75")                            # 37.5 × 6 ÷ 3
        self.assertEqual((g["standard_first"], g["diff_first"]), ("75", "-15"))

    def test_梱包力は合計どうしで割る(self) -> None:
        g = self.g
        self.assertEqual([(r["label"], r["standard_pm"], r["actual_pm"], r["power"])
                          for r in g["teams"]],
                         [("A", "300", "260", "115"), ("B", "300", "360", "83")])
        self.assertEqual((g["total"]["standard_pm"], g["total"]["actual_pm"],
                          g["total"]["power"]), ("600", "620", "97"))
        # A班とB班の平均(99)ではない
        self.assertNotEqual(g["total"]["power"], f"{(300 / 260 + 300 / 360) / 2 * 100:.0f}")

    def test_難しさで均す例(self) -> None:
        easy, hard = self.g["difficulty"]
        self.assertEqual((easy["per_hour"], hard["per_hour"]), ("3.0", "1.5"))
        self.assertEqual((easy["power"], hard["power"]), ("100", "100"))

    def test_数えない理由は計算と同じ文言(self) -> None:
        reasons = self.g["excluded"]
        self.assertEqual(len(reasons), 6)
        self.assertEqual(len(set(reasons)), 6)
        self.assertIn("作業人数が0", reasons)
        self.assertEqual(self.g["min_samples"], logic.MIN_SAMPLES)


class GuidePageTests(WebTestCase):
    def test_専用の面がある(self) -> None:
        self.assertIn("guide", [k for k, _, _ in view.TABS])
        page = self.get("/standard-time").get_data(as_text=True)
        self.assertEqual(re.findall(r'role="tab" data-key="(\w+)"', page),
                         [k for k, _, _ in view.TABS])
        self.assertIn('id="panel-guide"', page)
        for part in ("guide-standard", "guide-calc", "guide-power", "guide-example",
                     "guide-basis", "guide-limits"):
            self.assertIn(f'id="{part}"', page)
            self.assertIn(f'href="#{part}"', page)                      # 目次から飛べる

    def test_式と例の数が出る(self) -> None:
        page = self.get("/standard-time").get_data(as_text=True)
        for part in ("1梱包あたりの人分 = 作業時間 × 作業人数 ÷ 梱包数",
                     "標準作業時間(分) = 標準(人分/梱包) × 梱包数 ÷ 作業人数",
                     "梱包力 = 標準人分の合計 ÷ 実人分の合計 × 100",
                     "<b>37.5 人分/梱包</b>", "<b>75分</b>", "<b>115</b>", "<b>83</b>",
                     "<b>97</b>", "作業人数が0"):
            self.assertIn(part, page)

    def test_根拠と_このツールで決めたことを分けて書く(self) -> None:
        page = self.get("/standard-time").get_data(as_text=True)
        basis = page[page.index('id="guide-basis"'):page.index('id="guide-limits"')]
        for word in ("JIS Z 8141", "実績資料法", "レイティング", "作業能率", "性能稼働率"):
            self.assertIn(word, basis)
        self.assertIn("このツールで決めたこと", basis)
        limits = page[page.index('id="guide-limits"'):]
        self.assertIn("あるべき速さ", limits)
        self.assertIn("停止は入っていません", limits)

    def test_しくみから飛べる(self) -> None:
        page = self.get("/standard-time").get_data(as_text=True)
        self.assertIn('href="#guide-power" data-goto-tab="guide"', page)
        self.assertIn('href="#guide-calc" data-goto-tab="guide"', page)
        js = (ROOT / "app" / "static" / "js" / "views" / "standard_time.js").read_text(
            encoding="utf-8")
        self.assertIn("a[data-goto-tab]", js)
        self.assertIn("scrollIntoView", js)


if __name__ == "__main__":
    unittest.main()
