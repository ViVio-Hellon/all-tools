"""アプリのフォルダを丸ごと上書きしても、データとこの端末の設定が残る・アプリのフォルダに書かない

業務ツール統合ランチャー 1.8.9「更新のため全 PC で止める」は、共有サーバーのツールのフォルダを
**丸ごと上書きして**更新する。動いているあいだにアプリのフォルダへ書いた物は、更新で消える・
古いものに戻る・書きかけのファイルが上書きを止める(共有なら何台もの PC が同じファイルに書く)。

    * 起動の入口(start_app.py・bridge.py・process_manager.py・export_distribution.py)が、
      アプリのフォルダに `__pycache__` を書かない(前は start_app.py・bridge.py で書いていた)
    * 作業用DB(nippou_local.sqlite3)・この端末の設定(user_config.json)はローカル領域にあり、
      アプリのフォルダを入れ替えても残る

試験はアプリの写しを一時フォルダに作り、本物の入口を子のプロセスで動かす。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
#: 写さないもの(試験・作るもの・配布設定)
SKIP = {"tests", ".git", "__pycache__", ".ruff_cache", ".pytest_cache", "配布設定", "配布設定.作成中", "配布先"}


def copy_app(dest: Path) -> Path:
    shutil.copytree(ROOT, dest, ignore=lambda _d, names: [n for n in names if n in SKIP])
    return dest


def listing(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))


class UpdateOverwriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="nippou_update_"))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.app = copy_app(self.base / "日報管理ツール")
        self.local = self.base / "LocalAppData"
        # 本番と同じ決め方にする(試験用の差し替えを外し、LOCALAPPDATA だけを一時フォルダへ)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("NIPPOU_", "ALLTOOLS_"))
               and k not in ("PYTHONPYCACHEPREFIX", "PYTHONDONTWRITEBYTECODE")}
        env.update(PYTHONIOENCODING="utf-8", LOCALAPPDATA=str(self.local), APPDATA=str(self.base / "AppData"),
                   XDG_DATA_HOME=str(self.local))
        self.env = env

    def run_in_app(self, *args: str, code: str = "") -> subprocess.CompletedProcess:
        cmd = [sys.executable, "-c", code] if code else [sys.executable, *args]
        # 試験の小さな台本は入口を通らないので、写しを書かせない(アプリのフォルダの中身を比べるため)
        env = dict(self.env, PYTHONDONTWRITEBYTECODE="1") if code else self.env
        return subprocess.run(cmd, cwd=str(self.app), env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=180)

    def test_起動の入口はアプリのフォルダにpycacheを書かない(self) -> None:
        before = listing(self.app)
        for args in (["start_app.py", "--check"], ["bridge.py"], ["process_manager.py", "--status"],
                     ["export_distribution.py", "--help"]):
            self.run_in_app(*args)
            caches = [p for p in listing(self.app) if "__pycache__" in p or p.endswith(".pyc")]
            self.assertEqual(caches, [], f"{' '.join(args)} がアプリのフォルダに Python の写しを書いた")
        self.assertEqual(listing(self.app), before, "アプリのフォルダに何かを書いた")
        self.assertTrue(list((self.local / "NippouTool" / "pycache").rglob("*.pyc")),
                        "ローカル領域の pycache に写しが無い")

    def test_アプリのフォルダを丸ごと上書きしても_データと設定が残る(self) -> None:
        done = self.run_in_app(code=(
            "from nippou import config, user_settings\n"
            "from nippou.db.connection import connect\n"
            "assert user_settings.save(config.KEY_ACCESS_DIR, r'\\\\srv\\share\\nippou')\n"
            "conn = connect(config.SETTINGS.sqlite_path)\n"
            "conn.execute('CREATE TABLE update_probe(v TEXT)')\n"
            "conn.execute(\"INSERT INTO update_probe VALUES ('打ちかけ')\")\n"
            "conn.commit(); conn.close()\n"
            "print(config.SETTINGS.sqlite_path); print(config.USER_CONFIG_PATH)\n"))
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        for line in done.stdout.decode("utf-8").splitlines():
            self.assertTrue(Path(line).is_file(), line)
            self.assertNotIn(str(self.app), line, "アプリのフォルダに書いた")
        before = listing(self.app)

        # 更新: アプリのフォルダを消して、新しい一式を置く
        shutil.rmtree(self.app)
        copy_app(self.app)
        self.assertEqual(listing(self.app), before)

        done = self.run_in_app(code=(
            "from nippou import config, user_settings\n"
            "from nippou.db.connection import connect\n"
            "print(user_settings.get(config.KEY_ACCESS_DIR))\n"
            "conn = connect(config.SETTINGS.sqlite_path)\n"
            "print(conn.execute('SELECT v FROM update_probe').fetchone()[0]); conn.close()\n"))
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        self.assertEqual(done.stdout.decode("utf-8").split(), ["\\\\srv\\share\\nippou", "打ちかけ"])


if __name__ == "__main__":
    unittest.main()
