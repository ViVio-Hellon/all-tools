"""印刷: 部数の扱い（VBA版と同じ）・選択順・二重実行防止・中止・エラー通知。"""
import os
import time
import unittest

from tests.helpers import make_cfg, make_tree, quiet_logger, temp_dir

from app.services.excel_service import DummyExcelBackend, ExcelService
from app.services.inspection_service import InspectionService
from app.services.print_service import COPIES_ERROR, PrintService, parse_copies
from app.errors import ApiError
from core.process_tracking import TrackedProcessRegistry


class ParseCopiesTest(unittest.TestCase):
    def test_vba_compatible_rules(self):
        self.assertEqual(parse_copies("2"), 2)
        self.assertEqual(parse_copies(" 3 "), 3)
        self.assertEqual(parse_copies("0"), 1)       # 1 未満は 1
        self.assertEqual(parse_copies("-5"), 1)
        self.assertEqual(parse_copies("250"), 100)   # 100 超は 100
        self.assertEqual(parse_copies("250", max_copies=300), 250)
        self.assertEqual(parse_copies("2.5"), 2)     # CInt と同じ銀行丸め
        self.assertEqual(parse_copies("3.5"), 4)
        self.assertEqual(parse_copies(7), 7)

    def test_non_numeric_is_error(self):
        for raw in ("", "abc", "１部", None, True):
            with self.assertRaises(ValueError) as ctx:
                parse_copies(raw)
            self.assertEqual(str(ctx.exception), COPIES_ERROR)


class FakeSettings:
    def __init__(self, root):
        self.root = root

    def effective_root_folder(self):
        return self.root


class PrintJobTest(unittest.TestCase):
    def setUp(self):
        self._tmp = temp_dir()
        tmp = self._tmp.name
        self.config = make_cfg(backend="dummy")
        root = os.path.join(tmp, "root")
        make_tree(root, ["c/s/c_s_A.xlsx", "c/s/c_s_B.xlsx", "c/s/c_s_エラーC.xlsx", "c/s/c_s_D.xlsx"])
        log = quiet_logger()
        self.inspection = InspectionService(self.config, FakeSettings(root), log)
        self.inspection.start_scan("test")
        self.inspection.wait(5)
        self.excel = ExcelService(self.config, log,
                                  TrackedProcessRegistry(os.path.join(tmp, "tracked.json")),
                                  os.path.join(tmp, "work"))
        self.excel.backend = DummyExcelBackend(print_delay=0.05, preview_delay=0)
        self.service = PrintService(self.inspection, self.excel, lambda: "テストプリンター", log)
        items = {i.name: i for c in self.inspection.inventory().categories
                 for s in c.subcategories for i in s.items}
        self.ids = items

    def tearDown(self):
        self.service.wait_idle(10)
        self._tmp.cleanup()

    def wait_done(self):
        self.assertTrue(self.service.wait_idle(10))
        return self.service.status()

    def test_prints_in_selected_order_with_errors_reported(self):
        order = [self.ids["D"].id, self.ids["エラーC"].id, self.ids["A"].id]
        status = self.service.start(order, "2")
        self.assertTrue(status["active"])
        job = self.wait_done()
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["copies"], 2)
        self.assertEqual([i["name"] for i in job["items"]], ["D", "エラーC", "A"])
        self.assertEqual([i["status"] for i in job["items"]], ["ok", "error", "ok"])
        self.assertEqual(job["items"][1]["message"], "印刷に失敗しました。")
        self.assertEqual((job["succeeded"], job["failed"]), (2, 1))
        self.assertEqual(job["printer"], "テストプリンター")

    def test_double_execution_is_rejected(self):
        self.excel.backend.print_delay = 0.3
        self.service.start([self.ids["A"].id, self.ids["B"].id], 1)
        with self.assertRaises(ApiError) as ctx:
            self.service.start([self.ids["D"].id], 1)
        self.assertEqual(ctx.exception.code, "PRINT_RUNNING")

    def test_cancel_stops_after_current_item(self):
        self.excel.backend.print_delay = 0.3
        self.service.start([self.ids["A"].id, self.ids["B"].id, self.ids["D"].id], 1)
        time.sleep(0.1)
        self.service.cancel()
        job = self.wait_done()
        self.assertEqual(job["state"], "cancelled")
        self.assertEqual(job["items"][0]["status"], "ok")
        self.assertEqual([i["status"] for i in job["items"][1:]], ["skipped", "skipped"])

    def test_validation(self):
        with self.assertRaises(ApiError) as ctx:
            self.service.start([], 1)
        self.assertEqual((ctx.exception.status, ctx.exception.code), (422, "NO_SELECTION"))
        self.assertEqual(ctx.exception.message, "印刷する点検表を選択してください。")
        with self.assertRaises(ApiError) as ctx:
            self.service.start([self.ids["A"].id], "abc")
        self.assertEqual(ctx.exception.message, COPIES_ERROR)
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(ApiError) as ctx:
            self.service.start(["unknown-id"], 1)
        self.assertEqual(ctx.exception.code, "ITEM_NOT_FOUND")

    def test_deleted_file_is_reported(self):
        os.remove(self.ids["B"].path)
        self.service.start([self.ids["B"].id], 1)
        job = self.wait_done()
        self.assertEqual(job["items"][0]["message"], "対象の点検表ファイルが見つかりません。")

    def test_busy_labels_while_printing(self):
        self.excel.backend.print_delay = 0.3
        self.assertEqual(self.service.busy_labels(), [])
        self.service.start([self.ids["A"].id, self.ids["B"].id], 1)
        self.assertEqual(self.service.busy_labels(), ["印刷"])
        self.wait_done()
        self.assertEqual(self.service.busy_labels(), [])

    def test_still_busy_until_results_are_recorded(self):
        """終わった直後(状態は完了・結果の記録はまだ)に「空いた」と言わない。
        CI で wait_idle の後に print.item・print.end が欠けたことがある。"""
        original = self.service._record_end
        recorded = []

        def slow_record_end(job, paths, **kw):
            time.sleep(0.3)
            original(job, paths, **kw)
            recorded.append(job.job_id)

        self.service._record_end = slow_record_end
        self.service.start([self.ids["A"].id], "1")
        self.assertTrue(self.service.wait_idle(10))
        self.assertEqual(len(recorded), 1, "記録を書き終えるまで待つ")
        self.assertEqual(self.service.busy_labels(), [])

    def test_preview_is_blocked_while_printing(self):
        from app.services.excel_service import ExcelError
        self.excel.backend.print_delay = 0.3
        self.service.start([self.ids["A"].id, self.ids["B"].id], 1)
        time.sleep(0.1)
        with self.assertRaises(ExcelError) as ctx:
            with self.excel.acquire("preview", timeout=0.1):
                pass
        self.assertEqual(ctx.exception.code, "EXCEL_BUSY")
        self.assertIn("印刷", ctx.exception.message)


if __name__ == "__main__":
    unittest.main()
