"""作業時間の計算 (VBA ``時間計算``)

VBA がシートへ直接打たせずフォームを挟んでいたのは、**入力した時点で
計算と変換を済ませてから出力するため**でした。時間まわりの順は

    時は 00〜23 / 分は 00〜59 か  →  開始と終了から作業時間  →
    停止時間があれば引く

で、どこかで引っかかったら `Exit Sub` して**何も書きません**。
ここが守っているのはその順と、断る4つの場面です。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import work_time                             # noqa: E402


def rows(**by_row) -> dict[int, dict[str, str]]:
    """`rows(1={"KZ": "08", ...})` と書けるようにする。"""
    return {int(k.lstrip("r")): v for k, v in by_row.items()}


class RangeTests(unittest.TestCase):
    """時は 00〜23、分は 00〜59。"""

    def test_通る時刻(self) -> None:
        data = rows(r1={"KZ": "00", "KH": "00", "SZ": "23", "SH": "59"})
        self.assertIsNone(work_time.check_ranges(data))

    def test_時が24以上なら断る(self) -> None:
        problem = work_time.check_ranges(rows(r1={"KZ": "24"}))
        self.assertIsNotNone(problem)
        self.assertEqual(problem.reason, work_time.REFUSE_RANGE)
        self.assertEqual(problem.field, "KZ")
        self.assertIn("1行目", problem.message)
        self.assertIn("開始時間（時）", problem.message)

    def test_分が60以上なら断る(self) -> None:
        problem = work_time.check_ranges(rows(r3={"SH": "60"}))
        self.assertEqual(problem.row, 3)
        self.assertEqual(problem.field, "SH")
        self.assertIn("終了時間（分）", problem.message)
        # 分のときだけ範囲を書き添える(VBA と同じ)
        self.assertIn("0～59", problem.message)

    def test_数字でなければ断る(self) -> None:
        problem = work_time.check_ranges(rows(r1={"KH": "ab"}))
        self.assertEqual(problem.reason, work_time.REFUSE_RANGE)

    def test_空欄は通す(self) -> None:
        """打っている途中を断らない。"""
        self.assertIsNone(work_time.check_ranges(rows(r1={"KZ": "", "KH": ""})))

    def test_いちばん上の行から断る(self) -> None:
        """12行ぶんまとめて出すと、どれから直せばよいか分からない。"""
        problem = work_time.check_ranges(
            rows(r5={"KZ": "99"}, r2={"KZ": "88"}))
        self.assertEqual(problem.row, 2)


class ElapsedTests(unittest.TestCase):
    """開始から終了までの分。"""

    def test_ふつうの計算(self) -> None:
        self.assertEqual(work_time.elapsed_minutes("22", "50", "00", "30"), 100)
        self.assertEqual(work_time.elapsed_minutes("07", "00", "15", "00"), 480)

    def test_日をまたぐ(self) -> None:
        """3直は夜から翌朝へまたぐ。"""
        self.assertEqual(work_time.elapsed_minutes("23", "00", "01", "00"), 120)

    def test_1桁でも読める(self) -> None:
        self.assertEqual(work_time.elapsed_minutes("2", "0", "4", "10"), 130)


class ShiftLimitTests(unittest.TestCase):
    """直の規定時間 (``GetShiftLimitMin`` / ``CalcShiftMinutes``)。"""

    TIMES = {"1": ("07:00", "15:00"), "2": ("15:00", "22:50"),
             "3": ("22:50", "07:00"), "昼": ("08:15", "17:00")}

    def test_直の長さ(self) -> None:
        self.assertEqual(work_time.shift_minutes("07:00", "15:00"), 480)
        self.assertEqual(work_time.shift_minutes("15:00", "22:50"), 470)

    def test_夜勤はまたぐ(self) -> None:
        self.assertEqual(work_time.shift_minutes("22:50", "07:00"), 490)

    def test_いまの直の長さを使う(self) -> None:
        self.assertEqual(work_time.shift_limit(self.TIMES, "1直"), 480)
        self.assertEqual(work_time.shift_limit(self.TIMES, "2直"), 470)
        self.assertEqual(work_time.shift_limit(self.TIMES, "3直"), 490)

    def test_管理者モードは全直の最大(self) -> None:
        """他の直のデータを開いて精査するので、いまの直で縛らない。"""
        self.assertEqual(
            work_time.shift_limit(self.TIMES, "1直", admin=True), 490)

    def test_取れなければ0(self) -> None:
        """**上限が分からないことを理由に入力を断らない。**"""
        self.assertEqual(work_time.shift_limit({}, "1直"), 0)
        self.assertEqual(work_time.shift_minutes("", ""), 0)


class ComputeTests(unittest.TestCase):
    """まとめて計算する。"""

    def test_開始終了から作業時間(self) -> None:
        result = work_time.compute(
            rows(r1={"KZ": "22", "KH": "50", "SZ": "00", "SH": "30"}))
        self.assertTrue(result.ok)
        self.assertEqual(result.times[1], "100")

    def test_停止時間を引く(self) -> None:
        """作業停止①②③のぶんを引く。"""
        result = work_time.compute(rows(r1={
            "KZ": "07", "KH": "00", "SZ": "09", "SH": "00",
            "TH": "20", "THS": "10", "THT": "5"}))
        self.assertEqual(result.times[1], "85")     # 120 - 35

    def test_停止が空なら引かない(self) -> None:
        result = work_time.compute(rows(r1={
            "KZ": "07", "KH": "00", "SZ": "09", "SH": "00", "TH": ""}))
        self.assertEqual(result.times[1], "120")

    def test_4つ埋まっていない行は計算しない(self) -> None:
        """打ち終わる前に断らない。"""
        result = work_time.compute(
            rows(r1={"KZ": "07", "KH": "00", "SZ": "", "SH": ""}))
        self.assertTrue(result.ok)
        self.assertNotIn(1, result.times)

    def test_開始と終了が同じなら断る(self) -> None:
        result = work_time.compute(
            rows(r2={"KZ": "07", "KH": "00", "SZ": "07", "SH": "00"}))
        self.assertFalse(result.ok)
        self.assertEqual(result.problem.reason, work_time.REFUSE_SAME_TIME)
        self.assertEqual(result.problem.row, 2)
        self.assertIn("開始時間と終了時間が同じ", result.problem.message)

    def test_直の規定時間を超えたら断る(self) -> None:
        result = work_time.compute(
            rows(r1={"KZ": "07", "KH": "00", "SZ": "23", "SH": "00"}),
            limit_minutes=480)
        self.assertEqual(result.problem.reason, work_time.REFUSE_OVER_SHIFT)
        self.assertIn("480分", result.problem.message)

    def test_上限0なら超過を見ない(self) -> None:
        result = work_time.compute(
            rows(r1={"KZ": "07", "KH": "00", "SZ": "23", "SH": "00"}))
        self.assertTrue(result.ok)

    def test_マイナスなら断る(self) -> None:
        """停止時間が作業時間を超えた。**ここだけ音も鳴る。**"""
        result = work_time.compute(rows(r1={
            "KZ": "07", "KH": "00", "SZ": "08", "SH": "00", "TH": "90"}))
        self.assertFalse(result.ok)
        self.assertEqual(result.problem.reason, work_time.REFUSE_NEGATIVE)
        self.assertTrue(result.problem.sounds)
        self.assertIn("マイナス", result.problem.message)

    def test_マイナス以外では鳴らさない(self) -> None:
        result = work_time.compute(
            rows(r1={"KZ": "07", "KH": "00", "SZ": "07", "SH": "00"}))
        self.assertFalse(result.problem.sounds)

    def test_断ったら何も返さない(self) -> None:
        """VBA は `Exit Sub` して1つも書かなかった。"""
        result = work_time.compute(rows(
            r1={"KZ": "07", "KH": "00", "SZ": "09", "SH": "00"},
            r2={"KZ": "99"}))
        self.assertFalse(result.ok)
        self.assertEqual(result.times, {})

    def test_停止だけの行はマイナスになる(self) -> None:
        """作業時間が無いのに停止だけ入っている ── VBA も同じ扱い。"""
        result = work_time.compute(rows(r4={"TH": "30"}))
        self.assertEqual(result.problem.reason, work_time.REFUSE_NEGATIVE)
        self.assertEqual(result.problem.row, 4)

    def test_複数行(self) -> None:
        result = work_time.compute(rows(
            r1={"KZ": "22", "KH": "50", "SZ": "00", "SH": "30", "TH": "20"},
            r2={"KZ": "00", "KH": "30", "SZ": "02", "SH": "00"},
            r3={"KZ": "02", "KH": "00", "SZ": "04", "SH": "10", "THS": "20"}))
        self.assertEqual(result.times, {1: "80", 2: "90", 3: "110"})


if __name__ == "__main__":
    unittest.main()
