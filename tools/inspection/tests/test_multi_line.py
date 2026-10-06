"""複数ラインで使うときに、互いに壊し合わないこと

想定している使い方:
- アプリ本体は共有フォルダに1つ置き、各ラインのPCから起動する
- 点検表は共有フォルダにあり、複数のPCが同時にプレビュー・印刷する
- 1台のPCで複数のラインを受け持つ(既定のプリンターを切り替える・2つの画面を開く)

各PCのアプリはそのPC・その利用者のローカル領域だけに書き、ポートも 127.0.0.1 の中だけ。
ここでは、それでも共有されるもの・入れ替わるものについて確かめる。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import make_cfg, make_tree, quiet_logger, temp_dir

from app.services.excel_service import ExcelError
from core import app_config, process_tracking
from core.process_tracking import TrackedProcessRegistry

_ROOT = Path(__file__).resolve().parent.parent


class _BackendCase(unittest.TestCase):
    def make(self, **excel):
        from tests.test_excel_protocol import FakeBackend
        self._tmp = temp_dir()
        self.addCleanup(self._tmp.cleanup)
        tmp = self._tmp.name
        cfg = make_cfg(print_item_timeout_sec=3, preview_timeout_sec=5, excel_exit_grace_sec=1, **excel)
        self.registry = TrackedProcessRegistry(os.path.join(tmp, "tracked.json"), quiet_logger())
        backend = FakeBackend(cfg, os.path.join(tmp, "work"), self.registry, quiet_logger())
        FakeBackend.extra = {}
        self.addCleanup(backend.shutdown, "試験の後片付け")
        root = os.path.join(tmp, "root")
        make_tree(root, ["a/b/a_b_one.xlsx"])
        self.file = os.path.join(root, "a", "b", "a_b_one.xlsx")
        return backend

    def print_one(self, backend):
        outcomes = backend.print_files([self.file], 1, lambda *a: None, lambda: True)
        self.assertTrue(outcomes[0].ok)


class PrinterSwitchTest(_BackendCase):
    """1台で複数ラインを受け持つ: 既定のプリンターを替えたら、そのプリンターへ出す。"""

    def test_既定のプリンターが替わったらExcelを起動し直す(self):
        backend = self.make()
        printer = {"name": "ライン1のプリンター"}
        backend.printer_provider = lambda: printer["name"]
        self.print_one(backend)
        first = backend._session
        self.print_one(backend)
        self.assertIs(backend._session, first, "同じプリンターなら使い回す")
        printer["name"] = "ライン2のプリンター"
        self.print_one(backend)
        self.assertIsNot(backend._session, first, "前のラインのプリンターへ出さない")
        self.assertEqual(backend._session.printer, "ライン2のプリンター")
        self.assertFalse(first.worker.alive())


class ClipboardOwnerTest(_BackendCase):
    """同じPCでほかの人が Ctrl+C しても、別のものを点検表として出さない。"""

    def test_ほかのアプリに書き換えられたらコピーし直す(self):
        from app.services import clipboard_image
        backend = self.make()
        png = b"\\x89PNG-test"
        reads = [clipboard_image.ClipboardOwnerError("ほかのアプリ"), (png, {"format": "emf"})]
        with mock.patch.object(clipboard_image, "read_image_png", side_effect=reads):
            image = backend.preview(self.file)
        self.assertEqual(image.data, png)

    def test_書き換えられ続けたら理由を出して止める(self):
        from app.services import clipboard_image
        backend = self.make()
        err = clipboard_image.ClipboardOwnerError("ほかのアプリがクリップボードを使いました(pid=1)")
        with mock.patch.object(clipboard_image, "read_image_png", side_effect=[err, err, err]):
            with self.assertRaises(ExcelError) as ctx:
                backend.preview(self.file)
        self.assertEqual(ctx.exception.code, "CLIPBOARD_BUSY")
        self.assertIn("もう一度", ctx.exception.message)


class SharedAppFolderUpdateTest(_BackendCase):
    """共有フォルダのアプリが動作中に入れ替えられたとき。"""

    def test_処理プログラムが入れ替わっていたら組み合わせない(self):
        backend = self.make()
        backend._worker_digest = "起動したときとは違う"
        with self.assertRaises(ExcelError) as ctx:
            self.print_one(backend)
        self.assertEqual(ctx.exception.code, "APP_UPDATED")
        self.assertIn("起動し直して", ctx.exception.message)

    def test_置かれた版を読める(self):
        with temp_dir() as tmp:
            path = Path(tmp) / "app.json"
            path.write_text('{"version": "9.9.9"}', encoding="utf-8")
            with mock.patch.object(app_config, "CONFIG_PATH", path), \
                 mock.patch.object(app_config, "_disk_version", (0.0, "")):
                self.assertEqual(app_config.version_on_disk(), "9.9.9")

    def test_読めなければ入れ替わっていないとみなす(self):
        with mock.patch.object(app_config, "CONFIG_PATH", Path("/no/such/app.json")), \
             mock.patch.object(app_config, "_disk_version", (0.0, "")):
            self.assertEqual(app_config.version_on_disk(), app_config.version())


class NoWritesIntoAppFolderTest(unittest.TestCase):
    """アプリ本体を共有フォルダに置いても、各PCが本体のフォルダへ書かない。"""

    def test_起動確認と停止で__pycache__を作らない(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "app"
            shutil.copytree(_ROOT, copy, ignore=shutil.ignore_patterns(
                ".git", "__pycache__", "*.pyc", "tests", "src-tauri"))
            env = {k: v for k, v in os.environ.items()
                   if k not in ("PYTHONPYCACHEPREFIX", "PYTHONDONTWRITEBYTECODE")}
            env["INSPECTION_LOCAL_DIR"] = str(Path(tmp) / "local")
            env["PYTHONIOENCODING"] = "utf-8"
            for args in (["start_app.py", "--check"], ["process_manager.py", "--status"]):
                # 子は日本語を UTF-8 で出す(Windows の既定の文字コードで読むと読めずに落ちる)
                result = subprocess.run([sys.executable, *args], cwd=str(copy), env=env,
                                        capture_output=True, text=True, encoding="utf-8",
                                        errors="replace", timeout=120)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            # デスクトップ版の入口(bridge.py)も。外枠の代わりに、起動したらすぐ閉じる
            proc = subprocess.run([sys.executable, "bridge.py"], cwd=str(copy),
                                  env={**env, "INSPECTION_DEMO": "1"}, input=b"",
                                  capture_output=True, timeout=120)
            self.assertIn(b'"started"', proc.stdout, proc.stderr[-2000:])
            written = [str(p.relative_to(copy)) for p in copy.rglob("__pycache__")]
            self.assertEqual(written, [], "アプリ本体のフォルダに書いた")


class RegistryOwnerTest(unittest.TestCase):
    """起動した Excel の片付けは、**持ち主が居なくなったものだけ**。"""

    def spawn(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(lambda: (proc.poll() is None and proc.kill(), proc.wait()))
        ident = None
        for _ in range(50):
            ident = process_tracking.get_process_identity(proc.pid)
            if ident:
                break
            time.sleep(0.05)
        self.assertIsNotNone(ident)
        return proc, ident

    def test_動いているほかのアプリのExcelは止めない(self):
        owner, owner_ident = self.spawn()         # 動いている別のアプリの代わり
        excel, excel_ident = self.spawn()         # その Excel の代わり
        with temp_dir() as tmp:
            reg = TrackedProcessRegistry(os.path.join(tmp, "t.json"))
            with mock.patch.object(process_tracking, "current_identity", return_value=owner_ident):
                reg.add(excel_ident, "excel")
            cleaned = reg.cleanup("ほかのアプリの起動時", None)
            self.assertEqual(cleaned, [])
            self.assertIsNone(excel.poll(), "動いているアプリの Excel を止めた")
            self.assertEqual(len(reg.entries()), 1, "記録も残す")

            owner.kill()
            owner.wait()
            cleaned = reg.cleanup("持ち主が終わったあと", None)
            self.assertEqual([c["pid"] for c in cleaned], [excel.pid])
            self.assertEqual(reg.entries(), [])


class TwoScreensOnOnePcTest(unittest.TestCase):
    """1台のPCで2つの画面(2つのラインの人)が使う。"""

    def setUp(self):
        from app.services.excel_service import DummyExcelBackend, ExcelService
        from app.services.inspection_service import InspectionService
        from app.services.print_service import PrintService

        self._tmp = temp_dir()
        self.addCleanup(self._tmp.cleanup)
        root = os.path.join(self._tmp.name, "root")
        make_tree(root, ["c/s/c_s_A.xlsx", "c/s/c_s_B.xlsx"])

        class Settings:
            def effective_root_folder(self):
                return root
        self.inspection = InspectionService(make_cfg(), Settings(), quiet_logger())
        self.inspection.start_scan("試験")
        self.inspection.wait(5)
        excel = ExcelService(make_cfg(backend="dummy"), quiet_logger(),
                             TrackedProcessRegistry(os.path.join(self._tmp.name, "t.json")),
                             os.path.join(self._tmp.name, "work"))
        excel.backend = DummyExcelBackend(print_delay=0.3, preview_delay=0)
        self.printing = PrintService(self.inspection, excel, lambda: "", quiet_logger())
        self.ids = [i.id for c in self.inspection.inventory().categories
                    for s in c.subcategories for i in s.items]

    def test_ほかの画面の印刷中は断って理由を出す(self):
        from app.errors import ApiError
        job = self.printing.start(self.ids, 1, screen_id="ライン1の画面")
        self.assertEqual(job["screen_id"], "ライン1の画面")
        with self.assertRaises(ApiError) as ctx:
            self.printing.start(self.ids[:1], 1, screen_id="ライン2の画面")
        self.assertEqual(ctx.exception.code, "PRINT_RUNNING")
        self.assertIn("ほかの画面で", ctx.exception.message)
        self.assertTrue(self.printing.wait_idle(10))


class ManyTabsPreviewTest(unittest.TestCase):
    """タブを何枚も開いて、プレビューを次々に頼んでも、Excel とサーバの手を塞がない。"""

    DELAY = 0.3

    def setUp(self):
        from app.repositories.preview_cache import PreviewCache
        from app.services.excel_service import DummyExcelBackend, ExcelService
        from app.services.inspection_service import InspectionService
        from app.services.preview_service import PreviewService

        self._tmp = temp_dir()
        self.addCleanup(self._tmp.cleanup)
        root = os.path.join(self._tmp.name, "root")
        make_tree(root, [f"c/s/c_s_{n:02d}.xlsx" for n in range(10)])

        class Settings:
            def effective_root_folder(self):
                return root
        inspection = InspectionService(make_cfg(), Settings(), quiet_logger())
        inspection.start_scan("試験")
        inspection.wait(5)
        self.excel = ExcelService(make_cfg(backend="dummy"), quiet_logger(),
                                  TrackedProcessRegistry(os.path.join(self._tmp.name, "t.json")),
                                  os.path.join(self._tmp.name, "work"))
        self.backend = DummyExcelBackend(print_delay=0, preview_delay=self.DELAY)
        made = []
        original = self.backend.preview
        self.backend.preview = lambda path: (made.append(os.path.basename(path)), original(path))[1]
        self.made = made
        self.excel.backend = self.backend
        self.preview = PreviewService(make_cfg(preview_timeout_sec=30), inspection, self.excel,
                                      PreviewCache(os.path.join(self._tmp.name, "cache")), quiet_logger())
        self.ids = [i.id for c in inspection.inventory().categories
                    for s in c.subcategories for i in s.items]

    def ask(self, item_id, screen, results, key):
        from app.errors import ApiError
        try:
            results[key] = self.preview.get_preview(item_id, screen_id=screen)["ok"]
        except ApiError as exc:
            results[key] = exc.code

    def test_同じ画面で次を頼んだら_前の要求は順番待ちをやめる(self):
        import threading
        results = {}
        threads = []
        # ↓キーを押しっぱなし: 1枚の画面から10件を立て続けに頼む
        for n, item_id in enumerate(self.ids):
            t = threading.Thread(target=self.ask, args=(item_id, "A", results, n))
            t.start()
            threads.append(t)
            time.sleep(0.02)
        started = time.monotonic()
        for t in threads:
            t.join(10)
        # 作ったのは、先に作り始めていた1件と、最後に頼んだ1件だけ
        self.assertEqual(results[len(self.ids) - 1], True)
        self.assertLessEqual(len(self.made), 2, self.made)
        self.assertIn(os.path.basename(self.preview.inspection.get_item(self.ids[-1]).path), self.made)
        self.assertTrue(all(v in (True, "SUPERSEDED") for v in results.values()), results)
        self.assertLess(time.monotonic() - started, self.DELAY * 4)

    def test_別の画面の要求は取りやめない(self):
        import threading
        results = {}
        threads = [threading.Thread(target=self.ask, args=(self.ids[n], f"画面{n}", results, n))
                   for n in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(results, {0: True, 1: True, 2: True})
        self.assertEqual(len(self.made), 3)

    def test_順番待ちが多すぎたら断って_サーバの手を残す(self):
        import threading
        from app.services import preview_service
        results = {}
        with self.excel.acquire("print", timeout=1):    # 印刷中(Excel は塞がっている)
            threads = [threading.Thread(target=self.ask, args=(self.ids[n], f"画面{n}", results, n))
                       for n in range(preview_service.MAX_WAITING + 2)]
            for t in threads:
                t.start()
            time.sleep(0.5)
            refused = [k for k, v in results.items() if v == "PREVIEW_QUEUE_FULL"]
            self.assertEqual(len(refused), 2, results)
        for t in threads:
            t.join(10)
        self.assertEqual(sum(1 for v in results.values() if v is True), preview_service.MAX_WAITING)

    def test_画面の番号が無い要求はこれまでどおり順番に作る(self):
        import threading
        results = {}
        threads = [threading.Thread(target=self.ask, args=(self.ids[n], "", results, n)) for n in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(results, {0: True, 1: True, 2: True})

    def test_控えのフォルダが動いているあいだに消されても作り直す(self):
        shutil.rmtree(self.preview.cache.dir)
        reply = self.preview.get_preview(self.ids[0], screen_id="A")
        self.assertTrue(reply["ok"])
        self.assertIsNotNone(self.preview.get_image(reply["key"]))


if __name__ == "__main__":
    unittest.main()
