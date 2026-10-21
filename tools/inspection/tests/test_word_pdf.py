"""Word の点検表のプレビューは PDF (VER2.4.0)

    プレビュー: Word が途中で終わりました。この PC で Word を開いているなら閉じてから、もう一度押してください。
    リモート サーバー マシンが存在しないか、利用できません。 0x1CE = VBScript 462: … (失敗した所: Word の1ページ目をコピーする所)
    そろそろ直してください

隠れた Word で1ページ目を絵にする(EnhMetaFileBits)・クリップボードへ写すと、Word ごと落ちる文書があった
(VER2.3.2 の「起こし直して1回やり直す」でも 1回目は word.page、2回目は word.copy で落ちた)。

【約束】
    ・Word のプレビューは Document.ExportAsFixedFormat の PDF(全ページ。長い文書は上限まで)
    ・PDF は画面の中に出す(Content-Disposition: inline。ダウンロードにしない)
    ・PDF は処理プログラムが自分の TEMP に書き、送ったらすぐ消す。残りは処理プログラムが起きたときに片付ける
    ・PDF にできない(古い Word・PDF にする所で落ちた・PDF の形でない)ときは、これまでの
      1ページ目の絵 → クリップボード経由。PDF にする所で落ちたら、起こし直した2回目は1ページ目の絵から
    ・Excel の点検表のプレビューはこれまでどおり画像。印刷は PrintOut のまま
"""
import os
import re
import time
import unittest
from unittest import mock

from tests.test_excel_protocol import FakeBackend
from tests.test_web import HAS_WEB, WebTestBase
from tests.test_word import _FakeWordCase, _vbs_body, _vbs_text, WORD_GONE_TEXT

from app.services import clipboard_image
from app.services.excel_service import PDF_MIME, STAGE_LABELS, ExcelError, looks_like_pdf
from app.services.png_encoder import encode_rgb
from app.services.settings_service import load_inspection_config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PDF = b"%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"


def fake_emf(png):
    return mock.patch.object(clipboard_image, "emf_to_png",
                             lambda data, scale, max_pixels: (png, {"format": "emf", "width": 2, "height": 2}))


class WordPdfTest(_FakeWordCase):
    def setUp(self):
        super().setUp()
        # 偽の処理プログラムの TEMP(本物は %TEMP%\isp_preview)
        self.temp = os.path.join(self._tmp.name, "isp_preview")
        os.makedirs(self.temp)
        patcher = mock.patch.dict(os.environ, {"ISP_FAKE_TEMP": self.temp})
        patcher.start()
        self.addCleanup(patcher.stop)

    def left(self):
        return sorted(os.listdir(self.temp))

    def test_WordのプレビューはPDFで返る(self):
        FakeBackend.extra = {"fake_pdf_hex": PDF.hex(), "fake_pages": "3"}
        image = self.backend.preview(self.word)
        self.assertEqual(image.mime, PDF_MIME)
        self.assertEqual(image.data, PDF)
        self.assertEqual(image.method, "word-pdf")
        self.assertEqual((image.pages, image.pdf_pages), (3, 3))
        self.assertEqual(image.range_address, "全 3 ページ")
        self.assertEqual(image.range_source, "word_pdf")

    def test_PDFの一時ファイルは送ったらすぐ消す(self):
        FakeBackend.extra = {"fake_pdf_hex": PDF.hex()}
        self.backend.preview(self.word)
        self.backend.preview(self.word)
        self.assertEqual(self.left(), [])

    def test_残った一時ファイルは処理プログラムが起きたときに片付ける_新しいものは残す(self):
        old = os.path.join(self.temp, "isp_pv_rad1.pdf")
        young = os.path.join(self.temp, "isp_pv_rad2.pdf")
        other = os.path.join(self.temp, "ほかのもの.pdf")
        for path in (old, young, other):
            with open(path, "wb") as fp:
                fp.write(PDF)
        two_hours_ago = time.time() - 7200
        os.utime(old, (two_hours_ago, two_hours_ago))
        os.utime(other, (two_hours_ago, two_hours_ago))
        FakeBackend.extra = {"fake_pdf_hex": PDF.hex()}
        self.backend.preview(self.word)          # ここで処理プログラムが起きる
        self.assertEqual(self.left(), sorted(["isp_pv_rad2.pdf", "ほかのもの.pdf"]),
                         "1時間より古い isp_ のものだけ消す(動いているほかの処理プログラムのものは残す)")

    def test_長い文書は上限のページまで(self):
        self.backend.excel_cfg.word_preview_max_pages = 20
        FakeBackend.extra = {"fake_pages": "25"}
        image = self.backend.preview(self.word)
        self.assertEqual((image.pages, image.pdf_pages), (25, 20))
        self.assertEqual(image.range_address, "1～20 ページ(全 25 ページ)")

    def test_PDFの形でなければ1ページ目の絵にする(self):
        png = encode_rgb(2, 2, bytes([1, 2, 3] * 4))
        FakeBackend.extra = {"fake_pdf": "bad"}
        with fake_emf(png):
            image = self.backend.preview(self.word)
        self.assertEqual(image.mime, "image/png")
        self.assertEqual(image.method, "word-page")

    def test_PDFにする所でWordが落ちたら_起こし直して1ページ目の絵で出す(self):
        png = encode_rgb(2, 2, bytes([4, 5, 6] * 4))
        FakeBackend.extra = {"fake_word_gone": "pdf"}
        with fake_emf(png):
            image = self.backend.preview(self.word)
        self.assertEqual(image.method, "word-page")
        lost = self.records("word.lost")
        self.assertEqual(len(lost), 1)
        self.assertIn("PDF にする所", lost[0]["stage"])

    def test_PDFにできず_1ページ目の絵で2回とも落ちたら分かる文で断る(self):
        FakeBackend.extra = {"fake_word_gone": "page:twice", "fake_pdf": "none"}
        with self.assertRaises(ExcelError) as ctx:
            self.backend.preview(self.word)
        self.assertEqual(ctx.exception.code, "WORD_GONE")
        self.assertEqual(ctx.exception.message, WORD_GONE_TEXT)

    def test_画像でと頼まれたら_これまでの1ページ目の絵(self):
        png = encode_rgb(2, 2, bytes([7, 8, 9] * 4))
        with fake_emf(png):
            image = self.backend.preview(self.word, pdf=False)
        self.assertEqual(image.method, "word-page")

    def test_設定でPDFを止められる(self):
        self.backend.excel_cfg.word_preview_pdf = False
        png = encode_rgb(2, 2, bytes([7, 8, 9] * 4))
        with fake_emf(png):
            self.assertEqual(self.backend.preview(self.word).method, "word-page")

    def test_Excelはこれまでどおり画像(self):
        png, patch = self.clipboard_png()
        with patch:
            image = self.backend.preview(self.excel)
        self.assertEqual(image.mime, "image/png")
        self.assertEqual(image.method, "clipboard-emf")

    def test_PDFの形(self):
        self.assertTrue(looks_like_pdf(PDF))
        self.assertFalse(looks_like_pdf(b"%PDF-1.7 half written"), "終わりの %%EOF が無い")
        self.assertFalse(looks_like_pdf(b"not a pdf %%EOF"))

    def test_設定ファイルの既定(self):
        cfg = load_inspection_config(os.path.join(ROOT, "config", "inspection.json"))
        self.assertTrue(cfg.excel.word_preview_pdf)
        self.assertEqual(cfg.excel.word_preview_max_pages, 20)


@unittest.skipUnless(HAS_WEB, "Flask が入っていないためスキップ")
class PdfRouteTest(WebTestBase):
    """模擬モードの Word の点検表(作業手順の確認.docx)は小さな PDF を返す。"""

    def item(self, ext):
        inv = self.get("/api/inventory").get_json()["inventory"]
        return next(it for c in inv["categories"] for s in c["subcategories"] for it in s["items"]
                    if it["ext"] == ext)

    def test_WordはPDFで返り_画面の中に出す(self):
        body = self.post("/api/preview", {"id": self.item("docx")["id"]}).get_json()
        self.assertEqual(body["format"], "pdf")
        self.assertEqual(body["image_url"], "")
        self.assertTrue(body["pdf_url"].startswith("/api/preview/pdf/"))
        res = self.get(body["pdf_url"])
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.mimetype, "application/pdf")
        self.assertTrue(res.data.startswith(b"%PDF-"))
        disposition = res.headers["Content-Disposition"]
        self.assertTrue(disposition.startswith("inline;"), disposition)
        self.assertNotIn("attachment", disposition, "ダウンロードにしない")
        self.assertIn("filename*=UTF-8''", disposition)
        self.assertIn("frame-ancestors 'self'", res.headers["Content-Security-Policy"],
                      "点検表の画面(同じ宛先)の枠に出せる")
        self.assertIn("private", res.headers["Cache-Control"])
        again = self.post("/api/preview", {"id": self.item("docx")["id"]}).get_json()
        self.assertTrue(again["cached"], "2回目からは控え")
        self.assertEqual(again["format"], "pdf")

    def test_PDFの宛先はトークンが要る(self):
        body = self.post("/api/preview", {"id": self.item("docx")["id"]}).get_json()
        self.assertEqual(self.client.get(body["pdf_url"]).status_code, 401)
        self.assertEqual(self.client.get(body["pdf_url"] + "?t=test-token").status_code, 200)

    def test_画像でと頼めば画像_Excelはこれまでどおり画像(self):
        image = self.post("/api/preview", {"id": self.item("docx")["id"], "image": True}).get_json()
        self.assertEqual(image["format"], "image")
        self.assertEqual(self.get(image["image_url"]).mimetype, "image/svg+xml")
        self.assertEqual(self.get(f"/api/preview/pdf/{image['key']}").status_code, 404, "画像を PDF として返さない")
        excel = self.post("/api/preview", {"id": self.item("xlsx")["id"]}).get_json()
        self.assertEqual(excel["format"], "image")
        self.assertEqual(excel["pdf_url"], "")


class WorkerSourceTest(unittest.TestCase):
    """処理プログラム(VBScript)の PDF の道。本物の Word で動かせないので、書き方の段階で確かめる。"""

    def setUp(self):
        self.text = _vbs_text()

    def test_ExportAsFixedFormatの引数の位置(self):
        """ExportAsFixedFormat(OutputFileName, ExportFormat, OpenAfterExport, OptimizeFor, Range, From, To,
        Item, IncludeDocProps, KeepIRM, CreateBookmarks, DocStructureTags, BitmapMissingFonts, UseISO19005_1)"""
        calls = re.findall(r"doc\.ExportAsFixedFormat (.*)\r?$", self.text, re.M)
        self.assertEqual(len(calls), 2)
        ranges = []
        for call in calls:
            a = [x.strip() for x in call.split(",")]
            self.assertEqual(len(a), 14, call)
            self.assertEqual(a[0], "outPath")
            self.assertEqual(a[1], "wdExportFormatPDF")
            self.assertEqual(a[2], "False", "OpenAfterExport: PDF を読むアプリを開かない")
            self.assertEqual(a[3], "wdExportOptimizeForOnScreen", "画面用で十分(小さく・速い)")
            self.assertEqual(a[7], "wdExportDocumentContent")
            ranges.append((a[4], a[5], a[6]))
        self.assertIn(("wdExportFromTo", "1", "lastPage"), ranges)
        self.assertIn(("wdExportAllDocument", "1", "1"), ranges)
        for const, value in (("wdExportFormatPDF", 17), ("wdExportOptimizeForOnScreen", 1),
                             ("wdExportAllDocument", 0), ("wdExportFromTo", 3), ("wdExportDocumentContent", 0)):
            self.assertIn(f"Const {const} = {value}\r\n", self.text)

    def test_PDFにする前に段階を知らせ_落ちたら覚える(self):
        body = _vbs_body(self.text, "Function EmitDocPdf(")
        self.assertLess(body.index('Stage "word.pdf"'), body.index("doc.ExportAsFixedFormat"))
        self.assertIn('If WordGoneNow() Then gWordLostStage = "word.pdf"', body)
        self.assertIn("word.pdf", STAGE_LABELS)

    def test_一時ファイルは送ったら消し_起きたときに片付ける(self):
        body = _vbs_body(self.text, "Function EmitDocPdf(")
        self.assertLess(body.index('EmitFileBase64(outPath, "PDFDATA")'),
                        body.index("DeleteTempFile outPath\r\n    If Not sent"))
        self.assertEqual(body.count("DeleteTempFile outPath"), 2, "失敗したときも消す")
        self.assertIn('"isp_preview"', _vbs_body(self.text, "Function TempFolder("))
        self.assertIn('"isp_pv_"', _vbs_body(self.text, "Function NewTempPath("))
        main = _vbs_body(self.text, "Function Main(")
        self.assertLess(main.index("SweepTempFiles"), main.index("Do\r\n"))
        sweep = _vbs_body(self.text, "Sub SweepTempFiles(")
        self.assertIn('DateDiff("n", f.DateLastModified, Now) > 60', sweep)

    def test_文書は読み取り専用で開いてからPDFにする(self):
        once = _vbs_body(self.text, "Function PreviewWordOnce(")
        self.assertLess(once.index("StartWord()"), once.index("OpenDoc(1, path)"))
        self.assertLess(once.index("OpenDoc(1, path)"), once.index("EmitDocPdf(doc)"))
        self.assertLess(once.index("EmitDocPdf(doc)"), once.index("EmitPageEmf(doc)"), "PDF → 1ページ目の絵 → コピー")
        self.assertLess(once.index("EmitPageEmf(doc)"), once.index("CopyWordRange(rng)"))

    def test_印刷はPrintOutのまま(self):
        self.assertNotIn("ExportAsFixedFormat", _vbs_body(self.text, "Function PrintOneWord("))
        self.assertIn("doc.PrintOut False, , , , , , , copies", self.text)


# 画面(Word は PDF の枠・Excel は画像・ダウンロードにしない・PDF を出せない画面では作る前に断る・
# 閉じたら枠を空にする)と、プレビューの画面に移っただけでは作らない(選んでから作る)は、見本の点検表で
# プレビューを開いて test_preview_browser.PreviewBrowserTests が見る


if __name__ == "__main__":
    unittest.main()
