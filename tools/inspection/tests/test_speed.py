"""重さの手当て(ラインPCで「起動も印刷もやや重い」)

1. 起動: 前回の一覧を先に出す。フォルダの確認は裏で続ける
2. 印刷・プレビュー: Excel を使い回す(test_excel_protocol.ExcelReuseTest)。
   点検表を選び始めたら Excel を裏で起動しておく
"""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
from unittest import mock

from tests.helpers import make_cfg, make_tree, quiet_logger, temp_dir

from app.services.inspection_service import InspectionService
from core.process_tracking import TrackedProcessRegistry


class FakeSettings:
    def __init__(self, root):
        self.root = root

    def effective_root_folder(self):
        return self.root


class InventoryCacheTest(unittest.TestCase):
    def setUp(self):
        self._tmp = temp_dir()
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.join(self._tmp.name, "点検表")
        make_tree(self.root, ["梱包/日常点検/梱包_日常点検_台車点検.xlsx",
                              "梱包/日常点検/梱包_日常点検_結束機点検.xlsm",
                              "梱包/日常点検/命名規則外.xlsx"])
        self.cache = os.path.join(self._tmp.name, "data", "inventory_cache.json")

    def service(self, root=None):
        return InspectionService(make_cfg(), FakeSettings(root or self.root), quiet_logger(),
                                 cache_path=self.cache)

    def scan(self, svc):
        svc.start_scan("試験")
        self.assertTrue(svc.wait(10))

    def test_確認が終わったら控えを残す(self):
        self.scan(self.service())
        self.assertTrue(os.path.isfile(self.cache))

    def test_次の起動では確認を待たずに前回の一覧が出る(self):
        first = self.service()
        self.scan(first)
        before = first.inventory()
        second = self.service()
        self.assertTrue(second.load_cache())
        inv = second.inventory()
        self.assertIsNotNone(inv, "確認より前に一覧がある")
        self.assertTrue(second.status()["from_cache"])
        self.assertEqual(inv.to_dict(), before.to_dict())
        item = next(iter(inv.by_id.values()))
        self.assertTrue(os.path.isfile(item.path), "控えからでも印刷に使うパスが戻る")
        self.scan(second)
        self.assertFalse(second.status()["from_cache"])

    def test_フォルダ設定が違えば使わない(self):
        self.scan(self.service())
        other = self.service(root=os.path.join(self._tmp.name, "別のフォルダ"))
        self.assertFalse(other.load_cache())
        self.assertIsNone(other.inventory())

    def test_壊れた控えは無視して確認し直す(self):
        os.makedirs(os.path.dirname(self.cache), exist_ok=True)
        with open(self.cache, "w", encoding="utf-8") as fp:
            fp.write("{壊れている")
        svc = self.service()
        self.assertFalse(svc.load_cache())
        self.scan(svc)
        self.assertEqual(svc.inventory().listed_files, 2)

    def test_形の違う控えは使わない(self):
        os.makedirs(os.path.dirname(self.cache), exist_ok=True)
        with open(self.cache, "w", encoding="utf-8") as fp:
            json.dump({"version": 999, "root": self.root}, fp)
        self.assertFalse(self.service().load_cache())

    def test_フォルダを変えたら前の一覧は出さない(self):
        svc = self.service()
        self.scan(svc)
        svc.clear()
        self.assertIsNone(svc.inventory())


class ReadyWithoutWaitingTest(unittest.TestCase):
    def test_前回の一覧があれば準備完了を待たない(self):
        import start_app
        srv = mock.Mock()
        business = mock.Mock()
        business.inspection.inventory.return_value = object()
        start_app._ready_when_scanned(srv, business)
        srv.mark_ready.assert_called_once_with(True)
        business.inspection.wait.assert_not_called()

    def test_一覧が無ければ確認を待つ(self):
        import start_app
        srv = mock.Mock()
        business = mock.Mock()
        business.inspection.inventory.return_value = None
        business.inspection.wait.return_value = True
        start_app._ready_when_scanned(srv, business)
        business.inspection.wait.assert_called_once()
        srv.mark_ready.assert_called_once_with(True)


class WarmUpTest(unittest.TestCase):
    """点検表を選び始めたら、Excel を裏で起動しておく。"""

    def setUp(self):
        from app.services.excel_service import ExcelService
        from tests.test_excel_protocol import FakeBackend

        self._tmp = temp_dir()
        self.addCleanup(self._tmp.cleanup)
        tmp = self._tmp.name
        cfg = make_cfg(backend="dummy", print_item_timeout_sec=3, preview_timeout_sec=5,
                       excel_exit_grace_sec=1)
        registry = TrackedProcessRegistry(os.path.join(tmp, "tracked.json"), quiet_logger())
        self.excel = ExcelService(cfg, quiet_logger(), registry, os.path.join(tmp, "work"))
        FakeBackend.extra = {}
        self.excel.backend = FakeBackend(cfg, os.path.join(tmp, "work"), registry, quiet_logger())
        self.addCleanup(self.excel.shutdown, "試験の後片付け")
        root = os.path.join(tmp, "root")
        make_tree(root, ["a/b/a_b_one.xlsx"])
        self.file = os.path.join(root, "a", "b", "a_b_one.xlsx")

    def wait_session(self):
        deadline = time.time() + 10
        while time.time() < deadline:
            session = self.excel.backend._session
            if session is not None and session.jobs >= 1 and not self.excel._lock.locked():
                return session
            time.sleep(0.05)
        self.fail("先回りの起動が終わらない")

    def test_先に起動したExcelで印刷が始まる(self):
        self.assertTrue(self.excel.warm_up())
        session = self.wait_session()
        with self.excel.acquire("print", timeout=10):
            outcomes = self.excel.print_files([self.file], 1, lambda *a: None, lambda: True)
        self.assertTrue(outcomes[0].ok)
        self.assertIs(self.excel.backend._session, session)
        self.assertEqual(session.jobs, 2)

    def test_ほかの処理中は先回りしない(self):
        with self.excel.acquire("print", timeout=1):
            self.assertFalse(self.excel.warm_up())

    def test_模擬のExcelでは何もしない(self):
        from app.services.excel_service import DummyExcelBackend
        self.excel.backend = DummyExcelBackend()
        self.assertFalse(self.excel.warm_up())


if __name__ == "__main__":
    unittest.main()
