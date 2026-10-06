import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic.calculations import package_count, unit_weight


class UnitWeightTests(unittest.TestCase):
    """Ports 単重計算(i): UNI = WEI / CON, computed only once (while UNI is
    blank), cleared in every other reachable branch."""

    def test_computes_when_uni_blank_and_lot_present(self) -> None:
        self.assertEqual(unit_weight(con="10", wei="100", uni="", lot="LOT1"), "10.00")

    def test_blank_when_lot_missing(self) -> None:
        # CON<>0 and UNI=="" but LOT=="" -> inner If has no Else, stays "".
        self.assertEqual(unit_weight(con="10", wei="100", uni="", lot=""), "")

    def test_clears_when_uni_already_set(self) -> None:
        # Matches the VBA quirk: a populated UNI gets wiped on next call.
        self.assertEqual(unit_weight(con="10", wei="100", uni="5.00", lot="LOT1"), "")

    def test_clears_when_con_is_zero(self) -> None:
        self.assertEqual(unit_weight(con="0", wei="100", uni="", lot="LOT1"), "")

    def test_clears_when_not_numeric(self) -> None:
        self.assertEqual(unit_weight(con="abc", wei="100", uni="", lot="LOT1"), "")
        self.assertEqual(unit_weight(con="10", wei="", uni="", lot="LOT1"), "")

    def test_rounds_to_two_decimals(self) -> None:
        self.assertEqual(unit_weight(con="3", wei="10", uni="", lot="LOT1"), "3.33")


class PackageCountTests(unittest.TestCase):
    """Ports 包み数計算(): CON = MAI / TUT, always overwritten (no
    "only if blank" guard, unlike 単重計算)."""

    def test_computes_integer_result(self) -> None:
        self.assertEqual(package_count(mai="120", tut="12"), "10")

    def test_blank_when_either_zero(self) -> None:
        self.assertEqual(package_count(mai="0", tut="12"), "")
        self.assertEqual(package_count(mai="120", tut="0"), "")

    def test_blank_when_not_numeric(self) -> None:
        self.assertEqual(package_count(mai="", tut="12"), "")
        self.assertEqual(package_count(mai="abc", tut="12"), "")

    def test_rounds_half_away_from_zero(self) -> None:
        # 5 / 2 = 2.5 -> "3" under VBA's Format rounding, not banker's "2".
        self.assertEqual(package_count(mai="5", tut="2"), "3")


if __name__ == "__main__":
    unittest.main()
