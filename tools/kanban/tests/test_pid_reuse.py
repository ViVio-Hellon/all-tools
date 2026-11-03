"""印の PID が使い回されても「起動中」と取り違えない(``launch_guard.lock_owner``)

別のツールで見つかった不具合: 動いたまま PC を終了・再起動すると印(``<mode>.lock``)が残り、
再起動のあとで**まったく別のプログラムが同じ番号を持つ**ことがある。看板は、ポートを取る前の
印(ポート 0)の PID が生きていると「ほぼ同時に起動されたもう 1 つが起動の途中です」と答え続け、
**二度と起動できなかった**。答えない本人の印は消して2つ目を立てていた(本人は残ったまま)。
stop.bat は答えない本人を「別のプロセス」として印だけ消し、「動いていません」と言っていた。

    印に PID と起動時刻を書く。両方が合うときだけ本人
    前の版の印(起動時刻なし)は「印より後に起動したプロセスは本人ではない」
    本人が5秒たっても答えなければ、本人だけを終わらせて起動し直す(stop.bat も同じ)

Windows の ``GetProcessTimes`` は Linux では呼べないので、起動時刻は ``/proc`` から出す。
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import launch_guard  # noqa: E402
import process_manager  # noqa: E402
from kanban import app_config, config  # noqa: E402

HAS_PROC = launch_guard.process_started(os.getpid()) is not None
SITE = config.MODE_SITE


def sleeper() -> subprocess.Popen:
    """前の起動の代わり(答えない・動き続ける)。"""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    threading.Thread(target=child.wait, daemon=True).start()     # 終わったらすぐ片付ける
    for _ in range(50):
        if launch_guard.process_started(child.pid) is not None:
            break
        time.sleep(0.05)
    return child


@unittest.skipUnless(HAS_PROC, "起動時刻を確かめられない環境")
class PidReuseTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        for p in (patch.object(launch_guard, "lock_path", lambda mode: self.dir / f"{mode}.lock"),
                  patch.object(launch_guard, "find_running", return_value=[]),
                  patch.object(launch_guard, "NO_ANSWER_WAIT_SEC", 0.4),
                  patch.object(launch_guard, "STARTUP_GRACE_SEC", 0.2)):
            p.start()
            self.addCleanup(p.stop)

    @property
    def path(self) -> Path:
        return self.dir / f"{SITE}.lock"

    def lock(self, pid: int, *, started: float = 0.0, port: int = 9, mtime: float = 0.0) -> None:
        info = launch_guard.LockInfo(app_id=app_config.app_id(), mode=SITE, pid=pid, port=port, url="",
                                     started_at=time.time() - 3600, token="t", process_started=started)
        launch_guard.write_lock(info)
        if mtime:
            os.utime(self.path, (mtime, mtime))

    def child(self) -> subprocess.Popen:
        child = sleeper()
        self.addCleanup(lambda: child.poll() is None and child.kill())
        return child

    def stop(self) -> tuple[int, list[str], str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = process_manager.main(["--mode", SITE])
        return code, out.getvalue().splitlines(), err.getvalue()

    def test_印に起動時刻を書く(self) -> None:
        info = launch_guard.build_lock_info(SITE, 8741, "t")
        self.assertAlmostEqual(info.process_started, launch_guard.my_started(), delta=0.5)

    def test_PIDは生きているが起動時刻が違えば_片付けて起動する(self) -> None:
        child = self.child()
        self.lock(child.pid, started=launch_guard.process_started(child.pid) - 86400)
        result = launch_guard.check_existing(SITE)
        self.assertTrue(result.should_start, result.reason)
        self.assertFalse(self.path.exists(), "残っていた印を片付けていない")
        self.assertIsNone(child.poll(), "関係のないプログラムを終わらせた")

    def test_ポート0の前回の印を起動の途中と見続けない(self) -> None:
        child = self.child()
        self.lock(child.pid, port=0, started=launch_guard.process_started(child.pid) - 86400)
        result = launch_guard.check_existing(SITE)
        self.assertTrue(result.should_start, result.reason)

    def test_前の版の印で_印より後に起動したプロセスなら本人ではない(self) -> None:
        child = self.child()
        self.lock(child.pid, mtime=time.time() - 3600)
        result = launch_guard.check_existing(SITE)
        self.assertTrue(result.should_start, result.reason)
        self.assertIsNone(child.poll(), "関係のないプログラムを終わらせた")
        self.assertFalse(self.path.exists())

    def test_本人が答えなければ5秒で終わらせて起動し直す(self) -> None:
        child = self.child()
        self.lock(child.pid, started=launch_guard.process_started(child.pid))
        result = launch_guard.check_existing(SITE)
        self.assertTrue(result.should_start, result.reason)
        self.assertIsNotNone(child.wait(timeout=5), "答えない本人を終わらせていない")
        self.assertFalse(self.path.exists())

    def test_ポート0のまま固まった本人も猶予のあと終わらせる(self) -> None:
        child = self.child()
        self.lock(child.pid, port=0, started=launch_guard.process_started(child.pid))
        result = launch_guard.check_existing(SITE)
        self.assertTrue(result.should_start, result.reason)
        self.assertIsNotNone(child.wait(timeout=5))

    def test_stopbat_接続できない前回の印は片付けて0と1行のログ(self) -> None:
        child = self.child()
        self.lock(child.pid, started=launch_guard.process_started(child.pid) - 86400)
        code, out, err = self.stop()
        self.assertEqual(code, process_manager.EXIT_STOPPED, out)
        self.assertIn("動いていません", out[0])
        self.assertEqual(err.count(launch_guard.CLEANED), 1, err)
        self.assertFalse(self.path.exists())
        self.assertIsNone(child.poll())

    def test_stopbat_本人が答えなければ終わらせて0(self) -> None:
        child = self.child()
        self.lock(child.pid, started=launch_guard.process_started(child.pid))
        code, out, err = self.stop()
        self.assertEqual(code, process_manager.EXIT_STOPPED, out)
        self.assertIn("止めました", out[0])
        self.assertIn(launch_guard.CLEANED, err)
        self.assertIsNotNone(child.wait(timeout=5))
        self.assertFalse(self.path.exists())

    def test_読めない印は消さない(self) -> None:
        self.path.write_text("{書きかけ", encoding="utf-8")
        launch_guard.check_existing(SITE)
        self.stop()
        self.assertTrue(self.path.exists(), "読めない印を消した")


if __name__ == "__main__":
    unittest.main()
