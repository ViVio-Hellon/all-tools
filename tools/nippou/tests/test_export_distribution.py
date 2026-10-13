"""配布設定を書き出すだけの入口(`export_distribution.py`)

日報複合ツールの大設定の「4ツールの分もまとめて書き出す」が、このフォルダで子として起こす。

- 設定画面の既定の選び方のまま書き出す(この端末のラインは入れない・音声ファイルも入れない)
- パスワードは聞かない(呼ぶ側の大設定が確かめる)
- 答えは標準出力に JSON 1行。書き出せる設定が無いときは ok=false・reason="nothing"(失敗と分ける)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou import config, distribution

ROOT = Path(__file__).resolve().parent.parent      # tools/nippou


class ExportEntryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="nippou_export_entry_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.folder = self.tmp / "配布設定"
        # 何も設定していない端末(この端末の設定ファイル・配布設定を一時フォルダへ)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("NIPPOU_")}
        self.env.update(NIPPOU_LOCAL_DIR=str(self.tmp / "local"), NIPPOU_DIST_DIR=str(self.folder),
                        PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")

    def configure(self, **values) -> None:
        path = self.tmp / "local" / "runtime" / "user_config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")

    def run_entry(self) -> dict:
        done = subprocess.run([sys.executable, "export_distribution.py"], cwd=ROOT, env=self.env,
                              capture_output=True, timeout=60)
        lines = done.stdout.decode("utf-8").splitlines()
        self.assertEqual(len(lines), 1, f"標準出力は答えの1行だけ: {lines}\n{done.stderr.decode('utf-8', 'replace')}")
        return json.loads(lines[0])

    def test_既定のままなら書き出さず_理由で分かる(self) -> None:
        reply = self.run_entry()
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["reason"], distribution.REFUSE_NOTHING)
        self.assertIn("書き出せる設定がありません", reply["message"])
        self.assertFalse(self.folder.exists())

    def test_値があれば画面の既定の選び方で書き出す_パスワードは聞かない(self) -> None:
        self.configure(**{config.KEY_ACCESS_DIR: r"\\srv\日報", config.KEY_TERMINAL_LINE: "AIM"})
        reply = self.run_entry()
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["applied"], ["共有の日報管理のパス"])
        self.assertEqual(reply["path"], str(self.folder))
        data = json.loads((self.folder / distribution.SETTINGS_NAME).read_text(encoding="utf-8"))
        self.assertEqual(data["settings"], {config.KEY_ACCESS_DIR: r"\\srv\日報"},
                         "この端末のラインは既定で外す(画面と同じ)")
        self.assertFalse((self.folder / distribution.SOUNDS_DIRNAME).exists(),
                         "音声ファイルは既定で入れない(画面と同じ)")

    def test_書き出す項目は画面の既定のチェックと同じ(self) -> None:
        html = (ROOT / "app" / "templates" / "settings.html").read_text(encoding="utf-8")
        self.assertIn('data-dist-item="{{ it.key }}" {% if it.default %}checked{% endif %}', html)
        self.assertIn('<input type="checkbox" data-dist-file="{{ f.key }}">', html, "音声ファイルは既定で外れている")
        script = (ROOT / "export_distribution.py").read_text(encoding="utf-8")
        self.assertIn("if item.default", script)
        self.assertIn('distribution.export("", items, [], admin=True)', script)

    def test_配布用フォルダに入る(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("nippou_make_dist_t", ROOT / "scripts" / "make_dist.py")
        make_dist = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(make_dist)
        self.assertIn("export_distribution.py", make_dist.INCLUDE)


if __name__ == "__main__":
    unittest.main()
