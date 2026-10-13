"""配布設定を書き出すだけの入口(``export_distribution.py``)

日報複合ツールの大設定の「4ツールの分もまとめて書き出す」が、このフォルダで子として起こす。

- 設定画面の既定の選び方のまま書き出す(担当ラインは入れない)。書き出した直後の確認も同じ
- パスワードは聞かない(呼ぶ側の大設定が確かめる)
- 答えは標準出力に JSON 1 行。書き出せる設定が無いときは ok=false・reason="nothing"(失敗と分ける)
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kanban import config, distribution

ROOT = Path(__file__).resolve().parent.parent


class ExportEntryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="kanban_export_entry_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.folder = self.tmp / "配布設定"
        # 何も設定していない端末(この端末の設定ファイル・配布設定を一時フォルダへ)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("KANBAN_")}
        self.env.update(
            KANBAN_CONFIG=str(self.tmp / "appdata" / "config.json"),
            KANBAN_SETTINGS_DIR=str(self.tmp / "data"),
            KANBAN_LOCAL_DIR=str(self.tmp / "local"),
            KANBAN_DISTRIBUTION_DIR=str(self.folder),
            PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1",
        )

    def configure(self, **values) -> None:
        keys = ("KANBAN_CONFIG", "KANBAN_SETTINGS_DIR", "KANBAN_LOCAL_DIR")
        with mock.patch.dict(os.environ, {k: self.env[k] for k in keys}):
            cfg = config.load_config()
            for key, value in values.items():
                setattr(cfg, key, value)
            config.save_config(cfg)

    def run_entry(self) -> dict:
        done = subprocess.run([sys.executable, "export_distribution.py"], cwd=ROOT, env=self.env,
                              capture_output=True, timeout=60)
        lines = done.stdout.decode("utf-8").splitlines()
        self.assertEqual(len(lines), 1,
                         f"標準出力は答えの 1 行だけ: {lines}\n{done.stderr.decode('utf-8', 'replace')}")
        return json.loads(lines[0])

    def test_nothing_to_write_is_told_apart_from_a_failure(self):
        reply = self.run_entry()
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["reason"], distribution.REFUSE_NOTHING)
        self.assertFalse(self.folder.exists())

    def test_writes_the_screen_default_choice_without_a_password(self):
        self.configure(shared_db_path=r"\\server\看板マスタ.sqlite3", import_interval_sec=45,
                       admin_password_hash=config.hash_password("secret"), line="AIM")
        reply = self.run_entry()
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["path"], str(self.folder))
        settings = json.loads((self.folder / "設定.json").read_text(encoding="utf-8"))["settings"]
        self.assertEqual(settings["shared_db_path"], r"\\server\看板マスタ.sqlite3")
        self.assertEqual(settings["import_interval_sec"], 45)
        self.assertIn("admin_password_hash", settings)
        self.assertNotIn("line", settings, "担当ラインは既定で外す(画面と同じ)")

    def test_the_check_after_writing_still_applies(self):
        """管理者パスワードを入れずに配ると、最初に押した人が決められる(画面で押したときと同じ確認)。"""
        self.configure(shared_db_path=r"\\server\看板マスタ.sqlite3")
        reply = self.run_entry()
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["reason"], "verify_failed")
        self.assertIn("管理者パスワード", reply["message"])

    def test_the_items_are_the_screen_defaults(self):
        html = (ROOT / "app" / "templates" / "settings.html").read_text(encoding="utf-8")
        self.assertIn('data-dist-item="{{ it.key }}" {% if it.default %}checked{% endif %}', html)
        script = (ROOT / "export_distribution.py").read_text(encoding="utf-8")
        self.assertIn("if default]", script)

    def test_it_goes_into_the_distribution(self):
        spec = importlib.util.spec_from_file_location("kanban_make_dist_t", ROOT / "scripts" / "make_dist.py")
        make_dist = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(make_dist)
        self.assertIn("export_distribution.py", make_dist.INCLUDE)


if __name__ == "__main__":
    unittest.main()
