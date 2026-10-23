"""起動の記録も「ログの出力パス」の nippou フォルダへ (v4.44.5)

    日報の起動の記録: 出力パスへ出す

前は いつもこの端末の logs 直下の `launcher.log` / `guard.log` で、ログの出力パス(共有)には
出ていなかった。いまは
    <ログの出力パス>/nippou/nippou_起動_<端末>.log      (start_app。前の launcher.log)
    <ログの出力パス>/nippou/nippou_起動監視_<端末>.log  (launch_guard ほか。前の guard.log)
出力パスに届かない・書けないときは、この端末の logs の nippou フォルダへ(起動を止めない)。
"""
from __future__ import annotations

import logging
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou import log_names, logging_setup  # noqa: E402


class LaunchLogPlaceTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.shared = self.root / "共有ログ"
        self.local = self.root / "この端末" / "logs"
        self.shared.mkdir()
        self.loggers: list[logging.Logger] = []

    def tearDown(self) -> None:
        for logger in self.loggers:
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()

    def settings(self, log_dir: Path):
        return mock.patch.object(logging_setup, "SETTINGS",
                                 SimpleNamespace(log_dir=log_dir, default_log_dir=self.local))

    def run(self, result=None):
        # ほかの試験が起動の記録を一時フォルダへ寄せたまま(`tests/_launch_logs`)でも、
        # ここでは本物の書き先の決め方で確かめる
        with mock.patch.object(logging_setup, "_launch_log_path", logging_setup.LAUNCH_LOG_PATH):
            return super().run(result)

    def write(self, component: str, role: str, text: str) -> None:
        logger = logging_setup.get_launch_logger(f"試験_{component}", filename=role)
        self.loggers.append(logger)
        logger.info(text)
        for handler in logger.handlers:
            handler.flush()

    def name(self, role: str) -> str:
        return log_names.launch_log_name(role, logging_setup._terminal())

    def test_出力パスの_nippou_フォルダへ_ツール名で始まる名前で書く(self) -> None:
        with self.settings(self.shared):
            self.assertEqual(logging_setup.launch_log_dir(), self.shared / "nippou")
            self.write("guard", "guard.log", "多重起動の判定")
            self.write("launcher", "launcher.log", "ブラウザを開く")
        guard = self.shared / "nippou" / self.name("guard.log")
        launcher = self.shared / "nippou" / self.name("launcher.log")
        self.assertTrue(guard.name.startswith("nippou_起動監視_"), guard.name)
        self.assertTrue(launcher.name.startswith("nippou_起動_"), launcher.name)
        self.assertIn("多重起動の判定", guard.read_text(encoding="utf-8"))
        self.assertIn("ブラウザを開く", launcher.read_text(encoding="utf-8"))
        # 前の名前・手元には書かない
        self.assertFalse((self.local / "guard.log").exists())
        self.assertFalse((self.local / "launcher.log").exists())
        self.assertFalse((self.shared / "guard.log").exists())
        self.assertEqual(sorted(p.name for p in self.shared.iterdir()), ["nippou"],
                         "出力パスの直下には nippou フォルダだけ")

    def test_出力パスに書けなければ_この端末の_logs_の_nippou_へ(self) -> None:
        blocked = self.root / "ファイル"           # フォルダを作れない場所(同じ名前のファイルがある)
        blocked.write_text("", encoding="utf-8")
        with self.settings(blocked / "下"):
            self.assertEqual(logging_setup.launch_log_dir(), self.local / "nippou")
            self.write("guard2", "guard.log", "共有が落ちている朝")
        path = self.local / "nippou" / self.name("guard.log")
        self.assertIn("共有が落ちている朝", path.read_text(encoding="utf-8"))

    def test_起動待ちの画面のログの場所も出力パスの_nippou(self) -> None:
        import boot_server
        with self.settings(self.shared), mock.patch.object(boot_server, "launch_log_dir",
                                                           logging_setup.launch_log_dir):
            html = boot_server.BootApp(0, "t").page().decode("utf-8")
        self.assertIn(str(self.shared / "nippou").replace("\\", "\\\\"), html)
        self.assertIn("nippou_起動監視_", html)


if __name__ == "__main__":
    unittest.main()
