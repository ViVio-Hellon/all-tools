"""業務ツール統合ランチャーの入口(launcher_check.bat・launcher_stop.bat → process_manager.py)

デスクトップ版の窓を探す・閉じる部分(Windows の ctypes)は、ここでは差し替えて分かれ道を見る。
本物は Windows の CI(`scripts/desktop_smoke.py`)が exe を起動して確かめる。
ブラウザ版の止め方は `stop.bat` と同じ(`test_web.py` の CloseAskTests と、docs/ランチャー連携.md の通し確認)。
"""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
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


class LauncherStopTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(process_manager, "DESKTOP_CLOSE_WAIT_SEC", 0.5)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_デスクトップ版は窓に閉じてと頼み_閉じたら0(self) -> None:
        states = iter([instance_guard.DESKTOP, instance_guard.DESKTOP, ""])
        with mock.patch.object(instance_guard, "running", side_effect=lambda: next(states, "")), \
                mock.patch.object(process_manager, "_desktop_windows", return_value=[101]), \
                mock.patch.object(desktop_window, "ask_to_close", return_value=1) as ask:
            code, last = run(process_manager.launcher_stop)
        ask.assert_called_once_with([101])
        self.assertEqual(code, 0)
        self.assertIn("閉じました", last)

    def test_確認に答えていなければ1と理由(self) -> None:
        with mock.patch.object(instance_guard, "running", return_value=instance_guard.DESKTOP), \
                mock.patch.object(process_manager, "_desktop_windows", return_value=[101]), \
                mock.patch.object(desktop_window, "ask_to_close", return_value=1):
            code, last = run(process_manager.launcher_stop)
        self.assertEqual(code, 1)
        self.assertIn("閉じてよいかの確認", last, "ランチャーは最後の1行を理由として見せる")

    def test_強制のときは窓に頼まずランチャーの止め方に任せる(self) -> None:
        with mock.patch.object(instance_guard, "running", return_value=instance_guard.DESKTOP), \
                mock.patch.object(desktop_window, "ask_to_close") as ask:
            code, _ = run(process_manager.launcher_stop, force=True)
        ask.assert_not_called()
        self.assertEqual(code, 1)

    def test_窓が見つからなければ落とさずに1(self) -> None:
        with mock.patch.object(instance_guard, "running", return_value=instance_guard.DESKTOP), \
                mock.patch.object(process_manager, "_desktop_windows", return_value=[]), \
                mock.patch.object(desktop_window, "ask_to_close") as ask:
            code, last = run(process_manager.launcher_stop)
        ask.assert_not_called()
        self.assertEqual(code, 1)
        self.assertIn("見つかりません", last)

    def test_ブラウザ版はstop_batと同じで_案内を最後に出さない(self) -> None:
        with mock.patch.object(instance_guard, "running", return_value=""), \
                mock.patch.object(process_manager, "stop", return_value=2) as stop:
            self.assertEqual(run(process_manager.launcher_stop)[0], 1)
        stop.assert_called_once_with(force=False, hints=False)
        with mock.patch.object(instance_guard, "running", return_value=""), \
                mock.patch.object(process_manager, "stop", return_value=0):
            self.assertEqual(run(process_manager.launcher_stop)[0], 0)


class DesktopWindowTests(unittest.TestCase):
    def test_Windows以外は確かめられない(self) -> None:
        with mock.patch("os.name", "posix"):
            self.assertIsNone(desktop_window.main_windows(instance_guard.DESKTOP_EXES, "統合ツール VER"))
            self.assertEqual(desktop_window.ask_to_close([1]), 0)


if __name__ == "__main__":
    unittest.main()
