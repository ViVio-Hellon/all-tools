"""「時:分」を分に直す読み方を1つに(段階2・案1)

前は4通りありました(直の境界 `shift.parse_hhmm`・時間の割り振り `time_split.parse_hhmm`・
過去日報の取り込み `nippou_sheet._minutes`・休憩の窓 `break_time.shift_window`)。
全角・秒付き・25:00 の扱いが少しずつ違い、同じ直の時刻を道ごとに違って読み得ました。
どれも直の境界と同じ読み方(`shift.normalize_hhmm`: 全角も読む・秒は捨てる・0〜23時/0〜59分)にします。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import break_time, nippou_sheet, shift, time_split  # noqa: E402


class HhmmMinutesTests(unittest.TestCase):
    def test_分に直す(self) -> None:
        cases = {"07:00": 420, "7:00": 420, "22:50": 1370, "０７：００": 420,
                 "07:00:00": 420, " 15:00 ": 900, "00:00": 0, "23:59": 1439}
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(shift.hhmm_minutes(text), want)

    def test_読めなければ無い(self) -> None:
        for text in ("", None, "7時", "25:00", "07:60", "7", "a:b", "07:00:00:00"):
            with self.subTest(text=text):
                self.assertIsNone(shift.hhmm_minutes(text))


class SameReadingTests(unittest.TestCase):
    """使う所が同じ読み方をする(いつもの値は前と同じ答え)。"""

    def test_時間の割り振り(self) -> None:
        self.assertEqual(time_split.parse_hhmm("07:00"), 420)
        self.assertEqual(time_split.parse_hhmm("22:50"), 1370)
        self.assertIsNone(time_split.parse_hhmm("24:00"))
        self.assertIsNone(time_split.parse_hhmm(""))
        self.assertEqual(time_split.parse_hhmm("０７：００"), 420)      # 前は読めなかった

    def test_過去日報の取り込みの直の始まり(self) -> None:
        self.assertEqual(nippou_sheet.shift_starts({"1": ("07:00", "15:00"), "2": ("15:00", "22:50"),
                                                    "3": ("22:50", "07:00")}),
                         {"1直": 420, "2直": 900, "3直": 1370})
        self.assertEqual(nippou_sheet.shift_starts(None), {"1直": 420, "2直": 900, "3直": 1370})

    def test_休憩の窓(self) -> None:
        self.assertEqual(break_time.shift_window("07:00", "15:00"), (420, 900))
        self.assertEqual(break_time.shift_window("22:50", "07:00"), (1370, 1860))
        self.assertEqual(break_time.shift_window("０７：００", "15:00"), (420, 900))
        self.assertIsNone(break_time.shift_window("07:00", "07:00"))
        self.assertIsNone(break_time.shift_window("", "15:00"))
        self.assertIsNone(break_time.shift_window("25:00", "15:00"))   # 前は 1500 分と読んでいた


if __name__ == "__main__":
    unittest.main()
