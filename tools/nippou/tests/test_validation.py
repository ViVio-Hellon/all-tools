import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic.validation import duplicate_lot_rows, find_duplicate_lots  # noqa: E402
from tests._web import WebTestCase  # noqa: E402


class DuplicateLotsTests(unittest.TestCase):
    def test_no_duplicates(self) -> None:
        self.assertEqual(find_duplicate_lots(["A", "B", "C"]), [])

    def test_blank_entries_ignored(self) -> None:
        self.assertEqual(find_duplicate_lots(["", "", "A"]), [])

    def test_detects_duplicates(self) -> None:
        self.assertEqual(find_duplicate_lots(["A", "B", "A", "C", "B"]), ["A", "B"])

    def test_whitespace_is_trimmed_before_comparison(self) -> None:
        self.assertEqual(find_duplicate_lots(["A", " A "]), ["A"])


class DuplicateLotRowsTests(unittest.TestCase):
    """入力画面の印(v4.44.10)。**決まりは `find_duplicate_lots` と同じ**で、行番号を添えるだけ。"""

    CASES = (
        ["A", "B", "C"],
        ["A", "", "", "A"],               # 空けた行(上の行の続き)は数えない・相手は離れていても
        ["A", " A ", "B", "B", "B"],      # 前後の空白は落として比べる
        ["", "", ""],
    )

    def test_重複のロットは保存の決まりと同じ(self) -> None:
        for lots in self.CASES:
            with self.subTest(lots=lots):
                self.assertEqual(sorted(duplicate_lot_rows(lots)), find_duplicate_lots(lots))

    def test_行番号は1から_相手の行も入る(self) -> None:
        self.assertEqual(duplicate_lot_rows(["A", "", "B", " A", "B", "A"]),
                         {"A": [1, 4, 6], "B": [3, 5]})


class ScreenAndSaveAgreeTests(WebTestCase):
    """入力画面の印(`/api/entry/state` の `lot_duplicates`)と保存の断りが、同じものを言う。"""

    def rows(self, *lots: str) -> dict:
        return {str(n): {"LOT": lot, "MAI": "1"} for n, lot in enumerate(lots, start=1)}

    def test_印と保存の断りが食い違わない(self) -> None:
        for lots in (("H5422S0", "", " H5422S0", "H5423S0"), ("H5422S0", "", "H5423S0"),
                     ("A", "B", "A", "B")):
            with self.subTest(lots=lots):
                rows = self.rows(*lots)
                state = self.post("/api/entry/state", {"rows": rows, "header": {}, "checks": {}})
                self.assertEqual(state.status_code, 200)
                marked = state.get_json()["lot_duplicates"]
                saved = self.post("/api/entry/save", {"rows": rows, "header": {"worker": "山田"},
                                                      "checks": {}})
                want = find_duplicate_lots(list(lots))
                self.assertEqual([i["lot"] for i in marked["items"]], want)
                if want:
                    self.assertEqual(saved.status_code, 422, saved.get_json())
                    self.assertEqual(saved.get_json()["duplicates"], want)
                    self.assertIn("LOTが重複しています: " + ", ".join(want),
                                  saved.get_json()["message"])
                    self.assertIn("LOTが重複しています: ", marked["text"])
                else:
                    self.assertNotIn("duplicates", saved.get_json() or {})
                    self.assertEqual(marked["text"], "")


if __name__ == "__main__":
    unittest.main()
