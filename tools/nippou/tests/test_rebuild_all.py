"""全ラインの集計の作り直し(v4.52.0 `services/rebuild_all`)。"""
from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from nippou.services import local_backup, rebuild_all


class RebuildAllTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "LocalBackup"
        for line in ("L-1", "機側"):
            (self.root / line).mkdir(parents=True)
        for p in (patch.object(local_backup, "root", return_value=self.root),
                  patch.object(local_backup, "reachable", return_value=True),
                  patch.object(local_backup, "lines_available", return_value=["L-1", "機側"]),
                  patch.object(local_backup, "_safe", side_effect=lambda s: s)):
            p.start()
            self.addCleanup(p.stop)
        rebuild_all.reset()
        self.addCleanup(rebuild_all.reset)

    def _run(self, line):
        repo = MagicMock()
        repo.shift_keys_between.return_value = [("2026-11-01", line, "1")]
        with patch("nippou.services.summary.rebuild", return_value=MagicMock(shifts=1)) as rb, \
                patch("nippou.services.day_csv.settle", return_value=[]) as st, \
                patch("nippou.services.agg_exclude.load"):
            msg = rebuild_all.run_if_asked(repo, line, force=True)
        return msg, rb, st

    def test_頼む_ラインごとに作る_済みを見る(self) -> None:
        self.assertIsNone(self._run("L-1")[0])          # 頼みが無ければ何もしない
        rebuild_all.request(date(2026, 11, 1), date(2026, 11, 16), by="管理者")
        self.assertEqual([r["done"] for r in rebuild_all.status()["lines"]], [False, False])
        msg, rb, st = self._run("L-1")
        self.assertIn("作り直しをしました", msg)
        rb.assert_called_once()
        self.assertEqual(rb.call_args.kwargs["line"], "L-1")
        st.assert_called_once()                          # 集計CSVも書き直す
        self.assertEqual([r["done"] for r in rebuild_all.status()["lines"]], [True, False])
        self.assertIsNone(self._run("L-1")[0])          # 同じ頼みは2回しない
        rebuild_all.request(date(2026, 11, 1), date(2026, 11, 2))
        self.assertEqual([r["done"] for r in rebuild_all.status()["lines"]], [False, False])
        self.assertIsNotNone(self._run("L-1")[0])       # 新しい頼みはまたする

    def test_失敗は印に残す(self) -> None:
        rebuild_all.request(date(2026, 11, 1), date(2026, 11, 1))
        with patch("nippou.services.summary.rebuild", side_effect=RuntimeError("壊れた")), \
                patch("nippou.services.agg_exclude.load"):
            msg = rebuild_all.run_if_asked(MagicMock(), "機側", force=True)
        self.assertIn("失敗", msg)
        row = rebuild_all.status()["lines"][1]
        self.assertTrue(row["done"])
        self.assertIn("壊れた", row["error"])


if __name__ == "__main__":
    unittest.main()
