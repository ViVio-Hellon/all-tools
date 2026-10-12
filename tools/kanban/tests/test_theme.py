"""画面の明るさ(ライト / ダーク)

色は ``tokens.css`` だけが持つ。ダークは同じ名前の色を ``:root[data-theme="dark"]`` で
決め直すだけなので、次を守れば部品の CSS を触らずに 2 通りで描ける:

- ダークで決め直す名前は、ライトにも必ずある(片方だけの名前は、もう片方で色が抜ける)
- 部品の CSS(base / components / layout)に色をじかに書かない(書くとダークで取り残される)
- どの画面にも切り替えのボタンがあり、描く前に明るさを決める小さなスクリプトがある
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
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


JS = ROOT / "app" / "static" / "js"
TEMPLATES = ROOT / "app" / "templates"


def _head_script(html: str) -> str:
    """base.html の頭の、描く前に明るさを決める小さなスクリプト。"""
    head = html[:html.index("</head>")]
    start = head.index("<script>\n  (function ()")
    return head[start + len("<script>"):head.index("</script>", start)]


class ShellDefaultTest(unittest.TestCase):
    """日報複合ツールの大設定「画面の色の既定」に付いていく(portal/static/js/shell.js の sendTheme)。

    VER3.4.0 では付いていかなかった ── 帯のボタンを 1 度でも押すと「選んだ」ことになり、
    選ばない状態へ戻る手段が無かった(大設定の既定ができる前に選んだものも残っていた)。
    """

    def setUp(self) -> None:
        self.js = (JS / "theme.js").read_text(encoding="utf-8")
        self.base = (TEMPLATES / "base.html").read_text(encoding="utf-8")

    def test_listener_is_registered_when_the_module_loads(self):
        """帯のボタンを繋ぐ処理とは切り離して、読み込んだ時点で受け付ける。"""
        self.assertIn("\nlistenShell();\n", self.js)
        self.assertIn("'alltools:theme'", self.js)
        self.assertIn("ev.source !== window.parent", self.js)

    def test_the_choice_can_go_back_to_the_default(self):
        """「◇ 既定」で覚えを消せる(選ばない = 大設定に従う)。"""
        self.assertIn("ORDER = ['light', 'dark', 'auto', DEFAULT_CHOICE]", self.js)
        self.assertIn("localStorage.removeItem(THEME_KEY)", self.js)

    def test_the_old_choice_is_not_read(self):
        """前の版の `kanban.theme`(大設定の既定が無かったころの選択)は読まずに消す。"""
        self.assertIn("localStorage.removeItem(LEGACY_THEME_KEY)", self.js)
        self.assertNotIn("getItem(LEGACY_THEME_KEY)", self.js)
        head = _head_script(self.base)
        self.assertNotIn("getItem('kanban.theme')", head)
        self.assertIn("kanban.theme_choice", head)
        self.assertIn("kanban.theme_default", head)

    def test_boot_screen_uses_the_same_rule_and_listens(self):
        from kanban import boot_screen

        page = boot_screen.render(display_name="看板", version_label="VER", token="t",
                                  app_id="a", poll_ms=300, home_url="/")
        self.assertIn("kanban.theme_choice", page)
        self.assertIn("kanban.theme_default", page)
        self.assertIn("'alltools:theme'", page)
        self.assertNotIn("getItem('kanban.theme')", page)


NODE = shutil.which("node")


@unittest.skipUnless(NODE, "node が無い(画面の JS を動かして確かめる試験)")
class ThemeBehaviourTest(unittest.TestCase):
    """theme.js と頭のスクリプトを node で実際に動かす(DOM は最小のまねもの)。"""

    def run_node(self, script: str) -> list:
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copy(JS / "theme.js", Path(tmp) / "theme.mjs")
            runner = Path(tmp) / "run.mjs"
            runner.write_text(script, encoding="utf-8")
            out = subprocess.run([NODE, str(runner)], capture_output=True, text=True,
                                 encoding="utf-8", timeout=60, cwd=tmp)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout.strip().splitlines()[-1])

    PRELUDE = r"""
const store = new Map(Object.entries(globalThis.INITIAL || {}));
const listeners = {};
const parent = {};
const root = { dataset: {}, setAttribute(k, v) { this.dataset[k.replace('data-', '').replace(/-(\w)/g, (_, c) => c.toUpperCase())] = v; } };
const btn = { on: {}, addEventListener(t, f) { this.on[t] = f; }, setAttribute() {} };
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};
globalThis.window = { parent, matchMedia: () => ({ matches: false, addEventListener() {} }),
  addEventListener: (t, f) => (listeners[t] = listeners[t] || []).push(f) };
globalThis.document = { documentElement: root, getElementById: () => btn };
const send = (theme, source = parent) =>
  (listeners.message || []).forEach((f) => f({ source, data: { type: 'alltools:theme', theme } }));
"""

    def test_follows_the_shell_default_until_chosen_and_can_go_back(self):
        got = self.run_node(
            "globalThis.INITIAL = { 'kanban.theme': 'light' };\n" + self.PRELUDE + r"""
const m = await import('./theme.mjs');
const seen = [store.has('kanban.theme')];
m.wireThemeToggle();
send('dark');      seen.push(root.dataset.theme, store.get('kanban.theme_default'));
send('light', {}); seen.push(root.dataset.theme);               // 大きなタブの画面以外は無視
btn.on.click();    seen.push(store.get('kanban.theme_choice'), root.dataset.theme);
send('dark');      seen.push(root.dataset.theme);               // 選んでいれば、そのまま
btn.on.click(); btn.on.click(); btn.on.click();                 // ダーク → 自動 → 既定
seen.push(store.has('kanban.theme_choice'), root.dataset.theme);
send('light');     seen.push(root.dataset.theme);
console.log(JSON.stringify(seen));
""")
        self.assertEqual(got, [False, "dark", "dark", "dark", "light", "light", "light",
                               False, "dark", "light"])

    def test_head_script_paints_the_shell_default_before_drawing(self):
        html = (TEMPLATES / "base.html").read_text(encoding="utf-8")
        head = _head_script(html)
        cases = [
            ({"kanban.theme_default": "dark"}, "dark"),
            ({"kanban.theme_default": "dark", "kanban.theme": "light"}, "dark"),   # 前の版の選択は読まない
            ({"kanban.theme_default": "dark", "kanban.theme_choice": "light"}, "light"),
            ({}, "light"),
        ]
        for initial, expected in cases:
            with self.subTest(initial=initial):
                got = self.run_node(
                    f"globalThis.INITIAL = {json.dumps(initial)};\n" + self.PRELUDE + head
                    + "\nconsole.log(JSON.stringify([root.dataset.theme]));\n")
                self.assertEqual(got, [expected])


class PlacesRefreshTest(unittest.TestCase):
    """設定画面「ファイルの場所」は、置き場所を変えたらその場で描き直す(views/settings.js)。"""

    def setUp(self) -> None:
        self.js = (JS / "views" / "settings.js").read_text(encoding="utf-8")

    def _body(self, head: str) -> str:
        start = self.js.index(head)
        return self.js[start:self.js.index("\n}\n", start)]

    def test_connection_change_redraws_the_places(self):
        self.assertIn("afterPathChange();", self._body("async function onAccdbChange()"))
        after = self._body("function afterPathChange()")
        self.assertIn("refreshPlaces();", after)
        self.assertIn("/api/file-places", self._body("async function refreshPlaces()"))

    def test_other_place_changes_redraw_too(self):
        for head in ("function wireHistoryPath()", "async function onAccessPathChange()",
                     "function wireLogDir()", "function wireCsvDir()"):
            with self.subTest(head=head):
                self.assertIn("refreshPlaces();", self._body(head))

    def test_template_has_a_box_for_every_group(self):
        html = (TEMPLATES / "settings.html").read_text(encoding="utf-8")
        for key in ("shared", "local", "app"):
            self.assertIn(f'<div data-places="{key}">', html)


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
