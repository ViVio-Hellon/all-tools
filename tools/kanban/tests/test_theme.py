"""画面の明るさ(ライト / ダーク)

色は ``tokens.css`` だけが持つ。ダークは同じ名前の色を ``:root[data-theme="dark"]`` で
決め直すだけなので、次を守れば部品の CSS を触らずに 2 通りで描ける:

- ダークで決め直す名前は、ライトにも必ずある(片方だけの名前は、もう片方で色が抜ける)
- 部品の CSS(base / components / layout)に色をじかに書かない(書くとダークで取り残される)
- どの画面にも切り替えのボタンがあり、描く前に明るさを決める小さなスクリプトがある
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSS = ROOT / "app" / "static" / "css"


def _block(text: str, selector: str) -> str:
    start = text.index("\n" + selector)   # 行の頭(説明の中の同じ文字は飛ばす)
    return text[text.index("{", start) + 1:text.index("\n}", start)]


def _names(block: str) -> set[str]:
    return set(re.findall(r"(--[\w-]+)\s*:", block))


class TokensTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tokens = (CSS / "tokens.css").read_text(encoding="utf-8")

    def test_every_dark_color_has_a_light_one(self):
        light = _names(_block(self.tokens, ":root {"))
        dark = _names(_block(self.tokens, ':root[data-theme="dark"]'))
        self.assertTrue(dark, "ダークの色が見つからない")
        self.assertEqual(dark - light, set(), "ダークにだけある色の名前")

    def test_status_colors_stay_the_same_in_dark(self):
        """赤・緑・黄の状態色は VBA の色のまま(10年見てきた色は変えない)。"""
        dark = _names(_block(self.tokens, ':root[data-theme="dark"]'))
        for name in ("--st-ordered", "--st-shipped", "--st-held"):
            self.assertNotIn(name, dark)

    def test_chart_series_are_redefined_for_dark(self):
        dark = _names(_block(self.tokens, ':root[data-theme="dark"]'))
        self.assertEqual({f"--series-{i}" for i in range(1, 9)} - dark, set())

    def test_component_css_has_no_raw_colors(self):
        for name in ("base.css", "components.css", "layout.css"):
            text = (CSS / name).read_text(encoding="utf-8")
            text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
            # トーストの文字だけは地(--toast-bg)がどちらでも暗いので白のまま
            text = text.replace("background: var(--toast-bg); color: #fff;", "")
            raw = re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", text)
            self.assertEqual(raw, [], f"{name} に色がじかに書いてある(tokens.css に名前を足す)")


try:
    import flask  # noqa: F401
    HAS_WEB = True
except ImportError:  # pragma: no cover
    HAS_WEB = False


if HAS_WEB:
    from test_web_app import RouteTestBase

    class PagesTest(RouteTestBase):
        def test_every_page_has_the_toggle_and_decides_before_painting(self):
            for path in ("/board", "/settings"):
                html = self.get(path).get_data(as_text=True)
                self.assertIn('id="theme-toggle"', html, path)
                head = html[:html.index("</head>")]
                # CSS より先に決める(あとからだと白く光ってから暗くなる)
                self.assertLess(head.index("kanban.theme"), head.index("tokens.css"), path)

        def test_boot_screen_follows_the_same_choice(self):
            from kanban import boot_screen

            page = boot_screen.render(display_name="看板", version_label="VER", token="t",
                                      app_id="a", poll_ms=300, home_url="/")
            self.assertIn("kanban.theme", page)
            self.assertIn(':root[data-theme="dark"]', page)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
