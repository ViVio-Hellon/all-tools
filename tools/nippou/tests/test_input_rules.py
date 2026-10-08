"""欄ごとの入力制限 (VBA ``_KeyPress`` + ``MoveText``)

「数字のみ」「2桁入力」が効いていない、という指摘に対して入れたもの。
規則の出どころは `constants.FOCUS_CHAIN` / `TEXT_LIKE_FAMILIES` の1か所で、
`logic/input_rules.py` はそれを画面が読める形に翻訳するだけ ── ここが
守っているのは「翻訳がずれていないこと」。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou import constants                                    # noqa: E402
from nippou.logic import input_rules                            # noqa: E402


class CharsetTests(unittest.TestCase):
    """どの欄に何が打てるか。"""

    def test_ロットは英数字だけで大文字になる(self) -> None:
        """VBA ``LOT*_KeyPress``: 0-9 / A-Z を通し、a-z は大文字へ。"""
        rule = input_rules.rule("LOT")
        self.assertEqual(rule.charset, input_rules.CHARSET_ALNUM_UPPER)
        self.assertTrue(rule.uppercase)

    def test_数字のみの欄(self) -> None:
        """``MoveText`` が「数字でなければ空にする」欄。"""
        for family in ("KEN", "KZ", "KH", "SZ", "SH", "HIT",
                       "MAI", "TUT", "TH", "THS", "THT"):
            with self.subTest(family=family):
                self.assertEqual(input_rules.rule(family).charset,
                                 input_rules.CHARSET_DIGITS)

    def test_自由に打てる欄(self) -> None:
        """``MoveText`` の2つめの Select Case に並んでいた欄(LOTを除く)。"""
        for family in ("ZAI", "SIZ", "VC", "ET", "S", "SS", "STH"):
            with self.subTest(family=family):
                self.assertEqual(input_rules.rule(family).charset,
                                 input_rules.CHARSET_ANY)

    def test_文字種の割り当ては定数と一致する(self) -> None:
        """`TEXT_LIKE_FAMILIES` を直したら、こちらも一緒に動く。"""
        for family in constants.FOCUS_CHAIN:
            rule = input_rules.rule(family)
            text_like = family in constants.TEXT_LIKE_FAMILIES
            if family == "LOT":
                continue                       # KeyPress で別に絞っている
            self.assertEqual(rule.charset == input_rules.CHARSET_ANY, text_like,
                             f"{family} の文字種が定数と食い違っています")


class AdvanceTests(unittest.TestCase):
    """何文字入れたら次の欄へ飛ぶか(``Mcount``)。"""

    def test_時分は2桁で飛ぶ(self) -> None:
        for family, nxt in (("KZ", "KH"), ("KH", "SZ"), ("SZ", "SH"), ("SH", "HIT")):
            with self.subTest(family=family):
                rule = input_rules.rule(family)
                self.assertEqual(rule.advance_at, 2)
                self.assertEqual(rule.next_family, nxt)
                self.assertTrue(rule.advances)

    def test_作業人数は1桁で飛ぶ(self) -> None:
        rule = input_rules.rule("HIT")
        self.assertEqual(rule.advance_at, 1)
        self.assertEqual(rule.next_family, "MAI")

    def test_停止時間は3桁で飛ぶ(self) -> None:
        for family in ("TH", "THS", "THT"):
            with self.subTest(family=family):
                self.assertEqual(input_rules.rule(family).advance_at, 3)

    def test_最後の欄からは飛ばない(self) -> None:
        """``MoveText`` は最後の停止時間(VBA は THT、v4.24.0 からは TH5)で SetFocus しない。"""
        rule = input_rules.rule("TH5")
        self.assertIsNone(rule.next_family)
        self.assertFalse(rule.advances)
        # ③の時間を3桁打てば④の記号へ(①→②→③と同じ)
        self.assertEqual(input_rules.rule("THT").next_family, "S4")

    def test_自由入力の欄は飛ばない(self) -> None:
        """VBA の既定 ``Mcount = 20`` は「飛ばない」の意味。"""
        for family in ("LOT", "ZAI", "SIZ", "VC", "ET", "S", "SS", "STH", "MAI", "TUT"):
            with self.subTest(family=family):
                rule = input_rules.rule(family)
                self.assertEqual(rule.advance_at, input_rules.NO_ADVANCE_LEN)
                self.assertFalse(rule.advances)


class MaxLengthTests(unittest.TestCase):
    """打ち込める上限。"""

    def test_短い欄だけ上限を付ける(self) -> None:
        self.assertEqual(input_rules.rule("KZ").max_length, 2)
        self.assertEqual(input_rules.rule("HIT").max_length, 1)
        self.assertEqual(input_rules.rule("TH").max_length, 3)

    def test_長い欄に上限は付けない(self) -> None:
        """20 は「飛ばない」であって「20文字まで」ではない。"""
        for family in ("ZAI", "SIZ", "MAI"):
            with self.subTest(family=family):
                self.assertIsNone(input_rules.rule(family).max_length)

    def test_ロットは7桁(self) -> None:
        """**飛ばないが、上限はある。**

        現物(`LS4LOT` の仕掛)は `N7131T0` `L715C50` と7文字ちょうど。
        VBA は文字の種類だけを絞って長さを見ておらず、8桁打ててしまう
        のにロット検索は7桁で走る、という食い違いがあった。
        """
        self.assertEqual(input_rules.rule("LOT").max_length, 7)
        # 7桁で次の欄へ飛ばしはしない(打ち直しに戻れなくなる)
        self.assertFalse(input_rules.rule("LOT").advances)


class AttributeTests(unittest.TestCase):
    """画面へ渡す形。テンプレートはこれをそのまま `<input>` に付ける。"""

    def test_時の欄(self) -> None:
        attrs = input_rules.as_attributes("KZ")
        self.assertEqual(attrs["data-charset"], "digits")
        self.assertEqual(attrs["maxlength"], "2")
        self.assertEqual(attrs["data-advance-at"], "2")
        self.assertEqual(attrs["data-next"], "KH")
        self.assertEqual(attrs["inputmode"], "numeric")

    def test_ロットの欄(self) -> None:
        attrs = input_rules.as_attributes("LOT")
        self.assertEqual(attrs["data-charset"], "alnum_upper")
        self.assertEqual(attrs["maxlength"], "7")
        self.assertNotIn("data-advance-at", attrs)
        # 英数字は数字キーボードにしない(A-Z も打つ)
        self.assertNotIn("inputmode", attrs)

    def test_自由入力の欄には数字キーボードを出さない(self) -> None:
        self.assertNotIn("inputmode", input_rules.as_attributes("VC"))

    def test_知らない欄には何も付けない(self) -> None:
        # 合紙(AI)は選ぶ欄なので、打つ制限は持たない
        self.assertEqual(input_rules.as_attributes("AI"), {})
        self.assertIsNone(input_rules.rule("AI"))


class AllowsTests(unittest.TestCase):
    """サーバ側でも同じ規則で見る(貼り付け・IME は画面を通り抜ける)。"""

    def test_数字の欄(self) -> None:
        self.assertTrue(input_rules.allows("KEN", "123"))
        self.assertTrue(input_rules.allows("KEN", ""))        # 空はいつでも可
        self.assertTrue(input_rules.allows("WEI", "1.5"))     # IsNumeric は小数も通す
        self.assertFalse(input_rules.allows("KEN", "12a3"))

    def test_ロットの欄(self) -> None:
        self.assertTrue(input_rules.allows("LOT", "H5422S0"))
        self.assertFalse(input_rules.allows("LOT", "H54-22"))
        self.assertFalse(input_rules.allows("LOT", "Ｈ５４"))   # 全角は通さない

    def test_自由入力の欄は何でも通す(self) -> None:
        self.assertTrue(input_rules.allows("VC", "A:VE-20NF_B:VE-20NF"))


if __name__ == "__main__":
    unittest.main()
