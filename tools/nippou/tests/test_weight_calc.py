"""単重から作業重量を出す (VBA ``Weight計算`` / ``CalculateWeights``)

打った値をそのままシートへ出すのではなく、**フォームの上で計算と変換を
済ませてから**出していました。重量まわりの順は

    実績合計 枚数 = 個装単位 枚数 × 梱包単位 包数
    単重が空の欄は、上の行までさかのぼっていちばん近い単重を使う
    実績合計 重量 = 実績合計 枚数 × その単重  (すでに値があれば触らない)
    直の合計 = 枚数の総和 / 重量の総和
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import calculations                          # noqa: E402


class PackageTotalTests(unittest.TestCase):
    """実績合計 枚数 = 個装単位 枚数 × 梱包単位 包数。"""

    def test_かけ算(self) -> None:
        self.assertEqual(calculations.package_total("3", "1"), "3")
        self.assertEqual(calculations.package_total("12", "4"), "48")

    def test_0なら空(self) -> None:
        """VBA は `If .Controls("CON" & i) = "0" Then ... = ""`。"""
        self.assertEqual(calculations.package_total("0", "5"), "")
        self.assertEqual(calculations.package_total("5", "0"), "")

    def test_空欄は0扱い(self) -> None:
        """VBA は `val()` を通すので、数字でない欄は 0。"""
        self.assertEqual(calculations.package_total("", "3"), "")
        self.assertEqual(calculations.package_total("abc", "3"), "")

    def test_整数に丸める(self) -> None:
        self.assertEqual(calculations.package_total("2.5", "2"), "5")


class PreviousUnitWeightTests(unittest.TestCase):
    """単重をさかのぼって探す (``FindPreviousUnitWeight``)。

    同じロットを何行にも分けて打つとき、単重は先頭の行にしか入らない。
    """

    def test_自分の行にあればそれ(self) -> None:
        rows = {1: {"UNI": "250.04"}, 2: {"UNI": "100"}}
        self.assertAlmostEqual(
            calculations.previous_unit_weight(rows, 2), 100.0)

    def test_空なら上へさかのぼる(self) -> None:
        rows = {1: {"UNI": "250.04"}, 2: {"UNI": ""}, 3: {"UNI": ""}}
        self.assertAlmostEqual(
            calculations.previous_unit_weight(rows, 3), 250.04)

    def test_見つからなければ0(self) -> None:
        rows = {1: {"UNI": ""}, 2: {"UNI": ""}}
        self.assertEqual(calculations.previous_unit_weight(rows, 2), 0.0)

    def test_下の行は見ない(self) -> None:
        rows = {1: {"UNI": ""}, 2: {"UNI": "999"}}
        self.assertEqual(calculations.previous_unit_weight(rows, 1), 0.0)


class WorkWeightTests(unittest.TestCase):
    """実績合計 重量 = 実績合計 枚数 × 単重。"""

    def test_かけ算(self) -> None:
        self.assertEqual(calculations.work_weight(
            con="3", unit=250.04314, mai="3", tut="1", wei=""), "750.1")

    def test_すでに入っていれば触らない(self) -> None:
        """現物を量って入れた値のほうが正しい。"""
        self.assertEqual(calculations.work_weight(
            con="3", unit=250.0, mai="3", tut="1", wei="800.0"), "800.0")

    def test_ゼロ埋めは空とみなす(self) -> None:
        """VBA は先に `If .Controls("WEI" & i) = "0.0" Then ... = ""`。"""
        self.assertEqual(calculations.work_weight(
            con="3", unit=100.0, mai="3", tut="1", wei="0.0"), "300.0")

    def test_単重が無ければ出さない(self) -> None:
        self.assertEqual(calculations.work_weight(
            con="3", unit=0.0, mai="3", tut="1", wei=""), "")

    def test_枚数か包数が空なら出さない(self) -> None:
        self.assertEqual(calculations.work_weight(
            con="3", unit=100.0, mai="", tut="1", wei=""), "")
        self.assertEqual(calculations.work_weight(
            con="3", unit=100.0, mai="3", tut="", wei=""), "")


class TotalsTests(unittest.TestCase):
    """直の合計 (``AlCount`` / ``AlWeight``)。"""

    def test_足す(self) -> None:
        rows = {1: {"CON": "3", "WEI": "750.1"},
                2: {"CON": "1", "WEI": "1626.9"},
                3: {"CON": "2", "WEI": "1001.2"}}
        self.assertEqual(calculations.totals(rows), ("6", "3378.2"))

    def test_空欄は0として足す(self) -> None:
        rows = {1: {"CON": "3", "WEI": "750.1"}, 2: {"CON": "", "WEI": ""}}
        self.assertEqual(calculations.totals(rows), ("3", "750.1"))

    def test_何も無ければ空(self) -> None:
        self.assertEqual(calculations.totals({1: {"CON": "", "WEI": ""}}),
                         ("", ""))


class ApplyWeightsTests(unittest.TestCase):
    """1画面ぶんをまとめて当てる。"""

    def test_紙の実例(self) -> None:
        """提出された印刷フォーマットの1行目と同じ数字になるか。

        L8=3(個装単位 枚数) M8=1(梱包単位 包数) → V8=3(実績合計 枚数)
        Y8=250.04314(単重)                     → W8=750.1(実績合計 重量)
        """
        rows = {1: {"MAI": "3", "TUT": "1", "UNI": "250.04314", "WEI": ""}}
        count, weight = calculations.apply_weights(rows, row_count=1)
        self.assertEqual(rows[1]["CON"], "3")
        self.assertEqual(rows[1]["WEI"], "750.1")
        self.assertEqual((count, weight), ("3", "750.1"))

    def test_単重をさかのぼって分けた行にも当てる(self) -> None:
        """**分けて打った日だけ合計が合わない、を作らない。**"""
        rows = {
            1: {"MAI": "1", "TUT": "1", "UNI": "1001.15495", "WEI": ""},
            2: {"MAI": "1", "TUT": "1", "UNI": "", "WEI": ""},
            3: {"MAI": "1", "TUT": "1", "UNI": "", "WEI": ""},
        }
        count, weight = calculations.apply_weights(rows, row_count=3)
        self.assertEqual(rows[2]["WEI"], "1001.2")
        self.assertEqual(rows[3]["WEI"], "1001.2")
        self.assertEqual(count, "3")

    def test_枚数を先に全部決めてから重量を出す(self) -> None:
        """1周で回すと、まだ決まっていない枚数を使ってしまう。"""
        rows = {
            1: {"MAI": "2", "TUT": "3", "UNI": "10", "WEI": ""},
            2: {"MAI": "4", "TUT": "5", "UNI": "", "WEI": ""},
        }
        calculations.apply_weights(rows, row_count=2)
        self.assertEqual(rows[1]["CON"], "6")
        self.assertEqual(rows[2]["CON"], "20")
        self.assertEqual(rows[2]["WEI"], "200.0")   # 20 × 10

    def test_空の行は空のまま(self) -> None:
        rows = {1: {}, 2: {}}
        count, weight = calculations.apply_weights(rows, row_count=2)
        self.assertEqual(rows[1]["CON"], "")
        self.assertEqual(rows[1]["WEI"], "")
        self.assertEqual((count, weight), ("", ""))

    def test_量った重量は残す(self) -> None:
        rows = {1: {"MAI": "3", "TUT": "1", "UNI": "250", "WEI": "755.5"}}
        calculations.apply_weights(rows, row_count=1)
        self.assertEqual(rows[1]["WEI"], "755.5")


class AskBeforeRedoingTests(unittest.TestCase):
    """枚数を直したのに重量が動かない ── **聞いてから直す。**

    【VBAはここで何もしませんでした】
    `Weight計算` は重量が入っている行を触りません。紙に「現物を量った
    重量」を書くことがあるので、勝手に上書きしない作りでした。

    ところが現場で多いのは**打ち間違えた枚数を直す**ほうです。直しても
    重量は前のまま ── 枚数と重量が食い違ったまま保存でき、食い違いは
    画面に出ないので集計まで通ります。

    かといって黙って上書きすると、量った重量が消えます。どちらも黙って
    やるには重すぎるので**聞きます。** ここが守るのは「いつ聞くか」です
    ── 聞きすぎると読まずに押す癖が付き、本当に要るときに効きません。
    """

    def rows(self, **over):
        values = {"MAI": "10", "TUT": "2", "UNI": "10", "WEI": "200.0"}
        values.update(over)
        return {1: values}

    def test_枚数を直したら聞く(self) -> None:
        """**ここが本体。** 20枚 → 30枚 なら、重量も 300.0 のはず。"""
        rows = self.rows(MAI="15")            # 15 × 2 = 30枚
        self.assertEqual(
            calculations.weight_needs_asking(rows, 1, "MAI"), "300.0")

    def test_包数を直しても聞く(self) -> None:
        rows = self.rows(TUT="3")             # 10 × 3 = 30枚
        self.assertEqual(
            calculations.weight_needs_asking(rows, 1, "TUT"), "300.0")

    def test_関係ない欄を直したときは聞かない(self) -> None:
        """LOTNOや時刻を直すたびに聞かれては、読まずに押すようになります。"""
        for family in ("LOT", "KZ", "SZ", "UNI", "WEI", ""):
            with self.subTest(family=family):
                self.assertEqual(
                    calculations.weight_needs_asking(self.rows(MAI="15"), 1,
                                                     family), "")

    def test_重量が空なら聞かない(self) -> None:
        """空ならふつうに `apply_weights` が入れます。**聞く必要が無い。**"""
        self.assertEqual(
            calculations.weight_needs_asking(self.rows(WEI=""), 1, "MAI"), "")
        # VBA が空として扱っていた "0.0" も同じ
        self.assertEqual(
            calculations.weight_needs_asking(self.rows(WEI="0.0"), 1, "MAI"),
            "")

    def test_同じ値になるなら聞かない(self) -> None:
        """打ち直して元に戻した、など。**変わらないのに聞かない。**"""
        self.assertEqual(
            calculations.weight_needs_asking(self.rows(), 1, "MAI"), "")

    def test_単重が無ければ聞かない(self) -> None:
        """計算し直しようがありません(量った値しか無い)。"""
        rows = self.rows(MAI="15", UNI="")
        self.assertEqual(calculations.weight_needs_asking(rows, 1, "MAI"), "")

    def test_はいと答えたときの値(self) -> None:
        """入っている重量を**無視して**出した値であること。"""
        rows = self.rows(MAI="15")
        self.assertEqual(calculations.weight_if_recalculated(rows, 1), "300.0")
        # 覗いただけで書き換えない
        self.assertEqual(rows[1]["WEI"], "200.0")

    def test_単重は上の行からさかのぼる(self) -> None:
        """同じロットを何行にも分けて打つとき、単重は先頭の行にしか
        入りません(`FindPreviousUnitWeight`)。聞く値もそれに従う。"""
        rows = {1: {"MAI": "10", "TUT": "1", "UNI": "10", "WEI": "100.0"},
                2: {"MAI": "5", "TUT": "2", "WEI": "60.0"}}
        self.assertEqual(
            calculations.weight_needs_asking(rows, 2, "MAI"), "100.0")

    def test_はいで入れ直せる(self) -> None:
        """画面は「はい」で重量を空にして送り直します ── そのあと
        `apply_weights` が入れるところまで。"""
        rows = self.rows(MAI="15")
        rows[1]["WEI"] = ""                   # 「はい」で空にした状態
        calculations.apply_weights(rows, row_count=1)
        self.assertEqual(rows[1]["WEI"], "300.0")


if __name__ == "__main__":
    unittest.main()
