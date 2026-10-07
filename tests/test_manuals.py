"""操作説明書(`portal/static/manual/`)と、大きなタブの版の表示

- 入口(統合ツール・大設定)と、載せている全ツールの説明書がある。一覧から辿れる
- 説明書の版は、そのツールの `config/app.json` の版と同じ(版を上げたら説明書も見直す)
- **外のサイトのものを使わない**(ラインPCは社外に出られない)。画像は手元にあり、重すぎない
- 大きなタブの画面に「説明書」ボタンと、説明書を重ねて開く枠がある
"""
from __future__ import annotations

import json
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

import tests  # noqa: F401  (一時フォルダへ向ける)

from portal import catalog as catalog_mod
from portal import web

ROOT = Path(__file__).resolve().parent.parent
MANUAL = ROOT / "portal" / "static" / "manual"
MAX_IMAGE = 250 * 1024
MAX_TOTAL = 12 * 1024 * 1024


class _Refs(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.refs: list[tuple[str, str, dict]] = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        for key in ("src", "href"):
            if d.get(key):
                self.refs.append((tag, d[key], d))


def refs(path: Path) -> list[tuple[str, str, dict]]:
    parser = _Refs()
    parser.feed(path.read_text(encoding="utf-8"))
    return parser.refs


def pages() -> dict[str, tuple[Path, str]]:
    """説明書の名前 → (ファイル, 書いてあるべき版)。"""
    catalog = catalog_mod.load()
    app = json.loads((ROOT / "config" / "app.json").read_text(encoding="utf-8"))
    out = {"index": (MANUAL / "index.html", app["version"]), "portal": (MANUAL / "portal.html", app["version"])}
    for tool in catalog.tools:
        out[tool.id] = (MANUAL / f"{tool.id}.html", tool.version())
    return out


class ManualTests(unittest.TestCase):
    def test_入口と全ツールの説明書があり_版が合う(self) -> None:
        for name, (path, version) in pages().items():
            with self.subTest(name=name):
                self.assertTrue(path.is_file(), f"{path} がありません")
                text = path.read_text(encoding="utf-8")
                self.assertIn(f"VER{version}", text, "説明書の版がツールの版と違います")
                self.assertIn('href="manual.css"', text)
                self.assertRegex(text, r'<html lang="ja" data-theme="light">')

    def test_一覧と上の並びから全部の説明書へ行ける(self) -> None:
        for name, (path, _) in pages().items():
            with self.subTest(name=name):
                linked = {href for tag, href, _ in refs(path) if tag == "a"}
                for other in pages():
                    self.assertIn(f"{other}.html", linked)

    def test_外のサイトのものを使わない_画像は手元にあって重すぎない(self) -> None:
        total = 0
        for name, (path, _) in pages().items():
            for tag, ref, attrs in refs(path):
                with self.subTest(name=name, ref=ref):
                    self.assertNotRegex(ref, r"^(https?:)?//", "外のサイトを読まない")
                    if tag == "img":
                        target = (path.parent / ref.split("?")[0]).resolve()
                        self.assertTrue(target.is_file(), f"画像がありません: {ref}")
                        self.assertTrue(attrs.get("alt"), "画像に説明(alt)が要る")
                        self.assertLessEqual(target.stat().st_size, MAX_IMAGE, ref)
                    elif tag in ("link", "script"):
                        self.assertTrue((path.parent / ref).is_file(), ref)
        for image in (MANUAL / "img").glob("*"):
            total += image.stat().st_size
        self.assertLessEqual(total, MAX_TOTAL)

    def test_ツールの説明書には画面写真がある(self) -> None:
        for tool in catalog_mod.load().tools:
            with self.subTest(tool=tool.id):
                images = [r for t, r, _ in refs(MANUAL / f"{tool.id}.html") if t == "img"]
                self.assertGreaterEqual(len(images), 3)

    def test_使われていない画像を残さない(self) -> None:
        used = set()
        for _, (path, _) in pages().items():
            used |= {Path(r.split("?")[0]).name for t, r, _ in refs(path) if t == "img"}
        left = {p.name for p in (MANUAL / "img").glob("*")} - used
        self.assertEqual(left, set())


class ShellTests(unittest.TestCase):
    def test_大きなタブに版を渡し_説明書を開ける(self) -> None:
        client = web.create_app(token="t", bridge=True).test_client()
        text = client.get("/", base_url="http://app.localhost").get_data(as_text=True)
        shell = json.loads(re.search(r"window\.SHELL = (\{.*?\});</script>", text).group(1))
        for tool in shell["tools"]:
            with self.subTest(tool=tool["id"]):
                self.assertEqual(tool["version"], catalog_mod.load().by_id(tool["id"]).version())
                self.assertRegex(tool["version"], r"^\d+\.\d+\.\d+$")
        self.assertIn('id="help"', text)
        self.assertIn('id="manual-frame"', text)
        js = (ROOT / "portal" / "static" / "js" / "shell.js").read_text(encoding="utf-8")
        self.assertIn("/static/manual/", js)
        self.assertIn('"F1"', js)
        self.assertIn('"F1"', (ROOT / "portal" / "static" / "js" / "embed.js").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
