"""多重起動の守り(launch_guard)と、自分のプロセスだけを止める見分け

- ロックは `O_CREAT | O_EXCL` で取る。2つ目は必ず失敗する
- 死んだロック・別物のロックは掃除して起動する
- 別のアプリ(app_id 違い)を自分と取り違えない
- ロックには PID の生成時刻を残し、PID の使い回しを見分ける
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from unittest import mock

import tests.helpers  # noqa: F401  (ローカル領域を一時フォルダへ)
import launch_guard
from core import app_config, process_tracking


class LockTests(unittest.TestCase):
    def setUp(self) -> None:
        launch_guard.remove_lock()
        self.addCleanup(launch_guard.remove_lock)

    def test_ロックは1つしか取れない(self) -> None:
        self.assertTrue(launch_guard.try_acquire())
        self.assertFalse(launch_guard.try_acquire())
        info = launch_guard.read_lock()
        self.assertEqual(info.pid, os.getpid())
        self.assertEqual(info.port, launch_guard.STARTING_PORT)

    def test_終わるときは自分のロックだけ消す(self) -> None:
        """入れ替えのとき、古いほうの後始末が新しいほうのロックを消さない(日報・カレンダーと同じ)。"""
        other = launch_guard.build_lock_info(8733, "tok")
        other.pid = os.getpid() + 1
        launch_guard.write_lock(other)
        launch_guard.release_lock()
        self.assertIsNotNone(launch_guard.read_lock(), "他人のロックを消した")
        launch_guard.remove_lock()
        self.assertTrue(launch_guard.try_acquire())
        launch_guard.release_lock()
        self.assertIsNone(launch_guard.read_lock())

    def test_ポートを書くのは自分のロックだけ(self) -> None:
        other = launch_guard.build_lock_info(8734, "theirs")
        other.pid = os.getpid() + 1
        launch_guard.write_lock(other)
        launch_guard.update_lock(launch_guard.build_lock_info(8733, "mine"))
        self.assertEqual(launch_guard.read_lock().port, 8734, "他人のロックを書き換えた")
        launch_guard.remove_lock()
        self.assertTrue(launch_guard.try_acquire())
        launch_guard.update_lock(launch_guard.build_lock_info(8733, "mine"))
        self.assertEqual(launch_guard.read_lock().port, 8733)

    def test_ロックに生成時刻を残す(self) -> None:
        info = launch_guard.build_lock_info(8733, "tok")
        self.assertEqual(info.app_id, app_config.app_id())
        self.assertEqual(info.token, "tok")
        self.assertEqual(info.create_time, process_tracking.current_identity().create_time)

    def test_ロックが無ければ起動する(self) -> None:
        self.assertTrue(launch_guard.check_existing().should_start)

    def test_死んだロックは掃除して起動する(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        info = launch_guard.build_lock_info(8733, "tok")
        info.pid = proc.pid
        launch_guard.write_lock(info)
        result = launch_guard.check_existing()
        self.assertTrue(result.should_start)
        self.assertIsNone(launch_guard.read_lock())

    def test_別のアプリが居たら自分ではない(self) -> None:
        launch_guard.write_lock(launch_guard.build_lock_info(8733, "tok"))
        with mock.patch.object(launch_guard, "probe_health",
                               return_value={"app_id": "someone.else"}):
            result = launch_guard.check_existing()
        self.assertTrue(result.should_start)
        self.assertIsNone(launch_guard.read_lock())

    def test_同じアプリが居れば合流する(self) -> None:
        launch_guard.write_lock(launch_guard.build_lock_info(8733, "tok"))
        health = {"app_id": app_config.app_id(), "version": app_config.version()}
        with mock.patch.object(launch_guard, "probe_health", return_value=health):
            result = launch_guard.check_existing()
        self.assertFalse(result.should_start)
        self.assertTrue(result.url.startswith("http://127.0.0.1:8733"))

    def test_版が違えば古いほうを止めて立て直す(self) -> None:
        launch_guard.write_lock(launch_guard.build_lock_info(8733, "tok"))
        health = {"app_id": app_config.app_id(), "version": "0.0.1"}
        with mock.patch.object(launch_guard, "probe_health", return_value=health), \
             mock.patch.object(launch_guard, "_replace_stale",
                               return_value=launch_guard.GuardResult(True, stale_version="0.0.1")) as rep:
            result = launch_guard.check_existing()
        rep.assert_called_once()
        self.assertTrue(result.should_start)

    def test_is_our_app(self) -> None:
        self.assertFalse(launch_guard.is_our_app(None))
        self.assertFalse(launch_guard.is_our_app({"app_id": "x"}))
        self.assertTrue(launch_guard.is_our_app({"app_id": app_config.app_id()}))


class ProcessIdentityTests(unittest.TestCase):
    def test_自分は自分と分かる(self) -> None:
        me = process_tracking.current_identity()
        self.assertTrue(process_tracking.is_same_process(me.pid, me.create_time))

    def test_生成時刻が違えば別のプロセス(self) -> None:
        """PID は使い回される。同じ番号でも生成時刻が違えば止めない。"""
        me = process_tracking.current_identity()
        if not me.create_time:
            self.skipTest("この環境では生成時刻を取れない")
        self.assertFalse(process_tracking.is_same_process(me.pid, me.create_time + 12345))

    def test_記録した子プロセスだけを片付ける(self) -> None:
        with tests.helpers.temp_dir() as tmp:
            reg = process_tracking.TrackedProcessRegistry(os.path.join(tmp, "t.json"))
            proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            try:
                ident = process_tracking.get_process_identity(proc.pid)
                self.assertIsNotNone(ident)
                reg.add(ident, "試験")
                cleaned = reg.cleanup("試験の後片付け", None)
                self.assertEqual([c["pid"] for c in cleaned], [proc.pid])
                self.assertTrue(cleaned[0]["terminated"])
                self.assertEqual(reg.entries(), [])
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()

class PortTests(unittest.TestCase):
    """再起動しても同じポートに戻る(開いたままの画面がつながり直せる)。"""

    def _listen(self):
        import socket
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        return srv, srv.getsockname()[1]

    def test_待ち受け中のポートは使用中(self) -> None:
        srv, port = self._listen()
        self.addCleanup(srv.close)
        self.assertFalse(launch_guard.is_port_free(port, "127.0.0.1"))

    def test_使えない理由をOSの番号つきで返す(self) -> None:
        srv, port = self._listen()
        self.addCleanup(srv.close)
        problem = launch_guard.port_problem(port, "127.0.0.1")
        self.assertRegex(problem, r"^\d+: ")       # Windows なら 10048 / 10013
        self.assertEqual(launch_guard.port_problem(0, "127.0.0.1"), "")

    @unittest.skipIf(os.name == "nt", "Windows では TIME_WAIT が bind を妨げない")
    def test_閉じた直後のTIME_WAITでも空きと判定する(self) -> None:
        import socket
        srv, port = self._listen()
        cli = socket.create_connection(("127.0.0.1", port))
        conn, _ = srv.accept()
        conn.close()                      # サーバ側から先に閉じる → サーバ側が TIME_WAIT
        cli.recv(1)
        cli.close()
        srv.close()
        self.assertTrue(launch_guard.is_port_free(port, "127.0.0.1"))


if __name__ == "__main__":
    unittest.main()


class RegistryConcurrencyTests(unittest.TestCase):
    def test_同時に片付けても壊れない(self):
        """stop.bat とアプリの終了処理が同時に記録を書き換えることがある。"""
        import threading
        with tests.helpers.temp_dir() as tmp:
            path = os.path.join(tmp, "tracked.json")
            regs = [process_tracking.TrackedProcessRegistry(path) for _ in range(4)]
            errors = []

            def work(reg):
                try:
                    for _ in range(30):
                        reg.cleanup("試験", None)
                except Exception as exc:        # noqa: BLE001
                    errors.append(exc)
            threads = [threading.Thread(target=work, args=(r,)) for r in regs]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            self.assertEqual(regs[0].entries(), [])
