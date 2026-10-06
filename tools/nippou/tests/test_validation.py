import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic.validation import find_duplicate_lots


class DuplicateLotsTests(unittest.TestCase):
    def test_no_duplicates(self) -> None:
        self.assertEqual(find_duplicate_lots(["A", "B", "C"]), [])

    def test_blank_entries_ignored(self) -> None:
        self.assertEqual(find_duplicate_lots(["", "", "A"]), [])

    def test_detects_duplicates(self) -> None:
        self.assertEqual(find_duplicate_lots(["A", "B", "A", "C", "B"]), ["A", "B"])

    def test_whitespace_is_trimmed_before_comparison(self) -> None:
        self.assertEqual(find_duplicate_lots(["A", " A "]), ["A"])


if __name__ == "__main__":
    unittest.main()
