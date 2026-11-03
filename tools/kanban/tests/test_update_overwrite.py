"""アプリのフォルダを丸ごと上書きしても、データとこの端末の設定が残る・アプリのフォルダに書かない

業務ツール統合ランチャー 1.8.9「更新のため全 PC で止める」は、共有サーバーのツールのフォルダを
**丸ごと上書きして**更新する。動いているあいだにアプリのフォルダへ書いた物は、更新で消える・
古いものに戻る・書きかけのファイルが上書きを止める(共有なら何台もの PC が同じファイルに書く)。

    * 起動の入口(start_app.py・bridge.py・main.py・process_manager.py・export_distribution.py)が、
      アプリのフォルダに `__pycache__` を書かない(前は main.py・process_manager.py・
      export_distribution.py で書いていた)
    * 作業用DB(kanban.sqlite3)はローカル領域、この端末の設定(config.json)は %APPDATA% にあり、
      アプリのフォルダを入れ替えても残る
    * 一時期の版がアプリのフォルダに置いた ``data\\config.json`` は、初回に引き継いでいるので、
      更新で消えても値は残る

試験はアプリの写しを一時フォルダに作り、本物の入口を子のプロセスで動かす。
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

ROOT = Path(__file__).resolve().parent.parent
#: 写さないもの(試験・作るもの・配布設定)
SKIP = {"tests", ".git", "__pycache__", ".ruff_cache", ".pytest_cache", "配布設定", "配布設定.作成中",
        "data", "DebugPrint", "build", "dist"}

#: 作業用DBの置き場所(start_app._load_config と同じ決め方)
STORE = ("from kanban import app_config, config\n"
         "cfg = config.load_config()\n"
         "path = cfg.sqlite_path or str(app_config.local_dir('data') / 'kanban.sqlite3')\n")


def copy_app(dest: Path) -> Path:
    shutil.copytree(ROOT, dest, ignore=lambda _d, names: [n for n in names if n in SKIP])
    return dest


def listing(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))


class UpdateOverwriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="kanban_update_"))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.app = copy_app(self.base / "資材発注看板")
        self.local = self.base / "LocalAppData"
        self.appdata = self.base / "AppData"
        # 本番と同じ決め方にする(試験用の差し替えを外し、LOCALAPPDATA・APPDATA だけを一時フォルダへ)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("KANBAN_", "ALLTOOLS_"))
               and k not in ("PYTHONPYCACHEPREFIX", "PYTHONDONTWRITEBYTECODE")}
        env.update(PYTHONIOENCODING="utf-8", LOCALAPPDATA=str(self.local), APPDATA=str(self.appdata), XDG_DATA_HOME=str(self.local))
        self.env = env

    def run_in_app(self, *args: str, code: str = "") -> subprocess.CompletedProcess:
        cmd = [sys.executable, "-c", code] if code else [sys.executable, *args]
        # 試験の小さな台本は入口を通らないので、写しを書かせない(アプリのフォルダの中身を比べるため)
        env = dict(self.env, PYTHONDONTWRITEBYTECODE="1") if code else self.env
        return subprocess.run(cmd, cwd=str(self.app), env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=180)

    def update(self) -> None:
        """更新: アプリのフォルダを消して、新しい一式を置く。"""
        before = [p for p in listing(self.app) if not p.startswith("data")]
        shutil.rmtree(self.app)
        copy_app(self.app)
        self.assertEqual(listing(self.app), before, "アプリのフォルダに何かを書いた")

    def test_起動の入口はアプリのフォルダにpycacheを書かない(self) -> None:
        before = listing(self.app)
        for args in (["start_app.py", "--check"], ["bridge.py"], ["main.py", "--show-config"],
                     ["process_manager.py"], ["export_distribution.py", "--help"]):
            self.run_in_app(*args)
            caches = [p for p in listing(self.app) if "__pycache__" in p or p.endswith(".pyc")]
            self.assertEqual(caches, [], f"{' '.join(args)} がアプリのフォルダに Python の写しを書いた")
        self.assertEqual(listing(self.app), before, "アプリのフォルダに何かを書いた")
        self.assertTrue(list((self.local / "KanbanSystem" / "pycache").rglob("*.pyc")),
                        "ローカル領域の pycache に写しが無い")

    def test_アプリのフォルダを丸ごと上書きしても_データと設定が残る(self) -> None:
        done = self.run_in_app(code=STORE + (
            "from kanban.db.store import Store\n"
            "cfg.shared_db_path = r'\\\\srv\\share\\看板マスタ.sqlite3'\n"
            "config.save_config(cfg)\n"
            "store = Store(path)\n"
            "store.connection.execute('CREATE TABLE update_probe(v TEXT)')\n"
            "store.connection.execute(\"INSERT INTO update_probe VALUES ('発注')\")\n"
            "store.connection.commit(); store.close()\n"
            "print(path); print(config.settings_path())\n"))
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        for line in done.stdout.decode("utf-8").splitlines():
            self.assertTrue(Path(line).is_file(), line)
            self.assertNotIn(str(self.app), line, "アプリのフォルダに書いた")

        self.update()

        done = self.run_in_app(code=STORE + (
            "import sqlite3\n"
            "print(cfg.shared_db_path)\n"
            "conn = sqlite3.connect(path)\n"
            "print(conn.execute('SELECT v FROM update_probe').fetchone()[0]); conn.close()\n"))
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        self.assertEqual(done.stdout.decode("utf-8").split(), ["\\\\srv\\share\\看板マスタ.sqlite3", "発注"])

    def test_前の版がアプリのフォルダに置いた設定は_引き継いでから更新で消えても残る(self) -> None:
        legacy = self.app / "data" / "config.json"
        legacy.parent.mkdir()
        legacy.write_text(json.dumps({"shared_db_path": r"\\old\share\看板マスタ.sqlite3", "line": "LVC"},
                                     ensure_ascii=False), encoding="utf-8")
        done = self.run_in_app(code=STORE + "print(cfg.shared_db_path); print(cfg.line)\n")
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        self.assertEqual(done.stdout.decode("utf-8").split(), ["\\\\old\\share\\看板マスタ.sqlite3", "LVC"])
        self.assertTrue((self.appdata / "KanbanSystem" / "config.json").is_file(), "この端末の設定へ引き継いでいない")

        self.update()                             # 更新で data\config.json は無くなる
        self.assertFalse(legacy.exists())

        done = self.run_in_app(code=STORE + "print(cfg.shared_db_path); print(cfg.line)\n")
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        self.assertEqual(done.stdout.decode("utf-8").split(), ["\\\\old\\share\\看板マスタ.sqlite3", "LVC"])


if __name__ == "__main__":
    unittest.main()
