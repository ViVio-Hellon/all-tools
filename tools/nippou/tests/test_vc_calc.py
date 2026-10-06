"""計算ボタン(CommandButton1_Click)の再現

期待値は VBA の式を手で計算したもの(実装を呼んで作っていない)。

vc-calculator `tests/test_vc_calc.py` の移植(期待値は同じ)。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest

from nippou.vc.calc import (REFUSE_BAD_VALUE, REFUSE_MISSING, Fields,
                            SheetSize, calculate, select_product)

SHEETS = [SheetSize("M1", "M1", 2010.0), SheetSize("M4", "M4", 2510.0),
          SheetSize("M5", "M5", 3065.0)]


def counts(result):
    return {c.key: c.value for c in result.counts}


class ForwardTest(unittest.TestCase):
    def test_nitto_2008_small_core(self):
        # π×((2×20+87)²−87²)÷(4×0.10)÷1000 = 67.230… → 67.2
        r = calculate(Fields(coatu="20", vcatu="0.10", inside="87.0",
                             prolen="1000"), SHEETS)
        self.assertTrue(r.ran)
        self.assertEqual(r.fields.vclen, "67.2")
        self.assertEqual(r.computed, ["vclen"])
        # 枚数は画面に出た 67.2 で割る(67200/2010 = 33.43… / 67200/2510 = 26.77…)
        self.assertEqual(counts(r), {"M1": "33.4", "M4": "26.8", "M5": "21.9"})
        self.assertEqual(r.mk, "67.2")

    def test_after_update_rounding_happens_first(self):
        # 肉厚 12.35 は欄を抜けた時点で 12.4 になり、12.4 で計算される
        r = calculate(Fields(coatu="12.35", vcatu="0.1", inside="95"), SHEETS)
        self.assertEqual(r.fields.coatu, "12.4")
        self.assertEqual(r.fields.vcatu, "0.10")
        self.assertEqual(r.fields.inside, "95.0")
        self.assertEqual(r.fields.vclen, "41.8")      # 41.838…

    def test_existing_length_is_not_overwritten(self):
        # VC長さが入っていれば上書きしない(VBA の If .VClen = "")
        r = calculate(Fields(coatu="20", vcatu="0.10", inside="87",
                             vclen="50.25"), SHEETS)
        self.assertEqual(r.fields.vclen, "50.3")       # 欄の書式だけ整う
        self.assertEqual(r.computed, [])
        # 50.3 で割る: 25.02… / 20.03… / 16.41…
        self.assertEqual(counts(r), {"M1": "25.0", "M4": "20.0", "M5": "16.4"})

    def test_prolen_is_not_rounded(self):
        r = calculate(Fields(coatu="20", vcatu="0.10", inside="87",
                             vclen="50.3", prolen="1234.5"), SHEETS)
        self.assertEqual(r.mk, "40.7")                 # 50300/1234.5 = 40.745…
        self.assertEqual(r.fields.prolen, "1234.5")

    def test_sheet_sizes_come_from_the_master(self):
        r = calculate(Fields(coatu="20", vcatu="0.10", inside="87"),
                      [SheetSize("X", "試し", 1000.0)])
        self.assertEqual(counts(r), {"X": "67.2"})


class RoundTripTest(unittest.TestCase):
    """順算と逆算が同じ式になったので、肉厚 → 長さ → 肉厚 が元に戻る(VER1.2.0)。"""

    def test_thickness_round_trip(self):
        from nippou.vc.calc import vc_length, wall_thickness
        for a, b in ((0.10, 87), (0.08, 95), (0.06, 94), (0.13, 83), (0.10, 98)):
            for y in (5, 12.3, 20, 33, 47, 58):
                with self.subTest(a=a, b=b, y=y):
                    self.assertAlmostEqual(wall_thickness(vc_length(y, a, b), a, b), y, places=9)

    def test_matches_geometry(self):
        """長さ = 巻き数(肉厚÷VC厚)× 1周の平均の長さ(π×(内径+肉厚))。"""
        import math
        from nippou.vc.calc import vc_length
        for a, b, y in ((0.10, 87, 20), (0.06, 94, 30), (0.13, 83, 58)):
            self.assertAlmostEqual(vc_length(y, a, b), (y / a) * math.pi * (b + y) / 1000, places=9)


class GuardTest(unittest.TestCase):
    def test_missing_input_does_nothing(self):
        for missing in ("coatu", "vcatu", "inside"):
            data = {"coatu": "20", "vcatu": "0.10", "inside": "87"}
            data[missing] = ""
            with self.subTest(missing=missing):
                r = calculate(Fields(**data), SHEETS)
                self.assertFalse(r.ran)
                self.assertEqual(r.reason, REFUSE_MISSING)
                self.assertEqual(r.fields.vclen, "")
                self.assertEqual(r.counts, [])

    def test_reverse_is_unreachable_by_default(self):
        r = calculate(Fields(vcatu="0.10", inside="87", vclen="67.2"), SHEETS)
        self.assertFalse(r.ran)
        self.assertIn("肉厚", r.message)

    def test_reverse_when_enabled(self):
        # (−87 + √(87² + 4×0.10×67.2×1000÷π)) ÷ 2 = 19.992… → 20.0
        r = calculate(Fields(vcatu="0.10", inside="87", vclen="67.2"), SHEETS,
                      reverse=True)
        self.assertTrue(r.ran)
        self.assertEqual(r.fields.coatu, "20.0")
        self.assertEqual(r.computed, ["coatu"])
        self.assertEqual(counts(r)["M1"], "33.4")

    def test_zero_film_thickness_is_refused(self):
        # VBA は 0 除算で止まる。0.004 は 0.00 に丸まるので同じ
        for vcatu in ("0", "0.004"):
            with self.subTest(vcatu=vcatu):
                r = calculate(Fields(coatu="20", vcatu=vcatu, inside="87"), SHEETS)
                self.assertFalse(r.ran)
                self.assertEqual(r.reason, REFUSE_BAD_VALUE)
                self.assertIn("vcatu", r.errors)

    def test_bad_numbers_are_refused_not_cleared(self):
        r = calculate(Fields(coatu="abc", vcatu="0.10", inside="-87"), SHEETS)
        self.assertFalse(r.ran)
        self.assertEqual(set(r.errors), {"coatu", "inside"})
        self.assertEqual(r.fields.coatu, "abc")        # 打った内容は消さない

    def test_full_width_digits(self):
        r = calculate(Fields(coatu="２０", vcatu="０．１", inside="８７"), SHEETS)
        self.assertEqual(r.fields.vclen, "67.2")

    def test_bad_prolen_only_skips_mk(self):
        for prolen in ("abc", "0", "-5"):
            with self.subTest(prolen=prolen):
                r = calculate(Fields(coatu="20", vcatu="0.10", inside="87",
                                     prolen=prolen), SHEETS)
                self.assertTrue(r.ran)
                self.assertEqual(r.mk, "")
                self.assertEqual(len(r.notes), 1)
                self.assertEqual(counts(r)["M1"], "33.4")


class SelectProductTest(unittest.TestCase):
    def test_fixed_core(self):
        f = select_product(Fields(coatu="20", vclen="67.1", prolen="1000"),
                           vcatu=0.06, inside=87.0)
        self.assertEqual(f, Fields(coatu="", vcatu="0.06", inside="87.0",
                                   vclen="", prolen="1000"))

    def test_core_to_choose(self):
        f = select_product(Fields(inside="95.0"), vcatu=0.1, inside=None)
        self.assertEqual((f.vcatu, f.inside), ("0.10", ""))


if __name__ == "__main__":
    unittest.main()


class ExplainTest(unittest.TestCase):
    """計算の経過: 段の値が計算の結果と食い違わないこと。"""

    def test_forward_steps(self):
        r = calculate(Fields(coatu="20", vcatu="0.10", inside="87", prolen="1000"), SHEETS)
        steps = {s["title"]: s for s in r.steps}
        self.assertEqual(steps["外径"]["value"], "127")
        self.assertEqual(steps["巻きの断面積"]["value"], "6,723")
        self.assertEqual(steps["VC長さ"]["value"], "67.23")
        self.assertIn("67.2 m", steps["VC長さ"]["note"])
        self.assertIn("巻き数 200 巻", steps["検算(巻き数)"]["note"])
        self.assertEqual([s["value"] for s in r.steps if s["title"].startswith("枚数")],
                         [c.value for c in r.counts] + [r.mk])
        self.assertEqual(r.geometry["outer"], 127.0)
        self.assertEqual(r.geometry["turns"], 200.0)

    def test_reverse_steps_end_with_the_result(self):
        r = calculate(Fields(vcatu="0.10", inside="87", vclen="67.2"), SHEETS, reverse=True)
        self.assertEqual([s["title"] for s in r.steps][:3], ["巻きの断面積", "外径", "肉厚"])
        self.assertEqual(r.steps[2]["value"], r.fields.coatu)

    def test_no_steps_when_not_calculated(self):
        r = calculate(Fields(vcatu="0.10", inside="87"), SHEETS)
        self.assertEqual((r.steps, r.geometry), ([], {}))
