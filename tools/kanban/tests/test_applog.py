"""デバッグログの冒頭 (:mod:`kanban.applog`)

**調べるときに手元にあるのはログだけ**です。現場から送られてきた
``DebugLog_*.txt`` を見て「どの端末の、どの版が、どのラインで」動いていたのかを
言い当てられなければ、そこから先へ進めません。

とくに版は、「その不具合はもう直っている版では?」を確かめる唯一の手がかりに
なります。
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kanban import app_config, applog


class LogHeaderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="kanban_applog_"))

    def tearDown(self) -> None:
        applog.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def header(self, line: str = "LVC") -> str:
        path = applog.initialize(self.dir, line_name=line)
        applog.close()
        self.assertIsNotNone(path, "ログファイルが作られていない")
        return Path(str(path)).read_text(encoding="utf-8")

    def test_header_carries_the_version(self):
        """**どの版が書いたログかを、ログ自身に残す。**

        送られてきたログだけを見て調べることになるので、ここに無いと
        「もう直っている版では?」を確かめる手がなくなる。
        """
        self.assertIn(app_config.version_label(), self.header())

    def test_header_carries_the_line(self):
        self.assertIn("LVC", self.header("LVC"))

    def test_closing_does_not_leave_a_dead_handler_on_dbkit(self):
        """**閉じたハンドラを dbkit 側に残さない。**

        ``initialize`` は同じハンドラの実体を dbkit のロガーにも付ける。
        こちらから外すだけだと、閉じたハンドラが向こうに残り、以後 dbkit が
        1 行でも書こうとした瞬間に落ちる ── 終了処理と入れ違いに動いている
        スレッドが踏みうる。
        """
        import logging

        applog.initialize(self.dir, line_name="LVC")
        dbkit_logger = logging.getLogger("dbkit")
        self.assertTrue(
            [h for h in dbkit_logger.handlers if isinstance(h, logging.FileHandler)],
            "前提が崩れている: initialize が dbkit にファイルハンドラを付けていない",
        )

        applog.close()

        # **閉じたファイルハンドラを残さない。** logging は例外を握りつぶして
        # stderr に出すだけなので、書いて落ちるかでは確かめられない
        left = [h for h in dbkit_logger.handlers if isinstance(h, logging.FileHandler)]
        self.assertEqual(left, [], "閉じたハンドラが dbkit 側に残っている")

    def test_an_unreadable_version_does_not_stop_the_log(self):
        """版が読めなくても、ログは書く。**調べる手段を先に潰さない。**"""
        with mock.patch.object(
            app_config, "version_label", side_effect=RuntimeError("壊れた app.json")
        ):
            text = self.header()
        self.assertIn("デバッグログ開始", text)
        self.assertIn("取得できません", text, "読めなかった理由も残す")


if __name__ == "__main__":
    unittest.main()
