"""ブラウザ版とデスクトップ版を同時に動かさない(`core/instance_guard.py`)

どちらを後から開いても、後から開いたほうが止まる。同じ種類なら今までどおり
(ブラウザ版はつなぐ・デスクトップ版は窓を前に出す)。錠は OS の名前付きの錠
(Windows は名前付きミューテックス、ここ(Linux)はファイルロック)。
Rust 側(`src-tauri/src/instance.rs`)も同じ名前・同じ手順で錠を扱う。
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
import unittest
from pathlib import Path

import tests.helpers  # noqa: F401  (ローカル領域を一時フォルダへ)

from core import instance_guard as guard

ROOT = Path(__file__).resolve().parent.parent


class ClaimTest(unittest.TestCase):
    def setUp(self) -> None:
        guard.release_all()
        self.addCleanup(guard.release_all)

    def test_ブラウザ版が先なら_デスクトップ版は止まる(self) -> None:
        first = guard.claim(guard.BROWSER)
        self.assertTrue(first.ok)
        second = guard.claim(guard.DESKTOP)
        self.assertFalse(second.ok)
        self.assertEqual(second.running, guard.BROWSER)
        self.assertTrue(second.other_kind_running)
        self.assertEqual(guard.running(), guard.BROWSER)

    def test_デスクトップ版が先なら_ブラウザ版は止まる(self) -> None:
        self.assertTrue(guard.claim(guard.DESKTOP).ok)
        second = guard.claim(guard.BROWSER)
        self.assertTrue(second.other_kind_running)
        self.assertEqual(guard.running(), guard.DESKTOP)

    def test_同じ種類なら相手の種類とは言わない(self) -> None:
        self.assertTrue(guard.claim(guard.BROWSER).ok)
        again = guard.claim(guard.BROWSER)
        self.assertFalse(again.ok)
        self.assertEqual(again.running, guard.BROWSER)
        self.assertFalse(again.other_kind_running)

    def test_終われば次が取れる(self) -> None:
        self.assertTrue(guard.claim(guard.DESKTOP).ok)
        guard.release_all()
        self.assertEqual(guard.running(), "")
        self.assertTrue(guard.claim(guard.BROWSER).ok)

    def test_名前はアプリごと(self) -> None:
        names = guard.names()
        self.assertTrue(all(n.startswith(guard.base_name()) for n in names.values()))
        self.assertEqual(len(set(names.values())), 3)


class CrossProcessTest(unittest.TestCase):
    """本当に別のプロセス同士で止め合う(落ちたら錠は残らない)。"""

    def hold(self, kind: str):
        code = textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {str(ROOT)!r})
            from core import instance_guard as g
            c = g.claim({kind!r})
            print("ok" if c.ok else "no:" + c.running, flush=True)
            time.sleep(60)
        """)
        proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
                                env=dict(os.environ))
        self.addCleanup(lambda: (proc.kill(), proc.wait(), proc.stdout.close()))
        self.assertEqual(proc.stdout.readline().strip(), "ok")
        return proc

    def test_別のプロセスのデスクトップ版が居るあいだはブラウザ版が止まり_落ちたら取れる(self) -> None:
        guard.release_all()
        desktop = self.hold(guard.DESKTOP)
        claim = guard.claim(guard.BROWSER)
        self.assertTrue(claim.other_kind_running)
        desktop.kill()
        desktop.wait()
        time.sleep(0.2)
        claim = guard.claim(guard.BROWSER)
        self.assertTrue(claim.ok)
        guard.release_all()


class BrowserStartRefusesTest(unittest.TestCase):
    def test_デスクトップ版が動いていればブラウザ版の起動は理由を出して止まる(self) -> None:
        import start_app
        guard.release_all()
        self.addCleanup(guard.release_all)
        self.assertTrue(guard.claim(guard.DESKTOP).ok)
        with self.assertRaises(start_app.StartupError) as ctx:
            start_app.start(open_browser=False, options={"demo": True})
        self.assertIn("デスクトップ版", str(ctx.exception))
        self.assertFalse(ctx.exception.show_page)           # ブラウザで起動エラーの画面を開かない


class ProcessManagerStatusTest(unittest.TestCase):
    def test_stopbatの状態はデスクトップ版を知らせる(self) -> None:
        import process_manager
        guard.release_all()
        self.addCleanup(guard.release_all)
        self.assertTrue(guard.claim(guard.DESKTOP).ok)
        self.assertIn("デスクトップ版", process_manager.describe_running())


if __name__ == "__main__":
    unittest.main()
