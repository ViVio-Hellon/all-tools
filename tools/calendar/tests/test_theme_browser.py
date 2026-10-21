"""画面の見た目(ライト / ダーク / 自動)を**本物のブラウザ**で確かめる (段階1)

前は ``tokens.css`` の字を読んで、ダークの2つの節が同じか・文字が読める濃さかを数えていた。
ここでは画面を開き、**ブラウザが実際に当てた色**で確かめる(節の選び方を間違えて当たって
いない、も見つかる)。

- 明るい表示 / OS がダーク(「自動」)/ 選んでダーク の3つで、色の札を全部読む
- OS がダークのときと選んでダークのときで、同じ色になる
- どの表示でも、文字が読める濃さ(4.5:1)。とくに塗りの上の文字
- 入力欄やスクロールバーも明暗に合わせる(``color-scheme``)
- 帯のボタンで切り替え、サーバが覚える。断られたら元に戻す
"""
from __future__ import annotations

import unittest

from calendar_app import settings as user_settings

from tests._page_browser import PageBrowserCase
from tests.test_theme import TEXT_PAIRS, _contrast

#: 画面に当たっている色の札(`--名前`)を全部読む。値は1色ずつブラウザに塗らせて rgb で返す
_READ_TOKENS = """() => {
    const names = new Set();
    const walk = (rules) => {
        for (const rule of rules) {
            if (rule.cssRules) walk(rule.cssRules);
            if (!rule.style) continue;
            for (const prop of rule.style) if (prop.startsWith('--')) names.add(prop);
        }
    };
    for (const sheet of document.styleSheets) walk(sheet.cssRules);
    const root = getComputedStyle(document.documentElement);
    const probe = document.createElement('i');
    document.body.appendChild(probe);
    const out = { tokens: {}, colors: {}, scheme: root.colorScheme };
    for (const name of names) {
        const value = root.getPropertyValue(name).trim();
        out.tokens[name.slice(2)] = value;
        probe.style.color = '';
        probe.style.color = value;
        if (probe.style.color) out.colors[name.slice(2)] = getComputedStyle(probe).color;
    }
    probe.remove();
    return out;
}"""


def _hex(rgb: str) -> str:
    parts = [int(float(p)) for p in rgb[rgb.index("(") + 1:rgb.index(")")].split(",")[:3]]
    return "#" + "".join(f"{p:02x}" for p in parts)


class ThemeBrowserTests(PageBrowserCase):
    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(user_settings.set_value, user_settings.KEY_THEME,
                        user_settings.THEME_AUTO)

    def read(self, theme: str, scheme: str) -> dict:
        user_settings.set_theme(theme)
        page = self.open("/calendar", self.new_context(color_scheme=scheme))
        got = page.evaluate(_READ_TOKENS)
        page.context.close()
        self.assertTrue(got["tokens"], "色の札が1つも当たっていない")
        return got

    def test_明暗の色は_OSがダークでも選んでダークでも同じで_どれも文字が読める(self) -> None:
        light = self.read("light", "light")
        os_dark = self.read("auto", "dark")
        chosen_dark = self.read("dark", "light")

        # OS がダークのときと、選んでダークにしたときで色が違うと、どちらかが古い
        self.assertEqual(os_dark["tokens"], chosen_dark["tokens"])
        self.assertNotEqual(light["tokens"]["ground"], chosen_dark["tokens"]["ground"],
                            "ダークの色が当たっていない")

        # 入力欄やスクロールバーも明暗に合わせる
        self.assertEqual(light["scheme"], "light")
        self.assertEqual(os_dark["scheme"], "dark")
        self.assertEqual(chosen_dark["scheme"], "dark")

        for name, got in (("ライト", light), ("ダーク(OS)", os_dark), ("ダーク(選んだ)", chosen_dark)):
            for ink, ground in TEXT_PAIRS:
                with self.subTest(theme=name, pair=(ink, ground)):
                    ratio = _contrast(_hex(got["colors"][ink]), _hex(got["colors"][ground]))
                    self.assertGreaterEqual(ratio, 4.5, f"{name}: {ink} on {ground} = {ratio:.2f}")
        self.assertEqual(self.errors, [])

    def test_帯のボタンで明暗を切り替え_サーバが覚える_断られたら戻す(self) -> None:
        user_settings.set_theme("auto")
        page = self.open("/calendar", self.new_context(color_scheme="light"))
        root = "document.documentElement.dataset.theme || ''"
        label = "#theme-toggle-label"
        # 「自動」で OS が明るい。**押すと何になるか**を書く
        page.wait_for_function("document.getElementById('theme-toggle-label').textContent === '暗くする'")
        self.assertEqual(page.evaluate(root), "")

        page.click("#theme-toggle")
        page.wait_for_function(f"({root}) === 'dark'")
        self.assertEqual(page.inner_text(label), "明るくする")
        self.wait_until(page, lambda: self.posts_to("/api/settings/theme"), "見た目を送る")
        self.assertEqual(self.posts_to("/api/settings/theme"), [{"theme": "dark"}])
        self.wait_until(page, lambda: user_settings.get_theme() == "dark", "サーバがダークを覚える")

        # 開き直しても、サーバが覚えた明暗で描く(帯の文字もそれに合う)
        page.reload(wait_until="load")
        page.wait_for_function("document.getElementById('theme-toggle-label').textContent === '明るくする'")
        self.assertEqual(page.evaluate(root), "dark")

        # 断られたら元(ダーク)に戻す
        self.refuse["/api/settings/theme"] = 500
        page.click("#theme-toggle")
        page.wait_for_selector(".toast")
        page.wait_for_function(f"({root}) === 'dark'")
        self.assertEqual(page.inner_text(label), "明るくする")
        self.assertEqual(user_settings.get_theme(), "dark")

        # 通れば明るくなる
        del self.refuse["/api/settings/theme"]
        page.click("#theme-toggle")
        page.wait_for_function(f"({root}) === 'light'")
        self.wait_until(page, lambda: user_settings.get_theme() == "light", "サーバがライトを覚える")
        self.assertEqual(self.errors, [])


    def test_この端末で選んだ明暗は_大設定の既定が残っていても上書きされない(self) -> None:
        """カレンダー VER4.2.3 で直した不具合(段階1の試験で見つかった)。

        画面へ渡す値(`window.APP`)に「この端末で選んだか」が入っておらず、`theme.js` はいつも
        「選んでいない」とみなしていました。ダークを選んでも、手元に大設定の既定(ライト)が残っていると
        帯のボタンが「暗くする」になり、押しても見た目が変わらず、大設定の既定が届くと上書きされた。
        """
        user_settings.set_theme("dark")
        ctx = self.new_context(color_scheme="light")
        ctx.add_init_script("try { localStorage.setItem('calendar.theme_default', 'light'); } catch (e) {}")
        page = self.open("/calendar", ctx)
        root = "document.documentElement.dataset.theme || ''"
        self.assertEqual(page.evaluate(root), "dark")
        page.wait_for_function("document.getElementById('theme-toggle-label').textContent !== '見た目'")
        self.assertEqual(page.inner_text("#theme-toggle-label"), "明るくする",
                         "選んだダークではなく、大設定の既定(ライト)を今の見た目とみなしています")
        page.click("#theme-toggle")                         # 1回押せば明るくなる
        page.wait_for_function(f"({root}) === 'light'")
        self.wait_until(page, lambda: user_settings.get_theme() == "light", "サーバがライトを覚える")
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
