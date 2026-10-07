"""過去日報(xlsx)の入れ直し: 3直の行を2直へ入れていたころの取り込みを、直したあとで入れ直す

取り込みはページごとの上書きです。前の取り込みで2直が2ページに増えていた
(3直の行まで2直に入っていた)とき、入れ直しても**余ったページが残る**と、
2直に3直の行が残ったままになります。xlsx は1日ぶん3直そろったシートなので、
今度のページ数を超える古いページは消します(下見で名指しする)。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.connection import connect
from nippou.db.models import DetailRecord, HeaderRecord
from nippou.db.repository import NippouRepository
from nippou.services import csv_import as import_service
from tests.test_nippou_sheet import DAY, sheet_cells, write_xlsx


class ReimportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.conn = connect(self.tmp / "t.sqlite3")
        self.addCleanup(self.conn.close)
        self.repo = NippouRepository(self.conn)
        self.out = self.tmp / "out"
        # 前の取り込み: 2直が2ページ(2ページ目は本当は3直の行)
        for page in (1, 2):
            key = dict(report_date=DAY, line="HVC", shift="2直", page=page)
            self.repo.save(HeaderRecord(**key, worker="2直の人"),
                           [DetailRecord(**key, row_no=1, lot=f"OLD{page}", kz="22", kh="50",
                                         sz="23", sh="30")])
        self.book = write_xlsx(self.tmp / "日付.xlsx", sheet_cells(
            **{"J98": "22", "K98": "50", "L98": "23", "M98": "30", "D98": "H9461S0"}))

    def test_下見で消えるページを名指しする(self) -> None:
        found = import_service.preview(self.repo, self.book, "HVC", out_dir=self.out)
        self.assertEqual(found.stale, [(DAY, "HVC", "2直", 2)])
        self.assertIn("前の取り込みで増えていた 1ページ", found.message)
        self.assertIn("3直へ移しました", found.message)
        self.assertEqual(self.repo.saved_pages(DAY, "HVC", "2直"), [1, 2], "下見で消した")

    def test_入れ直すと2直の余りが消え3直に入る(self) -> None:
        _found, result = import_service.apply(self.repo, self.book, line="HVC", out_dir=self.out)
        self.assertEqual(result.removed, 1, result.message)
        self.assertEqual(self.repo.saved_pages(DAY, "HVC", "2直"), [1])
        second = self.repo.load(DAY, "HVC", "2直", 1)[1]
        self.assertNotIn("H9461S0", [d.lot for d in second])
        third = self.repo.load(DAY, "HVC", "3直", 1)[1]
        self.assertEqual((third[0].lot, third[0].kz, third[0].kh), ("H9461S0", "22", "50"))

    def test_CSVは一部のページだけのこともあるので消さない(self) -> None:
        csv = self.tmp / "明細.csv"
        from nippou.reporting import csv_export

        csv_export.write_daily_detail_csv(self.repo, DAY, "HVC", csv)
        self.conn.execute("DELETE FROM daily_detail WHERE page=2")
        self.conn.commit()
        found = import_service.preview(self.repo, csv, out_dir=self.out)
        self.assertEqual(found.stale, [])


if __name__ == "__main__":
    unittest.main()
