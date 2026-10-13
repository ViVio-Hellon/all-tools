"""配布設定を書き出すだけの入口(``export_distribution.py``)

日報複合ツールの大設定の「4ツールの分もまとめて書き出す」が、このフォルダで子として起こす。

- 設定画面の既定の選び方のまま書き出す(ラインは入れない)
- パスワードは聞かない(呼ぶ側の大設定が確かめる。``distribution.export(admin=True)``)
- 答えは標準出力に JSON 1行。書き出せる設定が無いときは ok=false・reason="nothing"(失敗と分ける)
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

from calendar_app import admin_password, distribution, settings as user_settings

_ROOT = Path(__file__).resolve().parent.parent


class ExportEntryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="calendar_export_entry_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.folder = self.tmp / "配布設定"
        # 何も設定していない端末(この端末の設定ファイル・配布設定を一時フォルダへ)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("CALENDAR_")}
        self.env.update(CALENDAR_HOME=str(self.tmp / "home"), CALENDAR_LOCAL_DIR=str(self.tmp / "local"),
                        CALENDAR_DISTRIBUTION_DIR=str(self.folder),
                        PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")

    def configure(self, **values) -> None:
        path = self.tmp / "home" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")

    def run_entry(self) -> dict:
        done = subprocess.run([sys.executable, "export_distribution.py"], cwd=_ROOT, env=self.env,
                              capture_output=True, timeout=60)
        lines = done.stdout.decode("utf-8").splitlines()
        self.assertEqual(len(lines), 1,
                         f"標準出力は答えの1行だけ: {lines}\n{done.stderr.decode('utf-8', 'replace')}")
        return json.loads(lines[0])

    def test_既定のままなら書き出さず_理由で分かる(self) -> None:
        reply = self.run_entry()
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["reason"], distribution.REFUSE_NOTHING)
        self.assertFalse(self.folder.exists())

    def test_値があれば画面の既定の選び方で書き出す_パスワードは聞かない(self) -> None:
        self.configure(**{user_settings.KEY_DATA_DB_DIR: r"\\srv\カレンダー",
                          user_settings.KEY_MY_LINE: "AIM",
                          # この端末でパスワードを変えてあっても、ここでは聞かない
                          admin_password.KEY: admin_password.hash_for_distribution("line2026")})
        reply = self.run_entry()
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["path"], str(self.folder))
        settings = json.loads((self.folder / distribution.SETTINGS_NAME).read_text(encoding="utf-8"))["settings"]
        self.assertEqual(settings[user_settings.KEY_DATA_DB_DIR], r"\\srv\カレンダー")
        self.assertIn(admin_password.KEY, settings)
        self.assertNotIn(user_settings.KEY_MY_LINE, settings, "ラインは既定で外す(画面と同じ)")

    def test_設定画面からはパスワードが要るまま(self) -> None:
        result = distribution.export("", [user_settings.KEY_DATA_DB_DIR])
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, distribution.REFUSE_NEED_PASSWORD)

    def test_書き出す項目は画面の既定のチェックと同じ(self) -> None:
        js = (_ROOT / "app" / "static" / "js" / "views" / "settings.js").read_text(encoding="utf-8")
        self.assertIn("input.checked = it.default;", js)
        script = (_ROOT / "export_distribution.py").read_text(encoding="utf-8")
        self.assertIn("if default]", script)

    def test_配布用フォルダに入る(self) -> None:
        spec = importlib.util.spec_from_file_location("calendar_make_dist_t", _ROOT / "tools" / "make_dist.py")
        make_dist = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(make_dist)
        self.assertIn("export_distribution.py", make_dist.INCLUDE)


if __name__ == "__main__":
    unittest.main()
