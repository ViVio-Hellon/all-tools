"""止める入口の決まり(業務ツール統合ランチャー 1.8.9「更新のため全 PC で止める」)

終了コード 0 = 止めた・動いていない / 3 = 処理中なので止めなかった / 1 = そのほか。
**標準出力の1行目が理由**(「実行中の処理があります: 共有DBへ未反映の操作が 3 件あります」)。
--force で処理中でも止める。pause は /nopause か STOP_NOPAUSE=1 で飛ばす。道は "%~dp0" で囲む。
"""
from __future__ import annotations

import io
import re
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import launch_guard  # noqa: E402
import process_manager  # noqa: E402
from kanban import config  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def bat_text() -> str:
    return (ROOT / "stop.bat").read_bytes().decode("cp932")


def bat_commands() -> list[str]:
    return [line for line in bat_text().splitlines()
            if not re.match(r"(?i)^\s*(rem\b|::|@?echo off)", line)]


class StopBatContractTests(unittest.TestCase):
    """stop.bat の決まり(.bat は Linux では動かせないので中身を読む)。"""

    def test_道はdp0で全部囲む(self) -> None:
        self.assertIn('pushd "%~dp0"', bat_text())
        for line in bat_commands():
            for m in re.finditer(r"%~dp0", line):
                self.assertEqual(line[m.start() - 1], '"', line)
                self.assertEqual(line.count('"') % 2, 0, line)

    def test_引数を渡して終了コードをそのまま返す(self) -> None:
        text = bat_text()
        self.assertRegex(text, r"(?m)^python process_manager\.py( --all)? %\*\r?$")
        self.assertIn('set "code=%errorlevel%"', text)
        self.assertIn("endlocal & exit /b %code%", text)

    def test_pauseはnopauseとSTOP_NOPAUSEで飛ばす(self) -> None:
        lines = bat_commands()
        pauses = [line.strip() for line in lines if re.search(r"(?i)\bpause\b", line)]
        self.assertTrue(pauses)
        for line in pauses:
            self.assertEqual(line.lower(), "if not defined nopause pause", line)
        text = "\n".join(lines)
        self.assertIn('if /i "%STOP_NOPAUSE%"=="1" set "NOPAUSE=1"', text)
        self.assertIn('for %%A in (%*) do if /i "%%~A"=="/nopause" set "NOPAUSE=1"', text)
        self.assertLess(text.index('set "NOPAUSE="'), text.index('pushd "%~dp0"'))

    def test_Pythonより前に標準出力へ出すのはそこで終わる知らせだけ(self) -> None:
        lines = bat_commands()
        call = next(i for i, line in enumerate(lines) if line.startswith("python process_manager.py"))
        depth = 0
        for line in lines[:call]:
            stripped = line.strip()
            if depth == 0:
                self.assertFalse(stripped.lower().startswith("echo"), line)
                if stripped.lower().startswith("python"):
                    self.assertIn(">nul", stripped)
            depth += 1 if stripped.endswith("(") else 0
            depth -= 1 if stripped == ")" else 0



BUSY = "共有DBへ未反映の操作が 3 件あります"


class StopModeTests(unittest.TestCase):
    """stop.bat が呼ぶ Python(`process_manager.py`)。"""

    def setUp(self) -> None:
        self.info = mock.Mock(port=8741, token="t", pid=111, app_root="")
        self.alive = True
        for p in (
            mock.patch.object(launch_guard, "read_lock",
                              side_effect=lambda mode: self.info if mode == config.MODE_SITE else None),
            mock.patch.object(launch_guard, "is_process_alive", side_effect=lambda pid: self.alive),
            mock.patch.object(launch_guard, "probe_health", return_value={"app_id": "x"}),
            mock.patch.object(launch_guard, "is_our_app", return_value=True),
            mock.patch.object(launch_guard, "remove_lock"),
            mock.patch.object(process_manager, "GRACEFUL_WAIT_SEC", 0.2),
            mock.patch.object(process_manager, "KILL_WAIT_SEC", 0.2),
        ):
            p.start()
        self.addCleanup(mock.patch.stopall)

    def run_main(self, *argv: str) -> tuple[int, list[str]]:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = process_manager.main(list(argv))
        return code, out.getvalue().splitlines()

    def test_処理中なら3で1行目が理由_断りの答えから(self) -> None:
        """以前は止める前の /api/health の busy を使い、無いと「処理中のため」としか出なかった。"""
        with mock.patch.object(process_manager, "_ask_shutdown", return_value=(False, BUSY)):
            code, out = self.run_main()
        self.assertEqual(code, 3)
        self.assertEqual(out[0], f"実行中の処理があります: {BUSY}")
        out[0].encode("cp932")
        self.assertIn("stop.bat --force", "\n".join(out))

    def test_答えなければ止めずに3(self) -> None:
        """書き戻しの最中で答えられないのかもしれない。勝手に落とさない。"""
        with mock.patch.object(process_manager, "_ask_shutdown", return_value=(False, "")), \
                mock.patch.object(process_manager, "_terminate") as term:
            code, out = self.run_main()
        self.assertEqual(code, 3)
        self.assertIn(process_manager.NO_ANSWER, out[0])
        term.assert_not_called()

    def test_止めたら0_動いていなければ0(self) -> None:
        def ask(port, token):  # noqa: ANN001
            self.alive = False
            return True, ""

        with mock.patch.object(process_manager, "_ask_shutdown", side_effect=ask):
            code, out = self.run_main("/nopause")
        self.assertEqual(code, 0)
        self.assertIn("止めました", out[0])
        with mock.patch.object(launch_guard, "read_lock", return_value=None):
            code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("動いていません", out[0])

    def test_forceなら処理中でもforceで頼む(self) -> None:
        asked = []

        def force(port, token, *, force=False):  # noqa: ANN001
            asked.append(force)
            self.alive = False
            return True

        with mock.patch.object(process_manager, "_ask_shutdown", return_value=(False, BUSY)), \
                mock.patch.object(launch_guard, "request_shutdown", side_effect=force):
            code, out = self.run_main("--force", "/NOPAUSE")
        self.assertEqual(code, 0, out)
        self.assertEqual(asked, [True])

    def test_止められなければ1(self) -> None:
        with mock.patch.object(process_manager, "_ask_shutdown", return_value=(True, "")), \
                mock.patch.object(launch_guard, "process_command_line", return_value="other.exe"):
            code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertTrue(out[0].startswith("停止できませんでした"), out[0])


class AskShutdownTests(unittest.TestCase):
    def test_409の理由を読む(self) -> None:
        import json
        import urllib.error

        def refuse(*a, **kw):  # noqa: ANN002, ANN003
            raise urllib.error.HTTPError("u", 409, "busy", {}, io.BytesIO(json.dumps(
                {"reason": "busy", "running": [BUSY], "busy": BUSY}).encode("utf-8")))

        with mock.patch.object(launch_guard, "local_request", side_effect=refuse):
            self.assertEqual(process_manager._ask_shutdown(8741, "t"), (False, BUSY))

        def down(*a, **kw):  # noqa: ANN002, ANN003
            raise urllib.error.URLError("refused")

        with mock.patch.object(launch_guard, "local_request", side_effect=down):
            self.assertEqual(process_manager._ask_shutdown(8741, "t"), (False, ""))


if __name__ == "__main__":
    unittest.main()
