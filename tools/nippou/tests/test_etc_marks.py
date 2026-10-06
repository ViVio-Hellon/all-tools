"""etc欄の押しボタン (VBA SPCommand〜MICommand + CommandLook_For)

【なにを守っているか】
紙の etc 欄(O列「反転・EX etc」)に何が入るかは、6つの押しボタンで
決まっていました。**ボタンは状態を持ちません** ── 値は etc 欄の文字
そのもので、行を移るたびに `CommandLook_For` がそこから読み直して
ボタンを塗り直していました。

ここが守るのは、その「文字が唯一の出どころ」であることと、
自動で押される2つ(EX・外注)の判定です。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import etc_marks  # noqa: E402


class MarkListTests(unittest.TestCase):
    def test_6つある(self) -> None:
        """写真のボタンと同じ6つ。増やすときは `MARKS` に1行足す。"""
        self.assertEqual([m.key for m in etc_marks.MARKS],
                         ["store", "ear", "outsource", "ex", "flip", "spot"])

    def test_書き込む文字はVBAのまま(self) -> None:
        """**紙に出る文字が変わらない。** 集計はこの文字を探して数える。"""
        self.assertEqual({m.key: m.text for m in etc_marks.MARKS}, {
            "store": "ストア", "ear": "耳付き", "outsource": "外注出荷",
            "ex": "EX", "flip": "反転作業", "spot": "スポット有"})

    def test_自動で押せるのは2つだけ(self) -> None:
        """残り4つは元データから決められない。**勝手に押さない。**"""
        self.assertEqual([m.key for m in etc_marks.MARKS if m.auto],
                         ["outsource", "ex"])


class ToggleTests(unittest.TestCase):
    """押すたびに入り切りする(VBA の `BackColor` 分岐)。"""

    def test_押すと書き足す(self) -> None:
        self.assertEqual(etc_marks.toggle("", "store"), "ストア")

    def test_もう一度押すと消える(self) -> None:
        self.assertEqual(etc_marks.toggle("ストア", "store"), "")

    def test_空白で区切って足す(self) -> None:
        """VBA も `ET & " " & 印` と空白を挟んでいた。"""
        self.assertEqual(etc_marks.toggle("ストア", "ex"), "ストア EX")

    def test_二重には押さない(self) -> None:
        self.assertEqual(etc_marks.add("ストア", "store"), "ストア")

    def test_消しても残りはくっつかない(self) -> None:
        """VBA は消したあと空白を全部消していたので、残った印が
        くっついていた(「スポット有ストア」)。ここは1つに詰める。"""
        text = etc_marks.toggle("スポット有 ストア EX", "store")
        self.assertEqual(text, "スポット有 EX")

    def test_知らない印は何もしない(self) -> None:
        self.assertEqual(etc_marks.toggle("ストア", "nope"), "ストア")

    def test_ロットが埋めた文字は消さない(self) -> None:
        """etc には包装仕様から来た文字も入る。印だけを抜く。"""
        text = etc_marks.toggle("板 外装紙点止め EX", "ex")
        self.assertEqual(text, "板 外装紙点止め")


class ActiveTests(unittest.TestCase):
    """VBA `CommandLook_For`。**etc の文字を読むだけ。**"""

    def test_入っている印を読む(self) -> None:
        self.assertEqual(etc_marks.active("外注出荷 EX"), ["outsource", "ex"])

    def test_空なら何も押されていない(self) -> None:
        self.assertEqual(etc_marks.active(""), [])
        self.assertEqual(etc_marks.active(None), [])

    def test_並びはボタンの並び(self) -> None:
        """押した順ではない。**ボタンの位置は動かない。**"""
        self.assertEqual(etc_marks.active("EX ストア"), ["store", "ex"])


class AutoTests(unittest.TestCase):
    """自動で押される2つ。"""

    def test_受注番号の3桁目が1ならEX(self) -> None:
        """VBA `If Mid(NowOder, 3, 1) = 1 Then Call EXCommand`。"""
        self.assertTrue(etc_marks.is_ex_order("AB1234"))
        self.assertFalse(etc_marks.is_ex_order("AB2234"))

    def test_3桁に足りなければ判定しない(self) -> None:
        for text in ("", "A", "AB", None):
            with self.subTest(text=text):
                self.assertFalse(etc_marks.is_ex_order(text))

    def test_輸出区分が1ならEX(self) -> None:
        self.assertTrue(etc_marks.is_ex_export("1"))
        self.assertTrue(etc_marks.is_ex_export(" 1 "))

    def test_輸出区分が別の値なら押さない(self) -> None:
        """python-web-tools は「空でなければ輸出」と読む。ここは現場の
        言う「1のとき」に合わせる ── 別の値が入り始めたときに、
        勝手に輸出扱いしないため。"""
        for text in ("", "0", "2", "X", None):
            with self.subTest(text=text):
                self.assertFalse(etc_marks.is_ex_export(text))

    def test_設備コースに3つのどれかが入れば外注(self) -> None:
        for course in ("GFS", "GCT", "GSS"):
            with self.subTest(course=course):
                self.assertTrue(etc_marks.is_outsourced(course))

    def test_部分一致で見る(self) -> None:
        """設備コースは「A-GFS-B」のように前後が付く。"""
        self.assertTrue(etc_marks.is_outsourced("A-GFS-B"))

    def test_ほかのコースは外注にしない(self) -> None:
        for course in ("", "GAA", "BOX", None):
            with self.subTest(course=course):
                self.assertFalse(etc_marks.is_outsourced(course))

    def test_両方成り立てば2つ押す(self) -> None:
        keys = etc_marks.auto_keys(order_no="AB1234", course="GFS")
        self.assertEqual(keys, ["outsource", "ex"])

    def test_自動は足すだけで消さない(self) -> None:
        """作業者が消した印を次の計算で戻すと、消せない印になる。"""
        self.assertEqual(etc_marks.apply_auto("ストア", ["ex"]), "ストア EX")
        self.assertEqual(etc_marks.apply_auto("EX", []), "EX")


if __name__ == "__main__":
    unittest.main()
