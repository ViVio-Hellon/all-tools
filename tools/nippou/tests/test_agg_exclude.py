"""集計から除く作業者・班 (v4.50.0)

    日報の集計から特定の班を除く、特定の作業者を除く。画面の集計、集計CSVから除きたい。
    複数の作業者がいて、その中に除く人が混ざっているときも除外で。
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.services import agg_exclude  # noqa: E402
from tests._web import HAS_FLASK, SKIP_REASON  # noqa: E402
from tests import test_scenario_shift_flow as flow  # noqa: E402


class RuleTests(unittest.TestCase):
    def test_作業者と班(self) -> None:
        rules = agg_exclude.parse([{"作業者": "山田", "有効": 1}, {"班": "B班"}, {"作業者": "鈴木", "有効": 0}])
        rules.roster = {"佐藤": "B", "田中": "A"}
        self.assertIn("山田", rules.why("山田"))
        self.assertIn("山田", rules.why("田中 山田"), "混ざっていても除く")
        self.assertIn("B班", rules.why("佐藤"))
        self.assertEqual(rules.why("田中"), "")
        self.assertEqual(rules.why("鈴木"), "", "有効 0 は読まない")


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class SummaryTests(flow.ScenarioCase):
    def setUp(self) -> None:
        super().setUp()
        agg_exclude.reset()
        self.addCleanup(agg_exclude.reset)

    def exclude(self, *rows: dict) -> None:
        from nippou.config import SETTINGS
        path = SETTINGS.gw_material_master_path
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute(agg_exclude.SCHEMA)
        conn.execute(f'DELETE FROM "{agg_exclude.TABLE}"')
        for r in rows:
            conn.execute(f'INSERT INTO "{agg_exclude.TABLE}" ("作業者","班","有効") VALUES (?,?,1)',
                         (r.get("作業者", ""), r.get("班", "")))
        conn.commit()
        conn.close()
        agg_exclude.reset()

    def test_除く人のページは集計にもCSVにも入らず_日報は残る(self) -> None:
        self.start_shift("2026-10-05 07:05", "山田 田中")
        self.at("2026-10-05 10:00")
        self.assertEqual(self.save(flow.clean_shift_rows("1直", "A"), worker="山田 田中").status_code, 200)
        report = self.repo().load_packing_report("2026年10月5日", "L-1", "1直")
        self.assertIsNotNone(report, "除く前は集計がある")
        self.exclude({"作業者": "山田"})
        res = self.post("/api/settings/rebuild-summary", {"start": "2026-10-05", "end": "2026-10-05"})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertIsNone(self.repo().load_packing_report("2026年10月5日", "L-1", "1直"), "集計から外れる")
        self.assertEqual(self.repo().saved_pages("2026年10月5日", "L-1", "1直"), [1], "日報は残る")

    def test_表が無ければ作って何も除かない(self) -> None:
        from nippou import source_db
        from nippou.config import SETTINGS
        path = SETTINGS.gw_material_master_path
        path.parent.mkdir(parents=True, exist_ok=True)
        sqlite3.connect(path).execute("CREATE TABLE IF NOT EXISTS t (a)").connection.close()
        rules = agg_exclude.load(force=True)
        self.assertFalse(rules)
        self.assertIn(agg_exclude.TABLE, source_db.list_tables(SETTINGS.gw_material_master_path))


if __name__ == "__main__":
    unittest.main()
