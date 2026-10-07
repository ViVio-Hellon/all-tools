"""背景のライト / ダークを帯で切り替える (v4.19.0)

    ダークモード ライトモードで背景チェンジできるようにしてください

【約束】
    ・帯(どの画面にも出る)に「ライト / ダーク」。早見表の別窓(帯なし)には出さない
    ・選んだものは `<html data-theme>`。**描く前に当てる**(`<head>` の script が CSS より先)
    ・選ぶまでは OS(Windows)の設定のまま ── 何も付けない
    ・暗い色は `tokens.css` の2か所(OS がダーク / 選んだダーク)が**同じ中身**
    ・ブラウザが描く部品(選ぶ欄の一覧・スクロールバー)も `color-scheme` で揃える
    ・ネオンの画面は、ライトを選んだら地を明るく、カードとグラフだけ濃紺の島に
    ・コイル・平板(枠の中の別の道具)にも同じ選択を渡す
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"


def tokens() -> str:
    return (STATIC / "css" / "tokens.css").read_text(encoding="utf-8")


def _vars(block: str) -> dict[str, str]:
    return {k: v.strip() for k, v in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block)}


class TokensTests(unittest.TestCase):
    def test_暗い色は2か所で同じ中身(self) -> None:
        css = tokens()
        media = css[css.index("@media (prefers-color-scheme: dark){"):
                    css.index("/* 利用者が明示的にダークを選んだとき")]
        chosen = css[css.index(':root[data-theme="dark"]{'):]
        chosen = chosen[:chosen.index("}")]
        self.assertEqual(_vars(media), _vars(chosen))
        self.assertIn(':root:not([data-theme="light"])', media)    # ライトを選べば OS がダークでも明るい

    def test_ブラウザが描く部品も揃える(self) -> None:
        css = tokens()
        self.assertEqual(css.count("color-scheme:dark;"), 2)
        self.assertEqual(css.count("color-scheme:light;"), 1)

    def test_ネオンの画面はライトを選べば明るい地にカードだけ濃紺(self) -> None:
        css = tokens()
        for page in ("graph", "agg", "standard"):
            with self.subTest(page=page):
                self.assertIn(f':root[data-skin="neon"]:not([data-theme="light"]) '
                              f'body[data-page="{page}"]', css)
                self.assertIn(f':root[data-skin="neon"][data-theme="light"] '
                              f'body[data-page="{page}"] :is(.tile, .chart-skin)', css)


class ScriptTests(unittest.TestCase):
    def js(self, name: str) -> str:
        return (STATIC / "js" / name).read_text(encoding="utf-8")

    def test_覚える場所とコイルの覚え(self) -> None:
        theme = self.js("theme.js")
        self.assertIn('const KEY = "nippou.theme";', theme)
        coil = (STATIC / "vc" / "coil" / "coil-theme.js").read_text(encoding="utf-8")
        coil_key = re.search(r"const KEY = '([^']+)'", coil).group(1)
        self.assertIn(f'const COIL_KEY = "{coil_key}";', theme)

    def test_帯を作り直すたびに繋ぐ(self) -> None:
        app = self.js("app.js")
        self.assertIn('import * as theme from "./theme.js";', app)
        body = app[app.index("function wireShell()"):]
        body = body[:body.index("\n}\n")]
        self.assertIn('theme.wire(document.getElementById("theme-switch"))', body)


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class PageTests(WebTestCase):
    def test_どの画面の帯にも切り替え(self) -> None:
        for path in ("/", "/gw", "/vc", "/records", "/graph", "/agg", "/standard-time", "/settings"):
            with self.subTest(path=path):
                page = self.get(path).get_data(as_text=True)
                self.assertIn('id="theme-switch"', page)
                self.assertRegex(page, r'data-theme-choice="light"[^>]*aria-pressed=')
                self.assertRegex(page, r'data-theme-choice="dark"[^>]*aria-pressed=')

    def test_描く前に当てる(self) -> None:
        page = self.get("/").get_data(as_text=True)
        script = page.index('localStorage.getItem("nippou.theme")')
        self.assertLess(script, page.index("css/tokens.css"))
        self.assertIn("document.documentElement.dataset.theme = theme", page)
        # 選ぶまではライト(統合ツールの4ツールでそろえる。OS のダークには付いていかない)
        self.assertIn('data-theme="light"', page[:page.index("<head>")])

    def test_早見表の別窓は帯が無いが_選んだ背景は効く(self) -> None:
        page = self.get("/vc?window=quick&tab=quick").get_data(as_text=True)
        self.assertNotIn('id="theme-switch"', page)
        self.assertIn('localStorage.getItem("nippou.theme")', page)


if __name__ == "__main__":
    unittest.main()
