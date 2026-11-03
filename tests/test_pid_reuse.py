"""印の PID が使い回されても「起動中」と取り違えない(`portal/proc_identity.py`)

別のツールで見つかった不具合: ツールが動いたまま PC を終了・再起動すると印(PID を書いた
ファイル)が残り、再起動のあとで**まったく別のプログラムが同じ番号を持つ**ことがある。
すると「起動中」と見続けて画面に接続できず、stop.bat も「接続できない」で終わって印を
消さなかった。入口(`portal.lock`)と、入口が見る各ツールの印で同じことが起き得た。

    印に PID と起動時刻を書く。両方が合うときだけ本人
    前の版の印(起動時刻なし)は「印より後に起動したプロセスは本人ではない」
    本人が5秒たっても答えなければ、本人だけを終わらせて起動し直す(stop.bat も同じ)
    読めない印は消さない

Windows の `GetProcessTimes` は Linux では呼べないので、起動時刻は `/proc` から出す
(作りは同じ: 起動時刻を比べる)。FILETIME をエポック秒にする計算は値で確かめる。
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import process_manager
import start_app
from portal import app_config, browser_tools, instance_guard, proc_identity

HAS_PROC = proc_identity.process_started(os.getpid()) is not None


def sleeper() -> subprocess.Popen:
    """本人の代わり(答えない・動き続ける)。"""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    # 終わったらすぐ片付ける(残ると「生きている」ように見える。Windows には無いこと)
    threading.Thread(target=child.wait, daemon=True).start()
    for _ in range(50):
        if proc_identity.process_started(child.pid) is not None:
            break
        time.sleep(0.05)
    return child


def stop_child(child: subprocess.Popen) -> None:
    if child.poll() is None:
        child.kill()
    child.wait(timeout=10)


class ProcessIdentityTests(unittest.TestCase):
    def test_FILETIMEをエポック秒にする(self) -> None:
        # 1970-01-01 00:00:00 UTC = 116444736000000000、そこから 1.5 秒
        self.assertEqual(proc_identity.filetime_to_epoch(116444736000000000), 0.0)
        self.assertAlmostEqual(proc_identity.filetime_to_epoch(116444736015000000), 1.5)

    @unittest.skipUnless(HAS_PROC, "起動時刻を確かめられない環境")
    def test_自分の起動時刻は今より前で_毎回同じ(self) -> None:
        first = proc_identity.my_started()
        self.assertGreater(first, 0)
        self.assertLessEqual(first, time.time() + 1)
        self.assertAlmostEqual(first, proc_identity.my_started(), delta=0.5)

    @unittest.skipUnless(HAS_PROC, "起動時刻を確かめられない環境")
    def test_PIDと起動時刻が合えば本人_合わなければ別のプロセス(self) -> None:
        alive = lambda pid: True                          # noqa: E731
        me, started = os.getpid(), proc_identity.my_started()
        self.assertEqual(proc_identity.owner(me, started, None, alive), proc_identity.SAME)
        self.assertEqual(proc_identity.owner(me, started - 3600, None, alive), proc_identity.OTHER)
        self.assertEqual(proc_identity.owner(me, started, None, lambda pid: False), proc_identity.GONE)

    @unittest.skipUnless(HAS_PROC, "起動時刻を確かめられない環境")
    def test_前の版の印は_印より後に起動したプロセスを本人とみなさない(self) -> None:
        alive = lambda pid: True                          # noqa: E731
        child = sleeper()
        self.addCleanup(stop_child, child)
        started = proc_identity.process_started(child.pid)
        self.assertEqual(proc_identity.owner(child.pid, 0, started - 600, alive), proc_identity.OTHER)
        self.assertEqual(proc_identity.owner(child.pid, 0, started + 1, alive), proc_identity.SAME)
        self.assertEqual(proc_identity.owner(child.pid, 0, None, alive), proc_identity.UNKNOWN)

    @unittest.skipUnless(HAS_PROC, "起動時刻を確かめられない環境")
    def test_起動時刻を取れない相手は本人ではない(self) -> None:
        """同じ利用者のツールなら必ず取れる(取れないのは別の利用者・守られたプロセス)。"""
        real = proc_identity.process_started
        with mock.patch.object(proc_identity, "process_started",
                               side_effect=lambda pid: None if pid == 4242 else real(pid)):
            self.assertEqual(proc_identity.owner(4242, 123.0, None, lambda pid: True), proc_identity.OTHER)

    def test_自分の起動時刻も取れない環境は確かめられない(self) -> None:
        with mock.patch.object(proc_identity, "process_started", return_value=None):
            self.assertEqual(proc_identity.owner(4242, 123.0, None, lambda pid: True), proc_identity.UNKNOWN)

    @unittest.skipUnless(HAS_PROC, "起動時刻を確かめられない環境")
    def test_止めるのは起動時刻まで合ったプロセスだけ(self) -> None:
        child = sleeper()
        self.addCleanup(stop_child, child)
        started = proc_identity.process_started(child.pid)
        self.assertFalse(proc_identity.terminate(child.pid, started - 3600, None, wait_sec=1))
        self.assertFalse(proc_identity.terminate(child.pid, 0, None, wait_sec=1), "確かめられないのに止めた")
        self.assertIsNone(child.poll(), "別のプロセス(起動時刻が違う)を止めた")
        self.assertTrue(proc_identity.terminate(child.pid, started, None, wait_sec=4))
        child.wait(timeout=5)


def write_portal_lock(pid: int, *, port: int = 9, started: float | None = None, mtime: float | None = None) -> dict:
    app_config.ensure_local_dirs()
    data = {"app_id": app_config.app_id(), "pid": pid, "port": port, "url": f"http://127.0.0.1:{port}/",
            "token": "t", "started_at": time.time()}
    if started is not None:
        data["process_started"] = started
    path = start_app.lock_path()
    path.write_text(json.dumps(data), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return data


@unittest.skipUnless(HAS_PROC, "起動時刻を確かめられない環境")
class PidReuseStopTests(unittest.TestCase):
    """stop.bat(`process_manager.py`)── 入口の印が前回のもの・本人が答えない。"""

    def setUp(self) -> None:
        for p in (mock.patch.object(instance_guard, "running", return_value=""),
                  mock.patch.object(browser_tools.BrowserTools, "running", return_value=[]),
                  mock.patch.object(process_manager, "NO_ANSWER_WAIT_SEC", 0.5),
                  mock.patch.object(process_manager, "cleanup_inspection_excel")):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(lambda: start_app.lock_path().unlink(missing_ok=True))

    def stop(self) -> tuple[int, list[str], str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = process_manager.stop()
        return code, out.getvalue().splitlines(), err.getvalue()

    def test_PIDは生きているが起動時刻が違えば_片付けて0と1行のログ(self) -> None:
        # 再起動のあとに、まったく別のプログラム(ここでは試験の自分)が同じ番号を持っている
        write_portal_lock(os.getpid(), started=proc_identity.my_started() - 86400)
        code, out, err = self.stop()
        self.assertEqual(code, 0, out)
        self.assertEqual(out[0], "日報複合ツールは動いていません(残っていた前回の印を片付けました)")
        self.assertEqual(err.count(start_app.CLEANED), 1, err)
        self.assertFalse(start_app.lock_path().exists(), "残っていた印を消していない")

    def test_前の版の印で_印より後に起動したプロセスなら本人ではない(self) -> None:
        child = sleeper()
        self.addCleanup(stop_child, child)
        write_portal_lock(child.pid, mtime=time.time() - 3600)      # 起動時刻の無い印・1時間前に書いた
        code, out, err = self.stop()
        self.assertEqual(code, 0, out)
        self.assertIn(start_app.CLEANED, err)
        self.assertIsNone(child.poll(), "関係のないプログラムを終わらせた")
        self.assertFalse(start_app.lock_path().exists())

    def test_本人が答えなければ5秒で終わらせて0(self) -> None:
        child = sleeper()
        self.addCleanup(stop_child, child)
        write_portal_lock(child.pid, port=9, started=proc_identity.process_started(child.pid))
        code, out, err = self.stop()
        self.assertEqual(code, 0, out)
        self.assertIn("止めました", out[0])
        self.assertIn(start_app.CLEANED, err)
        self.assertIsNotNone(child.wait(timeout=5), "本人を終わらせていない")
        self.assertFalse(start_app.lock_path().exists())

    def test_読めない印は消さない(self) -> None:
        app_config.ensure_local_dirs()
        start_app.lock_path().write_text("{書きかけ", encoding="utf-8")
        code, out, _ = self.stop()
        self.assertEqual((code, out[0]), (0, "日報複合ツールは動いていません"))
        self.assertTrue(start_app.lock_path().exists(), "読めない印を消した")

    def test_起動確認も前回の印を動いていると言わない(self) -> None:
        write_portal_lock(os.getpid(), started=proc_identity.my_started() - 86400)
        out = io.StringIO()
        with redirect_stdout(out):
            code = process_manager.check()
        self.assertEqual((code, out.getvalue().strip()), (process_manager.CHECK_STOPPED, "動いていません"))


@unittest.skipUnless(HAS_PROC, "起動時刻を確かめられない環境")
class PidReuseStartTests(unittest.TestCase):
    """起動(`start_app._join_or_take_over`)── 錠は握られているが、印の本人が答えない。"""

    def setUp(self) -> None:
        for p in (mock.patch.object(start_app, "NO_ANSWER_WAIT_SEC", 0.5),
                  mock.patch.object(start_app, "STARTING_WAIT_SEC", 0.6)):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(lambda: start_app.lock_path().unlink(missing_ok=True))
        self.log = mock.Mock()

    def test_本人が答えなければ終わらせて_錠を取り直して起動する(self) -> None:
        child = sleeper()
        self.addCleanup(stop_child, child)
        write_portal_lock(child.pid, port=9, started=proc_identity.process_started(child.pid))
        took = instance_guard.Claim(instance_guard.BROWSER, ok=True)
        with mock.patch.object(instance_guard, "claim", return_value=took):
            claim = start_app._join_or_take_over(self.log, open_browser=False)
        self.assertIs(claim, took)
        self.assertIsNotNone(child.wait(timeout=5), "答えない本人を終わらせていない")
        self.assertFalse(start_app.lock_path().exists())

    def test_前回の印なら別のプログラムを終わらせず_起動の途中の入口を待つ(self) -> None:
        child = sleeper()
        self.addCleanup(stop_child, child)
        write_portal_lock(child.pid, started=proc_identity.process_started(child.pid) - 86400)
        with mock.patch.object(instance_guard, "claim") as claim, \
                self.assertRaises(start_app.StartupError):
            start_app._join_or_take_over(self.log, open_browser=False)
        claim.assert_not_called()
        self.assertIsNone(child.poll(), "関係のないプログラムを終わらせた")

    def test_本人が答えればつなぐ(self) -> None:
        write_portal_lock(os.getpid(), port=8700, started=proc_identity.my_started())
        with mock.patch.object(start_app, "_answers", return_value=True), \
                redirect_stdout(io.StringIO()) as out:
            self.assertIsNone(start_app._join_or_take_over(self.log, open_browser=False))
        self.assertIn("もう動いています", out.getvalue())


@unittest.skipUnless(HAS_PROC, "起動時刻を確かめられない環境")
class ToolLockTests(unittest.TestCase):
    """入口が見る各ツールの印(タブの「終わった」・stop.bat が止める相手)。"""

    def test_使い回されたPIDの印は生きていない(self) -> None:
        path = Path(app_config.local_dir("runtime")) / "tool-probe.lock"
        path.write_text("{}", encoding="utf-8")
        self.addCleanup(path.unlink)
        me = os.getpid()
        self.assertTrue(browser_tools.lock_alive({"pid": me, "process_started": proc_identity.my_started()}, path))
        self.assertFalse(browser_tools.lock_alive({"pid": me, "process_started": proc_identity.my_started() - 86400},
                                                  path))
        os.utime(path, (time.time() - 86400 * 365, time.time() - 86400 * 365))
        self.assertFalse(browser_tools.lock_alive({"pid": me}, path), "前の版の印: 印より後に起動したのに本人とみなした")


if __name__ == "__main__":
    unittest.main()
