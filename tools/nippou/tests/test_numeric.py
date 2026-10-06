import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic.numeric import format_fixed, is_numeric, vba_round_half_away_from_zero


class IsNumericTests(unittest.TestCase):
    def test_blank_is_not_numeric(self) -> None:
        self.assertFalse(is_numeric(""))
        self.assertFalse(is_numeric("   "))

    def test_plain_numbers(self) -> None:
        self.assertTrue(is_numeric("123"))
        self.assertTrue(is_numeric("-4.5"))
        self.assertTrue(is_numeric("+4.5"))
        self.assertTrue(is_numeric(3))
        self.assertTrue(is_numeric(3.5))

    def test_non_numeric_text(self) -> None:
        self.assertFalse(is_numeric("abc"))
        self.assertFalse(is_numeric("12abc"))
        self.assertFalse(is_numeric(None))


class RoundingTests(unittest.TestCase):
    def test_half_rounds_away_from_zero(self) -> None:
        # Python's banker's rounding would give 2.0 here; VBA's Format gives 2.5 -> 3.
        self.assertEqual(vba_round_half_away_from_zero(2.5, 0), 3.0)
        self.assertEqual(vba_round_half_away_from_zero(-2.5, 0), -3.0)

    def test_format_fixed(self) -> None:
        self.assertEqual(format_fixed(0.125, 2), "0.13")  # away-from-zero, exact binary fraction
        self.assertEqual(format_fixed(3.0, 0), "3")
        self.assertEqual(format_fixed(2.0 / 3.0, 2), "0.67")


if __name__ == "__main__":
    unittest.main()
