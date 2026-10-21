"""画面の見た目(ライト / ダーク / 自動)

【確かめること】
- 選べるのは「自動・ライト・ダーク」だけ。この端末の settings.json に覚える
- **HTML を返す時点で** ``<html data-theme>`` が付く(開くたびに一瞬白く光らない)。
  「自動」は付けない(OS の設定に任せる)
- ダークの2つの節(OS がダーク / 明示的にダーク)が同じ値
- 明暗どちらでも、文字が読める濃さ(コントラスト比)。とくに
  **塗りの上の文字**(押すボタンなど)── ダークでは塗りが明るくなるので、
  白い字のままだと読めない
  (この2つは画面に当たった色で ``test_theme_browser`` が見る。比べる組と計算はここに置く)
"""
from __future__ import annotations

import unittest
from pathlib import Path

from . import _isolation

_isolation.ensure_isolated()

from calendar_app import settings as user_settings  # noqa: E402

from . import _web  # noqa: E402


def _contrast(a: str, b: str) -> float:
    def lum(color: str) -> float:
        r, g, bl = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
        f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4  # noqa: E731
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(bl)
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


#: 文字と、その下の地。WCAG の本文の基準 4.5:1
TEXT_PAIRS = (
    ("ink", "ground"), ("ink", "surface"), ("muted", "surface"), ("muted", "ground"),
    ("accent", "surface"), ("day-ink", "day-bg"), ("day-sat-ink", "day-sat-bg"),
    ("day-sun-ink", "day-sun-bg"), ("kind-rest", "kind-rest-bg"),
    ("kind-comment", "kind-comment-bg"), ("state-ok", "state-ok-bg"),
    ("state-warn", "state-warn-bg"), ("state-error", "state-error-bg"),
    ("state-info", "state-info-bg"),
    # 塗りの上の文字(押すボタン・現在地の番号)
    ("on-accent", "accent"), ("on-error", "state-error"),
    ("hist-accent-ink", "hist-accent"),
)


class ColorTests(unittest.TestCase):
    # ダークの2つの節が同じ値・明暗どちらでも文字が読める・入力欄やスクロールバーも明暗に
    # 合わせる(color-scheme)は、画面に当たった色で test_theme_browser.ThemeBrowserTests が見る

    def test_塗りの上に白い字を直に書かない(self) -> None:
        """白のままだと、ダークの明るい塗りの上で読めない(色は tokens から)。"""
        css_dir = Path(__file__).resolve().parent.parent / "app" / "static" / "css"
        for name in ("components.css", "layout.css", "base.css"):
            text = (css_dir / name).read_text(encoding="utf-8")
            with self.subTest(file=name):
                self.assertNotRegex(text, r"color:\s*#fff\b")


class SettingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(user_settings.set_value, user_settings.KEY_THEME,
                        user_settings.THEME_AUTO)

    def test_既定はライト(self) -> None:
        """日報複合ツールの4ツールで既定をそろえる(以前は自動 = Windows の設定に合わせていた)。"""
        user_settings.set_value(user_settings.KEY_THEME, "")
        self.assertEqual(user_settings.get_theme(), "light")

    def test_知らない値はライトとして読む(self) -> None:
        user_settings.set_value(user_settings.KEY_THEME, "ピンク")
        self.assertEqual(user_settings.get_theme(), "light")

    def test_自動を選べば自動のまま(self) -> None:
        user_settings.set_theme("auto")
        self.assertEqual(user_settings.get_theme(), "auto")

    def test_選んでいなければ大設定の既定に従う印(self) -> None:
        """選んでいない(未設定)ときだけ、日報複合ツールの大設定の既定で塗る(`theme.js`)。"""
        user_settings.set_value(user_settings.KEY_THEME, "")
        self.assertFalse(user_settings.theme_is_chosen())
        user_settings.set_theme("dark")
        self.assertTrue(user_settings.theme_is_chosen())

    def test_一覧に無い値は覚えない(self) -> None:
        with self.assertRaises(ValueError):
            user_settings.set_theme("pink")


class WebTests(unittest.TestCase):
    def setUp(self) -> None:
        _web.bind_db(self)
        _web.reset_sync()
        self.addCleanup(_web.reset_sync)
        self.addCleanup(user_settings.set_value, user_settings.KEY_THEME,
                        user_settings.THEME_AUTO)
        self.client = _web.make_client()

    def post(self, theme: str):
        return self.client.post("/api/settings/theme", headers=_web.auth(),
                                json={"theme": theme})

    def page(self, path: str = "/calendar") -> str:
        return self.client.get(f"{path}?t={_web.TOKEN}").get_data(as_text=True)

    def test_ダークを選ぶと画面を返す時点で付いている(self) -> None:
        res = self.post("dark")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["theme"], "dark")
        self.assertEqual(user_settings.get_theme(), "dark")
        for path in ("/calendar", "/history", "/settings"):
            with self.subTest(path=path):
                self.assertIn('<html lang="ja" data-theme="dark">', self.page(path))

    def test_自動なら属性を付けない(self) -> None:
        self.post("light")
        self.assertIn('data-theme="light"', self.page())
        self.post("auto")
        html = self.page()
        self.assertIn('<html lang="ja">', html)
        self.assertNotIn("data-theme=", html.split("<head>")[0])

    def test_一覧に無いものは断る(self) -> None:
        self.post("dark")
        res = self.post("pink")
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"], "not_listed")
        self.assertEqual(user_settings.get_theme(), "dark")     # 変わっていない

    # 帯の切り替えのボタン(押すと何になるかの文字・送ってサーバが覚える・断られたら戻す)は
    # test_theme_browser.ThemeBrowserTests が押して見る

    def test_設定画面に選べるものが届く(self) -> None:
        payload = self.client.get("/api/settings", headers=_web.auth()).get_json()
        self.assertEqual([c["value"] for c in payload["theme_choices"]],
                         ["auto", "light", "dark"])
        self.assertEqual(payload["theme"], "auto")

    def test_印刷は見た目に関係なく白い紙(self) -> None:
        self.post("dark")
        html = self.client.get(f"/print?year=2026&month=9&t={_web.TOKEN}").get_data(as_text=True)
        self.assertNotIn("data-theme", html)
        self.assertNotIn("tokens.css", html)


if __name__ == "__main__":
    unittest.main()
