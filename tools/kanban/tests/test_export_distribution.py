"""配布設定を書き出すだけの入口(``export_distribution.py``)

日報複合ツールの大設定の「4ツールの分もまとめて書き出す」が、このフォルダで子として起こす。

- 設定画面の既定の選び方のまま書き出す(担当ラインは入れない)。書き出した直後の確認も同じ
- ただし**前に設定画面で入れていた担当ラインは残す**(値も前のまま)。前に入れていなければ入れない
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
        """管理者パスワードを入れずに配ると、配った先は既定の値のまま(画面で押したときと同じ確認)。"""
        self.configure(shared_db_path=r"\\server\看板マスタ.sqlite3")
        reply = self.run_entry()
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["reason"], "verify_failed")
        self.assertIn("管理者パスワード", reply["message"])

    def screen_export(self, items: list[str]) -> None:
        """設定画面の「配布設定を書き出す」で項目を選んで書き出したのと同じ(この端末・この配布設定)。"""
        code = ("import json, sys; from kanban import distribution as d; "
                "r = d.export(json.loads(sys.argv[1])); sys.exit(0 if r.ok else r.message)")
        subprocess.run([sys.executable, "-c", code, json.dumps(items)], cwd=ROOT, env=self.env,
                       check=True, capture_output=True, timeout=60)

    def test_a_line_put_in_before_is_kept_with_its_value(self):
        """大設定の「まとめて書き出す」で、設定画面で入れていた担当ラインが消えた(既定の選び方で丸ごと書き直していた)。"""
        hashed = config.hash_password("secret")
        self.configure(shared_db_path=r"\\server\看板マスタ.sqlite3", admin_password_hash=hashed, line="L-1")
        default = [k for k, _, d in distribution.ITEMS if d]
        self.screen_export(default + ["line"])
        # そのあと、この端末(配る元)の担当ラインと接続先を変えた。残すのは**前に入れていた値**
        self.configure(shared_db_path=r"\\server\看板マスタ2.sqlite3", line="L-9")
        reply = self.run_entry()
        self.assertTrue(reply["ok"], reply)
        data = json.loads((self.folder / "設定.json").read_text(encoding="utf-8"))
        self.assertEqual(data["settings"]["shared_db_path"], r"\\server\看板マスタ2.sqlite3", "ほかはいまの値")
        self.assertEqual(data["settings"]["line"], "L-1", "前に入れていた担当ラインは値もそのまま")
        self.assertIn("line", data["labels"])
        self.assertEqual(reply["kept"], ["担当ライン(L-1)"])
        self.assertIn("そのまま残しました", reply["message"])

    def test_csv_dir_and_log_dir_put_in_before_are_kept_too(self):
        """「CSVの書き出し先と記録の置き場所も残してください」。前に入れていれば値のまま、いまの値ではない。"""
        hashed = config.hash_password("secret")
        self.configure(shared_db_path=r"\\server\看板マスタ.sqlite3", admin_password_hash=hashed,
                       csv_dir=r"\\server\看板CSV", log_dir=r"\\server\看板ログ")
        default = [k for k, _, d in distribution.ITEMS if d]
        self.screen_export(default + ["csv_dir", "log_dir"])
        self.configure(csv_dir=r"C:\あとで変えた", log_dir=r"C:\あとで変えたログ")
        reply = self.run_entry()
        self.assertTrue(reply["ok"], reply)
        data = json.loads((self.folder / "設定.json").read_text(encoding="utf-8"))
        self.assertEqual(data["settings"]["csv_dir"], r"\\server\看板CSV")
        self.assertEqual(data["settings"]["log_dir"], r"\\server\看板ログ")
        self.assertEqual(reply["kept"], [r"CSV の書き出し先(\\server\看板CSV)", r"記録(ログ)の置き場所(\\server\看板ログ)"])

    def test_no_line_unless_it_was_put_in_before(self):
        self.configure(shared_db_path=r"\\server\看板マスタ.sqlite3",
                       admin_password_hash=config.hash_password("secret"), line="L-1")
        self.screen_export([k for k, _, d in distribution.ITEMS if d])
        reply = self.run_entry()
        self.assertTrue(reply["ok"], reply)
        self.assertNotIn("line", json.loads((self.folder / "設定.json").read_text(encoding="utf-8"))["settings"])
        self.assertEqual(reply["kept"], [])

    def test_the_screen_can_still_leave_the_line_out(self):
        self.configure(shared_db_path=r"\\server\看板マスタ.sqlite3",
                       admin_password_hash=config.hash_password("secret"), line="L-1")
        default = [k for k, _, d in distribution.ITEMS if d]
        self.screen_export(default + ["line"])
        self.screen_export(default)
        self.assertNotIn("line", json.loads((self.folder / "設定.json").read_text(encoding="utf-8"))["settings"])

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
