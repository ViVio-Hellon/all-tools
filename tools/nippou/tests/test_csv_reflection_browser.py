"""記録を見る「集計CSV」の欄を本物のブラウザで (v4.46.9)

画面を開くと、共有フォルダの集計CSVを読みに行き、直ごとに「入っています」「まだ」を出す。
「まだ」は開くと違うところが見える。直してから「集計CSVを読み直す」で変わる。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.models import DetailRecord, HeaderRecord  # noqa: E402
from tests.test_records_browser import RecordsBrowserCase  # noqa: E402

DAY = "2026年9月15日"


class CsvColumnTests(RecordsBrowserCase):
    def setUp(self) -> None:
        super().setUp()
        from nippou import config, user_settings

        self.out = self.tmp / "出力先"
        self.out.mkdir()
        user_settings.save_many({config.KEY_REPORT_OUT_DIR: str(self.out)})

    def put(self, lot: str) -> None:
        from nippou.services import summary

        repo = self.repo()
        key = dict(report_date=DAY, line="L-1", shift="1直", page=1)
        repo.save(HeaderRecord(**key, worker="前の人"),
                  [DetailRecord(**key, row_no=1, lot=lot, ken="10", con="10",
                                kz="07", kh="00", sz="15", sh="00")])
        summary.refresh_shift(repo, DAY, "L-1", "1直")

    def write_csv(self) -> None:
        from nippou.reporting import csv_export

        csv_export.write_daily_set(self.repo(), DAY, "L-1", self.out)

    def cell(self) -> str:
        return '#records-table td[data-csv-check][data-shift="1直"]'

    def test_入っていなければまだ_書き直せば入っている(self) -> None:
        self.put("OLD0001")
        self.write_csv()
        self.put("NEW0001")                       # 直したが、集計CSVはまだ前の中身
        page = self.open_records()
        page.wait_for_selector(f'{self.cell()}[data-csv-state="not_yet"]', timeout=8000)
        page.click(f"{self.cell()} summary")
        text = page.inner_text(self.cell())
        self.assertIn("OLD0001", text)
        self.assertIn("NEW0001", text)
        self.write_csv()                          # そのラインのPCが書き直した
        page.click("#csv-check-again")
        page.wait_for_selector(f'{self.cell()}[data-csv-state="done"]', timeout=8000)
        self.assertIn("入っています", page.inner_text(self.cell()))
        self.assertEqual(self.errors, [])
