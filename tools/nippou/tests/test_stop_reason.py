import sys
import unittest
from datetime import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic.stop_reason import build_all_stop_entry, working_minutes


class WorkingMinutesTests(unittest.TestCase):
    def test_same_day_shift(self) -> None:
        # 1直: 08:00 - 17:00 = 540分
        self.assertEqual(working_minutes("08", "00", "17", "00"), 540)

    def test_overnight_shift_wraps_to_next_day(self) -> None:
        # 3直: 22:00 - 翌08:00 = 600分
        self.assertEqual(working_minutes("22", "00", "08", "00"), 600)

    def test_subtracts_given_minutes(self) -> None:
        self.assertEqual(working_minutes("08", "00", "17", "00", subtract_minutes=60), 480)

    def test_invalid_input_returns_minus_one(self) -> None:
        self.assertEqual(working_minutes("", "", "17", "00"), -1)
        self.assertEqual(working_minutes("ab", "00", "17", "00"), -1)


class BuildAllStopEntryTests(unittest.TestCase):
    def test_builds_row1_from_shift_bounds(self) -> None:
        entry = build_all_stop_entry(time(8, 0), time(17, 0), "設備故障")
        self.assertEqual(entry.kz, "08")
        self.assertEqual(entry.kh, "00")
        self.assertEqual(entry.sz, "17")
        self.assertEqual(entry.sh, "00")
        self.assertEqual(entry.s, "設備故障")
        self.assertEqual(entry.th, "540")

    def test_overnight_shift(self) -> None:
        entry = build_all_stop_entry(time(22, 0), time(8, 0), "材料待ち")
        self.assertEqual(entry.th, "600")


if __name__ == "__main__":
    unittest.main()
