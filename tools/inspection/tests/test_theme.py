"""画面の明るさ ── 日報複合ツールの大設定「画面の色の既定」に従う (VER2.4.0)

    > 大設定で ダークにしているのに ライト表示される(大設定の値がデフォルトになっていない)

【約束】
    ・選んでいなければ「既定」= 大設定の既定に従う(届いていなければライト)
    ・この画面で選んだら、そちらが優先(`theme_choice` = light / dark。サーバが覚える)
    ・前の版の `dark_mode` は一度だけ移す。**ライト(= 既定の値)は選んだに数えない**。
      ダークだけ「ダークを選んだ」として残す
    ・帯のボタンは ☀ ライト → ☾ ダーク → ◇ 既定 と回る(選ばない状態へ戻れる。看板 VER3.5.0 と同じ)
    ・大設定の合図は theme.js を読み込んだ時点で受ける。起動待機画面も覚える
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.helpers import make_cfg, quiet_logger, temp_dir
from tests.test_web import HAS_WEB, WebTestBase

from app.repositories.settings_repository import SettingsRepository
from app.services import distribution
from app.services.admin_password import AdminPassword
from app.services.distribution import Distribution
from app.services.settings_service import SettingsService
from core import app_config

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "app" / "static" / "js"
NODE = shutil.which("node")


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = temp_dir()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = os.path.join(self._tmp.name, "data")
        self.path = os.path.join(self.data_dir, "user_settings.json")

    def service(self, saved=None) -> SettingsService:
        if saved is not None:
            os.makedirs(self.data_dir, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as fp:
                json.dump(saved, fp)
        repo = SettingsRepository(self.data_dir, os.path.join(self._tmp.name, "backup"))
        return SettingsService(make_cfg(), repo, quiet_logger())

    def saved(self):
        with open(self.path, encoding="utf-8") as fp:
            return json.load(fp)

    def test_選んでいなければ既定(self):
        s = self.service()
        self.assertEqual(s.theme_choice(), "default")
        self.assertFalse(s.theme_chosen())
        self.assertFalse(s.dark_mode(), "大設定の既定が届くまではライト")

    def test_前の版のライトは選んだに数えない(self):
        """報告の端末: 既定のライトが保存されていて「選んだ」扱いになり、大設定のダークを受けなかった。"""
        s = self.service({"dark_mode": False, "root_folder": r"\\srv\点検表"})
        self.assertFalse(s.theme_chosen())
        self.assertEqual(s.theme_choice(), "default")
        self.assertEqual(self.saved(), {"root_folder": r"\\srv\点検表"}, "一度だけ移す(ほかの値は残す)")

    def test_前の版のダークはダークを選んだものとして残す(self):
        s = self.service({"dark_mode": True})
        self.assertTrue(s.theme_chosen())
        self.assertEqual(s.theme_choice(), "dark")
        self.assertTrue(s.dark_mode())
        self.assertEqual(self.saved(), {"theme_choice": "dark"})

    def test_選んだものが優先_既定に戻せる(self):
        s = self.service()
        s.update(theme="light")
        self.assertTrue(s.theme_chosen())
        self.assertEqual(self.service().theme_choice(), "light", "サーバが覚える")
        s.update(theme="default")
        self.assertFalse(s.theme_chosen())
        self.assertNotIn("theme_choice", self.saved())
        with self.assertRaises(ValueError):
            s.update(theme="auto")


class DistributionTest(unittest.TestCase):
    """配布設定の「表示」も同じ決まり(前の版の配布設定のライトは配らない)。"""

    def setUp(self):
        self._tmp = temp_dir()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.dist_dir = self.tmp / "配布設定"
        self.dist_dir.mkdir()

    def line(self) -> SettingsService:
        local = self.tmp / "line"
        settings = SettingsService(make_cfg(), SettingsRepository(str(local / "data"), str(local / "backup")),
                                   quiet_logger())
        admin = AdminPassword(settings, quiet_logger(), default="nisk")
        Distribution(settings, admin, quiet_logger(), base_dir=self.dist_dir).apply_on_start()
        return settings

    def bundle(self, settings):
        (self.dist_dir / distribution.SETTINGS_NAME).write_text(json.dumps({
            "format": 1, "app_id": app_config.app_id(), "settings": settings}), encoding="utf-8")

    def test_前の版の配布設定のライトは選んだに数えない(self):
        self.bundle({"root_folder": r"\\srv\点検表", "dark_mode": False})
        self.assertFalse(self.line().theme_chosen())

    def test_前の版の配布設定のダークはダーク(self):
        self.bundle({"root_folder": r"\\srv\点検表", "dark_mode": True})
        self.assertEqual(self.line().theme_choice(), "dark")

    def test_いまの配布設定(self):
        self.bundle({"theme_choice": "light"})
        self.assertEqual(self.line().theme_choice(), "light")


@unittest.skipUnless(HAS_WEB, "Flask が入っていないためスキップ")
class PageTest(WebTestBase):
    def setUp(self):
        super().setUp()
        self.addCleanup(self.post, "/api/settings", {"theme": "default"})

    def page(self) -> str:
        return self.client.get("/").get_data(as_text=True)

    # 選んでいなければ描く前に大設定の既定を当てる・選んだら当てない・帯のボタンの文字は、
    # 画面の JS を止めて開いて test_theme_browser.ThemeBrowserTests が見る

    def test_知らない明暗は断る_前の版の画面の送り方も受ける(self):
        res = self.post("/api/settings", {"theme": "auto"})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error"]["code"], "BAD_THEME")
        self.post("/api/settings", {"dark_mode": True})
        self.assertEqual(self.get("/api/settings").get_json()["settings"]["theme"], "dark")


class SourceTest(unittest.TestCase):
    """**残す理由**: 合図を読み込んだ時点で受ける(`listenShell()` を読み込みの最上段で呼ぶ・app.js が
    theme.js を最初に読む)は、帯のボタンを繋ぐ `wireThemeToggle` も同じ受け口を開くので、画面が
    ふつうに動いているかぎり動きでは見分けられない(守りが二重。配線の途中で止まったときのための守り)。

    親の枠からの合図だけを受ける・ボタンの回り順は test_theme_browser と下の BehaviourTest が動かして見る。
    起動待機画面が既定を覚えるのも test_theme_browser が見る。
    """

    def test_合図は読み込んだ時点で受ける(self):
        js = (JS / "theme.js").read_text(encoding="utf-8")
        self.assertIn("\nlistenShell();\n", js)
        app = (JS / "app.js").read_text(encoding="utf-8")
        self.assertLess(app.index('import "./theme.js";'), app.index('import { api }'), "最初に読む")


@unittest.skipUnless(NODE, "node が無い(画面の JS を動かして確かめる試験)")
class BehaviourTest(unittest.TestCase):
    """theme.js を node で実際に動かす(DOM・api・toast は最小のまねもの)。"""

    PRELUDE = r"""
const store = new Map(Object.entries(globalThis.INITIAL || {}));
const listeners = {};
const parent = {};
const root = { dataset: {} };
const btn = { on: {}, textContent: '', addEventListener(t, f) { this.on[t] = f; }, setAttribute() {} };
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};
globalThis.window = { parent, APP: globalThis.APP_VALUES || {},
  addEventListener: (t, f) => (listeners[t] = listeners[t] || []).push(f) };
globalThis.document = { documentElement: root, getElementById: () => btn };
globalThis.POSTS = [];
const send = (theme, source = parent) =>
  (listeners.message || []).forEach((f) => f({ source, data: { type: 'alltools:theme', theme } }));
const flush = () => new Promise((r) => setTimeout(r, 0));
"""

    def run_node(self, script: str) -> list:
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copy(JS / "theme.js", Path(tmp) / "theme.js")
            (Path(tmp) / "package.json").write_text('{"type": "module"}', encoding="utf-8")
            (Path(tmp) / "api.js").write_text(
                "export const api = { post: async (path, body) => { globalThis.POSTS.push(body); return {}; } };\n",
                encoding="utf-8")
            (Path(tmp) / "toast.js").write_text("export const toastError = () => {};\n", encoding="utf-8")
            runner = Path(tmp) / "run.js"
            runner.write_text(script, encoding="utf-8")
            out = subprocess.run([NODE, str(runner)], capture_output=True, text=True,
                                 encoding="utf-8", timeout=60, cwd=tmp)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_選んでいなければ大設定に従い_選んだら優先_既定に戻れる(self):
        got = self.run_node(
            "globalThis.APP_VALUES = { theme: 'default' };\n" + self.PRELUDE + r"""
const m = await import('./theme.js');
const seen = [];
send('dark');      seen.push(root.dataset.theme, store.get('inspection.theme_default'));  // 配線の前でも受ける
m.wireThemeToggle();
seen.push(btn.textContent);
send('light', {}); seen.push(root.dataset.theme);               // 大きなタブの画面以外は無視
btn.on.click(); await flush();                                  // 既定(ダーク)→ 見た目が変わるライト
seen.push(root.dataset.theme, btn.textContent, JSON.stringify(globalThis.POSTS.at(-1)));
send('dark');      seen.push(root.dataset.theme);               // 選んでいれば、そのまま
btn.on.click(); await flush(); btn.on.click(); await flush();   // ライト → ダーク → 既定
seen.push(btn.textContent, JSON.stringify(globalThis.POSTS.at(-1)), root.dataset.theme);
send('light');     seen.push(root.dataset.theme);
console.log(JSON.stringify(seen));
""")
        self.assertEqual(got, ["dark", "dark", "◇ 既定", "dark",
                               "light", "☀ ライト", '{"theme":"light"}',
                               "light",
                               "◇ 既定", '{"theme":"default"}', "dark",
                               "light"])

    def test_サーバが覚えている選びを使う(self):
        got = self.run_node(
            "globalThis.APP_VALUES = { theme: 'light' };\n"
            "globalThis.INITIAL = { 'inspection.theme_default': 'dark' };\n" + self.PRELUDE + r"""
const m = await import('./theme.js');
m.wireThemeToggle();
console.log(JSON.stringify([root.dataset.theme, btn.textContent, m.shellDefault()]));
""")
        self.assertEqual(got, ["light", "☀ ライト", "dark"])


if __name__ == "__main__":
    unittest.main()
