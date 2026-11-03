"""止める入口の決まり(業務ツール統合ランチャー 1.8.9「更新のため全 PC で止める」)

終了コード 0 = 止めた・動いていない / 3 = 処理中なので止めなかった / 1 = そのほか。
**標準出力の1行目が理由**(「実行中の処理があります: 印刷」)。--force で処理中でも止める。
pause は /nopause か STOP_NOPAUSE=1 で飛ばす。道は "%~dp0" で囲む。
"""
from __future__ import annotations

import io
import re
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests.helpers  # noqa: E402,F401  (ローカル領域を一時フォルダへ)
import process_manager  # noqa: E402


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


def run_main(*argv: str) -> tuple[int, list[str]]:
    out = io.StringIO()
    with redirect_stdout(out):
        code = process_manager.main(list(argv))
    return code, out.getvalue().splitlines()


class StopReportTests(unittest.TestCase):
    """stop.bat が呼ぶ Python(`process_manager.py`)の結果の出し方。"""

    def result(self, *, stopped=False, busy=(), message="") -> "process_manager.StopResult":
        r = process_manager.StopResult()
        r.stopped, r.busy_jobs, r.message = stopped, list(busy), message
        return r

    def setUp(self) -> None:
        patcher = mock.patch.object(process_manager, "cleanup_excel", return_value=0)
        patcher.start()
        self.addCleanup(patcher.stop)
        from core import instance_guard
        patcher = mock.patch.object(instance_guard, "running", return_value="")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.stop = mock.patch.object(process_manager, "stop").start()
        self.addCleanup(mock.patch.stopall)

    def test_処理中なら3で1行目が理由(self) -> None:
        self.stop.return_value = self.result(busy=["印刷"],
                                             message="実行中の処理があります: 印刷\n    中断して…")
        code, out = run_main()
        self.assertEqual(code, 3)
        self.assertEqual(out[0], "実行中の処理があります: 印刷")
        out[0].encode("cp932")
        self.assertIn("stop.bat --force", "\n".join(out))

    def test_止めた_動いていないは0(self) -> None:
        self.stop.return_value = self.result(stopped=True, message="起動していません")
        self.assertEqual(run_main()[0], 0)
        self.stop.return_value = self.result(stopped=True, message="正常に終了しました")
        code, out = run_main()
        self.assertEqual(code, 0)
        self.assertIn("正常に終了しました", out[0])

    def test_そのほか止められなければ1で1行目が理由(self) -> None:
        self.stop.return_value = self.result(message="終了要求に応じません。強制的に止めるには --force\n    2行目")
        code, out = run_main()
        self.assertEqual(code, 1)
        self.assertEqual(out[0], "終了要求に応じません。強制的に止めるには --force")

    def test_forceを渡し_nopauseは読み飛ばす(self) -> None:
        self.stop.return_value = self.result(stopped=True, message="正常に終了しました")
        run_main("/nopause")
        self.stop.assert_called_with(force=False)
        run_main("--force", "/NOPAUSE")
        self.stop.assert_called_with(force=True)


class StopBusyTests(unittest.TestCase):
    """止め口の 409 を「処理中」として返し、--force なら force で頼む。"""

    def test_409なら止めずに処理の名前を返す_forceはforceで頼む(self) -> None:
        import launch_guard

        info = mock.Mock(port=8733, token="t", pid=111, app_root="", create_time=None)
        asked = []

        def ask(port, token, *, force):  # noqa: ANN001
            asked.append(force)
            return {"ok": True} if force else {"ok": False, "busy": True, "running": ["印刷"]}

        with mock.patch.object(launch_guard, "read_lock", return_value=info), \
                mock.patch.object(launch_guard, "probe_health", return_value={"app_id": "x"}), \
                mock.patch.object(launch_guard, "is_our_app", return_value=True), \
                mock.patch.object(launch_guard, "remove_lock"), \
                mock.patch.object(process_manager, "_request_shutdown", side_effect=ask), \
                mock.patch.object(process_manager, "_wait_gone", return_value=True):
            busy = process_manager.stop()
            self.assertFalse(busy.stopped)
            self.assertEqual(busy.busy_jobs, ["印刷"])
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(process_manager.report(busy), 3)
            self.assertEqual(out.getvalue().splitlines()[0], "実行中の処理があります: 印刷")
            forced = process_manager.stop(force=True)
        self.assertTrue(forced.stopped)
        self.assertEqual(asked, [False, True])


if __name__ == "__main__":
    unittest.main()
