"""start.bat の窓の進み具合と、Excel の確認をブラウザのあとへ回したこと

実機(ラインPC)で、start.bat の「実行環境の確認」が Excel を起動して閉じきるまで
待っていたため、ブラウザが出るまで十数秒かかり、そのあいだ窓も止まって見えた。
"""
from __future__ import annotations

import io
import logging
import os
import time
import unittest

from tests.helpers import make_cfg, quiet_logger, temp_dir

from core import console_progress
from core.console_progress import ConsoleProgress


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


class _Cp932(io.StringIO):
    """■□ を出せない窓の代わり。"""

    def write(self, text: str) -> int:
        text.encode("ascii")
        return super().write(text)


def frames(text: str):
    return [f for f in text.replace("\n", "\r").split("\r") if f.strip()]


class ConsoleProgressTest(unittest.TestCase):
    def test_窓では同じ行を書き換えて棒を伸ばす(self) -> None:
        out = _Tty()
        p = ConsoleProgress(4, out)
        p.step("一つ目")
        p.step("二つ目")
        p.finish("できました")
        shots = frames(out.getvalue())
        self.assertIn("[□□□□□□□□□□□□□□□□]   0%  1/4 一つ目", shots[0])
        self.assertIn("[■■■■□□□□□□□□□□□□]  25%  2/4 二つ目", shots[1])
        self.assertIn("[■■■■■■■■■■■■■■■■] 100%  できました", shots[-1])
        self.assertEqual(out.getvalue().count("\n"), 1)   # 改行は終わりの1回だけ

    def test_待っているあいだも秒数が進む(self) -> None:
        out = _Tty()
        p = ConsoleProgress(2, out)
        p.step("Excel を起動して確かめています")
        p.step_started -= 3                      # 3秒たったことにする
        time.sleep(console_progress.TICK_SEC * 2.5)
        p.finish()
        self.assertTrue(any("Excel を起動して確かめています  3秒" in f for f in frames(out.getvalue())),
                        frames(out.getvalue()))

    def test_短くなった行は前の文字を消す(self) -> None:
        out = _Tty()
        p = ConsoleProgress(2, out)
        p.step("とても長い段の名前です")
        p.step("短い")
        p.finish()
        second = out.getvalue().split("\r")[2]
        self.assertTrue(second.endswith(" " * 10), repr(second))

    def test_ファイルへ流すときは1段1行(self) -> None:
        out = io.StringIO()
        p = ConsoleProgress(2, out)
        p.step("一つ目")
        p.step("二つ目")
        p.finish("できました")
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[:2], ["[1/2] 一つ目", "[2/2] 二つ目"])
        self.assertTrue(lines[2].startswith("できました ("))
        self.assertNotIn("\r", out.getvalue())

    def test_窓が無ければ何もしない(self) -> None:
        import sys
        saved = sys.stdout
        sys.stdout = None                         # pythonw(Start.vbs)
        try:
            p = ConsoleProgress(2)
            p.step("一つ目")
            p.finish()
        finally:
            sys.stdout = saved

    def test_四角を出せない窓では記号に替える(self) -> None:
        out = _Cp932()
        p = ConsoleProgress(2, out, live=True)
        p.step("step")
        p.finish("done")
        self.assertIn("[----------------]   0%  1/2 step", out.getvalue())
        self.assertIn("[################] 100%  done", out.getvalue())

    def test_出しているあいだ窓のログは注意以上だけ_終われば戻す(self) -> None:
        logger = logging.getLogger("inspection.test-progress")
        logger.propagate = False
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.INFO)
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        logger.setLevel(logging.INFO)

        out = _Tty()
        p = ConsoleProgress(2, out)
        p.step("一つ目")
        logger.info("棒に割り込む行")
        logger.warning("注意の行")
        p.finish()
        self.assertNotIn("棒に割り込む行", stream.getvalue())
        self.assertIn("注意の行", stream.getvalue())
        # 注意の行の前に、棒の行を消している
        self.assertIn("\r" + " " * 10, out.getvalue())
        self.assertEqual(handler.level, logging.INFO)
        logger.info("終わったあとの行")
        self.assertIn("終わったあとの行", stream.getvalue())


class ExcelCheckKeepsExcelTest(unittest.TestCase):
    """アプリの中で確かめた Excel は閉じずに、最初のプレビュー・印刷に使う。"""

    def make(self):
        from app.services.excel_service import ExcelService
        from core.process_tracking import TrackedProcessRegistry
        tmp = temp_dir()
        self.addCleanup(tmp.cleanup)
        excel = ExcelService(make_cfg(backend="dummy"), quiet_logger(),
                             TrackedProcessRegistry(os.path.join(tmp.name, "t.json")),
                             os.path.join(tmp.name, "work"))
        closed = []
        excel.shutdown = lambda reason="": closed.append(reason)
        return excel, closed

    def test_残すときは閉じない(self) -> None:
        excel, closed = self.make()
        self.assertTrue(excel.selftest(keep=True)["ok"])
        self.assertEqual(closed, [])

    def test_診断のときは閉じる(self) -> None:
        excel, closed = self.make()
        self.assertTrue(excel.selftest()["ok"])
        self.assertEqual(closed, ["動作確認のあと"])

    def test_起動の確認は画面が出たあとでExcelを残す(self) -> None:
        import start_app
        excel, closed = self.make()

        class Business:
            demo = False
        business = Business()
        business.excel = excel
        import contextlib
        shown = io.StringIO()
        with contextlib.redirect_stdout(shown):
            start_app._check_excel_in_app(business)
        self.assertEqual(closed, [])
        self.assertIn("Excel の確認: OK", shown.getvalue())


if __name__ == "__main__":
    unittest.main()
