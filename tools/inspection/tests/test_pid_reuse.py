"""印の PID が使い回されても「起動中」と取り違えない(`launch_guard.lock_owner`)

別のツールで見つかった不具合: 動いたまま PC を終了・再起動すると印(`app.lock`)が残り、
再起動のあとで**まったく別のプログラムが同じ番号を持つ**ことがある。点検表は印に作成時刻
(`create_time`)を書いていたが、stop.bat は作成時刻が合わないと「このアプリではないようなので
止めません」で終わり、印を消さなかった。起動は、答えない本人の印を消して2つ目を立てていた
(本人は残ったまま)。

    日報複合ツールの入口も各ツールの印を見るので、作成時刻に加えて起動時刻(エポック秒)も書く

    印に PID と起動時刻を書く。両方が合うときだけ本人
    前の版の印(起動時刻なし)は「印より後に起動したプロセスは本人ではない」
    本人が5秒たっても答えなければ、本人だけを終わらせて起動し直す(stop.bat も同じ)

Windows の `GetProcessTimes` は Linux では呼べないので、起動時刻は `/proc` から出す。
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

import tests.helpers  # noqa: F401,E402  (ローカル領域を一時フォルダへ)
import launch_guard  # noqa: E402
import process_manager  # noqa: E402

HAS_PROC = launch_guard.process_started(os.getpid()) is not None


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
        self.path = Path(tmp.name) / "app.lock"
        for p in (patch.object(launch_guard, "lock_path", lambda: self.path),
                  patch.object(launch_guard, "NO_ANSWER_WAIT_SEC", 0.4)):
            p.start()
            self.addCleanup(p.stop)

    def lock(self, pid: int, *, started: float = 0.0, port: int = 9, mtime: float = 0.0) -> None:
        info = launch_guard.LockInfo(app_id=launch_guard.app_config.app_id(), pid=pid, port=port, url="",
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
            code = process_manager.report(process_manager.stop())
        return code, out.getvalue().splitlines(), err.getvalue()

    def test_印に起動時刻を書く(self) -> None:
        info = launch_guard.build_lock_info(8733, "t")
        self.assertAlmostEqual(info.process_started, launch_guard.my_started(), delta=0.5)

    def test_PIDは生きているが起動時刻が違えば_片付けて起動する(self) -> None:
        child = self.child()
        self.lock(child.pid, started=launch_guard.process_started(child.pid) - 86400)
        result = launch_guard.check_existing()
        self.assertTrue(result.should_start, result.reason)
        self.assertFalse(self.path.exists(), "残っていた印を片付けていない")
        self.assertIsNone(child.poll(), "関係のないプログラムを終わらせた")

    def test_前の版の印で_印より後に起動したプロセスなら本人ではない(self) -> None:
        child = self.child()
        self.lock(child.pid, mtime=time.time() - 3600)
        result = launch_guard.check_existing()
        self.assertTrue(result.should_start, result.reason)
        self.assertIsNone(child.poll(), "関係のないプログラムを終わらせた")
        self.assertFalse(self.path.exists())

    def test_前の版の印でも_印より前に起動した本人は本人(self) -> None:
        child = self.child()
        self.lock(child.pid)                                    # 起動してから書いた
        self.assertEqual(launch_guard.lock_owner(launch_guard.read_lock()), launch_guard.OWNER_SAME)

    def test_本人が答えなければ5秒で終わらせて起動し直す(self) -> None:
        child = self.child()
        self.lock(child.pid, started=launch_guard.process_started(child.pid))
        result = launch_guard.check_existing()
        self.assertTrue(result.should_start, result.reason)
        self.assertIsNotNone(child.wait(timeout=5), "答えない本人を終わらせていない")
        self.assertFalse(self.path.exists())

    def test_前の版の点検表の印は作成時刻で見分ける(self) -> None:
        from core import process_tracking

        child = self.child()
        real = process_tracking.get_process_identity(child.pid).create_time
        for create_time, want in ((real, launch_guard.OWNER_SAME), (real - 10_000_000 * 3600, launch_guard.OWNER_OTHER)):
            info = launch_guard.LockInfo(app_id="x", pid=child.pid, port=9, url="", started_at=time.time(),
                                         create_time=create_time)
            self.assertEqual(launch_guard.lock_owner(info), want)

    def test_stopbat_接続できない前回の印は片付けて0と1行のログ(self) -> None:
        child = self.child()
        self.lock(child.pid, started=launch_guard.process_started(child.pid) - 86400)
        code, out, err = self.stop()
        self.assertEqual(code, process_manager.EXIT_STOPPED, out)
        self.assertIn("片付けました", out[0])
        self.assertEqual(err.count(launch_guard.CLEANED), 1, err)
        self.assertFalse(self.path.exists())

    def test_stopbat_本人が答えなければ終わらせて0(self) -> None:
        child = self.child()
        self.lock(child.pid, started=launch_guard.process_started(child.pid))
        code, out, err = self.stop()
        self.assertEqual(code, process_manager.EXIT_STOPPED, out)
        self.assertIn(launch_guard.CLEANED, err)
        self.assertIsNotNone(child.wait(timeout=5))
        self.assertFalse(self.path.exists())

    def test_stopbat_別のPythonのstart_appを止めない(self) -> None:
        """前はコマンドラインに start_app.py があれば自分とみなし、別のツールの Python を止め得た。"""
        child = self.child()
        self.lock(child.pid, started=launch_guard.process_started(child.pid) - 86400)
        with patch.object(launch_guard, "process_command_line", return_value="python start_app.py"):
            code, _, _ = self.stop()
        self.assertEqual(code, process_manager.EXIT_STOPPED)
        self.assertIsNone(child.poll(), "関係のないプログラムを終わらせた")

    def test_読めない印は消さない(self) -> None:
        self.path.write_text("{書きかけ", encoding="utf-8")
        launch_guard.check_existing()
        self.stop()
        self.assertTrue(self.path.exists(), "読めない印を消した")


if __name__ == "__main__":
    unittest.main()
