"""Word の点検表(.doc / .docx / .docm)も一覧に載せて、プレビュー・印刷する (VER2.3.0)

    点検表 選択・印刷: リストへの表示ルールは同じで Word にも対応願います

【約束】
    ・一覧の決まりは Excel と同じ(カテゴリ > サブカテゴリ > 「カテゴリ_サブカテゴリ_」で始まる名前)
    ・既定の拡張子に doc / docx / docm を足す。前の版の既定(xlsm / xlsx / xls だけ)のままの
      設定ファイルは Word も足して読む。自分で決めた並びは触らない
    ・どちらで開くかは拡張子で決める(`kind.<i>` を処理プログラムへ添える)
    ・Word のプレビューは1ページ目(EMFDATA)。描けなければクリップボード経由(COPY → COPIED)
    ・Word を起動できなくても、同じ印刷の Excel の点検表は刷る
    ・Word は処理のたびに終える。利用者の文書が開いていれば残して、止めない
"""
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from tests.helpers import make_cfg, make_tree, quiet_logger, temp_dir
from tests.test_excel_protocol import FakeBackend

from app.services import clipboard_image
from app.services.excel_service import ExcelError, WORD_EXTENSIONS, kind_of
from app.services.inspection_service import scan_folder
from app.services.png_encoder import encode_rgb
from app.services.settings_service import (DEFAULT_EXTENSIONS, InspectionConfig,
                                           load_inspection_config)
from core.process_tracking import TrackedProcessRegistry, get_process_identity

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class KindTest(unittest.TestCase):
    def test_拡張子でWordかExcelかを決める(self):
        self.assertEqual(kind_of(r"\\srv\点検\a_b_手順.docx"), "word")
        self.assertEqual(kind_of("a_b_手順.DOC"), "word")
        self.assertEqual(kind_of("a_b_手順.docm"), "word")
        self.assertEqual(kind_of("a_b_点検.xlsx"), "excel")
        self.assertEqual(kind_of("a_b_点検.xls"), "excel")
        self.assertEqual(kind_of(""), "excel")

    def test_Wordの拡張子は既定の一覧にすべて入っている(self):
        for ext in WORD_EXTENSIONS:
            self.assertIn(ext, DEFAULT_EXTENSIONS)


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self._tmp = temp_dir()
        self.addCleanup(self._tmp.cleanup)

    def load(self, extensions):
        path = os.path.join(self._tmp.name, "inspection.json")
        with open(path, "w", encoding="utf-8") as fp:
            json.dump({"extensions": extensions}, fp)
        return load_inspection_config(path)

    def test_既定はExcelとWord(self):
        self.assertEqual(InspectionConfig().extensions,
                         ["xlsm", "xlsx", "xls", "doc", "docx", "docm"])

    def test_前の版の既定のままならWordも足す(self):
        self.assertEqual(self.load(["xlsm", "xlsx", "xls"]).extensions, list(DEFAULT_EXTENSIONS))
        self.assertEqual(self.load(["XLS", ".xlsx", "xlsm"]).extensions, list(DEFAULT_EXTENSIONS))

    def test_自分で決めた並びは触らない(self):
        self.assertEqual(self.load(["xlsx"]).extensions, ["xlsx"])
        self.assertEqual(self.load(["xlsx", "docx"]).extensions, ["xlsx", "docx"])

    def test_配る設定ファイルにもWordが入っている(self):
        cfg = load_inspection_config(os.path.join(ROOT, "config", "inspection.json"))
        for ext in WORD_EXTENSIONS:
            self.assertIn(ext, cfg.extensions)


class ScanTest(unittest.TestCase):
    """一覧の決まりは Excel と同じ。"""

    def setUp(self):
        self._tmp = temp_dir()
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.join(self._tmp.name, "root")
        make_tree(self.root, [
            "梱包/日常点検/梱包_日常点検_台車点検.xlsx",
            "梱包/日常点検/梱包_日常点検_作業手順.docx",
            "梱包/日常点検/梱包_日常点検_旧手順.doc",
            "梱包/日常点検/梱包_日常点検_マクロ付き.docm",
            "梱包/日常点検/~$梱包_日常点検_作業手順.docx",     # Word が作る一時ロックファイル
            "梱包/日常点検/旧_手順.docx",                       # 名前の決まりに合わない
            "梱包/日常点検/梱包_日常点検_メモ.txt",              # 対象外の拡張子
        ])

    def test_Wordも同じ決まりで一覧に載る(self):
        inv = scan_folder(self.root, list(DEFAULT_EXTENSIONS), True)
        daily = inv.categories[0].subcategories[0]
        self.assertEqual(sorted(i.name for i in daily.items), ["マクロ付き", "作業手順", "台車点検", "旧手順"])
        self.assertEqual({i.ext for i in daily.items}, {"xlsx", "docx", "doc", "docm"})
        self.assertEqual([u.file_name for u in daily.unmatched], ["旧_手順.docx"])
        self.assertEqual(inv.total_files, 5)
        self.assertEqual(inv.listed_files, 4)

    def test_拡張子を絞ればWordは載らない(self):
        inv = scan_folder(self.root, ["xlsx"], True)
        daily = inv.categories[0].subcategories[0]
        self.assertEqual([i.name for i in daily.items], ["台車点検"])


class WordProtocolTest(unittest.TestCase):
    """偽の処理プログラムで、Word の点検表の受け渡しを確かめる(Windows でなくても動く)。"""

    def setUp(self):
        self._tmp = temp_dir()
        tmp = self._tmp.name
        self.config = make_cfg(print_item_timeout_sec=3, preview_timeout_sec=5, excel_exit_grace_sec=1)
        self.registry = TrackedProcessRegistry(os.path.join(tmp, "tracked.json"), quiet_logger())
        self.backend = FakeBackend(self.config, os.path.join(tmp, "work"), self.registry, quiet_logger())
        FakeBackend.extra = {}
        root = os.path.join(tmp, "root")
        make_tree(root, ["a/b/a_b_one.xlsx", "a/b/a_b_手順.docx", "a/b/a_b_three.xls"])
        self.excel = os.path.join(root, "a", "b", "a_b_one.xlsx")
        self.word = os.path.join(root, "a", "b", "a_b_手順.docx")
        self.excel2 = os.path.join(root, "a", "b", "a_b_three.xls")

    def tearDown(self):
        FakeBackend.extra = {}
        self.backend.shutdown("試験の後片付け")
        self._tmp.cleanup()

    def test_ExcelとWordを混ぜて刷れる(self):
        events = []
        outcomes = self.backend.print_files([self.excel, self.word, self.excel2], 1,
                                            lambda *a: events.append(a), lambda: True)
        self.assertEqual([o.ok for o in outcomes], [True, True, True])
        self.assertEqual([e for e in events if e[0] == "end"],
                         [("end", 1, True), ("end", 2, True), ("end", 3, True)])

    def test_Wordだけでも刷れる(self):
        outcomes = self.backend.print_files([self.word], 2, lambda *a: None, lambda: True)
        self.assertTrue(outcomes[0].ok)

    def test_Wordを起動できなくてもExcelの点検表は刷る(self):
        FakeBackend.extra = {"fake_no_word": "1"}
        outcomes = self.backend.print_files([self.excel, self.word, self.excel2], 1,
                                            lambda *a: None, lambda: True)
        self.assertEqual([o.ok for o in outcomes], [True, False, True])
        self.assertEqual(outcomes[1].code, "WORD_CREATE_FAILED")
        self.assertIn("Word", outcomes[1].message)

    def test_Wordのプレビューは1ページ目の絵(self):
        png = encode_rgb(2, 2, bytes([0, 0, 255] * 4))
        FakeBackend.extra = {"fake_emf_hex": "deadbeef0102"}
        got = {}

        def fake_emf_to_png(data, scale, max_pixels):
            got["data"] = data
            return png, {"format": "emf", "width": 2, "height": 2}

        with mock.patch.object(clipboard_image, "emf_to_png", fake_emf_to_png):
            image = self.backend.preview(self.word)
        self.assertEqual(got["data"], bytes.fromhex("deadbeef0102"))
        self.assertEqual(image.data, png)
        self.assertEqual(image.method, "word-page")
        self.assertEqual(image.range_source, "word_page")

    def test_描けなければクリップボード経由に切り替える(self):
        png = encode_rgb(2, 2, bytes([0, 255, 0] * 4))

        def broken(data, scale, max_pixels):
            raise clipboard_image.ClipboardImageError("描けない")

        def from_clipboard(*args, **kwargs):
            return png, {"format": "emf", "width": 2, "height": 2}

        with mock.patch.object(clipboard_image, "emf_to_png", broken), \
                mock.patch.object(clipboard_image, "read_image_png", from_clipboard):
            image = self.backend.preview(self.word)
        self.assertEqual(image.data, png)
        self.assertEqual(image.method, "clipboard-emf")

    def test_どちらも駄目なら理由を出す(self):
        def broken(*args, **kwargs):
            raise clipboard_image.ClipboardImageError("描けない")

        with mock.patch.object(clipboard_image, "emf_to_png", broken), \
                mock.patch.object(clipboard_image, "read_image_png", broken):
            with self.assertRaises(ExcelError) as ctx:
                self.backend.preview(self.word)
        self.assertEqual(ctx.exception.code, "COPY_FAILED")

    def test_Wordを起動できなければプレビューもそう言う(self):
        FakeBackend.extra = {"fake_no_word": "1"}
        with self.assertRaises(ExcelError) as ctx:
            self.backend.preview(self.word)
        self.assertEqual(ctx.exception.code, "WORD_CREATE_FAILED")
        self.assertIn("Word", ctx.exception.message)

    def test_Excelのプレビューはこれまでどおり(self):
        png = encode_rgb(2, 2, bytes([255, 0, 0] * 4))
        with mock.patch.object(clipboard_image, "read_image_png",
                               lambda *a, **k: (png, {"format": "emf", "width": 2, "height": 2})):
            image = self.backend.preview(self.excel)
        self.assertEqual(image.method, "clipboard-emf")
        self.assertEqual(image.sheet, "Sheet1")


class WordLifetimeTest(unittest.TestCase):
    """Word は処理のたびに終える。利用者の文書が開いていれば止めない。"""

    def setUp(self):
        self._tmp = temp_dir()
        self.addCleanup(self._tmp.cleanup)
        self.registry = TrackedProcessRegistry(os.path.join(self._tmp.name, "t.json"), quiet_logger())
        self.backend = FakeBackend(make_cfg(excel_exit_grace_sec=1), os.path.join(self._tmp.name, "w"),
                                   self.registry, quiet_logger())
        self.proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(self.proc.wait)
        self.addCleanup(self.proc.kill)

    def session_with_word(self):
        from app.services.excel_service import _Session
        identity = get_process_identity(self.proc.pid)
        self.assertIsNotNone(identity)
        session = _Session(worker=None, cscript=None)
        session.word = identity
        self.registry.add(identity, "word")
        return session

    def test_利用者の文書が開いていれば残して止めない(self):
        session = self.session_with_word()
        self.backend._word_closed(session, quit_=False)
        self.assertIsNone(session.word)
        self.assertEqual(self.registry.entries(), [])
        self.assertIsNone(self.proc.poll(), "利用者の Word を止めてはいけない")

    def test_終えたWordが残っていても_Wordでなければ止めない(self):
        """止めるのは確かに WINWORD.EXE のときだけ(PID の取り違えで別のものを止めない)。"""
        session = self.session_with_word()
        self.backend._word_closed(session, quit_=True)
        self.assertIsNone(session.word)
        self.assertIsNone(self.proc.poll())


class WorkerSourceTest(unittest.TestCase):
    """処理プログラム(VBScript)に Word の道があり、ASCII・CRLF のまま。"""

    def setUp(self):
        with open(os.path.join(ROOT, "app", "vbscript", "excel_worker.vbs"), "rb") as fp:
            self.data = fp.read()
        self.text = self.data.decode("ascii")

    def test_Wordの道がある(self):
        for needle in ('CreateObject("Word.Application")', "Function RunPreviewWord()",
                       "Function PrintOneWord(", "EnhMetaFileBits", 'Emit "EMFDONE 1"',
                       'Emit "WORDBYE 1"', 'Emit "WORDBYE 0"', "doc.Close wdDoNotSaveChanges",
                       'JobValue("kind." & index, "excel")'):
            self.assertIn(needle, self.text)

    def test_利用者の文書は閉じない(self):
        """Word を終えるのは、開いている文書が 0 のときだけ(全部閉じる処理を持たない)。"""
        self.assertNotIn("gWd.Documents(i).Close", self.text)
        finish = self.text[self.text.index("Sub FinishWord()"):]
        finish = finish[:finish.index("End Sub")]
        self.assertIn("If openDocs = 0 Then", finish)

    def test_ASCIIとCRLF(self):
        self.assertEqual(self.data.count(b"\n"), self.data.count(b"\r\n"))


if __name__ == "__main__":
    unittest.main()
