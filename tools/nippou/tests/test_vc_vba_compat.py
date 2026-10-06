"""VBA の Format / IsNumeric のなぞり方

vc-calculator `tests/test_vba_compat.py` の移植(期待値は同じ)。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest

from nippou.vc.vba_compat import (excel_to_int, normalize_text, parse_number,
                                  vba_format_fixed)


class FormatFixedTest(unittest.TestCase):
    def test_half_away_from_zero(self):
        # Round() と違い、0.5 は常に 0 から遠い側へ
        self.assertEqual(vba_format_fixed(0.25, 1), "0.3")
        self.assertEqual(vba_format_fixed(0.35, 1), "0.4")
        self.assertEqual(vba_format_fixed(0.125, 2), "0.13")

    def test_decimal_view_of_double(self):
        # 2進では 12.3499999… だが、VBA は有効15桁の 12.35 として丸める
        self.assertEqual(vba_format_fixed(12.35, 1), "12.4")
        self.assertEqual(vba_format_fixed(2.675, 2), "2.68")
        self.assertEqual(vba_format_fixed(1.005, 2), "1.01")
        self.assertEqual(vba_format_fixed(50.25, 1), "50.3")

    def test_padding_and_zero(self):
        self.assertEqual(vba_format_fixed(20, 1), "20.0")
        self.assertEqual(vba_format_fixed(0.1, 2), "0.10")
        self.assertEqual(vba_format_fixed(0.004, 2), "0.00")
        self.assertEqual(vba_format_fixed(-0.04, 1), "0.0")

    def test_rejects_nan(self):
        with self.assertRaises(ValueError):
            vba_format_fixed(float("nan"), 1)


class ExcelToIntTest(unittest.TestCase):
    def test_rounddown_and_round(self):
        self.assertEqual(excel_to_int(146.968, "切り捨て"), 146)
        self.assertEqual(excel_to_int(146.968, "四捨五入"), 147)
        self.assertEqual(excel_to_int(0.5, "四捨五入"), 1)
        # 2進の誤差で整数の少し下になっても落とさない
        self.assertEqual(excel_to_int(0.1 * 3 * 10, "切り捨て"), 3)


class ParseNumberTest(unittest.TestCase):
    def test_plain_numbers(self):
        self.assertEqual(parse_number("12"), 12.0)
        self.assertEqual(parse_number(" 12.5 "), 12.5)
        self.assertEqual(parse_number(".5"), 0.5)
        self.assertEqual(parse_number("12."), 12.0)

    def test_full_width_is_accepted(self):
        self.assertEqual(normalize_text("２０．５"), "20.5")
        self.assertEqual(parse_number("２０．５"), 20.5)

    def test_refuses_what_val_would_misread(self):
        # VBA の Val("1,000") は 1 になる(docs/VBA解析.md §14 D6)
        for text in ("1,000", "&H10", "12mm", "", "-", ".", "abc", "1.2.3"):
            with self.subTest(text=text):
                self.assertIsNone(parse_number(text))


if __name__ == "__main__":
    unittest.main()
