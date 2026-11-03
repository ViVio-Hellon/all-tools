"""業務ツール統合ランチャーの入口(launcher_check.bat・launcher_stop.bat → process_manager.py)

デスクトップ版の窓を探す・閉じる部分(Windows の ctypes)は、ここでは差し替えて分かれ道を見る。
本物は Windows の CI(`scripts/desktop_smoke.py`)が exe を起動して確かめる。
ブラウザ版の止め方は `stop.bat` と同じ(`test_web.py` の CloseAskTests と、docs/ランチャー連携.md の通し確認)。
"""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import process_manager
from portal import desktop_window, instance_guard


def run(fn, *args, **kw) -> tuple[int, str]:
    out = io.StringIO()
    with redirect_stdout(out):
        code = fn(*args, **kw)
    lines = [line for line in out.getvalue().splitlines() if line.strip()]
    return code, (lines[-1] if lines else "")


class CheckTests(unittest.TestCase):
    def test_デスクトップ版_窓が出ていれば使える_まだなら準備中(self) -> None:
        with mock.patch.object(instance_guard, "running", return_value=instance_guard.DESKTOP):
            with mock.patch.object(process_manager, "_desktop_windows", return_value=[101]):
                self.assertEqual(run(process_manager.check)[0], 0)
            with mock.patch.object(process_manager, "_desktop_windows", return_value=[]):
                code, last = run(process_manager.check)
                self.assertEqual(code, 2)
                self.assertIn("窓を開いています", last)
            # 窓を確かめられない環境(Windows 以外)は錠を信じる
            with mock.patch.object(process_manager, "_desktop_windows", return_value=None):
                self.assertEqual(run(process_manager.check)[0], 0)

    def test_何も動いていなければ1(self) -> None:
        with mock.patch.object(instance_guard, "running", return_value=""), \
                mock.patch("start_app.read_lock", return_value=None):
            self.assertEqual(run(process_manager.check), (1, "動いていません"))

    def test_ブラウザ版の起動中は準備中(self) -> None:
        with mock.patch.object(instance_guard, "running", return_value=instance_guard.BROWSER), \
                mock.patch("start_app.read_lock", return_value=None):
            self.assertEqual(run(process_manager.check)[0], 2)


def run_out(fn, *args, **kw) -> tuple[int, list[str], str]:
    """(終了コード, 標準出力の行, 標準エラー)。"""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = fn(*args, **kw)
    return code, [line for line in out.getvalue().splitlines() if line.strip()], err.getvalue()


class LauncherStopTests(unittest.TestCase):
    """終了の入口: 0 止めた / 3 処理中・確認中 / 1 そのほか。**標準出力は理由の1行だけ**
    (1.8.9 は1行目、1.7 は最後の1行を見せる。1行なら両方に合う)。"""

    def setUp(self) -> None:
        for name, value in (("DESKTOP_CLOSE_WAIT_SEC", 0.5), ("DESKTOP_FORCE_GRACE_SEC", 0.3),
                            ("DESKTOP_KILL_WAIT_SEC", 0.5)):
            patcher = mock.patch.object(process_manager, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(process_manager, "cleanup_inspection_excel")
        self.cleanup = patcher.start()
        self.addCleanup(patcher.stop)

    def test_デスクトップ版は窓に閉じてと頼み_閉じたら0(self) -> None:
        states = iter([instance_guard.DESKTOP, instance_guard.DESKTOP, ""])
        with mock.patch.object(instance_guard, "running", side_effect=lambda: next(states, "")), \
                mock.patch.object(process_manager, "_desktop_windows", return_value=[101]), \
                mock.patch.object(desktop_window, "ask_to_close", return_value=1) as ask:
            code, out, _ = run_out(process_manager.launcher_stop)
        ask.assert_called_once_with([101])
        self.assertEqual(code, 0)
        self.assertEqual(len(out), 1, out)
        self.assertIn("閉じました", out[0])

    def test_確認に答えていなければ3と理由の1行(self) -> None:
        with mock.patch.object(instance_guard, "running", return_value=instance_guard.DESKTOP), \
                mock.patch.object(process_manager, "_desktop_windows", return_value=[101]), \
                mock.patch.object(desktop_window, "ask_to_close", return_value=1):
            code, out, _ = run_out(process_manager.launcher_stop)
        self.assertEqual(code, 3, "処理中・確認中は 3(ランチャーは止めずに待つ)")
        self.assertEqual(len(out), 1, "1行目 = 最後の1行 = 理由")
        self.assertIn("閉じてよいかの確認", out[0])

    def test_強制は窓に頼み_閉じなければexeを子ごと止める(self) -> None:
        """外枠(exe)・入口・4ツール(exe の子の Python)すべて。途中の処理は捨てる。"""
        alive = {"desktop": True}
        killed = []

        def kill(pid):
            killed.append(pid)
            alive["desktop"] = False
            return True

        with mock.patch.object(instance_guard, "running",
                               side_effect=lambda: instance_guard.DESKTOP if alive["desktop"] else ""), \
                mock.patch.object(process_manager, "_desktop_windows", return_value=[101]), \
                mock.patch.object(desktop_window, "ask_to_close", return_value=1) as ask, \
                mock.patch.object(desktop_window, "exe_pids", return_value=[4321]), \
                mock.patch("portal.browser_tools.terminate_tree", side_effect=kill):
            code, out, _ = run_out(process_manager.launcher_stop, force=True)
        ask.assert_called_once_with([101])
        self.assertEqual(killed, [4321])
        self.assertEqual(code, 0)
        self.assertEqual(len(out), 1, out)
        self.assertIn("強制終了しました", out[0])
        self.cleanup.assert_called_once()

    def test_強制でも窓が自分で閉じればexeは止めない(self) -> None:
        states = iter([instance_guard.DESKTOP, instance_guard.DESKTOP, ""])
        with mock.patch.object(instance_guard, "running", side_effect=lambda: next(states, "")), \
                mock.patch.object(process_manager, "_desktop_windows", return_value=[101]), \
                mock.patch.object(desktop_window, "ask_to_close", return_value=1), \
                mock.patch.object(desktop_window, "exe_pids") as pids, \
                mock.patch("portal.browser_tools.terminate_tree") as kill:
            code, out, _ = run_out(process_manager.launcher_stop, force=True)
        self.assertEqual(code, 0)
        pids.assert_not_called()
        kill.assert_not_called()

    def test_強制でexeを止められなければ1(self) -> None:
        with mock.patch.object(instance_guard, "running", return_value=instance_guard.DESKTOP), \
                mock.patch.object(process_manager, "_desktop_windows", return_value=None), \
                mock.patch.object(desktop_window, "exe_pids", return_value=None):
            code, out, _ = run_out(process_manager.launcher_stop, force=True)
        self.assertEqual(code, 1)
        self.assertIn("Windows 以外", out[0])
        with mock.patch.object(instance_guard, "running", return_value=instance_guard.DESKTOP), \
                mock.patch.object(process_manager, "_desktop_windows", return_value=[]), \
                mock.patch.object(desktop_window, "exe_pids", return_value=[77]), \
                mock.patch("portal.browser_tools.terminate_tree", return_value=False):
            code, out, _ = run_out(process_manager.launcher_stop, force=True)
        self.assertEqual(code, 1)
        self.assertIn("止められませんでした", out[0])

    def test_窓が見つからなければ落とさずに1(self) -> None:
        with mock.patch.object(instance_guard, "running", return_value=instance_guard.DESKTOP), \
                mock.patch.object(process_manager, "_desktop_windows", return_value=[]), \
                mock.patch.object(desktop_window, "ask_to_close") as ask:
            code, out, _ = run_out(process_manager.launcher_stop)
        ask.assert_not_called()
        self.assertEqual(code, 1)
        self.assertIn("見つかりません", out[0])

    def test_ブラウザ版はstop_batと同じで_理由の1行だけを出す(self) -> None:
        busy = process_manager.Outcome(3, "実行中の処理があります: 日報(取り込み)",
                                       details=["止めませんでした:", "日報: 実行中の処理があります。"],
                                       hint=process_manager.FORCE_HINT)
        with mock.patch.object(instance_guard, "running", return_value=""), \
                mock.patch.object(process_manager, "run_stop", return_value=busy) as stop:
            code, out, err = run_out(process_manager.launcher_stop)
        stop.assert_called_once_with(force=False)
        self.assertEqual(code, 3)
        self.assertEqual(out, ["実行中の処理があります: 日報(取り込み)"])
        self.assertNotIn("stop.bat --force", err, "ランチャーの入口では stop.bat の案内を出さない")
        with mock.patch.object(instance_guard, "running", return_value=""), \
                mock.patch.object(process_manager, "run_stop",
                                  return_value=process_manager.Outcome(0, "日報複合ツールは動いていません")):
            self.assertEqual(run_out(process_manager.launcher_stop)[0], 0)


class DesktopWindowTests(unittest.TestCase):
    def test_大きなタブの窓だけを選ぶ(self) -> None:
        """題名は画面に合わせて「日報複合ツール — 日報」と変わる(CI で「日報複合ツール VER…」では見つからなかった)。
        確認の窓(持ち主がいる)・別窓(題名はツールの名前)・見えない窓には頼まない。"""
        windows = [(1, "日報複合ツール — 日報", False, True),
                   (2, "日報複合ツール", True, True),            # 終了の確認(大きなタブの窓が持ち主)
                   (3, "日報管理ツール — 印刷", False, True),  # 別窓
                   (4, "日報複合ツール", False, False)]          # 見えない
        with mock.patch("os.name", "nt"), \
                mock.patch.object(desktop_window, "exe_windows", return_value=windows):
            self.assertEqual(desktop_window.main_windows(instance_guard.DESKTOP_EXES, "日報複合ツール"), [1])

    def test_Windows以外は確かめられない(self) -> None:
        with mock.patch("os.name", "posix"):
            self.assertIsNone(desktop_window.main_windows(instance_guard.DESKTOP_EXES, "日報複合ツール VER"))
            self.assertEqual(desktop_window.ask_to_close([1]), 0)


if __name__ == "__main__":
    unittest.main()


class StuckToolTests(unittest.TestCase):
    """組み立てに失敗したツールの Python が、止め口に答えても終わらない/答えないとき。

    現場から: stop.bat を実行しても all-tools フォルダを消せず(実行中)、タスクマネージャーで
    Python を切ったら消せた。以前は頼んだだけで「終わってもらいました」と言っていた。
    """

    def setUp(self) -> None:
        import subprocess
        import sys

        from portal import browser_tools, catalog

        # 止まらないツールの Python(この試験の子ではない孫にする。本物も stop.bat の子ではない)
        out = subprocess.run([sys.executable, "-c",
                              "import subprocess, sys; p = subprocess.Popen([sys.executable, '-c', "
                              "'import time; time.sleep(60)'], stdin=subprocess.DEVNULL, "
                              "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); print(p.pid)"],
                             capture_output=True, text=True, timeout=30)
        self.pid = int(out.stdout.strip())
        self.addCleanup(browser_tools.terminate_tree, self.pid)
        tool = catalog.load().tools[0]
        self.running = browser_tools.Running(tool, {"pid": self.pid, "port": 1, "token": "t"})
        patches = [
            mock.patch.object(instance_guard, "running", return_value=""),
            mock.patch("start_app.read_lock", return_value=None),
            mock.patch.object(browser_tools.BrowserTools, "running", return_value=[self.running]),
            mock.patch.object(browser_tools.BrowserTools, "busy", return_value=[]),
            mock.patch.object(browser_tools.BrowserTools, "busy_tools", return_value=[]),
            mock.patch.object(browser_tools.BrowserTools, "stop_all"),
            mock.patch.object(process_manager, "TOOL_EXIT_WAIT_SEC", 0.5),
            mock.patch.object(process_manager, "TOOL_FORCE_WAIT_SEC", 0.5),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_終わらなければ終わったと言わない(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = process_manager.stop()
        self.assertEqual(code, 3, "終わるのを待っている(あとで止め直す)")
        self.assertTrue(out.getvalue().startswith("終わらなかったツールがあります"), "1行目が理由")
        self.assertNotIn("終わってもらいました", out.getvalue())
        self.assertIn("stop.bat --force", out.getvalue())
        from portal.browser_tools import is_alive
        self.assertTrue(is_alive(self.pid), "--force なしで止めた")

    def test_forceなら止める(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), \
                mock.patch.object(process_manager, "cleanup_inspection_excel") as cleanup:
            code = process_manager.stop(force=True)
        self.assertEqual(code, 0, out.getvalue())
        from portal.browser_tools import is_alive
        self.assertFalse(is_alive(self.pid))
        self.assertIn("終わらないので止めます", err.getvalue(), "途中の経過は標準エラーへ")
        self.assertIn("終わらないので止めました", out.getvalue())
        self.assertNotIn("終わってもらいました", out.getvalue(), "PID で止めたものを「終わってもらった」と言わない")
        cleanup.assert_called_once()
