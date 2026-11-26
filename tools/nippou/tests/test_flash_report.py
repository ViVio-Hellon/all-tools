"""ライン速報(v4.54.0 `services/flash_report`)。

    日報にて枚数入力時、リアルタイムでライン毎に枚数カウントを把握したい
    → 速報ファイルを指定パスに吐けるように設定に追加 / 今の直・今日の合計・目標との差 / 除く人は数えない
"""
from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou import config, user_settings  # noqa: E402
from nippou.services import agg_exclude, flash_report, summary  # noqa: E402
from tests._web import HAS_FLASK, SKIP_REASON  # noqa: E402
from tests import test_scenario_shift_flow as flow  # noqa: E402


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class FlashReportTests(flow.ScenarioCase):
    def setUp(self) -> None:
        super().setUp()
        agg_exclude.reset()
        self.addCleanup(agg_exclude.reset)
        self.out = self.tmp / "速報"
        for p in (patch("nippou.services.day_csv.owner_line", return_value="L-1"),
                  patch("nippou.services.targets.of", return_value=100.0)):
            p.start()
            self.addCleanup(p.stop)

    def page(self, day: str, shift: str, page: int, worker: str, count: int) -> None:
        from nippou.db.models import DetailRecord, HeaderRecord

        key = dict(report_date=day, line="L-1", shift=shift, page=page)
        start = "07" if shift == "1直" else "15"
        self.repo().save(HeaderRecord(**key, worker=worker),
                         [DetailRecord(**key, row_no=1, lot=f"L{page}{shift[0]}", kz=start, kh="00",
                                       sz=str(int(start) + 1), sh="00", mai=str(count), tut="1",
                                       con=str(count), wei="100")])
        summary.refresh_shift(self.repo(), day, "L-1", shift)

    def read(self) -> dict:
        return json.loads((self.out / "L-1.json").read_text(encoding="utf-8"))

    def test_空なら書かない(self) -> None:
        self.page("2026年11月26日", "1直", 1, "山田", 10)
        self.assertFalse(self.out.exists())

    def test_今の直_今日の合計_目標との差_除く人は数えない(self) -> None:
        user_settings.save(config.KEY_FLASH_DIR, str(self.out))
        self.page("2026年11月26日", "1直", 1, "山田", 60)
        got = self.read()
        self.assertEqual((got["line"], got["date"], got["shift"], got["shift_count"]), ("L-1", "2026-11-26", "1直", 60.0))
        self.page("2026年11月26日", "2直", 1, "佐藤", 30)
        got = self.read()
        self.assertEqual(got["shift"], "2直")
        self.assertEqual(got["shift_count"], 30.0)
        self.assertEqual(got["shifts"], {"1直": 60.0, "2直": 30.0})
        self.assertEqual(got["day_total"], 90.0)
        self.assertEqual((got["target"], got["diff"]), (100.0, -10.0))
        # 集計から除く人のページは数えない
        master = config.SETTINGS.gw_material_master_path
        master.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(master)
        conn.execute(agg_exclude.SCHEMA)
        conn.execute(f'INSERT INTO "{agg_exclude.TABLE}" ("作業者","理由","有効") VALUES ("鈴木","試験",1)')
        conn.commit()
        conn.close()
        agg_exclude.reset()
        self.page("2026年11月26日", "2直", 2, "鈴木", 500)
        got = self.read()
        self.assertEqual(got["shift_count"], 30.0)
        self.assertEqual(got["day_total"], 90.0)

    def test_古い日の直しで今日を上書きしない(self) -> None:
        user_settings.save(config.KEY_FLASH_DIR, str(self.out))
        self.page("2026年11月26日", "1直", 1, "山田", 60)
        self.page("2026年11月20日", "1直", 1, "山田", 5)
        self.assertEqual(self.read()["date"], "2026-11-26")

    def test_ほかのラインの直しは書かない(self) -> None:
        user_settings.save(config.KEY_FLASH_DIR, str(self.out))
        with patch("nippou.services.day_csv.owner_line", return_value="機側"):
            self.page("2026年11月26日", "1直", 1, "山田", 60)
        self.assertFalse((self.out / "L-1.json").exists())

    def test_書けなくても保存は止めない(self) -> None:
        user_settings.save(config.KEY_FLASH_DIR, str(self.out))
        with patch.object(Path, "replace", side_effect=OSError("届かない")):
            self.page("2026年11月26日", "1直", 1, "山田", 60)
        self.assertIsNotNone(self.repo().load_packing_report("2026年11月26日", "L-1", "1直"))


if __name__ == "__main__":
    unittest.main()
