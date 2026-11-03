"""一式のフォルダを丸ごと上書きしても、この端末の設定が残る・一式のフォルダに書かない

業務ツール統合ランチャー 1.8.9「更新のため全 PC で止める」は、共有サーバーの一式のフォルダを
**丸ごと上書きして**更新する。動いているあいだに一式のフォルダへ書いた物は、更新で消える・
古いものに戻る・書きかけのファイルが上書きを止める(共有なら何台もの PC が同じファイルに書く)。

    * 起動の入口(start_app.py・bridge.py・process_manager.py)が、一式のフォルダに
      `__pycache__` を書かない(手元の領域の pycache へ)
    * 大設定で決めた値(この端末の設定)は手元の領域にあり、一式を入れ替えても残る

試験は一式の写しを一時フォルダに作り、本物の入口を子のプロセスで動かす。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import tests  # noqa: F401  (一時フォルダへ向ける)

ROOT = Path(__file__).resolve().parent.parent
#: 写さないもの(試験・開発用・作るもの)。tools\ は大きいので入口の試験には要らない
SKIP = {"tests", "tools", "src-tauri", "docs", ".git", ".claude", "__pycache__", ".ruff_cache",
        ".pytest_cache", "配布設定", "配布設定.作成中", "logs", "data"}


def copy_app(dest: Path) -> Path:
    shutil.copytree(ROOT, dest, ignore=lambda _d, names: [n for n in names if n in SKIP])
    (dest / "tools").mkdir()
    return dest


def listing(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))


class UpdateOverwriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="alltools_update_"))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.app = copy_app(self.base / "日報複合ツール")
        self.local = self.base / "LocalAppData"
        # 本番と同じ決め方にする(試験用の差し替えを外し、LOCALAPPDATA だけを一時フォルダへ)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("ALLTOOLS_") and k not in ("PYTHONPYCACHEPREFIX", "PYTHONDONTWRITEBYTECODE")}
        env.update(PYTHONIOENCODING="utf-8", LOCALAPPDATA=str(self.local), APPDATA=str(self.base / "AppData"),
                   XDG_DATA_HOME=str(self.local), ALLTOOLS_LOG_QUIET="1",
                   ALLTOOLS_LOGIN_ID="tester", ALLTOOLS_PC_NAME="TEST-PC")
        self.env = env

    def run_in_app(self, *args: str, code: str = "") -> subprocess.CompletedProcess:
        cmd = [sys.executable, "-c", code] if code else [sys.executable, *args]
        # 試験の小さな台本は入口を通らないので、写しを書かせない(一式のフォルダの中身を比べるため)
        env = dict(self.env, PYTHONDONTWRITEBYTECODE="1") if code else self.env
        return subprocess.run(cmd, cwd=str(self.app), env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=120)

    def test_起動の入口は一式のフォルダにpycacheを書かない(self) -> None:
        before = listing(self.app)
        for args in (["start_app.py", "--check"], ["bridge.py"], ["process_manager.py", "--status"]):
            self.run_in_app(*args)
            caches = [p for p in listing(self.app) if "__pycache__" in p or p.endswith(".pyc")]
            self.assertEqual(caches, [], f"{' '.join(args)} が一式のフォルダに Python の写しを書いた")
        self.assertEqual(listing(self.app), before, "一式のフォルダに何かを書いた")
        self.assertTrue(list((self.local / "AllTools" / "pycache").rglob("*.pyc")),
                        "手元の領域の pycache に写しが無い")

    def test_一式を丸ごと上書きしても_この端末の設定が残る(self) -> None:
        done = self.run_in_app(code=(
            "from portal import user_settings as u\n"
            "u.update({u.KEY_SHARED_DIR: r'\\\\srv\\share\\db', 'theme_default': 'dark'})\n"
            "print(u.path())\n"))
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        where = Path(done.stdout.decode("utf-8").strip())
        self.assertTrue(where.is_file())
        self.assertNotIn(str(self.app), str(where), "この端末の設定を一式のフォルダに書いた")
        before = listing(self.app)

        # 更新: 一式のフォルダを消して、新しい一式を置く
        shutil.rmtree(self.app)
        copy_app(self.app)
        self.assertEqual(listing(self.app), before)

        done = self.run_in_app(code=(
            "from portal import user_settings as u\n"
            "print(u.get(u.KEY_SHARED_DIR)); print(u.get('theme_default'))\n"))
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        self.assertEqual(done.stdout.decode("utf-8").split(), ["\\\\srv\\share\\db", "dark"])


if __name__ == "__main__":
    unittest.main()
