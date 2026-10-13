"""参照設定: フォルダは見えるのにファイルだけ見つからない ── 一瞬の「無い」(v4.31.1)

    実際に見る道 \\\\nlmsrvngy03\\Read\\【New】仕掛\\台帳  このフォルダはあります
    仕掛ロット SIKALOT.sqlite3  このファイルがありません
    (パスワード認証したら)読めます ── パスは変えてません

仕掛の SIKALOT / SIKAHIKI / SIKAODR は毎時00分にまとめて作り直されていて、入れ替えのあいだの
一瞬だけファイルが無い。その瞬間に画面を開くと「ありません」と出て、少しあとに開き直すと
「読めます」になる。**フォルダが見えてファイルだけ無いときは「いま見つかりません(時刻)」と
出し、画面が数秒後に確かめ直す。**
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.presenters import settings as presenter  # noqa: E402
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402


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

    def test_フォルダごと無ければ確かめ直さない(self) -> None:
        v = self.view(self.dir / "無いフォルダ" / "SIKALOT.sqlite3")
        self.assertFalse(v.exists)
        self.assertFalse(v.recheck)

    def test_あれば確かめ直さない(self) -> None:
        path = self.dir / "SIKALOT.sqlite3"
        import sqlite3
        sqlite3.connect(str(path)).close()
        v = self.view(path)
        self.assertTrue(v.exists)
        self.assertFalse(v.recheck)


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
