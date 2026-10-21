"""紙の窓・早見表・フォルダを選ぶ: **ブラウザ版はブラウザ、デスクトップ版は外枠に頼む**(段階1)

前は `entry.js` / `records.js` / `vc.js` / `desktop.js` / `settings.js` の字を探して、
「外枠へ頼む行・`save: "1"` があること」を見ていました(`test_bridge` の `WiringTests`・
`test_print_files` の `test_日報入力の印刷ボタンは残すと頼む`)。ここでは本当に押します:

    - 日報入力の「印刷」は、この直ぜんぶの紙を**出力先へも残すよう頼む**(`save=1`)。
      記録を見るの「この直ぜんぶの紙」は**見るだけ**(`save` を付けない)
    - ブラウザ版は新しいタブ(`window.open`)。デスクトップ版(`window.__TAURI__` がある)は
      外枠に別の窓を頼み(`open_window`)、タブは開かない
    - VC 早見表は名前つきの窓(`vc-quick`。開いていれば前に出す)
    - 参照設定の「フォルダを選ぶ…」は OS の標準のダイアログ(`pick_path`)。選んだ道が欄に入る

外枠は試験の偽物です(頼まれたことを `window.__invoked` に積むだけ)。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._screen_browser import ScreenBrowserCase  # noqa: E402

#: 新しいタブを開いたことを覚える(本当には開かない)
OPEN_SPY = """
  window.__opened = [];
  window.open = function (url, name) { window.__opened.push({ url: String(url), name: name || "" }); return null; };
"""

#: デスクトップ版の外枠(偽物)。頼まれたことを覚え、フォルダを選べば決まった道を返す
FAKE_TAURI = r"""
  window.__invoked = [];
  window.__TAURI__ = { core: { invoke(cmd, args) {
    window.__invoked.push({ cmd, args });
    return Promise.resolve(cmd === "pick_path" ? "C:\\試験\\選んだフォルダ" : null);
  } } };
"""


def query(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


class PaperCase(ScreenBrowserCase):
    def setUp(self) -> None:
        super().setUp()
        self.ctx.add_init_script(OPEN_SPY)

    def opened(self, page) -> list[dict]:
        return page.evaluate("window.__opened")

    def invoked(self, page, cmd: str = "") -> list[dict]:
        return [c for c in page.evaluate("window.__invoked || []") if not cmd or c["cmd"] == cmd]


class BrowserEditionTests(PaperCase):
    def test_印刷は出力先へも残すよう頼み_記録を見るの紙は見るだけ(self) -> None:
        page = self.open_entry()
        self.assertFalse(page.evaluate("document.documentElement.classList.contains('is-desktop')"))
        page.click("#print-page")
        self.wait_until(page, lambda: self.opened(page), "紙のタブ")
        url = self.opened(page)[-1]["url"]
        self.assertTrue(url.startswith("/report/nippou?"), url)
        q = query(url)
        self.assertEqual((q.get("page"), q.get("save"), q.get("t")), ("all", "1", "test-token"))
        self.assertTrue(q.get("report_date") and q.get("shift"), q)

        page.click('nav.rail a[href="/records"]')
        page.wait_for_function("location.pathname === '/records'", timeout=8000)
        page.wait_for_selector("#print-all")
        page.wait_for_timeout(400)
        page.click("#print-all")
        self.wait_until(page, lambda: len(self.opened(page)) >= 2, "記録を見るの紙のタブ")
        q = query(self.opened(page)[-1]["url"])
        self.assertEqual(q.get("page"), "all")
        self.assertNotIn("save", q, "記録を見るは見るだけ(出力先へ残さない)")
        self.assertEqual(self.errors, [])


class DesktopEditionTests(PaperCase):
    def setUp(self) -> None:
        super().setUp()
        self.ctx.add_init_script(FAKE_TAURI)

    def test_紙と記録の紙は外枠の別窓で開きタブは開かない(self) -> None:
        page = self.open_entry()
        self.assertTrue(page.evaluate("document.documentElement.classList.contains('is-desktop')"))
        page.click("#print-page")
        self.wait_until(page, lambda: self.invoked(page, "open_window"), "外枠への頼み")
        args = self.invoked(page, "open_window")[-1]["args"]
        self.assertEqual(args["title"], "日報の紙")
        self.assertEqual(query(args["url"]).get("save"), "1")

        page.click('nav.rail a[href="/records"]')
        page.wait_for_function("location.pathname === '/records'", timeout=8000)
        page.wait_for_selector("#print-all")
        page.wait_for_timeout(400)
        page.click("#print-all")
        self.wait_until(page, lambda: len(self.invoked(page, "open_window")) >= 2, "記録を見るの頼み")
        self.assertNotIn("save", query(self.invoked(page, "open_window")[-1]["args"]["url"]))
        self.assertEqual(self.opened(page), [], "デスクトップ版の窓には新しいタブがありません")

    def test_早見表は名前つきの窓で開く(self) -> None:
        page = self.open_screen("/vc")
        page.click("#vc-open-quick")
        self.wait_until(page, lambda: self.invoked(page, "open_window"), "外枠への頼み")
        args = self.invoked(page, "open_window")[-1]["args"]
        self.assertEqual((args["url"], args["label"]), ("/vc?window=quick&tab=quick", "vc-quick"))
        self.assertEqual(self.opened(page), [])

    def test_フォルダを選ぶは標準のダイアログで_選んだ道が欄に入る(self) -> None:
        page = self.open_screen("/settings?tab=paths")
        btn = page.locator("[data-browse]").first
        key = btn.get_attribute("data-browse")
        btn.click()
        self.wait_until(page, lambda: self.invoked(page, "pick_path"), "外枠への頼み")
        self.assertEqual(self.invoked(page, "pick_path")[-1]["args"]["kind"], "folder")
        page.wait_for_function(
            f"document.getElementById('path-{key}').value === 'C:\\\\試験\\\\選んだフォルダ'",
            timeout=3000)
        self.assertTrue(page.is_hidden("#browse"), "いつもの一覧は出さない")


if __name__ == "__main__":
    unittest.main()
