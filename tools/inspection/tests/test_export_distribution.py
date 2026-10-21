"""配布設定を書き出すだけの入口(`export_distribution.py`)

日報複合ツールの大設定の「4ツールの分もまとめて書き出す」が、このフォルダで子として起こす。

- 設定画面の既定の選び方のまま書き出す(表示(ダーク/ライト)は入れない)
- パスワードは聞かない(呼ぶ側の大設定が確かめる)
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

from app.services import distribution

_ROOT = Path(__file__).resolve().parent.parent


class ExportEntryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="inspection_export_entry_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.folder = self.tmp / "配布設定"
        # 何も設定していない端末(この端末の設定ファイル・配布設定を一時フォルダへ)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("INSPECTION_")}
        self.env.update(INSPECTION_LOCAL_DIR=str(self.tmp / "local"),
                        INSPECTION_DISTRIBUTION_DIR=str(self.folder),
                        PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")

    def configure(self, **values) -> None:
        path = self.tmp / "local" / "data" / "user_settings.json"
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
        self.configure(root_folder=r"\\srv\点検表", dark_mode=True, log_keep_days=30)
        reply = self.run_entry()
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["path"], str(self.folder))
        self.assertEqual(reply["applied"], ["点検表フォルダ", "ログの保存日数"])
        settings = json.loads((self.folder / distribution.SETTINGS_NAME).read_text(encoding="utf-8"))["settings"]
        self.assertEqual(settings, {"root_folder": r"\\srv\点検表", "log_keep_days": 30},
                         "表示(ダーク/ライト)は既定で外す(画面と同じ)")

    # 書き出す項目が画面の既定のチェックと同じことは、設定の「配布設定」を既定のまま書き出して
    # test_settings_browser.DistributionBrowserTests が入口の書き出しと中身を比べて見る

    def test_配布用フォルダに入る(self) -> None:
        """点検表は make_dist を持たない。日報複合ツールの一式の make_dist が一覧で写す。"""
        root = _ROOT.parent.parent
        if not (root / "scripts" / "make_dist.py").is_file():
            self.skipTest("日報複合ツールの一式の中ではない")
        spec = importlib.util.spec_from_file_location("alltools_make_dist_t", root / "scripts" / "make_dist.py")
        make_dist = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(make_dist)
        self.assertIn("export_distribution.py", make_dist.GENERIC_INCLUDE["inspection"])


if __name__ == "__main__":
    unittest.main()
