"""外枠(Rust/Tauri、`src-tauri/`)と、入口・各ツール(Python・JS)との取り決め

言語をまたぐ取り決めは、片方だけ直すと**黙って**食い違う(既定の値で動いてしまう・
頼みごとが届かない)。ここでソースを突き合わせて見つける。
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

import tests  # noqa: F401  (一時フォルダへ向ける)

from portal import app_config, catalog as catalog_mod, instance_guard

ROOT = Path(__file__).resolve().parent.parent
RUST = ROOT / "src-tauri" / "src"


def rust(name: str) -> str:
    return (RUST / name).read_text(encoding="utf-8")


class CatalogTests(unittest.TestCase):
    """`config/tools.json` は外枠と入口が同じものを読む。"""

    def setUp(self) -> None:
        self.raw = json.loads((ROOT / "config" / "tools.json").read_text(encoding="utf-8"))
        self.catalog = catalog_mod.load()

    def test_各ツールの置き場所に入口がある(self) -> None:
        for tool in self.catalog.tools:
            with self.subTest(tool=tool.id):
                for name in ("bridge.py", "start_app.py", "config/app.json", "app/static"):
                    self.assertTrue((tool.dir / name).exists(), f"{tool.dir / name}")

    def test_宛先と名前は英小文字で重ならない(self) -> None:
        entries = [self.raw["portal"], *self.raw["tools"]]
        schemes = [e.get("scheme") or e["id"] for e in entries]
        ids = [e["id"] for e in entries]
        for value in schemes + ids:
            self.assertRegex(value, r"^[a-z0-9]+$")
        self.assertEqual(len(set(schemes)), len(schemes))
        self.assertEqual(len(set(ids)), len(ids))
        self.assertEqual(self.raw["portal"]["id"], "portal")
        self.assertIn('pub const PORTAL: &str = "portal";', rust("catalog.rs"))

    def test_環境変数の頭はツールが読む名前と同じ(self) -> None:
        """外枠は <頭>_TOKEN を渡す。ツールの bridge.py がその名前を読んでいること。"""
        for tool in self.catalog.tools:
            with self.subTest(tool=tool.id):
                bridge = (tool.dir / "bridge.py").read_text(encoding="utf-8")
                self.assertIn(tool.token_env, bridge)
        self.assertIn('format!("{}_TOKEN", self.env_prefix)', rust("catalog.rs"))
        self.assertIn('format!("{}_PYTHON", self.env_prefix)', rust("catalog.rs"))
        self.assertIn("ALLTOOLS_TOKEN", (ROOT / "bridge.py").read_text(encoding="utf-8"))

    def test_手元の領域の名前はツールの設定と同じ(self) -> None:
        for tool in self.catalog.tools:
            with self.subTest(tool=tool.id):
                self.assertEqual(tool.local_dir_name, tool.app_config().get("local_dir_name"))


class OriginTests(unittest.TestCase):
    def test_宛先の頭の作り方はPythonとRustで同じ(self) -> None:
        catalog_rs = rust("catalog.rs")
        self.assertIn('format!("http://{scheme}.localhost")', catalog_rs)
        self.assertIn('format!("{scheme}://localhost")', catalog_rs)
        expected = "http://kanban.localhost" if sys.platform == "win32" else "kanban://localhost"
        self.assertEqual(app_config.origin_of("kanban"), expected)

    def test_ツールへ渡すHostは各ツールが受け付ける名前(self) -> None:
        relay = rust("relay.rs")
        self.assertIn('pub const TOOL_HOST: &str = "app.localhost";', relay)
        self.assertIn('pub const TOOL_ORIGIN: &str = "http://app.localhost";', relay)
        self.assertIn("app.localhost", app_config.BRIDGE_HOSTS)


class ServicesTests(unittest.TestCase):
    """ツールの画面が外枠に頼むこと(`window.__TAURI__.core.invoke`)は、外枠が全部受ける。"""

    def test_ツールの画面が頼む名前は外枠が受け持つ(self) -> None:
        services = rust("services.rs")
        handled = set(re.findall(r'^\s*"([a-z_]+)" =>', services, re.M))
        asked: dict[str, set[str]] = {}
        for js in ROOT.glob("tools/*/app/static/js/**/*.js"):
            text = js.read_text(encoding="utf-8")
            for name in re.findall(r"invoke\(\s*['\"]([a-z_]+)['\"]", text):
                asked.setdefault(name, set()).add(str(js.relative_to(ROOT)))
        self.assertTrue(asked, "ツールの desktop.js が見つからない")
        missing = {name: sorted(where) for name, where in asked.items() if name not in handled}
        self.assertEqual(missing, {}, "外枠(services.rs)が受けない頼みごと")

    def test_入口の画面が呼ぶ命令は外枠に登録されている(self) -> None:
        main = rust("main.rs")
        for js in ("shell.js", "settings.js"):
            text = (ROOT / "portal" / "static" / "js" / js).read_text(encoding="utf-8")
            for name in re.findall(r"invoke\(\s*['\"]([a-z_]+)['\"]", text):
                with self.subTest(js=js, name=name):
                    self.assertRegex(main, rf"(services|drops)::{name}\b")
        # 埋め込みの台本は、ツールの画面の頼みごとを tool_invoke / tool_invoke_raw で取り次ぐ
        embed = (ROOT / "portal" / "static" / "js" / "embed.js").read_text(encoding="utf-8")
        handled = set(re.findall(r'^\s*"([a-z_]+)" =>', rust("services.rs"), re.M))
        for name in re.findall(r"invoke\(\s*['\"]([a-z_]+)['\"]", embed):
            with self.subTest(js="embed.js", name=name):
                self.assertTrue(name in handled or re.search(rf"services::{name}\b", main), name)

    def test_埋め込みの台本の差し込み口(self) -> None:
        embed = (ROOT / "portal" / "static" / "js" / "embed.js").read_text(encoding="utf-8")
        shell_rs = rust("shell.rs")
        for placeholder in ('"__ALLTOOLS_TOOL__"', '"__ALLTOOLS_SHELL__"'):
            self.assertIn(placeholder, embed)
            self.assertIn(placeholder.replace('"', '\\"'), shell_rs)


class VersionTests(unittest.TestCase):
    def test_版は3か所で揃っている(self) -> None:
        """config/app.json・Cargo.toml・tauri.conf.json。食い違うと exe の版がずれる。"""
        app = json.loads((ROOT / "config" / "app.json").read_text(encoding="utf-8"))["version"]
        cargo = (ROOT / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8")
        conf = json.loads((ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
        self.assertEqual(re.search(r'^version = "([^"]+)"', cargo, re.M).group(1), app)
        self.assertEqual(conf["version"], app)

    def test_exeの名前(self) -> None:
        cargo = (ROOT / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8")
        self.assertRegex(cargo, r'(?m)^name = "AllTools"$')
        self.assertIn("AllTools.exe", instance_guard.DESKTOP_EXES)
        self.assertIn("統合ツール.exe", instance_guard.DESKTOP_EXES)


class InstanceNameTests(unittest.TestCase):
    """ブラウザ版(Python)とデスクトップ版(Rust)が同じ名前の錠を見る。"""

    def test_錠の名前の作り方はPythonとRustで揃っている(self) -> None:
        source = rust("instance.rs")
        for suffix in ("instance", "desktop", "browser"):
            self.assertIn(f'format!("{{base}}.{suffix}")', source)
            self.assertTrue(instance_guard.names()[suffix if suffix == "instance" else suffix]
                            .endswith(f".{suffix}"))
        self.assertIn('format!("Local\\\\{name}")', source)
        self.assertIn('"._-"', source)
        self.assertEqual(instance_guard.base_name(), "nlm.all-tools")
        self.assertIn('"nlm.all-tools"', source)



class DialogTests(unittest.TestCase):
    """画面の confirm は**ブラウザのもの(答えを待つ)**のまま。

    tauri-plugin-dialog は window.confirm を答えを待たない版(Promise を返す)に置き換える。
    `if (!confirm("消しますか?")) return;` が訊かずに進む(Windows では各ツールの画面にも入る)。
    外枠の native_dialogs.js が dialog より先に走って、ブラウザのものを固定する。
    """

    def test_ブラウザの確認ダイアログを_dialogより先に固定する(self) -> None:
        main = rust("main.rs")
        guard = main.index('include_str!("native_dialogs.js")')
        self.assertIn("js_init_script_on_all_frames", main[main.rindex(".plugin(", 0, guard):guard + 40])
        self.assertLess(guard, main.index("tauri_plugin_dialog::init()"))
        script = (ROOT / "src-tauri" / "src" / "native_dialogs.js").read_text(encoding="utf-8")
        for name in ("alert", "confirm", "prompt"):
            self.assertIn(f'"{name}"', script)
        self.assertIn("writable: false", script)

    def test_起動確認がどの画面の確認ダイアログも見る(self) -> None:
        self.assertIn("確認ダイアログ", (ROOT / "portal" / "static" / "js" / "embed.js").read_text(encoding="utf-8"))
        self.assertIn("dialogsKind()", (ROOT / "portal" / "static" / "js" / "shell.js").read_text(encoding="utf-8"))
        self.assertIn("確認ダイアログ", (ROOT / "scripts" / "desktop_smoke.py").read_text(encoding="utf-8"))



class ConsoleEncodingTests(unittest.TestCase):
    """Windows のコマンドの日本語の出力(Shift-JIS)で、ツールが起動できなくならない。

    tasklist はそのプロセスが無いとき「情報: 指定された条件に一致するタスクは…」と返す。
    外枠が Python を UTF-8 モード(`-X utf8`)で起こしていたため、これを UTF-8 で読もうとして
    落ち、日報が起動できなかった(前の起動の印が残っていたとき)。
    """

    FILES = ("portal/browser_tools.py", "tools/nippou/launch_guard.py", "tools/kanban/launch_guard.py",
             "tools/calendar/launch_guard.py", "tools/inspection/launch_guard.py")

    def test_ツールのPythonをUTF8モードで起こさない(self) -> None:
        bridge = rust("bridge.rs")
        self.assertNotIn('.arg("utf8")', bridge)
        self.assertIn('.env_remove("PYTHONUTF8")', bridge)

    def test_tasklistとwmicはコンソールの文字コードで_読めない字は置き換えて読む(self) -> None:
        for name in self.FILES:
            source = (ROOT / name).read_text(encoding="utf-8")
            calls = re.findall(r"subprocess\.run\((?:[^()]|\([^()]*\))*\)", source, re.S)
            calls = [c for c in calls if "tasklist" in c or "wmic" in c]
            with self.subTest(file=name):
                self.assertTrue(calls)
                for call in calls:
                    self.assertIn("encoding=CONSOLE_ENCODING", call)
                    self.assertIn('errors="replace"', call)
                self.assertIn('CONSOLE_ENCODING = "oem" if os.name == "nt" else None', source)
                self.assertNotRegex(source, r"\bout\.stdout\b(?! or)")



class UploadTests(unittest.TestCase):
    """ファイルを送る(FormData)は、embed.js が中身を読んでから1つのバイト列にして送る。

    WebView2 は外枠の宛先へ送る FormData のファイル(ディスクのファイルを指す部分)を
    外枠へ渡さず、看板の「Access の最新で表の中身を入れ替える」が「ファイルが小さすぎます」
    になっていた(ブラウザ版では起きない)。
    """

    def test_ファイル入りのFormDataを組み立て直して送る(self) -> None:
        embed = (ROOT / "portal" / "static" / "js" / "embed.js").read_text(encoding="utf-8")
        self.assertIn("window.fetch = function", embed)
        self.assertIn("arrayBuffer()", embed)
        self.assertIn("multipart/form-data; boundary=", embed)


class DropTests(unittest.TestCase):
    """ファイルのドラッグ&ドロップは外枠が OS の仕組みで受け、落ちた場所のツールの画面へ渡す。

    WebView2 に任せると、大きなタブの枠(iframe)の中の画面へ落としても届かなかった
    (現場の Windows で「ドラッグ&ドロップが効かない」)。
    """

    def test_大きなタブの窓は落下をTauriで受ける(self) -> None:
        main = (ROOT / "src-tauri" / "src" / "main.rs").read_text(encoding="utf-8")
        window = main[main.index('WebviewWindowBuilder::new(app, "main"'):]
        window = window[:window.index(".build()?")]
        self.assertNotIn(".disable_drag_drop_handler()", window, "大きなタブの窓で落下を受けていない")
        self.assertIn("WindowEvent::DragDrop", main)
        self.assertIn("drops::shell_dropped_file", main)

    def test_画面へ渡すのは位置と名前だけで中身は落とされた枠の頼みにだけ(self) -> None:
        drops = (ROOT / "src-tauri" / "src" / "drops.rs").read_text(encoding="utf-8")
        self.assertIn("window.__shell.fileDrag", drops)
        shell = (ROOT / "portal" / "static" / "js" / "shell.js").read_text(encoding="utf-8")
        self.assertIn("fileDrag(info)", shell)
        self.assertIn("drag.drop.id !== id || drag.drop.seq !== data.seq", shell)
        self.assertIn("devicePixelRatio", shell)
        embed = (ROOT / "portal" / "static" / "js" / "embed.js").read_text(encoding="utf-8")
        for needle in ('d.type === "alltools:drag"', 'fire("drop"', "over.defaultPrevented",
                       "alltools:dropped-file", "alltools:drop-refused"):
            self.assertIn(needle, embed)


class ThemeDefaultTests(unittest.TestCase):
    """選んでいなければ、どのツールもライト(以前はツールごとに 自動 / ダーク とバラバラだった)。"""

    def text(self, *parts: str) -> str:
        return ROOT.joinpath(*parts).read_text(encoding="utf-8")

    def test_入口と説明書はライト(self) -> None:
        self.assertIn('data-theme="light"', self.text("portal", "templates", "shell.html").split("<head>")[0])
        for page in (ROOT / "portal" / "static" / "manual").glob("*.html"):
            with self.subTest(page=page.name):
                self.assertIn('<html lang="ja" data-theme="light">', page.read_text(encoding="utf-8"))

    def test_日報はライト(self) -> None:
        base = self.text("tools", "nippou", "app", "templates", "base.html")
        self.assertIn('<html lang="ja" data-skin="neon" data-theme="light">', base)
        self.assertIn('return saved() || DEFAULT;', self.text("tools", "nippou", "app", "static", "js", "theme.js"))
        self.assertIn("const DEFAULT = 'light'", self.text("tools", "nippou", "app", "static", "vc", "coil", "coil-theme.js"))

    def test_看板はライト(self) -> None:
        self.assertIn("localStorage.getItem('kanban.theme') || 'light'",
                      self.text("tools", "kanban", "app", "templates", "base.html"))
        self.assertIn("const DEFAULT = 'light';", self.text("tools", "kanban", "app", "static", "js", "theme.js"))

    def test_カレンダーと点検表はライト(self) -> None:
        self.assertIn("value = get(KEY_THEME, THEME_LIGHT)",
                      self.text("tools", "calendar", "calendar_app", "settings.py"))
        self.assertIn("return False if value is None else bool(value)",
                      self.text("tools", "inspection", "app", "services", "settings_service.py"))


class LocationViewTests(unittest.TestCase):
    """大設定の「共有の DB の置き場所」: 反映しているか、打ちかけかを見分けられる。"""

    def test_反映済みと打ちかけで見え方が変わる(self) -> None:
        js = (ROOT / "portal" / "static" / "js" / "settings.js").read_text(encoding="utf-8")
        css = (ROOT / "portal" / "static" / "css" / "shell.css").read_text(encoding="utf-8")
        self.assertIn("function paintLocState", js)
        self.assertIn("まだ反映していません", js)
        self.assertIn("反映しています", js)
        self.assertIn(".pathform input.is-edited", css)
        self.assertIn('id="loc-state"', (ROOT / "portal" / "templates" / "settings.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
