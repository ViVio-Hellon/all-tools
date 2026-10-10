"""配布設定: 日報複合ツールの一式の中なら、置き場所と配り方を言う

    少なくとも日報管理ツールでは配布設定してもフォルダは生成されていない

日報複合ツールでは、日報の配布設定は `<一式>\\tools\\nippou\\配布設定\\` にできる。
単品のころの「起動用の Start.vbs と同じフォルダ」と書くと、一式の直下(Start.vbs が
ある所)を探して見つからない。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.distribution import integrated_where

ROOT = Path(__file__).resolve().parent.parent      # tools/nippou


class WhereTests(unittest.TestCase):
    def test_日報複合ツールの中なら置き場所とmake_distを言う(self) -> None:
        text = integrated_where(ROOT / "配布設定")
        self.assertIn("tools\\nippou\\配布設定", text)
        self.assertIn(str(ROOT / "配布設定"), text)
        self.assertIn("scripts\\make_dist.bat", text)

    def test_単品なら言わない(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(integrated_where(Path(tmp) / "日報管理ツール" / "配布設定"), "")


if __name__ == "__main__":
    unittest.main()
