"""ブラウザ版とデスクトップ版を同時に動かさない(`portal/instance_guard.py`)

どちらを後から開いても、後から開いたほうが止まる。同じ種類なら今までどおり
(ブラウザ版はつなぐ・デスクトップ版は窓を前に出す)。錠は OS の名前付きの錠
(Windows は名前付きミューテックス、ここ(Linux)はファイルロック)。
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
import unittest
from pathlib import Path

import tests  # noqa: F401  (一時フォルダへ向ける)

from portal import instance_guard as guard

ROOT = Path(__file__).resolve().parent.parent


class ClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        guard.release_all()
        self.addCleanup(guard.release_all)

    def test_ブラウザ版が先なら_デスクトップ版は止まる(self) -> None:
        self.assertTrue(guard.claim(guard.BROWSER).ok)
        second = guard.claim(guard.DESKTOP)
        self.assertFalse(second.ok)
        self.assertEqual(second.running, guard.BROWSER)
        self.assertTrue(second.other_kind_running)
        self.assertEqual(guard.running(), guard.BROWSER)

    def test_デスクトップ版が先なら_ブラウザ版は止まる(self) -> None:
        self.assertTrue(guard.claim(guard.DESKTOP).ok)
        second = guard.claim(guard.BROWSER)
        self.assertFalse(second.ok)
        self.assertEqual(second.running, guard.DESKTOP)
        self.assertTrue(second.other_kind_running)
        self.assertEqual(guard.running(), guard.DESKTOP)

    def test_同じ種類なら種類を言う(self) -> None:
        self.assertTrue(guard.claim(guard.BROWSER).ok)
        again = guard.claim(guard.BROWSER)
        self.assertFalse(again.ok)
        self.assertEqual(again.running, guard.BROWSER)
        self.assertFalse(again.other_kind_running, "ブラウザ版どうしは、つなぐ")

    def test_放せば取れる(self) -> None:
        first = guard.claim(guard.DESKTOP)
        first.release()
        guard.release_all()
        self.assertEqual(guard.running(), "")
        self.assertTrue(guard.claim(guard.BROWSER).ok)


class ProcessTests(unittest.TestCase):
    """別のプロセス(本物の起動と同じ)どうしで止め合う。プロセスが終われば OS が錠を外す。"""

    def test_別のプロセスのデスクトップ版を見て止まり_終われば取れる(self) -> None:
        guard.release_all()
        holder = subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent("""
                import sys, time
                sys.path.insert(0, sys.argv[1])
                import tests  # noqa: F401
                from portal import instance_guard as g
                assert g.claim(g.DESKTOP).ok
                print("held", flush=True)
                time.sleep(60)
            """), str(ROOT)],
            stdout=subprocess.PIPE, text=True, env=dict(os.environ), cwd=str(ROOT))
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        claim = guard.claim(guard.BROWSER)
        self.assertFalse(claim.ok)
        self.assertEqual(claim.running, guard.DESKTOP)
        holder.kill()
        holder.wait(timeout=10)
        deadline = time.monotonic() + 5
        while guard.running() and time.monotonic() < deadline:
            time.sleep(0.1)
        claim = guard.claim(guard.BROWSER)
        self.assertTrue(claim.ok, "落ちたプロセスの錠は残らない")
        guard.release_all()


class StartAppTests(unittest.TestCase):
    """ブラウザ版の起動(`start_app.py`)は、デスクトップ版が動いていれば理由を出して止まる。"""

    def test_デスクトップ版が動いていればブラウザ版は起動しない(self) -> None:
        import start_app

        guard.release_all()
        self.addCleanup(guard.release_all)
        self.assertTrue(guard.claim(guard.DESKTOP).ok)
        with self.assertRaises(start_app.StartupError) as caught:
            start_app.start(open_browser=False)
        self.assertIn("デスクトップ版", str(caught.exception))
        self.assertIn("終了", caught.exception.hint)


if __name__ == "__main__":
    unittest.main()
