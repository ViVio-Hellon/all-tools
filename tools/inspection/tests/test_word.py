"""Word の点検表(.doc / .docx / .docm)も一覧に載せて、プレビュー・印刷する (VER2.3.0)

    点検表 選択・印刷: リストへの表示ルールは同じで Word にも対応願います

【約束】
    ・一覧の決まりは Excel と同じ(カテゴリ > サブカテゴリ > 「カテゴリ_サブカテゴリ_」で始まる名前)
    ・既定の拡張子に doc / docx / docm を足す。前の版の既定(xlsm / xlsx / xls だけ)のままの
      設定ファイルは Word も足して読む。自分で決めた並びは触らない
    ・どちらで開くかは拡張子で決める(`kind.<i>` を処理プログラムへ添える)
    ・Word のプレビューは PDF(VER2.4.0。PDFDATA。tests/test_word_pdf.py)。PDF にできない古い Word では
      1ページ目(EMFDATA)、描けなければクリップボード経由(COPY → COPIED)
    ・Word を起動できなくても、同じ印刷の Excel の点検表は刷る
    ・Word は処理のたびに終える。利用者の文書が開いていれば残して、止めない
"""
import json
import os
import re
import subprocess
import sys
import unittest
from unittest import mock

from tests.helpers import make_cfg, make_tree, quiet_logger, temp_dir
from tests.test_excel_protocol import FakeBackend

from app.services import clipboard_image, excel_service
from app.services.excel_service import (STAGE_LABELS, ExcelError, WORD_EXTENSIONS, explain_errno,
                                        kind_of)
from app.services.inspection_service import scan_folder
from app.services.png_encoder import encode_rgb
from app.services.settings_service import (DEFAULT_EXTENSIONS, InspectionConfig,
                                           load_inspection_config)
from app import why_hints
from core import event_log, process_tracking
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

    def test_PDFにできない古いWordなら1ページ目の絵(self):
        png = encode_rgb(2, 2, bytes([0, 0, 255] * 4))
        FakeBackend.extra = {"fake_emf_hex": "deadbeef0102", "fake_pdf": "none"}
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
        FakeBackend.extra = {"fake_pdf": "none"}

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
        FakeBackend.extra = {"fake_pdf": "none"}

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


class WordDiagnosticsTest(unittest.TestCase):
    """VER2.3.0 の後: ラインPCで「Word のプレビューも印刷もできない」。次に失敗したとき、
    **画面と記録だけで「なぜ」が分かる**こと(どこで・何番のエラーで・どんな窓が出ていたか)。"""

    def setUp(self):
        self._tmp = temp_dir()
        tmp = self._tmp.name
        self.config = make_cfg(print_item_timeout_sec=3, preview_timeout_sec=3, excel_exit_grace_sec=1)
        self.registry = TrackedProcessRegistry(os.path.join(tmp, "tracked.json"), quiet_logger())
        self.backend = FakeBackend(self.config, os.path.join(tmp, "work"), self.registry, quiet_logger())
        FakeBackend.extra = {}
        root = os.path.join(tmp, "root")
        make_tree(root, ["a/b/a_b_手順.docx"])
        self.word = os.path.join(root, "a", "b", "a_b_手順.docx")

    def tearDown(self):
        FakeBackend.extra = {}
        self.backend.shutdown("試験の後片付け")
        self._tmp.cleanup()

    def test_開けない理由はエラー番号の意味と失敗した所まで出す(self):
        FakeBackend.extra = {"fake_word_open_fail": "1"}
        with self.assertRaises(ExcelError) as ctx:
            self.backend.preview(self.word)
        self.assertEqual(ctx.exception.code, "OPEN_FAILED")
        detail = ctx.exception.detail
        for needle in ("パスワードが正しくありません", "0x800A1520", "Word 5408", "Word で文書を開く所"):
            self.assertIn(needle, detail)

    def test_印刷でも開けない理由を出す(self):
        FakeBackend.extra = {"fake_word_open_fail": "1"}
        outcomes = self.backend.print_files([self.word], 1, lambda *a: None, lambda: True)
        self.assertEqual(outcomes[0].code, "OPEN_FAILED")
        self.assertIn("Word 5408", outcomes[0].detail)

    def test_Wordを起動できなければ番号と意味も出す(self):
        FakeBackend.extra = {"fake_no_word": "1"}
        with self.assertRaises(ExcelError) as ctx:
            self.backend.preview(self.word)
        self.assertEqual(ctx.exception.code, "WORD_CREATE_FAILED")
        self.assertIn("0x800A01AD", ctx.exception.detail)
        self.assertIn("入っていない", ctx.exception.detail)

    def test_プレビューで止まったら止まった所を言う(self):
        FakeBackend.extra = {"fake_word_hang": "open"}
        with self.assertRaises(ExcelError) as ctx:
            self.backend.preview(self.word)
        self.assertEqual(ctx.exception.code, "TIMEOUT")
        self.assertIn("止まった所: Word で文書を開く所", ctx.exception.detail)

    def test_印刷で止まったら止まった所を言う(self):
        FakeBackend.extra = {"fake_word_hang": "print"}
        outcomes = self.backend.print_files([self.word], 1, lambda *a: None, lambda: True)
        self.assertEqual(outcomes[0].code, "TIMEOUT")
        self.assertIn("止まった所: Word で印刷する所", outcomes[0].detail)

    def test_止まったWordが出していた窓を読んでから止める(self):
        """隠れた確認の窓で止まった Word。文書を開く前に PID を聞いておき(WORDPID)、
        止める前にその窓の題名と文を読む。"""
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        FakeBackend.extra = {"fake_word_hang": "open", "fake_word_pid": str(proc.pid)}
        asked = []

        def fake_windows(pid, limit=8):
            asked.append(pid)
            return ["[#32770・隠れている] Microsoft Word : この文書はパスワードで保護されています"]

        real_identity = excel_service.get_process_identity

        def as_word(pid):
            # 代わりの子は python.exe。Windows では WINWORD.EXE でない PID は使わないので、
            # 名前だけ Word にする(止めるときの照合は本物の印で行う)
            found = real_identity(pid)
            if found is None or pid != proc.pid:
                return found
            return process_tracking.ProcessIdentity(found.pid, found.create_time,
                                                    r"C:\Program Files\Microsoft Office\WINWORD.EXE")

        with mock.patch.object(excel_service, "window_texts", fake_windows), \
                mock.patch.object(excel_service, "get_process_identity", as_word):
            with self.assertRaises(ExcelError) as ctx:
                self.backend.preview(self.word)
        self.assertEqual(ctx.exception.code, "TIMEOUT")
        self.assertIn(proc.pid, asked)
        self.assertIn("出ていた窓: Word [#32770・隠れている] Microsoft Word", ctx.exception.detail)
        self.assertIn("パスワードで保護", ctx.exception.detail)

    def test_エラー番号の意味(self):
        self.assertIn("Word 5408", explain_errno("0x800A1520"))
        self.assertTrue(explain_errno("0x800A1520").startswith("0x800A1520 = "))
        self.assertIn("入っていない", explain_errno("0x800A01AD"))
        self.assertEqual(explain_errno("0x800A03EC"), "0x800A03EC")     # 意味を知らない番号はそのまま
        self.assertEqual(explain_errno("abc?"), "abc?")
        self.assertEqual(explain_errno(""), "")

    def test_起動時の片付けはWordも止める(self):
        """記録の WINWORD.EXE を「想定外」として止めずに記録だけ消していた。"""
        self.assertIn("winword.exe", process_tracking.EXCEL_PROCESS_NAMES)

    def test_窓を読めなくても止まらない(self):
        self.assertIsInstance(process_tracking.window_texts(0), list)


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

    # ---- VER2.3.0 の後: Windows + Word で動かしたことが無い道を、書き方の段階で確かめる ----
    def body(self, header):
        """`Function X(` / `Sub X(` の本体(End まで)。"""
        start = self.text.index(header)
        kind = "Function" if header.startswith("Function") else "Sub"
        return self.text[start:self.text.index(f"End {kind}", start)]

    @staticmethod
    def args(call_text):
        return [a.strip() for a in call_text.split(",")]

    def test_Documents_Openの引数の位置(self):
        """Open(FileName, ConfirmConversions, ReadOnly, AddToRecentFiles, PasswordDocument,
        PasswordTemplate, Revert, WritePasswordDocument, WritePasswordTemplate, Format, Encoding, Visible)。
        Visible より後ろは Word の版で順番が違うので渡さない。"""
        calls = re.findall(r"gWd\.Documents\.Open\((.*)\)\r?$", self.text, re.M)
        self.assertEqual(len(calls), 2)
        for call in calls:
            a = self.args(call)
            self.assertEqual(len(a), 12, call)
            self.assertEqual(a[0], "path")
            self.assertEqual(a[1], "False", "ConfirmConversions: 変換の確認の窓を出さない")
            self.assertEqual(a[2], "True", "ReadOnly: 「使用中」の窓を出さない・書き換えない")
            self.assertEqual(a[3], "False", "AddToRecentFiles")
            self.assertEqual(a[9], "wdOpenFormatAuto")
            self.assertEqual(a[11], "False", "Visible")
        self.assertIn("Const wdOpenFormatAuto = 0", self.text)
        with_pwd = [self.args(c) for c in calls if self.args(c)[4]]
        self.assertEqual([(a[4], a[5]) for a in with_pwd], [("pwd", "pwd")])

    def test_PrintOutの部数は8番目(self):
        """PrintOut(Background, Append, Range, OutputFileName, From, To, Item, Copies)"""
        call = re.search(r"doc\.PrintOut (.*)\r?$", self.text, re.M).group(1)
        a = self.args(call)
        self.assertEqual(len(a), 8)
        self.assertEqual(a[0], "False", "Background = False: 刷り終えてから閉じる")
        self.assertEqual(a[7], "copies")

    def test_止まりうる命令の前に段階を知らせる(self):
        for header, stage, call in (
                ("Function OpenDoc(", "word.open", "gWd.Documents.Open("),
                ("Function PrintOneWord(", "word.print", "doc.PrintOut "),
                ("Function StartWord(", "word.create", 'CreateObject("Word.Application")'),
                ("Function EmitPageEmf(", "word.page", "EnhMetaFileBits"),
                ("Sub FinishWord(", "word.quit", "gWd.Quit"),
                ("Function OpenBook(", "excel.open", "gXl.Workbooks.Open("),
                ("Function DoPrintOut(", "excel.print", "ws.PrintOut")):
            body = self.body(header)
            self.assertIn(f'Stage "{stage}"', body, header)
            self.assertLess(body.index(f'Stage "{stage}"'), body.index(call), header)

    def test_段階の名前はPythonが知っている(self):
        for stage in re.findall(r'Stage "([a-z.]+)"', self.text):
            self.assertIn(stage, STAGE_LABELS)

    def test_失敗には失敗した所を添える(self):
        self.assertIn('WriteResult "item." & index & ".step", gStage', self.body("Sub SetItemError("))

    def test_WordのPIDは文書を開く前に知らせる(self):
        start = self.body("Function StartWord(")
        self.assertIn('Emit "WORDPID " & pid', start)
        self.assertLess(start.index('CreateObject("Word.Application")'), start.index('Emit "WORDPID "'))
        find = self.body("Function NewWordProcessId(")
        self.assertIn("WINWORD.EXE", find)
        self.assertIn('"embedding"', find)
        self.assertIn("If count = 1 Then", find, "候補が1つに決まらなければ知らせない(止めることがあるため)")

    def test_文書を開く前に確認の窓とマクロを止める(self):
        start = self.body("Function StartWord(")
        for needle in ("gWd.DisplayAlerts = wdAlertsNone", "gWd.AutomationSecurity = msoAutomationSecurityForceDisable",
                       "gWd.FeatureInstall = msoFeatureInstallNone"):
            self.assertIn(needle, start)
        # VER2.3.2: 1回の試しは PreviewWordOnce / TryPrintWord(Word が終わったら2回目がある)
        for header, use in (("Function PreviewWordOnce(", "OpenDoc(1, path)"),
                            ("Function TryPrintWord(", "PrintOneWord(index, path, copies)")):
            body = self.body(header)
            self.assertLess(body.index("StartWord()"), body.index(use), header)

    def test_宣言していない変数が無い(self):
        """Option Explicit の VBScript は、宣言していない変数を**その行を通ったときに**
        エラーにする(Excel では通らない Word の道だけが壊れる)。書き方の段階で見つける。"""
        self.assertEqual(_undeclared(self.text), [])


_VBS_WORDS = {w.lower() for w in """
    If Then Else ElseIf End Function Sub Dim Set Const For Each Next To Step Do Loop While Until Wend
    Exit And Or Not Is Nothing True False Empty Null Call Select Case New Option Explicit On Error Resume
    GoTo ByVal ByRef In Mod Xor With ReDim Preserve
    Err WScript CreateObject GetObject Len Mid Replace LCase UCase Trim Split UBound LBound CStr CLng
    CInt Hex AscW ChrW Right Left InStr vbCr vbLf TypeName IsObject Abs DateDiff Now""".split()}


def _undeclared(text):
    """手続きの中で、宣言(Dim・引数・Const・手続き名・全体の Dim)の無い名前を使っている行。"""
    lines = text.replace("\r", "").split("\n")
    known_global, procs, current = set(), [], None
    for raw in lines:
        code = re.sub(r'"(?:[^"]|"")*"', '""', raw).split("'")[0].strip()
        m = re.match(r"(?:Function|Sub)\s+(\w+)\s*(?:\(([^)]*)\))?", code)
        if m:
            known_global.add(m.group(1).lower())
            params = {p.strip().lower() for p in (m.group(2) or "").split(",") if p.strip()}
            current = {"name": m.group(1), "known": params | {m.group(1).lower()}, "lines": []}
            procs.append(current)
            continue
        if re.match(r"End (?:Function|Sub)\b", code):
            current = None
            continue
        m = re.match(r"(?:Dim|Const)\s+(.+)", code)
        if m:
            names = {part.split("=")[0].strip().lower() for part in m.group(1).split(",")}
            (current["known"] if current else known_global).update(names)
            continue
        if current is not None and code:
            current["lines"].append(code)
    found = []
    for proc in procs:
        known = proc["known"] | known_global | _VBS_WORDS
        for code in proc["lines"]:
            bare = re.sub(r"\.\w+", "", code)          # obj.Member の Member は名前ではない
            for name in re.findall(r"\b[A-Za-z]\w*\b", bare):
                if name.lower() not in known:
                    found.append(f"{proc['name']}: {name} ({code})")
    return found



# ======================================================================
# VER2.3.2
#   印刷範囲の取得に失敗しました。リモート サーバー マシンが存在しないか、利用できません。 0x1CE
#   (失敗した所: Word の1ページ目をコピーする所)
#   これ例えばどこかで開きっぱなしにしていた場合にもこのエラーになるのでは?
#   docのときはWordを読んでいますと出したほうがいい
#
#   もしエクセル、ワードが開いている場合は(他PCで)強制で閉じてもよいです。点検表原紙なので
#   編集する必要のないもので印刷オンリーです。開いたままになっているのがおかしいです
# ======================================================================
WORD_GONE_TEXT = "Word が途中で終わりました。この PC で Word を開いているなら閉じてから、もう一度押してください。"


def _vbs_text():
    with open(os.path.join(ROOT, "app", "vbscript", "excel_worker.vbs"), "rb") as fp:
        return fp.read().decode("ascii")


def _vbs_body(text, header):
    start = text.index(header)
    kind = "Function" if header.startswith("Function") else "Sub"
    return text[start:text.index(f"End {kind}", start)]


class _FakeWordCase(unittest.TestCase):
    files = ["a/b/a_b_手順.docx", "a/b/a_b_one.xlsx", "a/b/a_b_二.doc"]

    def setUp(self):
        self._tmp = temp_dir()
        tmp = self._tmp.name
        self.config = make_cfg(print_item_timeout_sec=3, preview_timeout_sec=3, excel_exit_grace_sec=1)
        self.registry = TrackedProcessRegistry(os.path.join(tmp, "tracked.json"), quiet_logger())
        self.backend = FakeBackend(self.config, os.path.join(tmp, "work"), self.registry, quiet_logger())
        FakeBackend.extra = {}
        self.root = os.path.join(tmp, "root")
        make_tree(self.root, self.files)
        self.paths = [os.path.join(self.root, *f.split("/")) for f in self.files]
        self.word, self.excel, self.word2 = self.paths
        self.ref = "1014-1236-" + os.urandom(2).hex().upper()
        token = event_log.current_ref.set(self.ref)
        self.addCleanup(event_log.current_ref.reset, token)
        event_log.set_hints(why_hints.hint)
        self.addCleanup(event_log.set_hints, lambda code, app="": ("", []))

    def tearDown(self):
        FakeBackend.extra = {}
        self.backend.shutdown("試験の後片付け")
        self._tmp.cleanup()

    def records(self, op):
        return [e for e in reversed(event_log.read(ref=self.ref, limit=200)) if e.get("op") == op]

    @staticmethod
    def clipboard_png():
        png = encode_rgb(2, 2, bytes([0, 128, 255] * 4))
        return png, mock.patch.object(clipboard_image, "read_image_png",
                                      lambda *a, **k: (png, {"format": "emf", "width": 2, "height": 2}))


class WordGoneTest(_FakeWordCase):
    """0x1CE(VBScript 462)= つかんでいた Word が途中で終わった。起こし直して1回だけやり直す。"""

    def test_1ページ目の絵でWordが落ちても_起こし直してクリップボード経由で出す(self):
        FakeBackend.extra = {"fake_word_gone": "page", "fake_pdf": "none"}
        png, patch = self.clipboard_png()
        with patch:
            image = self.backend.preview(self.word)
        self.assertEqual(image.data, png)
        self.assertEqual(image.method, "clipboard-emf")
        lost = self.records("word.lost")
        self.assertEqual(len(lost), 1)
        self.assertIn("1ページ目を絵にする所", lost[0]["stage"])

    def test_開く所でWordが終わっても_起こし直して1ページ目の絵を出す(self):
        FakeBackend.extra = {"fake_word_gone": "open", "fake_emf_hex": "0a0b0c", "fake_pdf": "none"}
        png = encode_rgb(2, 2, bytes([9, 9, 9] * 4))
        got = []

        def fake_emf(data, scale, max_pixels):
            got.append(data)
            return png, {"format": "emf", "width": 2, "height": 2}

        with mock.patch.object(clipboard_image, "emf_to_png", fake_emf):
            image = self.backend.preview(self.word)
        self.assertEqual(image.method, "word-page")
        self.assertEqual(got, [bytes.fromhex("0a0b0c")], "1回目の書きかけを混ぜない")

    def test_2回とも終わったら_利用者がすることの分かる文で断る(self):
        FakeBackend.extra = {"fake_word_gone": "page:twice", "fake_pdf": "none"}
        with self.assertRaises(ExcelError) as ctx:
            self.backend.preview(self.word)
        self.assertEqual(ctx.exception.code, "WORD_GONE")
        self.assertEqual(ctx.exception.message, WORD_GONE_TEXT)
        for needle in ("0x1CE", "VBScript 462", "途中で終わった", "失敗した所: Word の1ページ目を絵にする所"):
            self.assertIn(needle, ctx.exception.detail)

    def test_起こし直したWordを覚え直し_終わったWordの記録は消す(self):
        """1回目の Word(WORDPID)は WORDLOST で手放し、2回目の Word を覚える。最後は Word を終えて記録も空。"""
        first = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        second = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        for proc in (first, second):
            self.addCleanup(proc.wait)
            self.addCleanup(proc.kill)
        FakeBackend.extra = {"fake_word_gone": "page", "fake_word_pid": str(first.pid),
                             "fake_word_pid2": str(second.pid), "fake_pdf": "none"}
        adopted = []
        real_adopt = self.backend._adopt_word

        def spy(session, word):
            adopted.append(word.pid if word else None)
            real_adopt(session, word)

        real_identity = excel_service.get_process_identity

        def as_word(pid):
            # 代わりの子は python.exe。Windows では WINWORD.EXE でない PID は使わないので、
            # 名前だけ Word にする(止めるときの照合は本物の印で行う)
            found = real_identity(pid)
            if found is None or pid not in (first.pid, second.pid):
                return found
            return process_tracking.ProcessIdentity(found.pid, found.create_time,
                                                    r"C:\Program Files\Microsoft Office\WINWORD.EXE")

        _png, patch = self.clipboard_png()
        with patch, mock.patch.object(self.backend, "_adopt_word", spy), \
                mock.patch.object(excel_service, "get_process_identity", as_word):
            self.backend.preview(self.word)
        self.assertEqual([pid for pid in adopted if pid], [first.pid, second.pid])
        self.assertEqual([e for e in self.registry.entries() if e.get("label") == "word"], [])
        self.assertEqual(self.records("word.lost")[0]["word_pid"], first.pid)

    def test_印刷も開く所で終わったら1回だけやり直して刷る(self):
        FakeBackend.extra = {"fake_word_gone": "open"}
        events = []
        outcomes = self.backend.print_files([self.word, self.excel], 1, lambda *a: events.append(a), lambda: True)
        self.assertEqual([o.ok for o in outcomes], [True, True])
        self.assertEqual(outcomes[0].detail, "")
        self.assertEqual([e for e in events if e[0] == "end"], [("end", 1, True), ("end", 2, True)])

    def test_印刷も2回とも終わったら分かる文で断る(self):
        FakeBackend.extra = {"fake_word_gone": "open:twice"}
        outcomes = self.backend.print_files([self.word, self.excel], 1, lambda *a: None, lambda: True)
        self.assertEqual([o.ok for o in outcomes], [False, True])
        self.assertEqual(outcomes[0].code, "WORD_GONE")
        self.assertEqual(outcomes[0].message, WORD_GONE_TEXT)
        self.assertIn("0x1CE = VBScript 462", outcomes[0].detail)

    def test_印刷の途中で終わったら二重に刷らないためにやり直さない(self):
        FakeBackend.extra = {"fake_word_gone": "print"}
        outcomes = self.backend.print_files([self.word], 1, lambda *a: None, lambda: True)
        self.assertEqual(outcomes[0].code, "WORD_GONE")
        self.assertIn("プリンターに出ていないか確かめて", outcomes[0].message)
        self.assertEqual(outcomes[0].message, excel_service.WORD_GONE_PRINTING)
        self.assertEqual(len(self.records("word.lost")), 1, "起こし直していない")

    def test_記録の種類は本当の原因にする_途中で終わったとは言わない(self):
        """利用者の報告では、ログの「なぜ」が「Excel を操作する処理プログラム(VBScript)が途中で
        終わった(WORKER_FAILED)」になっていた(実際は FINISH 4 = 点検表1件の失敗)。"""
        FakeBackend.extra = {"fake_word_gone": "page:twice", "fake_pdf": "none"}
        with self.assertRaises(ExcelError):
            self.backend.preview(self.word)
        job = self.records("excel.job")[-1]
        self.assertEqual(job["code"], "WORD_GONE")
        self.assertEqual(job["app"], "Word")
        self.assertEqual(job["finish_code"], 4)
        self.assertNotIn("Excel", "".join(job["why"]))
        self.assertIn("つかんでいた Word が途中で終わった", job["why"][1])

    def test_処理プログラムが本当に途中で終わったときも_Wordの点検表ならWordと言う(self):
        FakeBackend.extra = {"fake_die_after": "1"}
        outcomes = self.backend.print_files([self.word], 1, lambda *a: None, lambda: True)
        self.assertTrue(outcomes[0].ok)      # 刷り終えてから落ちた
        job = self.records("excel.job")[-1]
        self.assertEqual(job["code"], "WORKER_FAILED")
        self.assertIn("Word を操作する処理プログラム(VBScript)が途中で終わった", job["why"][1])
        self.assertNotIn("Excel を操作する", "".join(job["why"]))

    def test_エラー番号_0x1CEの意味(self):
        for text in ("0x1CE", "0x800A01CE"):
            self.assertIn("VBScript 462", explain_errno(text))
            self.assertIn("途中で終わった", explain_errno(text))
        self.assertIn("命令の途中で落ちた", explain_errno("0x800706BE"))
        self.assertEqual(explain_errno("0x1"), "0x1")             # 意味を知らない番号はそのまま

    def test_Excelの点検表はこれまでどおりExcelの言葉(self):
        self.assertEqual(why_hints.hint("WORKER_FAILED", "Excel")[0],
                         "Excel を操作する処理プログラム(VBScript)が途中で終わった")
        self.assertEqual(why_hints.hint("WORKER_FAILED", "Word")[0],
                         "Word を操作する処理プログラム(VBScript)が途中で終わった")
        self.assertEqual(why_hints.hint("WORKER_FAILED")[0],
                         "Excel を操作する処理プログラム(VBScript)が途中で終わった")
        self.assertEqual(why_hints.hint("OPEN_FAILED", "Word")[0], "Word が点検表のファイルを開けなかった")
        self.assertEqual(excel_service.message_for("WORKER_FAILED", "word"),
                         "Word処理プログラム（VBScript）の実行に失敗しました。")
        self.assertEqual(excel_service.message_for("WORKER_FAILED", "excel"),
                         "Excel処理プログラム（VBScript）の実行に失敗しました。")


class UserWordTest(_FakeWordCase):
    """利用者の Word(この PC で開いている文書がある Word)を、こちらの後始末で止めない。"""

    def test_CreateObjectが利用者のWordを返しても_時間切れの後始末で止めない(self):
        user_word = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(user_word.wait)
        self.addCleanup(user_word.kill)
        # 利用者の Word の窓が届いても(fake_shared_window)、WORDSHARED の後は覚えない
        FakeBackend.extra = {"fake_user_word": "1", "fake_shared_window": "1", "fake_word_hwnd": "4242",
                             "fake_word_hang": "open"}
        with mock.patch.object(excel_service, "pid_from_hwnd",
                               lambda hwnd: user_word.pid if hwnd == 4242 else None):
            with self.assertRaises(ExcelError) as ctx:
                self.backend.preview(self.word)
        self.assertEqual(ctx.exception.code, "TIMEOUT")
        self.assertIsNone(user_word.poll(), "利用者の Word を止めてはいけない")
        self.assertNotIn(user_word.pid, [e.get("pid") for e in self.registry.entries()])
        self.assertTrue(self.records("word.shared"))

    def test_利用者のWordで刷っても_終えずに残す(self):
        FakeBackend.extra = {"fake_user_word": "1"}
        closed = []
        real = self.backend._word_closed
        with mock.patch.object(self.backend, "_word_closed",
                               lambda session, quit_: (closed.append(quit_), real(session, quit_=quit_))):
            outcomes = self.backend.print_files([self.word], 1, lambda *a: None, lambda: True)
        self.assertTrue(outcomes[0].ok)
        self.assertEqual(closed, [False], "WORDBYE 0: 利用者の Word は終えない")

    def test_処理プログラムは利用者のWordを隠さず_設定を戻し_終えない(self):
        text = _vbs_text()
        start = _vbs_body(text, "Function StartWord(")
        self.assertIn('Emit "WORDSHARED 1"', start)
        self.assertIn("If vis Or docs > 0 Then gWdShared = True", start)
        self.assertLess(start.index("If Not gWdShared Then"), start.index("gWd.Visible = False"))
        self.assertIn("gSavedAlerts = gWd.DisplayAlerts", start)
        finish = _vbs_body(text, "Sub FinishWord(")
        shared = finish[finish.index("If gWdShared Then"):finish.index("ElseIf openDocs = 0 Then")]
        self.assertIn('Emit "WORDBYE 0"', shared)
        self.assertIn("gWd.DisplayAlerts = gSavedAlerts", shared)
        self.assertIn("gWd.AutomationSecurity = gSavedSecurity", shared)
        self.assertNotIn(".Quit", shared)
        self.assertIn("gWdShared", _vbs_body(text, "Sub ReportWordWindow("), "利用者の Word の窓は知らせない")

    def test_やり直しは1回だけ(self):
        text = _vbs_text()
        preview = _vbs_body(text, "Function RunPreviewWord(")
        self.assertEqual(preview.count("PreviewWordOnce("), 2)
        # PDF にする所で落ちたら2回目は1ページ目の絵、絵で落ちたら2回目はクリップボード経由(VER2.4.0)
        self.assertIn('If lostAt = "word.pdf" Then\r\n                way = "page"', preview)
        self.assertIn('ElseIf lostAt = "word.page" Then\r\n                way = "copy"', preview)
        item = _vbs_body(text, "Function PrintWordItem(")
        self.assertEqual(item.count("TryPrintWord("), 2)
        self.assertIn('If lostAt = "word.print" Then', item, "印刷の途中で終わったら刷り直さない")
        gone = _vbs_body(text, "Function IsWordGoneError(")
        for number in ("462", "-2146827826", "-2147023174", "-2147023170", "-2147417848"):
            self.assertIn(number, gone)


class OpenElsewhereTest(_FakeWordCase):
    """この PC で開いたままのその点検表は閉じてから。ほかの PC のものは読み取り専用で開いて刷る。"""

    def test_閉じる指示が処理プログラムへ渡る(self):
        self.assertEqual(self.backend._base_params()["close_open_copy"], 1)
        off = FakeBackend(make_cfg(close_open_copy=False), os.path.join(self._tmp.name, "w2"),
                          self.registry, quiet_logger())
        self.assertEqual(off._base_params()["close_open_copy"], 0)

    def test_設定ファイルで止められる(self):
        path = os.path.join(self._tmp.name, "inspection.json")
        with open(path, "w", encoding="utf-8") as fp:
            json.dump({"excel": {"close_open_copy": False}}, fp)
        self.assertFalse(load_inspection_config(path).excel.close_open_copy)
        self.assertTrue(load_inspection_config(os.path.join(ROOT, "config", "inspection.json")).excel.close_open_copy)

    def test_この_PCで開いたままだったら閉じてからプレビューしたと知らせる(self):
        FakeBackend.extra = {"fake_open_here": "1"}
        with mock.patch.object(clipboard_image, "emf_to_png",
                               lambda *a: (b"png", {"format": "emf", "width": 1, "height": 1})):
            image = self.backend.preview(self.word)
        self.assertEqual(image.notice, "開いたままだった「a_b_手順」を閉じてからプレビューしました"
                                       "(この PC の Word。保存はしていません)")
        self.assertEqual(self.records("word.close_open")[0]["file"], self.word)

    def test_印刷では閉じたものにだけ知らせを付ける(self):
        FakeBackend.extra = {"fake_open_here": "1,2"}
        outcomes = self.backend.print_files([self.word, self.excel, self.word2], 1, lambda *a: None, lambda: True)
        self.assertEqual([o.ok for o in outcomes], [True, True, True])
        self.assertIn("開いたままだった「a_b_手順」を閉じてから印刷しました", outcomes[0].notice)
        self.assertIn("この PC の Excel", outcomes[1].notice)
        self.assertEqual(outcomes[2].notice, "")

    def test_閉じない設定なら閉じない(self):
        self.backend.excel_cfg.close_open_copy = False
        FakeBackend.extra = {"fake_open_here": "1"}
        outcomes = self.backend.print_files([self.word], 1, lambda *a: None, lambda: True)
        self.assertEqual(outcomes[0].notice, "")

    def write_owner(self, owner_file, name, utf16_at):
        """Word・Excel の所有者ファイルの形: 長さ1バイト + ANSI の名前 …(utf16_at)長さ + UTF-16 の名前。"""
        data = bytearray(b" " * 162)
        ansi = name.encode("cp932")
        data[0] = len(name)
        data[1:1 + len(ansi)] = ansi
        data[utf16_at] = len(name)
        data[utf16_at + 1] = 0
        data[utf16_at + 2:utf16_at + 2 + len(name) * 2] = name.encode("utf-16-le")
        path = os.path.join(os.path.dirname(self.word), owner_file)
        with open(path, "wb") as fp:
            fp.write(bytes(data))
        return path

    def test_所有者ファイルから開いている人の名前を読む(self):
        word_owner = self.write_owner("~$b_手順.docx", "山田 太郎", 54)       # Word: 先頭2文字を置き換え
        excel_owner = self.write_owner("~$a_b_one.xlsx", "suzuki", 55)       # Excel: 名前の前に足す
        self.assertEqual(excel_service.owner_of(self.word), "山田 太郎")
        self.assertEqual(excel_service.owner_of(self.excel), "suzuki")
        self.assertEqual(excel_service.owner_of(self.word2), "")
        self.assertTrue(os.path.exists(word_owner) and os.path.exists(excel_owner))

    def test_ほかの人が開いていても刷れて_名前を知らせ_所有者ファイルは消さない(self):
        owner = self.write_owner("~$b_手順.docx", "山田 太郎", 54)
        outcomes = self.backend.print_files([self.word], 1, lambda *a: None, lambda: True)
        self.assertTrue(outcomes[0].ok)
        self.assertEqual(outcomes[0].notice, "山田 太郎さんが「a_b_手順」を開いたままです(印刷はできます)")
        self.assertTrue(os.path.exists(owner), "~$ ファイルは消さない")
        self.assertEqual(self.records("file.in_use")[0]["owner"], "山田 太郎")

    def test_この_PCで閉じたなら_閉じたことだけを言う(self):
        self.write_owner("~$b_手順.docx", "山田 太郎", 54)            # この PC で開いていた分
        FakeBackend.extra = {"fake_open_here": "1"}
        outcomes = self.backend.print_files([self.word], 1, lambda *a: None, lambda: True)
        self.assertIn("閉じてから印刷しました", outcomes[0].notice)
        self.assertNotIn("山田", outcomes[0].notice)

    def test_処理プログラムは_そのファイルだけを保存せずに閉じる(self):
        text = _vbs_text()
        for header, close, app in (("Sub CloseOpenWordCopy(", "d.Close wdDoNotSaveChanges", '"Word.Application"'),
                                   ("Sub CloseOpenExcelCopy(", "wb.Close False", '"Excel.Application"')):
            body = _vbs_body(text, header)
            self.assertIn(f"GetObject(, {app})", body, header)
            self.assertEqual(body.count(".Close"), 1, header)
            self.assertLess(body.index("If same Then"), body.index(close), "同じファイルのときだけ閉じる")
            self.assertIn("same = SameFile(name, path)", body)
            self.assertNotIn("Quit", body, "アプリごと止めない")
            self.assertNotIn(".Save", body, "保存しない")
        self.assertIn("mine = (app.Hwnd = gXl.Hwnd)", _vbs_body(text, "Sub CloseOpenExcelCopy("),
                      "こちらの隠れた Excel は調べない")
        self.assertIn('If JobValue("close_open_copy", "0") <> "1" Then Exit Sub',
                      _vbs_body(text, "Sub CloseOpenCopy("))
        # 開く前に閉じる(Word を起動する前・Excel を起動する前)
        for header, before in (("Function RunPreviewWord(", "PreviewWordOnce("),
                               ("Function RunPreview(", "StartExcel()"),
                               ("Function RunPrint(", "StartExcel()")):
            body = _vbs_body(text, header)
            self.assertLess(body.index("CloseOpenCopy "), body.index(before), header)

    def test_ほかのPCで開かれていても_読み取り専用で確認の窓を出さずに開く(self):
        text = _vbs_text()
        for call in re.findall(r"gWd\.Documents\.Open\((.*)\)\r?$", text, re.M):
            args = [a.strip() for a in call.split(",")]
            self.assertEqual(args[2], "True", "ReadOnly")
            self.assertEqual(args[1], "False", "ConfirmConversions")
        calls = re.findall(r"gXl\.Workbooks\.Open\((.*)\)\r?$", text, re.M)
        self.assertEqual(len(calls), 2)
        for call in calls:
            args = [a.strip() for a in call.split(",")]
            self.assertEqual(args[2], "True", "ReadOnly")
            self.assertEqual(args[6], "True", "IgnoreReadOnlyRecommended")
            self.assertEqual(args[10], "False", "Notify: 使えるようになったら知らせる窓を出さない")
        self.assertIn("gWd.DisplayAlerts = wdAlertsNone", _vbs_body(text, "Function StartWord("))
        self.assertIn("gXl.DisplayAlerts = False", _vbs_body(text, "Function StartExcel("))


class WordWordingTest(unittest.TestCase):
    """Word の点検表のときは「Word を…」と出す(画面とサーバの両方)。"""

    def read(self, *parts):
        with open(os.path.join(ROOT, *parts), encoding="utf-8") as fp:
            return fp.read()

    def test_プレビューの読み込み中は点検表の種類で言い分ける(self):
        js = self.read("app", "static", "js", "preview.js")
        self.assertIn("${sheet.appOf(item)} で画像を作成しています", js)
        self.assertNotIn('"Excel で画像を作成しています"', js)
        self.assertIn("data.notice", js)
        inspection = self.read("app", "static", "js", "inspection.js")
        self.assertIn('const WORD_EXTS = new Set(["doc", "docx", "docm"]);', inspection)
        self.assertEqual(set(WORD_EXTENSIONS), {"doc", "docx", "docm"}, "画面とサーバで同じ拡張子")

    def test_印刷の待ちと知らせ(self):
        js = self.read("app", "static", "js", "printing.js")
        self.assertIn('${job.app || "Excel"} の空き待ち...', js)
        self.assertNotIn('"Excel の空き待ち..."', js)
        self.assertIn("item.notice", js)

    def test_種類の名前(self):
        self.assertEqual(excel_service.app_label(["word"]), "Word")
        self.assertEqual(excel_service.app_label(["word", "word"]), "Word")
        self.assertEqual(excel_service.app_label(["excel"]), "Excel")
        self.assertEqual(excel_service.app_label([]), "Excel")
        self.assertEqual(excel_service.app_label(["excel", "word"]), "Excel・Word")

    def test_印刷の進み具合もWordならWordと言う(self):
        from app.services.excel_service import ExcelService, ItemOutcome
        from app.services.print_service import PrintService
        tmp = temp_dir()
        self.addCleanup(tmp.cleanup)
        root = os.path.join(tmp.name, "root")
        make_tree(root, ["c/s/c_s_手順.docx"])
        path = os.path.join(root, "c", "s", "c_s_手順.docx")
        item = mock.Mock(id="1", category="c", subcategory="s", path=path)
        item.name = "手順"
        inspection = mock.Mock(get_item=lambda _id: item)
        excel = ExcelService(make_cfg(backend="dummy"), quiet_logger(),
                             TrackedProcessRegistry(os.path.join(tmp.name, "t.json")), os.path.join(tmp.name, "w"))
        seen = {}

        def fake_print(files, copies, on_event, should_continue):
            seen["message"] = service.status()["message"]
            on_event("begin", 1)
            on_event("end", 1, True)
            return [ItemOutcome(1, ok=True, processed=True, notice="山田さんが「手順」を開いたままです(印刷はできます)")]

        excel.print_files = fake_print
        service = PrintService(inspection, excel, lambda: "", quiet_logger())
        job = service.start(["1"], "1")
        self.assertEqual(job["app"], "Word")
        self.assertTrue(service.wait_idle(10))
        self.assertEqual(seen["message"], "Wordを起動しています...")
        self.assertEqual(service.status()["items"][0]["notice"], "山田さんが「手順」を開いたままです(印刷はできます)")

if __name__ == "__main__":
    unittest.main()
