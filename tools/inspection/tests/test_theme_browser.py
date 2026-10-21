"""画面の明るさを**本物のブラウザ**で ── 大設定の「画面の色の既定」の受け方 (段階1)

前は画面の HTML に ``localStorage.getItem("inspection.theme_default")`` の字があるか、
``theme.js`` に ``event.source !== window.parent`` の字があるかを探していた。ここでは
日報複合ツールの大きなタブの画面(親の枠)を試験が作り、その中で点検表を開いて確かめる。

- 選んでいなければ、大設定の既定を**描く前に**当てる(画面の JS を止めても当たっている)。
  選んだら当てない
- 親の枠から届いた既定で塗り直す。親でない窓から届いたものは無視する。選んでいればそのまま
- 起動待機画面でも既定を受けて覚え、続いて出る点検表の画面がその色で描く
"""
from __future__ import annotations

import unittest

from tests._page_browser import ORIGIN, PageBrowserCase

KEY = "inspection.theme_default"

#: 日報複合ツールの大きなタブの画面の代わり。中に点検表を開き、色の既定を送れる
SHELL = """<!doctype html><meta charset="utf-8"><title>外枠</title>
<iframe id="tool" src="/" style="width:1200px;height:800px"></iframe>
<script>
  window.sendTheme = (theme) => document.getElementById('tool').contentWindow
    .postMessage({ type: 'alltools:theme', theme }, '*');
</script>"""


class ThemeBrowserTests(PageBrowserCase):
    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(self.post, "/api/settings", {"theme": "default"})
        self.pages["/shell"] = SHELL

    def remembered(self, theme: str):
        ctx = self.new_context()
        ctx.add_init_script(f"try {{ localStorage.setItem('{KEY}', '{theme}'); }} catch (e) {{}}")
        return ctx

    @staticmethod
    def theme_of(target) -> str:
        return target.evaluate("document.documentElement.dataset.theme || ''")

    def test_選んでいなければ描く前に大設定の既定を当て_選んだら当てない(self) -> None:
        # 画面の JS を止めて開く: 当たっているなら、頭の小さなスクリプトが描く前に当てた
        self.refuse["/static/js/"] = 404
        page = self.open("/", self.remembered("dark"), wait_list=False)
        self.assertEqual(self.theme_of(page), "dark")
        page.context.close()

        self.assertEqual(self.post("/api/settings", {"theme": "light"}).status_code, 200)
        page = self.open("/", self.remembered("dark"), wait_list=False)
        self.assertEqual(self.theme_of(page), "light", "選んだのに大設定の既定で塗った")
        page.context.close()

        # JS も動かす。帯のボタンは選び方を言う
        del self.refuse["/static/js/"]
        self.post("/api/settings", {"theme": "default"})
        page = self.open("/", self.remembered("dark"))
        page.wait_for_function("document.getElementById('btn-theme').textContent === '◇ 既定'")
        self.assertEqual(self.theme_of(page), "dark")
        page.context.close()
        self.post("/api/settings", {"theme": "dark"})
        page = self.open("/", self.remembered("light"))
        page.wait_for_function("document.getElementById('btn-theme').textContent === '☾ ダーク'")
        self.assertEqual(self.theme_of(page), "dark")
        self.assertEqual(self.errors, [])

    def test_親の枠から届いた既定で塗り直し_親でない窓からは無視し_選んでいればそのまま(self) -> None:
        page = self.open("/shell", wait_list=False)
        tool = page.frame_locator("#tool")
        tool.locator("#items .item").first.wait_for(state="attached")
        frame = page.frame(url=f"{ORIGIN}/")
        self.assertEqual(self.theme_of(frame), "light")

        page.evaluate("sendTheme('dark')")
        frame.wait_for_function("document.documentElement.dataset.theme === 'dark'")
        self.assertEqual(frame.evaluate(f"localStorage.getItem('{KEY}')"), "dark")

        # 親でない窓(点検表の画面そのもの)から送っても塗り直さない
        frame.evaluate("window.postMessage({ type: 'alltools:theme', theme: 'light' }, '*')")
        page.wait_for_timeout(300)
        self.assertEqual(self.theme_of(frame), "dark")

        # 既定(ダーク)から押すと、見た目が変わるライトを選ぶ。選んだら大設定の既定は効かない
        frame.click("#btn-theme")
        frame.wait_for_function("document.documentElement.dataset.theme === 'light'")
        self.wait_until(page, lambda: self.get("/api/settings").get_json()["settings"]["theme"] == "light",
                        "ライトを選んだと覚える")
        page.evaluate("sendTheme('dark')")
        page.wait_for_timeout(300)
        self.assertEqual(self.theme_of(frame), "light")
        self.assertEqual(self.errors, [])

    def test_起動待機画面で受けた既定で_続いて出る点検表の画面を描く(self) -> None:
        self.app.config["READY"] = False
        page = self.open("/shell", wait_list=False)
        frame = page.frame(url=f"{ORIGIN}/")
        frame.wait_for_selector("#steps", state="attached")      # 起動待機画面
        page.evaluate("sendTheme('dark')")
        self.wait_until(page, lambda: page.evaluate(f"localStorage.getItem('{KEY}')") == "dark",
                        "起動待機画面が既定を覚える")
        # 親でない窓(起動待機画面そのもの)から送っても覚え直さない
        frame.evaluate("window.postMessage({ type: 'alltools:theme', theme: 'light' }, '*')")
        page.wait_for_timeout(300)
        self.assertEqual(page.evaluate(f"localStorage.getItem('{KEY}')"), "dark")

        # 起動し終えると点検表の画面に移る。最初からダークで描く
        self.app.config["READY"] = True
        page.frame_locator("#tool").locator("#items .item").first.wait_for(state="attached", timeout=15000)
        frame = page.frame(url=lambda url: url.startswith(f"{ORIGIN}/?t="))
        self.assertEqual(self.theme_of(frame), "dark")
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
