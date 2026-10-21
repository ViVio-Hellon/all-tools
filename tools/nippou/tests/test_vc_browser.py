"""VC長さ計算: 画面の配線を**本物のブラウザ**で確かめる (段階1)

前は `views/vc.js` やコイル・平板の CSS/JS の字を探して「仕組みが入っていること」だけを
見ていました(`test_web_vc`・`test_vc_quick_window` の一部)。ここでは画面を開いて押します:

    - 早見表を別の窓で開くと、**早見表だけの窓**(`/vc?window=quick&tab=quick`)が開く
    - 早見表の面の「設定」の案内で、読み直さずに設定の面へ移る(品種を選ぶ `select` と取り違えない)
    - 設定の面: 選んだ枠の欄の例(空ならこのまま)・「空のマスにも入れる」を外せば外して送る・
      足す/行を消す/切り替える/枠を消す/品種を消す がそれぞれの口へ届き、消す前に何が消えるかを数で訊く
    - 置き場所の欄は行の残りいっぱいに伸び、狭い窓でははみ出さない
    - 早見表の印刷: 刷るあいだだけ紙を組み、刷り終えたら・画面を移ったら片付ける
    - コイル・平板: 1920 の幅では高さに合わせて詰める/縮める(ページは動かない)・枠は作業面の残りちょうど・
      背景のライト/ダーク(既定はライト・覚える・印刷は白い紙)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._other_screen_browser import ORIGIN, ScreenBrowserCase  # noqa: E402

COIL = "/static/vc/coil/index.html"


class VcBrowserCase(ScreenBrowserCase):
    def setUp(self) -> None:
        super().setUp()
        (self.tmp / "ref").mkdir(parents=True, exist_ok=True)

    def open_vc(self, tab: str = ""):
        page = self.open_screen(f"/vc?tab={tab}" if tab else "/vc", "#vcTabs")
        return page

    def panel_shown(self, page, key: str) -> bool:
        return page.evaluate(f"!document.getElementById('panel-{key}').hidden")


class QuickTests(VcBrowserCase):
    def test_早見表を別の窓で開くと早見表だけの窓が開く(self) -> None:
        page = self.open_vc()
        with page.expect_popup() as info:
            page.click("#vc-open-quick")
        popup = info.value
        popup.wait_for_load_state("load")
        url = popup.url.split(ORIGIN, 1)[1]
        self.assertEqual(url, "/vc?window=quick&tab=quick")
        # 開いた窓には、ほかの画面へ移る道(レール)が無い
        popup.wait_for_selector("#panel-quick")
        self.assertIsNone(popup.query_selector("nav.rail"))
        self.assertEqual(self.errors, [])

    def test_早見表の案内から読み直さずに設定の面へ移る(self) -> None:
        page = self.open_vc("quick")
        page.wait_for_function("!document.getElementById('panel-quick').hidden")
        page.evaluate("window.__stay = 1")            # 読み直すと消える印
        page.click('#panel-quick a[data-vc-tab="settings"]')
        page.wait_for_function("!document.getElementById('panel-settings').hidden", timeout=5000)
        self.assertEqual(page.evaluate("window.__stay"), 1, "ページを読み直しています")
        self.assertTrue(page.evaluate(
            "document.getElementById('tab-settings').getAttribute('aria-selected') === 'true'"))
        self.assertFalse(self.panel_shown(page, "quick"))
        self.assertEqual(self.errors, [])

    def test_早見表を刷るあいだだけ紙を組み_刷り終えたら_移ったら片付ける(self) -> None:
        page = self.open_vc("quick")
        page.wait_for_selector("#vc-quick-blocks .vc-qblock")
        page.evaluate("() => { window.print = () => { window.__printed = (window.__printed || 0) + 1; }; }")
        # 枠の出どころ: 固定値の枠は、なぜそうなのかまで言う
        sources = page.locator("#vc-quick-blocks .vc-qblock__src").all_text_contents()
        self.assertIn("固定値(打った長さ)", sources)
        page.click("#vc-print")
        self.assertEqual(page.evaluate("window.__printed"), 1)
        self.assertTrue(page.evaluate("document.body.classList.contains('vc-printing')"))
        self.assertGreater(page.locator("#vc-qprint .vc-qpage").count(), 0)
        page.evaluate("window.dispatchEvent(new Event('afterprint'))")
        self.assertFalse(page.evaluate("document.body.classList.contains('vc-printing')"))
        self.assertIsNone(page.query_selector("#vc-qprint"))
        # 刷り終えの合図が来ないまま画面を移っても、ほかの画面の印刷を隠したままにしない
        page.click("#vc-print")
        self.assertTrue(page.evaluate("document.body.classList.contains('vc-printing')"))
        page.click('nav.rail a[href="/gw"]')
        page.wait_for_function("location.pathname === '/gw'", timeout=8000)
        page.wait_for_selector("#dim-count")
        self.assertFalse(page.evaluate("document.body.classList.contains('vc-printing')"))
        self.assertIsNone(page.query_selector("#vc-qprint"))
        page.emulate_media(media="print")
        self.assertNotEqual(page.evaluate("getComputedStyle(document.querySelector('main#main')).display"),
                            "none", "梱包資材の画面が紙に出ません")
        self.assertEqual(self.errors, [])


class SettingsPanelTests(VcBrowserCase):
    def setUp(self) -> None:
        super().setUp()
        self.get("/api/vc/state")                     # 初めて使う → マスタを作る
        self.unlock()                                 # マスタ管理と同じ鍵

    def wait_reply(self, page, mark: int, path: str) -> dict:
        self.wait_until(page, lambda: any(p == path for p, _ in self.sent[mark:]), f"{path} の要求")
        page.wait_for_function(
            "!document.getElementById('vc-add-make').dataset.sendLocked", timeout=8000)
        page.wait_for_timeout(200)
        return next(b for p, b in self.sent[mark:] if p == path)

    def test_選んだ枠の欄の例と_空のマスにも入れるを外して送る(self) -> None:
        page = self.open_vc("settings")
        page.wait_for_selector("#vc-grid-block option", state="attached")
        page.select_option("#vc-grid-block", "R575B")            # 固定値の枠
        page.wait_for_selector("#vc-grid-length-fixed:not([hidden])")
        self.assertIn("(空ならこのまま)", page.get_attribute("#vc-grid-insides", "placeholder"))
        self.assertIn("(空ならこのまま)", page.get_attribute("#vc-grid-thicknesses", "placeholder"))
        page.select_option("#vc-grid-length", "value")
        page.fill("#vc-grid-vcatu", "0.06")
        page.fill("#vc-grid-insides", "88")
        page.fill("#vc-grid-thicknesses", "6")
        page.uncheck("#vc-grid-fill")
        mark = len(self.sent)
        page.click("#vc-grid-make")
        body = self.wait_reply(page, mark, "/api/vc/quick-grid")
        self.assertIs(body["fill_empty"], False, "外したのに「空のマスにも入れる」で送っています")
        self.assertEqual((body["block"], body["length"], body["vcatu"]), ("R575B", "value", "0.06"))
        page.check("#vc-grid-fill")
        page.fill("#vc-grid-insides", "88")
        page.fill("#vc-grid-thicknesses", "7")
        mark = len(self.sent)
        page.click("#vc-grid-make")
        self.assertIs(self.wait_reply(page, mark, "/api/vc/quick-grid")["fill_empty"], True)
        self.assertEqual(self.errors, [])

    def test_足す_消す_切り替えるはそれぞれの口へ届き_消す前に数で訊く(self) -> None:
        page = self.open_vc("settings")
        page.wait_for_selector("#vc-blocks tr")
        # 足す(新しい VC品種ごと)
        page.select_option("#vc-add-product", "__new__")
        page.fill("#vc-add-name", "畳論マン")
        page.fill("#vc-add-vcatu", "0.1")
        page.fill("#vc-add-insides", "88")
        page.fill("#vc-add-thicknesses", "5, 20, 30")
        mark = len(self.sent)
        page.click("#vc-add-make")
        self.assertEqual(self.wait_reply(page, mark, "/api/vc/quick-block")["new_product"], "畳論マン")
        self.assertIn("畳論マン", page.locator("#vc-blocks").inner_text())
        self.assertEqual(page.input_value("#vc-grid-block"), "畳論マン")   # 続けて直せるよう選んでおく
        # 肉厚 30 の列を消す(何マス消えるかを訊く)
        mark, asked = len(self.sent), len(self.dialogs)
        page.click('#vc-edit-thicknesses button:has-text("30 ×")')
        body = self.wait_reply(page, mark, "/api/vc/quick-cells/delete")
        self.assertEqual((body["block"], body["thicknesses"]), ("畳論マン", "30"))
        self.assertIn("「畳論マン」の 肉厚 30 の列(マス 1)を消します", self.dialogs[asked])
        # 式 ⇔ 固定値(固定値にする前に訊く)
        page.select_option("#vc-edit-source", "fixed")
        mark, asked = len(self.sent), len(self.dialogs)
        page.click("#vc-edit-source-save")
        body = self.wait_reply(page, mark, "/api/vc/quick-block/source")
        self.assertEqual((body["block"], body["product"]), ("畳論マン", ""))
        self.assertIn("「畳論マン」を固定値にします", self.dialogs[asked])
        # 枠を消す(枠とマスの数を言う)
        mark, asked = len(self.sent), len(self.dialogs)
        page.click("#vc-edit-delete")
        self.assertEqual(self.wait_reply(page, mark, "/api/vc/quick-block/delete")["block"], "畳論マン")
        self.assertIn("早見表から「畳論マン」の枠とマス 2 を消します", self.dialogs[asked])
        self.assertNotIn("畳論マン", [t.strip() for t in
                                     page.locator("#vc-blocks td:first-child").all_text_contents()])
        # VC品種も消す
        page.select_option("#vc-del-product", "畳論マン")
        mark, asked = len(self.sent), len(self.dialogs)
        page.click("#vc-del-product-go")
        self.assertEqual(self.wait_reply(page, mark, "/api/vc/product/delete")["product"], "畳論マン")
        self.assertIn("VC品種「畳論マン」を消します", self.dialogs[asked])
        self.assertEqual(self.errors, [])

    def test_置き場所の欄は行の残りいっぱいに伸び_狭い窓ではみ出さない(self) -> None:
        page = self.open_vc("settings")
        box = page.locator("#vc-place-dir")
        wide = box.bounding_box()["width"]
        self.assertGreater(wide, 500, "置き場所の欄が短いままです")
        page.set_viewport_size({"width": 420, "height": 900})
        page.wait_for_timeout(300)
        narrow = page.evaluate("""() => {
            const box = document.getElementById('vc-place-dir').getBoundingClientRect();
            const card = document.getElementById('vc-place').getBoundingClientRect();
            return { right: box.right, cardRight: card.right, width: box.width };
        }""")
        self.assertLessEqual(narrow["right"], narrow["cardRight"] + 1, "狭い窓で欄がはみ出しています")
        self.assertGreater(narrow["width"], 100)


class CoilTests(VcBrowserCase):
    def coil(self, height: int = 1080):
        page = self.watch(self.ctx.new_page())
        page.set_viewport_size({"width": 1920, "height": height})
        page.goto(f"{ORIGIN}{COIL}", wait_until="load")
        page.wait_for_function("document.documentElement.dataset.fit", timeout=8000)
        page.wait_for_timeout(500)
        return page

    @staticmethod
    def fit(page) -> dict:
        return page.evaluate("""() => ({ fit: document.documentElement.dataset.fit,
            scroll: document.scrollingElement.scrollHeight, room: innerHeight,
            bottom: Math.round(document.body.getBoundingClientRect().bottom) })""")

    def test_1920の幅では高さに合わせて詰め_縮め_ページを動かさない(self) -> None:
        page = self.coil(1080)
        expect = {1080: ("as-is",), 800: ("as-is", "compact"), 600: ("compact", "zoom")}
        for height, modes in expect.items():
            with self.subTest(height=height):
                page.set_viewport_size({"width": 1920, "height": height})
                page.wait_for_timeout(600)
                now = self.fit(page)
                self.assertIn(now["fit"].split()[0], modes, now)
                self.assertLessEqual(now["scroll"], now["room"] + 1, f"ページが動きます: {now}")
                # 縮めても窓の下まで使う(下に空きの帯を残さない)
                self.assertLessEqual(abs(now["bottom"] - now["room"]), 2, f"窓の下が空きます: {now}")
                # 決めた段で落ち着く(行き来し続けない)
                page.wait_for_timeout(700)
                self.assertEqual(self.fit(page)["fit"], now["fit"])
        # 0.75 倍でも収まらないほど低ければ、縮めすぎずページごと動かす
        page.set_viewport_size({"width": 1920, "height": 420})
        page.wait_for_timeout(600)
        now = self.fit(page)
        self.assertEqual(now["fit"], "scroll", now)
        self.assertGreater(now["scroll"], now["room"])
        self.assertEqual(self.errors, [])

    def test_コイル平板の枠は作業面の残りちょうど(self) -> None:
        page = self.open_vc()
        page.set_viewport_size({"width": 1920, "height": 1080})
        page.click("#tab-coil")
        page.wait_for_function("document.getElementById('vc-coil-frame').getAttribute('src')")
        page.wait_for_timeout(500)
        gap = """() => {
            const frame = document.getElementById('vc-coil-frame');
            const work = frame.closest('.work');
            const pad = parseFloat(getComputedStyle(work).paddingBottom) || 0;
            return { spill: work.scrollHeight - work.clientHeight,
                     left: work.getBoundingClientRect().bottom - pad - frame.getBoundingClientRect().bottom };
        }"""
        for height in (1080, 760):
            with self.subTest(height=height):
                page.set_viewport_size({"width": 1920, "height": height})
                page.wait_for_timeout(400)
                now = page.evaluate(gap)
                self.assertLessEqual(now["spill"], 1, f"作業面ごと動きます: {now}")
                self.assertLess(abs(now["left"]), 3, f"枠の下が空くか、はみ出しています: {now}")
        self.assertEqual(self.errors, [])

    def test_背景は既定がライト_選べば覚える_印刷は白い紙(self) -> None:
        page = self.coil()
        bg = "getComputedStyle(document.body).backgroundColor"
        self.assertEqual(page.evaluate("document.documentElement.dataset.theme"), "light")
        light = page.evaluate(bg)
        page_var = "getComputedStyle(document.documentElement).getPropertyValue('--page').trim()"
        light_page = page.evaluate(page_var)
        page.click('[data-theme-choice="dark"]')
        self.assertEqual(page.evaluate("document.documentElement.dataset.theme"), "dark")
        dark = page.evaluate(bg)
        self.assertNotEqual(dark, light)
        self.assertEqual(page.get_attribute('[data-theme-choice="dark"]', "aria-pressed"), "true")
        page.reload(wait_until="load")
        self.assertEqual(page.evaluate("document.documentElement.dataset.theme"), "dark", "選んだ背景を覚えていません")
        self.assertEqual(page.evaluate(bg), dark)
        page.emulate_media(media="print")
        paper = page.evaluate(f"{bg}.match(/\\d+/g).slice(0, 3).map(Number)")
        self.assertGreater(min(paper), 240, f"ダークのまま紙に刷ります: {paper}")
        # 紙の上では色の変数もライトのまま(ダークは画面だけ)
        self.assertEqual(page.evaluate(page_var), light_page, "ダークの色が紙にも効いています")
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    import unittest

    unittest.main()
