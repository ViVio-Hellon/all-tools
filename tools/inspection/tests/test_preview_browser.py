"""プレビューを**本物のブラウザ**で ── Word は PDF の枠、Excel は画像 (段階1)

前は ``preview.js`` / ``main.html`` の字(``data.format === "pdf"``・``focusId == null ? -1``・
``clearPdf();`` など)を探していた。ここでは見本の点検表(Word と Excel)を選んでプレビューを開き、

- 「プレビュー」ボタンで開いただけでは**何も作らない**(選んでから作る)。選んだ行の ◧ で開けば、それを出す
- 作っているあいだは**点検表の種類で**言い分ける(Word なのに「Excel で…」と出していた)
- Word は PDF の枠、Excel は画像。次の点検表へ移ったら前の PDF を出したままにしない。閉じたら枠を空にする
- ダウンロードにはしない
- サーバの知らせ(開いたままだったのを閉じた など)を出す
- PDF を出せない画面では、作る前に断り、「画像で表示する」で1ページ目の絵を頼める
"""
from __future__ import annotations

import json
import unittest

from app.services.excel_service import WORD_EXTENSIONS
from app.services.settings_service import DEFAULT_EXTENSIONS

from tests._page_browser import PageBrowserCase
from tests.test_web import AUTH

NOTICE = "山田さんが「作業手順の確認」を開いたままです(プレビューはできます)"


class PreviewBrowserTests(PageBrowserCase):
    def setUp(self) -> None:
        super().setUp()
        self.word = self.item("作業手順の確認")
        self.excel = self.item("消火器点検")
        self.downloads: list[str] = []

    def open_with(self, *items, pdf_viewer: bool = True):
        ctx = self.new_context()
        ids = json.dumps([it["id"] for it in items])
        ctx.add_init_script(f"sessionStorage.setItem('isp.selection', {json.dumps(ids)});")
        # ヘッドレスの Chromium は PDF の表示を持たない。ふつうの Edge・WebView2 のように「出せる」にする
        ctx.add_init_script("Object.defineProperty(navigator, 'pdfViewerEnabled', "
                            f"{{ get: () => {str(pdf_viewer).lower()} }});")
        # PDF そのものは小さなページで返す。ヘッドレスの Chromium(とくに Windows の CI)は、枠に PDF を読むと
        # 表示できずにダウンロードにする ── アプリの作りではなく試験のブラウザの事情なので、ここでは
        # 「枠の src に PDF の道を入れる・ダウンロードの道(a[download])を作らない」までを見る
        ctx.route("**/api/preview/pdf/**", lambda route: route.fulfill(
            status=200, body="<p>PDF</p>", headers={"content-type": "text/html; charset=utf-8"}))
        page = self.open("/", ctx)
        page.on("download", lambda d: self.downloads.append(d.suggested_filename))
        page.wait_for_selector("#sel-list li")
        return page

    def previews(self) -> list[dict]:
        return self.posts_to("/api/preview")

    @staticmethod
    def message(page) -> str:
        return page.inner_text("#pv-message")

    def test_開いただけでは作らず_選んだ点検表を種類に合わせて出し_移ると閉じると枠を空にする(self) -> None:
        original = self.biz.excel.backend.preview

        def with_notice(path, pdf=True):
            image = original(path, pdf=pdf)
            if path.endswith(".docx"):
                image.notice = NOTICE
            return image

        self.biz.excel.backend.preview = with_notice
        page = self.open_with(self.word, self.excel)

        # 「プレビュー」ボタン: 何も作らない
        page.click("#btn-preview")
        page.wait_for_selector("#preview-dialog[open]")
        page.wait_for_timeout(400)
        self.assertEqual(self.previews(), [])
        self.assertEqual(page.inner_text("#pv-status"), "点検表を選んでください")
        self.assertTrue(page.is_hidden("#pv-pdf"))
        self.assertTrue(page.is_hidden("#pv-image"))

        # Word を選ぶ: 作っているあいだは「Word で PDF を…」。できたら PDF の枠
        self.hold = ("/api/preview",)
        page.click("#pv-list li:nth-child(1)")
        self.wait_held(page)
        # 要求が止まった直後は、まだ文言を塗っていないことがある(重い CI)。出るまで待つ
        page.wait_for_function("document.getElementById('pv-message').innerText.includes('Word で PDF を作成しています')")
        self.hold = ()
        self.release(page)
        page.wait_for_selector("#pv-pdf:not([hidden])")
        src = page.get_attribute("#pv-pdf", "src")
        self.assertIn("/api/preview/pdf/", src)
        self.assertIn("t=", src, "枠の src にトークンが無い")
        self.assertTrue(page.is_hidden("#pv-image"))
        page.wait_for_selector(f".toast:has-text('{NOTICE}')")
        self.assertEqual(self.previews()[-1], {"id": self.word["id"], "force": False, "image": False})

        # Excel へ移る: 「Excel で画像を…」。前の PDF は出したままにしない
        self.hold = ("/api/preview",)
        page.click("#pv-list li:nth-child(2)")
        self.wait_held(page)
        # 要求が止まった直後は、まだ文言を塗っていないことがある(重い CI)。出るまで待つ
        page.wait_for_function("document.getElementById('pv-message').innerText.includes('Excel で画像を作成しています')")
        page.wait_for_selector("#pv-pdf", state="hidden")
        self.assertIsNone(page.get_attribute("#pv-pdf", "src"))
        self.hold = ()
        self.release(page)
        page.wait_for_selector("#pv-image:not([hidden])")
        self.assertTrue(page.is_hidden("#pv-pdf"))

        # もう一度 Word を出してから閉じる: 枠は空になる
        page.click("#pv-list li:nth-child(1)")
        page.wait_for_selector("#pv-pdf:not([hidden])")
        page.click("#pv-close")
        page.wait_for_selector("#preview-dialog:not([open])", state="attached")
        # `open` の印は close() ですぐ消えるが、後始末の `close` の合図はあとから届く。済むまで待つ
        page.wait_for_function("!document.getElementById('pv-pdf').hasAttribute('src')", timeout=3000)
        self.assertTrue(page.is_hidden("#pv-pdf"))

        self.assertEqual(self.downloads, [], "PDF をダウンロードさせた")
        self.assertEqual(page.locator("a[download]").count(), 0)
        self.assertEqual(self.errors, [])

    def test_作り直すと_作り直した画像を出す(self) -> None:
        """**作り直す → 出す → 新しい絵。** 「作り直す」で前の絵のまま出さない。

        画像の名前(キー)は点検表の更新日時・大きさで決まるので、中身を変えずに作り直すと
        同じ名前になる。画像はブラウザに1日控えさせているので、以前は作り直しても
        ブラウザが**前の絵**を出していた(PDF は作った時刻を付けて避けていた)。
        """
        original = self.biz.excel.backend.preview
        made: list[str] = []

        def numbered(path, pdf=True):
            image = original(path, pdf=pdf)
            made.append(path)
            image.data = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{100 * len(made)}" '
                          'height="50"><rect width="100%" height="100%" fill="#ccc"/></svg>').encode("utf-8")
            return image

        self.biz.excel.backend.preview = numbered
        page = self.open_with(self.excel)
        page.click("#sel-list li button[aria-label='プレビュー']")
        page.wait_for_function("(() => { const i = document.getElementById('pv-image');"
                               " return !i.hidden && i.naturalWidth === 100; })()")
        before = page.get_attribute("#pv-image", "src")
        page.wait_for_timeout(300)   # 作った時刻が変わるように(秒より細かいが、念のため)
        page.click("#pv-regenerate")
        page.wait_for_function("(() => { const i = document.getElementById('pv-image');"
                               " return !i.hidden && i.complete; })()")
        page.wait_for_function("document.getElementById('pv-status').innerText.startsWith('表示中')")
        self.assertEqual(len(made), 2, "作り直していない")
        self.assertEqual(self.previews()[-1]["force"], True)
        page.wait_for_function("document.getElementById('pv-image').naturalWidth !== 0")
        self.assertEqual(page.evaluate("document.getElementById('pv-image').naturalWidth"), 200,
                         "作り直したのに前の絵を出した")
        # この試験のブラウザは要求をすべて横取りするので、ブラウザの控えが効かない。本物の
        # 画面では、同じ道の画像は控え(1日)から出る ── **作り直したら道を変える**ことを見る
        after = page.get_attribute("#pv-image", "src")
        self.assertNotEqual(after, before, "作り直しても画像の道が同じ(ブラウザの控えの前の絵が出る)")
        res = self.client.get(after.split("#")[0], headers=AUTH)
        self.assertEqual(res.status_code, 200)
        self.assertIn("max-age", res.headers.get("Cache-Control", ""), "前提: 画像は控えさせている")
        self.assertEqual(self.errors, [])

    def test_PDFを出せない画面では作る前に断り_画像で表示するで1ページ目の絵を頼む(self) -> None:
        page = self.open_with(self.word, pdf_viewer=False)
        # 選んだ行の ◧ で開く: その点検表を出す(選んで押したのと同じ)
        page.click("#sel-list li button[aria-label='プレビュー']")
        page.wait_for_selector("#preview-dialog[open]")
        page.wait_for_selector("#pv-message:has-text('この画面では PDF を表示できません。')")
        page.wait_for_timeout(300)
        self.assertEqual(self.previews(), [], "断る前に作り始めた")
        self.assertTrue(page.is_hidden("#pv-pdf"))

        # 画像にするのも Word。作っているあいだも「Word で…」と言う
        self.hold = ("/api/preview",)
        page.click("#pv-message button:has-text('画像で表示する')")
        self.wait_held(page)
        self.assertIn("Word で画像を作成しています", self.message(page))
        self.hold = ()
        self.release(page)
        page.wait_for_selector("#pv-image:not([hidden])")
        self.assertEqual(self.previews(), [{"id": self.word["id"], "force": False, "image": True}])
        self.assertTrue(page.is_hidden("#pv-pdf"))
        self.assertEqual(self.errors, [])

    def test_画面の点検表の種類の決め方はサーバと同じ拡張子(self) -> None:
        """画面は拡張子で Word か Excel かを決めて言い分ける。サーバ(`kind_of`)と食い違わないこと。"""
        page = self.open("/")
        got = page.evaluate("""async (exts) => {
            const app = document.querySelector('script[type=module][src*="app.js"]').src;
            const sheet = await import(new URL('./inspection.js', app).pathname);
            return Object.fromEntries(exts.map((e) => [e, sheet.appOf({ ext: e.toUpperCase() })]));
        }""", sorted(DEFAULT_EXTENSIONS))
        self.assertEqual({e for e, app in got.items() if app == "Word"}, set(WORD_EXTENSIONS))
        self.assertTrue({e for e, app in got.items() if app == "Excel"})
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
