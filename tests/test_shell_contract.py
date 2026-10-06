"""外枠(Rust/Tauri、`src-tauri/`)と、入口・各ツール(Python・JS)との取り決め

言語をまたぐ取り決めは、片方だけ直すと**黙って**食い違う(既定の値で動いてしまう・
頼みごとが届かない)。ここでソースを突き合わせて見つける。
"""
from __future__ import annotations

import json
import re
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
        self.assertEqual(app_config.origin_of("kanban"), "kanban://localhost")   # ここ(Linux)

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
                    self.assertRegex(main, rf"services::{name}\b")
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


if __name__ == "__main__":
    unittest.main()
