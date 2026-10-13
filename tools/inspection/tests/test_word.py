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
from core import process_tracking
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

        with mock.patch.object(excel_service, "window_texts", fake_windows):
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
        for header, use in (("Function RunPreviewWord(", "OpenDoc(1, path)"),
                            ("Function RunPrint(", "PrintOneWord(i, path, copies)")):
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
    CInt Hex AscW ChrW Right Left InStr vbCr vbLf TypeName IsObject Abs""".split()}


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


if __name__ == "__main__":
    unittest.main()
