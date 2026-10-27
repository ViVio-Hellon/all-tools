"""配布設定を書き出すだけの入口(`export_distribution.py`)

日報複合ツールの大設定の「4ツールの分もまとめて書き出す」が、このフォルダで子として起こす。

- 設定画面の既定の選び方のまま書き出す(この端末のラインは入れない・音声ファイルも入れない)
- ただし**前に設定画面で入れていたラインと音声ファイル(`配布設定\\音`)は残す**(ラインは前の値のまま)
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

    def screen_export(self, items: list[str], files: list[str]) -> None:
        """設定画面の「配布設定を書き出す」で項目を選んで書き出したのと同じ(この端末・この配布設定)。"""
        code = ("import json, sys; from nippou import distribution as d; "
                "r = d.export('', json.loads(sys.argv[1]), json.loads(sys.argv[2]), admin=True); "
                "sys.exit(0 if r.ok else r.message)")
        subprocess.run([sys.executable, "-c", code, json.dumps(items), json.dumps(files)], cwd=ROOT,
                       env=self.env, check=True, capture_output=True, timeout=60)

    def configure_line_and_sounds(self) -> bytes:
        """共有の日報管理のパス・この端末のライン・鳴らしている音声ファイル1つ。音の中身を返す。"""
        from nippou.logic.sound import SOUNDS

        sounds = self.tmp / "共有の音"
        sounds.mkdir()
        spec = next(s for s in SOUNDS if s.default_file)
        body = b"RIFF" + spec.key.encode()
        (sounds / spec.default_file).write_bytes(body)
        self.configure(**{config.KEY_ACCESS_DIR: r"\\srv\日報", config.KEY_TERMINAL_LINE: "L-1",
                          config.KEY_SOUND_DIR: str(sounds)})
        return body

    def test_前に入れていたラインと音声ファイルは残す_ほかは既定の選び方(self) -> None:
        """大設定の「まとめて書き出す」で、設定画面で入れていた音とラインが消えた(既定の選び方で丸ごと書き直していた)。"""
        body = self.configure_line_and_sounds()
        default = [i.key for i in distribution.ITEMS if i.default]
        self.screen_export(default + [distribution.ITEM_LINE], [distribution.FILE_SOUNDS])
        before = json.loads((self.folder / distribution.SETTINGS_NAME).read_text(encoding="utf-8"))["settings"]
        self.assertEqual(before[config.KEY_TERMINAL_LINE], "L-1")
        # そのあと、この端末(配る元)のラインを変えた。残すのは**前に入れていた値**
        self.configure(**{config.KEY_ACCESS_DIR: r"\\srv\日報2", config.KEY_TERMINAL_LINE: "L-9",
                          config.KEY_SOUND_DIR: str(self.tmp / "共有の音")})

        reply = self.run_entry()
        self.assertTrue(reply["ok"], reply)
        data = json.loads((self.folder / distribution.SETTINGS_NAME).read_text(encoding="utf-8"))
        self.assertEqual(data["settings"][config.KEY_ACCESS_DIR], r"\\srv\日報2", "ほかは既定の選び方でいまの値")
        self.assertEqual(data["settings"][config.KEY_TERMINAL_LINE], "L-1", "前に入れていたラインは値もそのまま")
        self.assertNotIn(config.KEY_SOUND_DIR, data["settings"],
                         "音声ファイルを残すので、音の置き場所は入れない(画面で音を入れたときと同じ)")
        self.assertIn(config.KEY_TERMINAL_LINE, data["labels"], "大設定の一覧に名前で出す")
        sounds = sorted((self.folder / distribution.SOUNDS_DIRNAME).iterdir())
        self.assertEqual([p.read_bytes() for p in sounds], [body], "音声ファイルは消さずに残す")
        self.assertEqual(reply["kept"], ["音声ファイル 1 個", "ライン(L-1)"])
        self.assertIn("そのまま残しました", reply["message"])

    def test_前に入れていなければラインも音も入れない(self) -> None:
        self.configure_line_and_sounds()
        self.screen_export([i.key for i in distribution.ITEMS if i.default], [])
        reply = self.run_entry()
        self.assertTrue(reply["ok"], reply)
        data = json.loads((self.folder / distribution.SETTINGS_NAME).read_text(encoding="utf-8"))
        self.assertNotIn(config.KEY_TERMINAL_LINE, data["settings"])
        self.assertIn(config.KEY_SOUND_DIR, data["settings"], "音を入れていなければ、音の置き場所は今までどおり入る")
        self.assertFalse((self.folder / distribution.SOUNDS_DIRNAME).exists())
        self.assertEqual(reply["kept"], [])

    def test_設定画面から書き出すと選んだとおり_音とラインを外せる(self) -> None:
        self.configure_line_and_sounds()
        default = [i.key for i in distribution.ITEMS if i.default]
        self.screen_export(default + [distribution.ITEM_LINE], [distribution.FILE_SOUNDS])
        self.screen_export(default, [])
        data = json.loads((self.folder / distribution.SETTINGS_NAME).read_text(encoding="utf-8"))
        self.assertNotIn(config.KEY_TERMINAL_LINE, data["settings"])
        self.assertFalse((self.folder / distribution.SOUNDS_DIRNAME).exists())

    def test_書き出す項目は画面の既定のチェックと同じ(self) -> None:
        html = (ROOT / "app" / "templates" / "settings.html").read_text(encoding="utf-8")
        self.assertIn('data-dist-item="{{ it.key }}" {% if it.default %}checked{% endif %}', html)
        self.assertIn('<input type="checkbox" data-dist-file="{{ f.key }}">', html, "音声ファイルは既定で外れている")
        script = (ROOT / "export_distribution.py").read_text(encoding="utf-8")
        self.assertIn("if item.default", script)
        self.assertIn('distribution.export("", items, [], admin=True, keep_previous=True)', script)

    def test_配布用フォルダに入る(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("nippou_make_dist_t", ROOT / "scripts" / "make_dist.py")
        make_dist = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(make_dist)
        self.assertIn("export_distribution.py", make_dist.INCLUDE)


if __name__ == "__main__":
    unittest.main()
