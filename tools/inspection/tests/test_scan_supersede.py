"""検索の途中でフォルダを変えても、前のフォルダの一覧を出さない(VER2.1.0 の直し)

以前は、検索の途中でフォルダを変えると ``start_scan`` が「もう走っている」で
何もせずに返り、走っていた検索が**前のフォルダの一覧**を入れて控えにも
書いた。画面は「フォルダを変更しました」と言うのに、出ているのは前の
フォルダの点検表 ── そのまま選んで印刷できてしまう。
"""
from __future__ import annotations

import json
import os
import threading
import unittest

from tests.helpers import make_cfg, make_tree, quiet_logger, temp_dir

from app.services import inspection_service
from app.services.inspection_service import InspectionService


class _Settings:
    def __init__(self, root: str) -> None:
        self.root = root

    def effective_root_folder(self) -> str:
        return self.root


class ScanSupersedeTests(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = temp_dir()
        self.addCleanup(self._tmp.cleanup)
        self.root_a = os.path.join(self._tmp.name, "A")
        self.root_b = os.path.join(self._tmp.name, "B")
        make_tree(self.root_a, ["a/x/a_x_古い.xlsx"])
        make_tree(self.root_b, ["b/y/b_y_新しい.xlsx"])
        self.settings = _Settings(self.root_a)
        self.cache = os.path.join(self._tmp.name, "cache", "inventory.json")
        self.svc = InspectionService(make_cfg(), self.settings, quiet_logger(), cache_path=self.cache)

        # A の検索だけを途中で止めておけるようにする(共有が遅い日)
        self.gate = threading.Event()
        self.entered = threading.Event()
        original = inspection_service.scan_folder

        def slow(root, *args, **kwargs):
            if root == self.root_a:
                self.entered.set()
                self.gate.wait(5)
            return original(root, *args, **kwargs)

        inspection_service.scan_folder = slow
        self.addCleanup(setattr, inspection_service, "scan_folder", original)
        self.addCleanup(self.gate.set)

    def names(self):
        inv = self.svc.inventory()
        return [i.name for c in inv.categories for s in c.subcategories for i in s.items]

    def test_走っている検索の結果を捨てて新しいフォルダで調べ直す(self) -> None:
        self.assertTrue(self.svc.start_scan("起動時"))
        self.assertTrue(self.entered.wait(5))
        # 検索の途中でフォルダを変える(設定画面の保存と同じ順)
        self.settings.root = self.root_b
        self.svc.clear()
        self.assertTrue(self.svc.start_scan("フォルダ設定の変更", supersede=True))
        self.assertEqual(self.svc.status()["state"], "scanning")
        self.gate.set()
        self.assertTrue(self.svc.wait(10))
        self.assertEqual(self.svc.inventory().root, self.root_b)
        self.assertEqual(self.names(), ["新しい"])
        self.assertEqual(self.svc.status()["state"], "done")
        # 控えにも前のフォルダを書いていない
        with open(self.cache, encoding="utf-8") as fp:
            self.assertEqual(json.load(fp)["root"], self.root_b)

    def test_supersedeでなければ今までどおり重ねて走らせない(self) -> None:
        self.assertTrue(self.svc.start_scan("起動時"))
        self.assertTrue(self.entered.wait(5))
        self.assertFalse(self.svc.start_scan("画面の再読込"))
        self.gate.set()
        self.assertTrue(self.svc.wait(10))
        self.assertEqual(self.names(), ["古い"])

    def test_世代を進めなくてもフォルダが違えば入れない(self) -> None:
        """設定だけ先に変わった(clear を呼ぶ前に検索が終わった)場合も、前の一覧を入れない。"""
        self.assertTrue(self.svc.start_scan("起動時"))
        self.assertTrue(self.entered.wait(5))
        self.settings.root = self.root_b
        self.gate.set()
        self.assertTrue(self.svc.wait(10))
        self.assertEqual(self.svc.inventory().root, self.root_b)
        self.assertEqual(self.names(), ["新しい"])

    def test_終わったあとはまた始められる(self) -> None:
        self.gate.set()
        self.assertTrue(self.svc.start_scan("起動時"))
        self.assertTrue(self.svc.wait(10))
        self.assertTrue(self.svc.start_scan("画面の再読込"))
        self.assertTrue(self.svc.wait(10))
        self.assertEqual(self.names(), ["古い"])


class SettingsRouteSupersedeTests(unittest.TestCase):
    """設定の API が、走っている検索を捨てさせる(``supersede=True``)。API を実際に呼んで見る。"""

    def setUp(self) -> None:
        try:
            import flask  # noqa: F401
        except ImportError:                       # pragma: no cover
            self.skipTest("Flask が入っていない")
        from unittest import mock

        from app import create_app
        from core import idle_exit
        idle_exit.reset()
        self.addCleanup(idle_exit.reset)
        self._tmp = temp_dir()
        self.addCleanup(self._tmp.cleanup)
        env = mock.patch.dict(os.environ, {"INSPECTION_DISTRIBUTION_DIR": os.path.join(self._tmp.name, "配布設定")})
        env.start()
        self.addCleanup(env.stop)
        app = create_app(token="t", port=8795, options={})
        self.biz = app.extensions["inspection"]
        self.biz.settings.put_many({k: None for k in list(self.biz.settings.values())})
        self.addCleanup(self.biz.settings.put_many, {"root_folder": None})
        app.config["READY"] = True
        self.client = app.test_client()
        self.calls = []
        patcher = mock.patch.object(self.biz.inspection, "start_scan",
                                    lambda reason, **kw: self.calls.append((reason, kw)) or True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, path, body):
        return self.client.post(path, json=body, headers={"X-Tool-Token": "t"})

    def test_フォルダ変更は走っている検索を捨てさせる(self) -> None:
        folder = os.path.join(self._tmp.name, "新しい")
        os.makedirs(folder)
        res = self.post("/api/settings", {"root_folder": folder})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(self.calls, [("フォルダ設定の変更", {"supersede": True})])

    def test_配布設定の読み込み直しでフォルダが替われば走っている検索を捨てさせる(self) -> None:
        self.biz.settings.update(root_folder=self._tmp.name)
        self.assertEqual(self.post("/api/settings/distribution/export",
                                   {"items": ["root_folder"], "password": "nisk"}).status_code, 200)
        self.biz.settings.put("root_folder", None)
        self.calls.clear()
        res = self.post("/api/settings/distribution/reapply", {"password": "nisk"})
        self.assertTrue(res.get_json()["rescan"])
        self.assertEqual(self.calls, [("配布設定の読み込み", {"supersede": True})])


# 一部のフォルダが読めなかっただけで、その中の選択を永久に消さない(画面側)は、「再読込」で読めない
# サブフォルダのある一覧を返して test_selection_held_browser.SelectionHeldBrowserTests が見る


if __name__ == "__main__":
    unittest.main()
