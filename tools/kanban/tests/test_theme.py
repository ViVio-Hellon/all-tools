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


# 日報複合ツールの大設定「画面の色の既定」に付いていく(portal/static/js/shell.js の sendTheme)こと
# ── 読み込んだ時点で受け付ける・「◇ 既定」で覚えを消せる・前の版の `kanban.theme` は読まずに消す・
# 起動待機画面も同じ決め方で同じ合図を受ける ── は、字を探すのではなく**本物のブラウザで
# 親の画面から合図を送って確かめます**(`test_theme_browser`。守りを外すと落ちる):
#   大設定の既定に付いていく・親の画面からだけ受ける・回り順と既定へ戻す
#                                     … `test_theme_browser.ShellDefaultTest`
#   外枠の配線(app.js)が止まっても合図を受ける(読み込んだ時点で受け付ける)
#                                     … `test_theme_browser.ShellDefaultTest.test_外枠の配線が途中で止まっても_色の合図は受ける`
#   前の版の選択は読まずに消す       … `test_theme_browser.BeforePaintTest`
#   起動待機画面                     … `test_theme_browser.BootScreenTest`


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


# 設定画面「ファイルの場所」を置き場所を変えたらその場で描き直すこと(views/settings.js の
# refreshPlaces)と、組ごとの箱(shared / local / app)は、**本物のブラウザで置き場所を変えて
# 確かめます**(`test_settings_places_browser`。守りを外すと落ちる)。


# どの画面にも切り替えのボタンがあり、**CSS より先に、描く前に**明るさを決めること・起動待機画面も
# 同じ選び方に従うことは、**本物のブラウザで CSS が届くのを止めて確かめます**
# (`test_theme_browser.BeforePaintTest` / `BootScreenTest`。守りを外すと落ちる)。


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
