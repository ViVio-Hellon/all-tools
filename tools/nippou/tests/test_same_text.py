"""終了時刻の引き継ぎ (`Same_Text`) ── **直の終わりでだけ止める**

    直の終了時間が入った場合だけ止めるんですよ
    15:00 / 22:50 / 07:00  現場dbだとこの3つです

行の終了時刻を打ち終えると、その値が次の行の開始時刻へ入ります。
前の作業が終わった時刻から次の作業が始まるので、打つ手が省けます。

**止めるのは、打たれた終了時刻が直の終わりの時刻だったときだけ**です。
そこで直が終わったのなら次の行に続きは無く、写せば「終わった時刻」が
次の行の開始として残り、打つ人がそれを消すところから始めることに
なります。

【VBA は1つしか見ていなかった】
`Same_Text` は「いま何直か」に当たる終了時刻1つとしか比べません。
1直で 22:50 と打てば写ってしまいます。直の変わり目をまたいで打って
いるときは画面の直と打っている時刻が食い違うので、**3つとも**見ます。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import navigation

#: 現場の終了時刻。伝送用ファイルの「時間用」表から来る値
SITE = {"1直": ("15", "00"), "2直": ("22", "50"), "3直": ("07", "00"),
        "日勤": ("17", "00")}


def carry(sz: str, sh: str, *, row: int = 1, shift: str = "1直"):
    return navigation.same_text(row=row, sz_text=sz, sh_text=sh,
                                day_temp=shift, shift_end_times=SITE)


class CarryTests(unittest.TestCase):
    """ふつうの行 ── 写します。"""

    def test_終了時刻は次の行の開始へ写る(self) -> None:
        self.assertEqual(carry("10", "00"), ("10", "00"))

    def test_最後の行からは写さない(self) -> None:
        """13行目はありません。"""
        self.assertIsNone(carry("10", "00", row=12))

    def test_分が2桁そろうまで写さない(self) -> None:
        """`1` と打った時点で 01分 として写ると、打ち終える前に動きます。"""
        self.assertIsNone(carry("10", "0"))
        self.assertIsNone(carry("10", ""))

    def test_時が空なら写さない(self) -> None:
        self.assertIsNone(carry("", "00"))


class ShiftEndTests(unittest.TestCase):
    """**直の終わりの時刻を打ったら写さない。** ここが本題。"""

    def test_現場の3つで止まる(self) -> None:
        for sz, sh in (("15", "00"), ("22", "50"), ("07", "00")):
            with self.subTest(終了=f"{sz}:{sh}"):
                self.assertIsNone(carry(sz, sh))

    def test_打っている直と違う直の終わりでも止まる(self) -> None:
        """**VBA はここが抜けていました。**

        1直の画面で 22:50 と打つのは、直の変わり目をまたいで打って
        いるときに起きます(画面の直は打ち始めたときのまま)。
        """
        self.assertIsNone(carry("22", "50", shift="1直"))
        self.assertIsNone(carry("07", "00", shift="1直"))
        self.assertIsNone(carry("15", "00", shift="3直"))

    def test_1分違えば写る(self) -> None:
        """止めるのは**ちょうどその時刻**のときだけ。"""
        self.assertEqual(carry("14", "59"), ("14", "59"))
        self.assertEqual(carry("15", "01"), ("15", "01"))
        self.assertEqual(carry("22", "51"), ("22", "51"))

    def test_最後の行はどちらにしても写さない(self) -> None:
        self.assertIsNone(carry("15", "00", row=12))

    def test_時刻表が空なら普通に写る(self) -> None:
        """マスタを取り込む前でも、引き継ぎだけは動きます。"""
        self.assertEqual(
            navigation.same_text(row=1, sz_text="15", sh_text="00",
                                 day_temp="1直", shift_end_times={}),
            ("15", "00"))


class CarryBackTests(unittest.TestCase):
    """**写したぶんの後始末** ── `carry_decision`。

        14:59 と打って写ったあと 15:00 に直しても、2行目には 14:59 が残る

    消してよいのはツールが入れた値だけ。人が打った値は消しません。
    """

    def decide(self, sz, sh, *, row=1, shift="1直", marks=()):
        return navigation.carry_decision(
            row=row, sz_text=sz, sh_text=sh, day_temp=shift,
            shift_end_times=SITE, carried_rows=set(marks))

    def test_写すときは写す先と値を返す(self) -> None:
        got = self.decide("10", "00")
        self.assertTrue(got.writes)
        self.assertEqual((got.row, got.kz, got.kh), (2, "10", "00"))

    def test_直の終わりに直したら写したぶんを引っ込める(self) -> None:
        """14:59 → 15:00 に直した場面。**ここが本題。**"""
        got = self.decide("15", "00", marks=[2])
        self.assertTrue(got.clears)
        self.assertEqual(got.row, 2)

    def test_人が打った値は消さない(self) -> None:
        """印が無い = 人が打った、として扱う。**迷ったら消さない。**"""
        self.assertEqual(self.decide("15", "00").action, navigation.CARRY_NONE)

    def test_終了時刻を消しても引っ込める(self) -> None:
        """打ち間違えて終了時刻ごと消した場面。下に残っては困ります。"""
        self.assertTrue(self.decide("10", "", marks=[2]).clears)
        self.assertTrue(self.decide("", "00", marks=[2]).clears)

    def test_写し直すときは消さずに上書き(self) -> None:
        """10:00 → 10:30 は写し直し。消してから書く必要はありません。"""
        got = self.decide("10", "30", marks=[2])
        self.assertTrue(got.writes)
        self.assertEqual((got.row, got.kz, got.kh), (2, "10", "30"))

    def test_別の行の印は関係ない(self) -> None:
        self.assertEqual(self.decide("15", "00", marks=[3, 5]).action,
                         navigation.CARRY_NONE)

    def test_12行目には13行目が無い(self) -> None:
        """印が付いていても、消す先がありません。"""
        self.assertEqual(self.decide("15", "00", row=12, marks=[13]).action,
                         navigation.CARRY_NONE)

    def test_打っている直と違う直の終わりでも引っ込める(self) -> None:
        self.assertTrue(self.decide("22", "50", shift="1直", marks=[2]).clears)


class DayShiftTests(unittest.TestCase):
    """日勤は保留 ── VBA の 18:00 の分岐はそのまま残してあります。"""

    def test_日勤の18時は写さない(self) -> None:
        self.assertIsNone(carry("18", "00", shift="日勤"))

    def test_1直の18時は写る(self) -> None:
        self.assertEqual(carry("18", "00", shift="1直"), ("18", "00"))


if __name__ == "__main__":
    unittest.main()
