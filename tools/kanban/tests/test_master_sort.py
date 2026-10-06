"""マスタ管理の並べ替え(列名を押す)。**表全体を並べてからページに分ける。**"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from kanban.db.shared import ROW_KEY, SharedDb
from kanban.presenters import master


class SortTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="kanban_sort_"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        path = self.dir / "看板マスタ.sqlite3"
        c = sqlite3.connect(str(path))
        c.execute("CREATE TABLE [看板_LVC] ([管理番号] NUMERIC, [資材] TEXT, [サイズ] TEXT, [更新日] DATETIME)")
        rows = [(n, f"資材{n % 7}", str(n * 10) if n % 5 else "", f"2026/{(n % 12) + 1}/1 9:00:00")
                for n in range(1, 251)]
        c.executemany("INSERT INTO [看板_LVC] VALUES (?, ?, ?, ?)", rows)
        c.commit()
        c.close()
        self.db = SharedDb(str(path), cache_dir=str(self.dir / "cache"))

    def all_rows(self, column, direction="asc"):
        out = []
        first = master.open_table(self.db, "看板_LVC", 0, sort=column, sort_dir=direction)
        for page in range(first.pages):
            out += master.open_table(self.db, "看板_LVC", page, sort=column, sort_dir=direction).rows
        return first, out

    def test_whole_table_is_sorted_before_paging(self):
        """見えている 100 行だけでなく、表全体の順。大きい順の 1 ページ目に 250 番が来る。"""
        view, rows = self.all_rows("管理番号", "desc")
        self.assertEqual((view.sort, view.sort_dir, view.pages), ("管理番号", "desc", 3))
        self.assertEqual(view.rows[0]["管理番号"], 250)
        self.assertEqual([r["管理番号"] for r in rows], list(range(250, 0, -1)))

    def test_numbers_as_numbers_and_blanks_last(self):
        """文字の順だと 100 が 20 より前になる。空欄は向きにかかわらず最後。"""
        for direction in ("asc", "desc"):
            _view, rows = self.all_rows("サイズ", direction)
            values = [r["サイズ"] for r in rows]
            filled = [int(v) for v in values if v]
            self.assertEqual(filled, sorted(filled, reverse=direction == "desc"))
            self.assertEqual(values[len(filled):], [""] * 50, "空欄が途中に混ざった")

    def test_dates_without_zero_padding(self):
        """VBA が書いた 2026/9/1 は 2026/10/1 より前。"""
        _view, rows = self.all_rows("更新日")
        months = [int(r["更新日"].split("/")[1]) for r in rows]
        self.assertEqual(months, sorted(months))

    def test_row_key_still_points_at_the_same_row(self):
        """並べ替えても、直す・消すは行を指す値(rowid)で行う。"""
        view = master.open_table(self.db, "看板_LVC", 0, sort="管理番号", sort_dir="desc")
        top = view.rows[0]
        self.assertEqual(top[ROW_KEY], 250)
        result = master.update_cell(self.db, "看板_LVC", top[ROW_KEY], "資材", "直した", expect_key=top["管理番号"])
        self.assertTrue(result.ok, result.message)
        self.db.forget_copy()
        after = master.open_table(self.db, "看板_LVC", 0, sort="管理番号", sort_dir="desc").rows[0]
        self.assertEqual((after["管理番号"], after["資材"]), (250, "直した"))

    def test_unknown_column_keeps_the_file_order(self):
        view = master.open_table(self.db, "看板_LVC", 0, sort="無い列")
        self.assertEqual(view.sort, "")
        self.assertEqual(view.rows[0]["管理番号"], 1)


if __name__ == "__main__":
    unittest.main()
