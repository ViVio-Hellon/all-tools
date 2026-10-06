"""共有フォルダのロック検証ツール(tools/check_shared_lock.py)のテスト。

本番の共有フォルダに対する検証はこのテストの対象外(実環境が必要)。
ここではローカルディスク上で短時間・少プロセス数の実行が壊れずに
完走し、自己申告件数と DB 実測値が一致することだけを確認する。

``multiprocessing`` の ``spawn`` は子プロセス側でターゲット関数を
再インポートするため、ツールは実際に ``python tools/check_shared_lock.py``
としてサブプロセス起動して検証する(importlib で動的ロードしたモジュール
だと子プロセスが見つけられずハングするため)。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOL_PATH = Path(__file__).resolve().parent.parent / "tools" / "check_shared_lock.py"


class CheckSharedLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="kanban_locktool_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def _run(self, *extra_args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(TOOL_PATH), *extra_args],
            capture_output=True,
            text=True,
            encoding="utf-8",   # Windows の既定(cp1252 / cp932)では日本語を読めない
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            timeout=60,
        )

    def test_short_run_reports_consistent_counts(self):
        path = str(self.dir / "_locktest.sqlite3")
        proc = self._run("--path", path, "--workers", "3", "--seconds", "1")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("問題は検出されませんでした", proc.stdout)
        self.assertIn("DB実測値", proc.stdout)
        # 自己申告件数と DB 実測値が一致していることを出力から確認する
        expected_line = next(
            line for line in proc.stdout.splitlines() if line.startswith("合計:")
        )
        match = re.search(r"自己申告\)=(\d+) / DB実測値=(\d+)", expected_line)
        self.assertIsNotNone(match, expected_line)
        self.assertEqual(match.group(1), match.group(2))

    def test_rejects_production_filename(self):
        proc = self._run("--path", str(self.dir / "kanban.sqlite3"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("同名", proc.stderr)


if __name__ == "__main__":
    unittest.main()
