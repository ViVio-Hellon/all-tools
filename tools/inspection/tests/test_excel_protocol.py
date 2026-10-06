"""Python ⇔ VBScript の通信手順（偽の worker で Windows 以外でも確認する）。"""
import os
import time
import sys
import unittest

from tests.helpers import make_cfg, make_tree, quiet_logger, temp_dir

from app.services.excel_service import ExcelError, VbsExcelBackend, decode_hex_utf16, encode_hex_utf16
from app.services.png_encoder import encode_rgb
from core.process_tracking import TrackedProcessRegistry

FAKE_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_worker.py")


class FakeBackend(VbsExcelBackend):
    extra = {}

    def _base_params(self):
        params = super()._base_params()
        params.update(self.extra)
        return params

    def _command(self):
        return [sys.executable, FAKE_WORKER, "session"]


class HexTest(unittest.TestCase):
    """日本語は UTF-16 の16進で ASCII の管を通す(excel_worker.vbs の HexW / UnHexW と同じ)。"""

    def test_roundtrip(self):
        from tests.fake_worker import hexw, unhexw
        for text in ("", "A1:P40", r"\\nlmsrvngy03\各課共有\【■】_参照用ファイル\点検表=1.xlsx", "𠮷野家 ①"):
            self.assertEqual(decode_hex_utf16(hexw(text)), text)
            self.assertEqual(unhexw(encode_hex_utf16(text)), text)
        self.assertEqual(encode_hex_utf16("AB"), "00410042")
        self.assertEqual(encode_hex_utf16(12), "00310032")

    def test_newlines_are_not_sent(self):
        self.assertEqual(decode_hex_utf16(encode_hex_utf16("a\r\nb")), "a  b")


class WorkerProtocolTest(unittest.TestCase):
    def setUp(self):
        self._tmp = temp_dir()
        tmp = self._tmp.name
        self.config = make_cfg(print_item_timeout_sec=3, preview_timeout_sec=5, excel_exit_grace_sec=1)
        self.work = os.path.join(tmp, "work")
        self.registry = TrackedProcessRegistry(os.path.join(tmp, "tracked.json"), quiet_logger())
        self.backend = FakeBackend(self.config, self.work, self.registry, quiet_logger())
        FakeBackend.extra = {}
        self.root = os.path.join(tmp, "root")
        make_tree(self.root, ["a/b/a_b_one.xlsx", "a/b/a_b_fail.xlsx", "a/b/a_b_three.xlsx"])
        self.files = [os.path.join(self.root, "a", "b", n) for n in ("a_b_one.xlsx", "a_b_fail.xlsx", "a_b_three.xlsx")]

    def tearDown(self):
        self.backend.shutdown("試験の後片付け")
        self._tmp.cleanup()

    def test_print_reports_each_item_in_order(self):
        events = []
        outcomes = self.backend.print_files(self.files, 2, lambda *a: events.append(a), lambda: True)
        self.assertEqual([o.ok for o in outcomes], [True, False, True])
        self.assertEqual(outcomes[1].code, "PRINT_FAILED")
        self.assertEqual(outcomes[1].message, "印刷に失敗しました。")
        self.assertIn("オフライン", outcomes[1].detail)
        self.assertEqual(outcomes[0].sheet, "点検シート")
        self.assertEqual(events, [("begin", 1), ("end", 1, True), ("begin", 2), ("end", 2, False),
                                  ("begin", 3), ("end", 3, True)])
        # 処理のあとも Excel は残す(次の処理を速くするため)。閉じれば記録は片付く
        self.assertEqual([e["label"] for e in self.registry.entries()], ["cscript:session"])
        self.backend.shutdown()
        self.assertEqual(self.registry.entries(), [])

    def test_stop_after_current_item(self):
        calls = {"n": 0}

        def should_continue():
            calls["n"] += 1
            return calls["n"] <= 1   # 1件目だけ許可

        outcomes = self.backend.print_files(self.files, 1, lambda *a: None, should_continue)
        self.assertTrue(outcomes[0].ok)
        self.assertEqual([o.code for o in outcomes[1:]], ["STOPPED", "STOPPED"])

    def test_timeout_kills_worker_and_marks_current_item(self):
        FakeBackend.extra = {"fake_hang_at": "2"}
        outcomes = self.backend.print_files(self.files, 1, lambda *a: None, lambda: True)
        self.assertTrue(outcomes[0].ok)
        self.assertEqual(outcomes[1].code, "TIMEOUT")
        self.assertEqual(outcomes[2].code, "STOPPED")
        self.assertEqual(self.registry.entries(), [])

    def test_excel_not_available(self):
        FakeBackend.extra = {"fake_no_excel": "1"}
        outcomes = self.backend.print_files(self.files, 1, lambda *a: None, lambda: True)
        self.assertEqual({o.code for o in outcomes}, {"EXCEL_CREATE_FAILED"})

    def test_preview_falls_back_to_chart_export_when_clipboard_unavailable(self):
        png = encode_rgb(2, 2, bytes([255, 0, 0] * 4))
        FakeBackend.extra = {"fake_png_hex": png.hex()}
        if os.name == "nt":
            self.skipTest("Windows では実際のクリップボードを読むため対象外")
        image = self.backend.preview(self.files[0])
        self.assertEqual(image.data, png)
        self.assertEqual(image.method, "chart-export")
        self.assertEqual(image.sheet, "Sheet1")
        self.assertEqual(image.range_address, "A1:P40")

    def test_missing_worker_command(self):
        class Broken(FakeBackend):
            def _command(self, *args):
                return [os.path.join(self_tmp, "no_such_cscript.exe")]
        self_tmp = self._tmp.name
        backend = Broken(self.config, self.work, self.registry, quiet_logger())
        with self.assertRaises(ExcelError) as ctx:
            backend.selftest()
        self.assertEqual(ctx.exception.code, "WORKER_FAILED")


class ResultsOverStdoutTest(unittest.TestCase):
    """実機で「プレビューも印刷もできない」(原因が画面に出ない)となったことへの手当て。

    原因は Microsoft Store 版 Python の書き込み先の振り替えで、Python が書いた
    ジョブファイルが cscript から「パスが見つかりません」(0x4C)だった。
    いまはファイルを使わず標準入出力だけでやり取りする。指示が届かなければ
    そう言うこと、こちらが止めていないものを「中止」と言わないことを確かめる。
    """

    def setUp(self):
        self._tmp = temp_dir()
        tmp = self._tmp.name
        self.config = make_cfg(print_item_timeout_sec=3, preview_timeout_sec=5, excel_exit_grace_sec=1)
        self.registry = TrackedProcessRegistry(os.path.join(tmp, "tracked.json"), quiet_logger())
        self.backend = FakeBackend(self.config, os.path.join(tmp, "work"), self.registry, quiet_logger())
        FakeBackend.extra = {}
        root = os.path.join(tmp, "root")
        make_tree(root, ["a/b/a_b_one.xlsx", "a/b/a_b_fail.xlsx"])
        self.files = [os.path.join(root, "a", "b", n) for n in ("a_b_one.xlsx", "a_b_fail.xlsx")]

    def tearDown(self):
        FakeBackend.extra = {}
        self.backend.shutdown("試験の後片付け")
        self._tmp.cleanup()

    def test_unreadable_job_is_reported_not_stopped(self):
        FakeBackend.extra = {"fake_job_unreadable": "1"}
        outcomes = self.backend.print_files(self.files, 1, lambda *a: None, lambda: True)
        self.assertEqual({o.code for o in outcomes}, {"JOB_READ_FAILED"})
        self.assertEqual(outcomes[0].message, "Excel処理プログラム（VBScript）が処理の指示を読み込めませんでした。")
        self.assertTrue(outcomes[0].detail)

    def test_unreadable_job_in_preview(self):
        """指示が届かなければ「指示を読めない」と言う。「ファイルが見つかりません」ではない
        (ラインPCでは点検表があるのにそう出ていた)。"""
        FakeBackend.extra = {"fake_job_unreadable": "1"}
        with self.assertRaises(ExcelError) as ctx:
            self.backend.preview(self.files[0])
        self.assertEqual(ctx.exception.code, "JOB_READ_FAILED")
        self.assertIn("項目しか届きませんでした", ctx.exception.detail)

    def test_japanese_paths_reach_the_worker(self):
        """指示はファイルではなく標準入力で届く。日本語・記号入りのパスもそのまま。"""
        root = os.path.join(self._tmp.name, "【■】_参照用ファイル", "梱包", "日常点検")
        make_tree(os.path.join(self._tmp.name, "【■】_参照用ファイル"), ["梱包/日常点検/梱包_日常点検_台車=1.xlsx"])
        outcomes = self.backend.print_files([os.path.join(root, "梱包_日常点検_台車=1.xlsx")], 2,
                                            lambda *a: None, lambda: True)
        self.assertTrue(outcomes[0].ok)

    def test_preview_error_detail(self):
        FakeBackend.extra = {"fake_copy_fail": "1"}
        with self.assertRaises(ExcelError) as ctx:
            self.backend.preview(self.files[0])
        self.assertEqual(ctx.exception.code, "COPY_FAILED")
        self.assertIn("CopyPicture", ctx.exception.detail)
        self.assertIn("0x800A03EC", ctx.exception.detail)

    def test_no_files_are_left_in_work_dir(self):
        """Python が書いたファイルを cscript に読ませない(Store 版 Python では見えない)。"""
        work = self.backend.work_dir
        before = set(os.listdir(work))
        self.backend.print_files(self.files, 1, lambda *a: None, lambda: True)
        self.assertEqual(set(os.listdir(work)), before)

    def test_not_stopped_by_us_is_not_called_stopped(self):
        """こちらは GO と言ったのに VBScript が読めなかった → 「中止」ではない。"""
        FakeBackend.extra = {"fake_stdin_eof": "1"}
        outcomes = self.backend.print_files(self.files, 1, lambda *a: None, lambda: True)
        self.assertEqual({o.code for o in outcomes}, {"WORKER_FAILED"})
        self.assertIn("StdIn.ReadLine failed", outcomes[0].detail)

    def test_stopped_only_when_we_stop(self):
        outcomes = self.backend.print_files(self.files, 1, lambda *a: None, lambda: False)
        self.assertEqual({o.code for o in outcomes}, {"STOPPED"})


class ExcelReuseTest(unittest.TestCase):
    """Excel は続けて使い回す(ラインPCで「起動も印刷もやや重い」)。"""

    def make(self, keep_alive_sec=120):
        self._tmp = temp_dir()
        self.addCleanup(self._tmp.cleanup)
        tmp = self._tmp.name
        cfg = make_cfg(print_item_timeout_sec=3, preview_timeout_sec=5, excel_exit_grace_sec=1,
                       keep_alive_sec=keep_alive_sec)
        self.registry = TrackedProcessRegistry(os.path.join(tmp, "tracked.json"), quiet_logger())
        backend = FakeBackend(cfg, os.path.join(tmp, "work"), self.registry, quiet_logger())
        self.addCleanup(backend.shutdown, "試験の後片付け")
        FakeBackend.extra = {}
        root = os.path.join(tmp, "root")
        make_tree(root, ["a/b/a_b_one.xlsx", "a/b/a_b_two.xlsx"])
        self.files = [os.path.join(root, "a", "b", n) for n in ("a_b_one.xlsx", "a_b_two.xlsx")]
        return backend

    def print_all(self, backend):
        outcomes = backend.print_files(self.files, 1, lambda *a: None, lambda: True)
        self.assertTrue(all(o.ok for o in outcomes))

    def test_2回目は同じExcelで処理する(self):
        backend = self.make()
        self.print_all(backend)
        first = backend._session
        self.assertIsNotNone(first, "処理のあとも Excel を残す")
        pid = first.worker.pid
        self.print_all(backend)
        self.assertIs(backend._session, first)
        self.assertEqual(backend._session.worker.pid, pid)
        self.assertEqual(backend._session.jobs, 2)

    def test_使われなければ閉じる(self):
        backend = self.make(keep_alive_sec=1)
        self.print_all(backend)
        worker = backend._session.worker
        deadline = time.time() + 10
        while backend._session is not None and time.time() < deadline:
            time.sleep(0.1)
        self.assertIsNone(backend._session)
        self.assertFalse(worker.alive())
        self.assertEqual(self.registry.entries(), [], "起動した cscript の記録が残っている")

    def test_0なら毎回閉じる(self):
        backend = self.make(keep_alive_sec=0)
        self.print_all(backend)
        self.assertIsNone(backend._session)

    def test_アプリの終了で閉じる(self):
        backend = self.make()
        self.print_all(backend)
        worker = backend._session.worker
        backend.shutdown()
        self.assertIsNone(backend._session)
        self.assertFalse(worker.alive())
        self.assertEqual(self.registry.entries(), [])

    def test_途中で落ちたら次は起動し直す(self):
        backend = self.make()
        FakeBackend.extra = {"fake_die_after": "1"}
        outcomes = backend.print_files(self.files, 1, lambda *a: None, lambda: True)
        self.assertTrue(all(o.processed for o in outcomes))
        self.assertIsNone(backend._session, "落ちた処理プログラムを使い回さない")
        FakeBackend.extra = {}
        self.print_all(backend)
        self.assertEqual(backend._session.jobs, 1)

    def test_応答が無ければ止めて次は起動し直す(self):
        backend = self.make()
        FakeBackend.extra = {"fake_hang_at": "1"}
        outcomes = backend.print_files(self.files, 1, lambda *a: None, lambda: True)
        self.assertEqual(outcomes[0].code, "TIMEOUT")
        self.assertIsNone(backend._session)
        FakeBackend.extra = {}
        self.print_all(backend)


class VbscriptSourceTest(unittest.TestCase):
    """Excel を動かす VBScript は ASCII のみ・CRLF(WSH は ANSI として読むため)。

    起動ファイル(Start.vbs / start.bat / stop.bat)は日本語入りの CP932 で、
    tests/test_launch_files.py が見ている。
    """

    def test_worker_is_ascii_and_crlf(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "app", "vbscript", "excel_worker.vbs"), "rb") as fp:
            data = fp.read()
        data.decode("ascii")  # 例外にならないこと
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"), "excel_worker.vbs は CRLF で保存してください")


if __name__ == "__main__":
    unittest.main()
