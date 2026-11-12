"""参照設定: フォルダは見えるのにファイルだけ見つからない ── 一瞬の「無い」(v4.31.1)

    実際に見る道 \\\\nlmsrvngy03\\Read\\【New】仕掛\\台帳  このフォルダはあります
    仕掛ロット SIKALOT.sqlite3  このファイルがありません
    (パスワード認証したら)読めます ── パスは変えてません

仕掛の SIKALOT / SIKAHIKI / SIKAODR は毎時00分にまとめて作り直されていて、入れ替えのあいだの
一瞬だけファイルが無い。その瞬間に画面を開くと「ありません」と出て、少しあとに開き直すと
「読めます」になる。**フォルダが見えてファイルだけ無いときは「いま見つかりません(時刻)」と
出し、画面が数秒後に確かめ直す。**

**確かめられないときも「このファイルがありません」とは出さない**(v4.44.17 の現場の声)

    設定・管理者:保存と出力: 起動後毎回第1パスが「このファイルがありません」となる。読み直すと解消はする

「無い」と言うのは、置き場所に届いて一覧が取れ、そこに無いときだけ(`db.connection.file_state`)。
届かない・権限・一覧には出ているのに開けない(起動して最初に共有へ触れたときなど)は「いま確かめられません
(時刻)」と出し、理由は吹き出しに。画面が数秒後に確かめ直す(フォルダごと見えないのも、Windows では
届かない共有と見分けられないので同じ)。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.presenters import settings as presenter  # noqa: E402
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402


@contextmanager
def stat_refused(path: Path, when=lambda: True):
    """そのファイルだけ **stat が断られる**(権限・共有の不調)。`when()` が真のあいだだけ。

    一覧(親のフォルダ)には出ているので、「無い」とは確かめられない。
    """
    real = os.stat
    target = os.path.normcase(os.path.abspath(path))

    def fake(name, *args, **kwargs):
        if (not isinstance(name, int) and when()
                and os.path.normcase(os.path.abspath(os.fspath(name))) == target):
            raise PermissionError(13, "アクセスが拒否されました", os.fspath(name))
        return real(name, *args, **kwargs)

    with patch("os.stat", fake):
        yield


@contextmanager
def share_unreachable(folder: Path):
    """そのフォルダが**届かない共有**(サーバが落ちている・つながっていない)。

    Windows では届かない共有の一覧も断られる(WinError 53「ネットワーク パスが見つかりません」)。
    フォルダが本当に無いのとは違い、上のフォルダからも確かめられない。フォルダを作ると届く。
    """
    real = os.listdir
    target = os.path.normcase(os.path.abspath(folder))

    def fake(name=".", *args, **kwargs):
        if (os.path.normcase(os.path.abspath(os.fspath(name))) == target
                and not os.path.isdir(target)):
            raise OSError(53, "ネットワーク パスが見つかりません", os.fspath(name))
        return real(name, *args, **kwargs)

    with patch("os.listdir", fake):
        yield


def _file_item(page: str, name: str) -> str:
    """設定画面の HTML から、そのファイルの行(`<li class="path__file">`)だけを切り出す。"""
    for chunk in page.split('<li class="path__file">')[1:]:
        item = chunk.split("</li>", 1)[0]
        if name in item:
            return item
    raise AssertionError(f"{name} の行がありません")


class FileViewTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def view(self, path: Path):
        return presenter._file_view("wip_lot", "仕掛ロット", lambda: path)

    def test_フォルダが見えてファイルだけ無ければ確かめ直す(self) -> None:
        v = self.view(self.dir / "SIKALOT.sqlite3")
        self.assertFalse(v.exists)
        self.assertTrue(v.recheck)
        self.assertRegex(v.checked_at, r"^\d\d:\d\d:\d\d$")

    def test_フォルダごと見えなければ無いとは言わず確かめ直す(self) -> None:
        # Windows では届かない共有もフォルダが無いのと同じ答え(FileNotFoundError)になる。
        # 「このファイルがありません」とは言えない
        with share_unreachable(self.dir / "無いフォルダ"):
            v = self.view(self.dir / "無いフォルダ" / "SIKALOT.sqlite3")
        self.assertFalse(v.exists)
        self.assertTrue(v.unreadable)
        self.assertTrue(v.recheck)
        self.assertIn("無いフォルダ", v.error)

    def test_確かめられなければ無いとは言わず確かめ直す(self) -> None:
        import sqlite3
        path = self.dir / "SIKALOT.sqlite3"
        conn = sqlite3.connect(str(path))
        conn.execute("VACUUM")
        conn.close()
        with stat_refused(path):
            v = self.view(path)
        self.assertFalse(v.exists)
        self.assertTrue(v.unreadable)
        self.assertTrue(v.recheck)
        self.assertIn("共有フォルダを読めませんでした", v.error)
        self.assertRegex(v.checked_at, r"^\d\d:\d\d:\d\d$")

    def test_確かめられないのを困りごとでありませんと数えない(self) -> None:
        with share_unreachable(self.dir / "無いフォルダ"):
            v = self.view(self.dir / "無いフォルダ" / "SIKALOT.sqlite3")
        texts = presenter._problems([], [v])
        self.assertEqual(len(texts), 1, texts)
        self.assertNotIn("ありません", texts[0])
        self.assertIn("確かめられません", texts[0])

    def test_置き場所のフォルダが本当に無ければ_このファイルがありません(self) -> None:
        # 上のフォルダは一覧できて、そこに置き場所のフォルダが無い(この端末にまだ作っていない)
        v = self.view(self.dir / "無いフォルダ" / "SIKALOT.sqlite3")
        self.assertFalse(v.exists)
        self.assertFalse(v.unreadable)

    def test_あれば確かめ直さない(self) -> None:
        path = self.dir / "SIKALOT.sqlite3"
        import sqlite3
        sqlite3.connect(str(path)).close()
        v = self.view(path)
        self.assertTrue(v.exists)
        self.assertFalse(v.recheck)
        self.assertFalse(v.unreadable)


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class ScreenTests(WebTestCase):
    def test_画面はいま見つかりませんと出して確かめ直す印を付ける(self) -> None:
        from nippou import user_settings
        from nippou.config import KEY_WIP_DIR
        folder = self.tmp / "台帳"
        folder.mkdir()
        user_settings.save(KEY_WIP_DIR, str(folder))
        page = self.get("/settings").get_data(as_text=True)
        self.assertIn("いま見つかりません(", page)
        self.assertIn("data-recheck", page)
        state = self.get("/api/settings/state").get_json()
        lot = [f for f in state["files"] if f["path"].endswith("SIKALOT.sqlite3")]
        self.assertTrue(lot and lot[0]["recheck"], lot)

    def test_確かめられないときはこのファイルがありませんと出さない(self) -> None:
        import sqlite3
        from nippou import user_settings
        from nippou.config import KEY_WIP_DIR
        folder = self.tmp / "台帳"
        folder.mkdir()
        lot = folder / "SIKALOT.sqlite3"
        conn = sqlite3.connect(str(lot))
        conn.execute("VACUUM")
        conn.close()
        user_settings.save(KEY_WIP_DIR, str(folder))
        with stat_refused(lot):
            page = self.get("/settings").get_data(as_text=True)
            state = self.get("/api/settings/state").get_json()
        item = _file_item(page, "SIKALOT.sqlite3")
        self.assertNotIn("このファイルがありません", item)
        self.assertIn("いま確かめられません(", item)
        self.assertIn("data-recheck", item)
        self.assertIn("アクセスが拒否されました", item)      # 理由は吹き出しに
        found = [f for f in state["files"] if f["path"].endswith("SIKALOT.sqlite3")]
        self.assertTrue(found and found[0]["unreadable"] and found[0]["recheck"], found)
        self.assertFalse(found[0]["exists"])
        self.assertFalse(any("SIKALOT.sqlite3" in p and "ありません" in p
                             for p in state["problems"]), state["problems"])

    def test_マスタの一覧も確かめられないときはありませんと出さず_ログに残す(self) -> None:
        """「絶対に存在するはずなんですが初回はほぼ読まない。エラーなのにログにも残らない」(v4.49.2)"""
        import sqlite3
        from nippou import user_settings
        from nippou.config import KEY_WIP_DIR
        folder = self.tmp / "台帳"
        folder.mkdir()
        lot = folder / "SIKALOT.sqlite3"
        conn = sqlite3.connect(str(lot))
        conn.execute("VACUUM")
        conn.close()
        user_settings.save(KEY_WIP_DIR, str(folder))
        with stat_refused(lot), self.assertLogs("nippou", level="WARNING") as logs:
            page = self.get("/settings").get_data(as_text=True)
        rows = page[page.index('id="master-rows"'):]
        row = rows[rows.index("SIKALOT.sqlite3"):][:1500]
        self.assertIn("いま確かめられません(", row)
        self.assertIn("data-recheck", row)
        self.assertIn('id="master-recheck"', page)
        self.assertTrue(any("確かめられませんでした" in line for line in logs.output), logs.output)

    def test_参照パスの欄も確かめられないときはありませんと出さない(self) -> None:
        from nippou.db.connection import SharedUnreadable

        def refused(path, **_kw):
            raise SharedUnreadable("共有フォルダを読めませんでした: [WinError 5] アクセスが拒否されました")

        with patch("nippou.db.connection.file_state", side_effect=refused):
            views = presenter.path_views()
        live = [v for v in views if not v.off]
        self.assertTrue(live)
        self.assertTrue(all(v.unreadable and not v.exists for v in live))
        self.assertIn("WinError 5", live[0].error)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class BrowserRecheckTests(WebTestCase):
    """本物のブラウザで: 開いたあとにファイルが戻ると、開き直さなくても「読めます」になる。"""

    @classmethod
    def setUpClass(cls) -> None:
        from tests._browser import chromium_or_skip
        cls.pw, cls.browser = chromium_or_skip()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.pw.stop()

    def test_入れ替えが終わると読めますに戻る(self) -> None:
        import sqlite3
        from nippou import user_settings
        from nippou.config import KEY_WIP_DIR
        from tests.test_seq_entry import BrowserTests

        folder = self.tmp / "台帳"
        folder.mkdir()
        user_settings.save(KEY_WIP_DIR, str(folder))
        ctx = self.browser.new_context(viewport={"width": 1400, "height": 900})
        self.addCleanup(ctx.close)
        ctx.route("http://app.test/**", lambda route: BrowserTests._forward(self, route))
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto("http://app.test/settings", wait_until="load")
        badge = f'.path__file:has(code:text-matches("SIKALOT")) .badge + .badge'
        page.wait_for_selector(badge, state="attached")
        self.assertIn("いま見つかりません", page.text_content(badge))
        # 入れ替えが終わった(ファイルが戻った)
        conn = sqlite3.connect(str(folder / "SIKALOT.sqlite3"))
        conn.execute("CREATE TABLE 仕掛 (LOT TEXT)")
        conn.commit()
        conn.close()
        page.wait_for_function(
            """() => [...document.querySelectorAll('.path__file')]
                   .find((li) => li.querySelector('code')?.textContent.includes('SIKALOT'))
                   ?.querySelector('.badge + .badge')?.textContent.includes('読めます')""",
            timeout=12000)
        self.assertEqual(errors, [])

    def test_確かめ直したときに確かめられなければこのファイルがありませんと出さない(self) -> None:
        """画面が確かめ直して描き直すとき(`settings.js` の `repaintFiles`)も、確かめられないのを「無い」と出さない。"""
        import sqlite3
        from nippou import user_settings
        from nippou.config import KEY_WIP_DIR
        from tests.test_seq_entry import BrowserTests

        folder = self.tmp / "台帳"
        folder.mkdir()
        lot = folder / "SIKALOT.sqlite3"
        conn = sqlite3.connect(str(lot))
        conn.execute("CREATE TABLE 仕掛 (LOT TEXT)")
        conn.commit()
        conn.close()
        # 引当は無い(フォルダは見えてファイルだけ無い)── 画面が数秒後に確かめ直す
        user_settings.save(KEY_WIP_DIR, str(folder))
        refused = {"on": False}
        ctx = self.browser.new_context(viewport={"width": 1400, "height": 900})
        self.addCleanup(ctx.close)
        ctx.route("http://app.test/**", lambda route: BrowserTests._forward(self, route))
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        with stat_refused(lot, when=lambda: refused["on"]):
            page.goto("http://app.test/settings", wait_until="load")
            badge = '.path__file:has(code:text-matches("SIKALOT")) .badge + .badge'
            page.wait_for_selector(badge, state="attached")
            self.assertIn("読めます", page.text_content(badge))
            refused["on"] = True                  # 開いたあとに確かめられなくなった
            page.wait_for_function(
                """() => [...document.querySelectorAll('.path__file')]
                       .find((li) => li.querySelector('code')?.textContent.includes('SIKALOT'))
                       ?.querySelector('.badge + .badge')?.textContent.includes('いま確かめられません')""",
                timeout=12000)
            text = page.text_content(badge)
            title = page.get_attribute(badge, "title") or ""
        self.assertNotIn("このファイルがありません", text)
        self.assertIn("アクセスが拒否されました", title)
        self.assertEqual(errors, [])
