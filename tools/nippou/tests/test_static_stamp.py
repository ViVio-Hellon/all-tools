"""静的ファイルの印 ── 入れ替えたら必ず取り直させる

【なにが起きたか】
静的ファイルには `Cache-Control: public, max-age=604800, immutable` を
付けています。`immutable` は「このURLの中身は絶対に変わらない」という
宣言なので、ブラウザは**1週間、確認すらしません。**

その前提は「中身が変わればURLが変わる」ことでした。ところがURLに付けて
いたのは `config/app.json` の版で、**上げ忘れると変わりません。**
画面の作りを直したのに版が `3.0.0` のままだったので、利用者のブラウザは
1週間ぶん古い JS を出し続け、新しいHTMLと噛み合わず
**どの画面もボタンが全部効かない**状態になりました。

人が覚えていることに頼る作りだったのが原因なので、いまは**中身から印を
作ります。** ここが守っているのはその性質です。
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HAS_FLASK, HEADERS, SKIP_REASON, WebTestCase  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class StampTests(unittest.TestCase):
    """印そのもの。Flask のアプリを組み立てずに確かめる。"""

    def stamp(self, folder=None, version: str = "9.9.9") -> str:
        from app import static_stamp

        return static_stamp(str(folder or STATIC), version)

    def test_版が頭に付く(self) -> None:
        """どの版が動いているかをURLから読めるようにする。"""
        self.assertTrue(self.stamp().startswith("9.9.9-"))
        self.assertRegex(self.stamp(), r"^9\.9\.9-[0-9a-f]{8}$")

    def test_同じ中身なら同じ印(self) -> None:
        """入れ直しただけで控えを捨てさせない。"""
        self.assertEqual(self.stamp(), self.stamp())

    def test_1文字変えれば印が変わる(self) -> None:
        """**これが本体。** JS を直せばURLが変わり、必ず取り直される。"""
        target = STATIC / "js" / "nav.js"
        before = self.stamp()
        original = target.read_bytes()
        try:
            target.write_bytes(original + b"\n// ")
            self.assertNotEqual(self.stamp(), before)
        finally:
            target.write_bytes(original)
        # 戻せば印も戻る(中身を見ているので、更新時刻には左右されない)
        self.assertEqual(self.stamp(), before)

    def test_ファイルが増えても印が変わる(self) -> None:
        added = STATIC / "js" / "__stamp_test__.js"
        before = self.stamp()
        try:
            added.write_text("// テスト用\n", encoding="utf-8")
            self.assertNotEqual(self.stamp(), before)
        finally:
            added.unlink(missing_ok=True)

    def test_読めなければ版だけを返す(self) -> None:
        """印が付かないより、粗くても付いたほうがよい。"""
        self.assertEqual(self.stamp(ROOT / "存在しないフォルダ"), "9.9.9")
        from app import static_stamp

        self.assertEqual(static_stamp(None, "9.9.9"), "9.9.9")


class ServedStampTests(WebTestCase):
    """実際に配られるHTMLとヘッダー。"""

    def test_HTMLのURLに印が入っている(self) -> None:
        html = self.client.get("/", headers=HEADERS).get_data(as_text=True)
        urls = re.findall(r'(?:href|src)="(/sv/[^"]+)"', html)
        self.assertTrue(urls, "静的ファイルを1つも読んでいません")
        for url in urls:
            with self.subTest(url=url):
                self.assertRegex(url, r"^/sv/\d+\.\d+\.\d+-[0-9a-f]{8}/")

    def test_data_viewにも印が入る(self) -> None:
        """画面ごとのモジュールも同じ道で配られる。

        ここが素のURLのままだと、**画面のモジュールだけが古いまま**に
        なる ── まさにそれで全画面のボタンが死んだ。
        """
        html = self.client.get("/", headers=HEADERS).get_data(as_text=True)
        views = re.findall(r'data-view="([^"]+)"', html)
        self.assertTrue(views, "data-view が1つもありません")
        for url in views:
            with self.subTest(url=url):
                self.assertRegex(url, r"^/sv/\d+\.\d+\.\d+-[0-9a-f]{8}/")

    def test_版は道の一部に入る(self) -> None:
        """**`?v=` では届きませんでした。**

        版が付くのは入口のURLだけで、その中の
        `import { api } from "../api.js"` には付きません。一度これを
        `no-cache` で直そうとしましたが**直りませんでした** ── それ以前に
        配った控えには `immutable` が付いており、`immutable` は「期限まで
        問い合わせるな」という意味だからです。

        版を道に入れると、相対の import が**同じ版へ解決されます** ──
        一式まるごと新しい道になるので、「入口は新しいのに中身が古い」が
        原理的に起きません。
        """
        stamp = self.app.config["STATIC_STAMP"]
        res = self.client.get(f"/sv/{stamp}/js/views/settings.js",
                              headers=HEADERS)
        self.assertEqual(res.status_code, 200)
        # 中身が変わればURLが変わる。**だから長く控えさせてよい**
        self.assertIn("immutable", res.headers["Cache-Control"])

    def test_相対importも同じ版の道になる(self) -> None:
        """`../api.js` が `/sv/<印>/js/api.js` へ解決されること。

        ブラウザの解決をここで真似ます ── これが崩れると、また
        `api.send is not a function` が戻ってきます。
        """
        from urllib.parse import urljoin

        stamp = self.app.config["STATIC_STAMP"]
        entry = f"/sv/{stamp}/js/views/settings.js"
        resolved = urljoin(entry, "../api.js")
        self.assertEqual(resolved, f"/sv/{stamp}/js/api.js")
        res = self.client.get(resolved, headers=HEADERS)
        self.assertEqual(res.status_code, 200)
        # 画面が呼ぶものが、本当にその版の中に入っている
        self.assertIn("send:", res.get_data(as_text=True))

    def test_昔のURLも配り続ける(self) -> None:
        """**すでに配った控えを持っている画面**が引きに来ます。

        ここで断ると、入れ替えた瞬間にそれらの画面が真っ白になります。
        配りますが、`no-cache` にして次からは必ず聞かせます。
        """
        res = self.client.get("/static/js/api.js", headers=HEADERS)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers["Cache-Control"], "no-cache")

    def test_画面のHTMLは控えさせない(self) -> None:
        """入れ替えたのに古いHTMLが出る、を起こさない。"""
        res = self.client.get("/", headers=HEADERS)
        self.assertEqual(res.headers["Cache-Control"], "no-store")


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class VersionTests(unittest.TestCase):
    def test_版は設定ファイルから来る(self) -> None:
        """表示用の版は `config/app.json` ただ1つが出どころ。"""
        data = json.loads((ROOT / "config" / "app.json").read_text(encoding="utf-8"))
        self.assertRegex(data["version"], r"^\d+\.\d+\.\d+$")


class VersionDisplayTests(WebTestCase):
    """どの版が動いているかを、画面から読めること。

    **版だけでは足りません。** HTML は毎回サーバが作り直すのでいつでも
    新しく見えますが、JS はブラウザが控えているかもしれない ──
    「入れ替えたのに古いまま」はそこで起きます。だから印(build)まで出し、
    画面(JS)の印と突き合わせられるようにします。
    """

    def html(self, path: str = "/") -> str:
        return self.client.get(path, headers=HEADERS).get_data(as_text=True)

    def test_帯に版が出る(self) -> None:
        """どの画面からでも、見れば答えられる。"""
        for path in ("/", "/gw", "/graph", "/records", "/settings"):
            with self.subTest(path=path):
                html = self.html(path)
                self.assertIn('id="app-version"', html)
                # 版の頭の数は上がる(3 → 4)。**決め打ちしない**
                self.assertIn(f"VER{self.app.config['VERSION']}", html)

    def test_帯のtitleに印が入る(self) -> None:
        """帯に8文字の英数字を並べても読めないが、聞かれたときには要る。"""
        stamp = self.app.config["STATIC_STAMP"]
        self.assertIn(f"build {stamp}", self.html())

    def test_画面に印を渡している(self) -> None:
        """`window.APP.stamp` を JS が自分の URL と突き合わせる。"""
        stamp = self.app.config["STATIC_STAMP"]
        html = self.html()
        self.assertRegex(html, r"stamp:\s*" + re.escape(f'"{stamp}"'))
        self.assertRegex(html, r"version:\s*\"VER" + re.escape(self.app.config["VERSION"]))

    def test_設定に印が2つ並ぶ(self) -> None:
        html = self.html("/settings")
        self.assertIn('id="server-stamp"', html)
        self.assertIn('id="client-stamp"', html)
        self.assertIn('id="stamp-verdict"', html)
        self.assertIn(self.app.config["STATIC_STAMP"], html)

    def test_健康確認でも印が読める(self) -> None:
        """画面を開かずに確かめられる(電話で聞かれたとき)。"""
        body = self.client.get("/api/health", headers=HEADERS).get_json()
        self.assertEqual(body["stamp"], self.app.config["STATIC_STAMP"])
        self.assertEqual(body["version"], self.app.config["VERSION"])

    def test_画面が自分の印を拾う仕掛けがある(self) -> None:
        """`app.js` は `<script src="app.js?v=印">` から読まれるので、
        自分の URL に印が入っている(`import.meta.url`)。"""
        text = (ROOT / "app" / "static" / "js" / "app.js").read_text(encoding="utf-8")
        self.assertIn("import.meta.url", text)
        self.assertIn("window.APP.stamp", text)
        # **入口を import させない**ので、window 経由で渡す
        self.assertIn("window.APP.runningStamp", text)


if __name__ == "__main__":
    unittest.main()
