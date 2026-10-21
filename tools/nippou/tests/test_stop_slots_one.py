"""作業停止①〜⑤の組と印は**サーバの1か所**(`constants.STOP_SLOTS`)から(段階2・案1)

前は画面の JS(`entry.js`)にも同じ組 `[["S","TH"], …]` と印「①②③④⑤」を書き、テンプレートと
CSV の見出しも `range(1, 6)` と「①②③④⑤」をそれぞれ持っていました。組を1つ足す・変えると、
画面だけ古い組のまま動きます。画面へは `entry.html` が JSON で渡します。
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou import constants  # noqa: E402
from tests._entry_browser import EntryBrowserCase  # noqa: E402
from tests._web import HAS_FLASK, HEADERS, SKIP_REASON, WebTestCase  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class OnePlaceTests(unittest.TestCase):
    def test_印は組の数だけ(self) -> None:
        self.assertEqual(constants.STOP_MARKS, ("①", "②", "③", "④", "⑤"))
        self.assertEqual(len(constants.STOP_MARKS), len(constants.STOP_SLOTS))

    def test_CSVの見出しも同じ印(self) -> None:
        from nippou.reporting import csv_export

        self.assertEqual(csv_export.STOP_SLOT_LABELS,
                         {1: "作業停止①", 2: "作業停止②", 3: "作業停止③", 4: "作業停止④", 5: "作業停止⑤"})

    def test_画面のJSは組を自分で持たない(self) -> None:
        js = (ROOT / "app/static/js/views/entry.js").read_text(encoding="utf-8")
        self.assertNotIn('["S", "TH"]', js)
        self.assertNotIn("①②③④⑤", js)
        html = (ROOT / "app/templates/entry.html").read_text(encoding="utf-8")
        self.assertNotIn("①②③④⑤", html)


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class PageTests(WebTestCase):
    def page(self) -> str:
        res = self.client.get("/", headers=HEADERS)
        self.assertEqual(res.status_code, 200)
        return res.get_data(as_text=True)

    def test_画面へ組と印をJSONで渡す(self) -> None:
        found = re.search(r'<script type="application/json" id="entry-stop-slots">(.*?)</script>',
                          self.page(), re.S)
        self.assertIsNotNone(found)
        seed = json.loads(found.group(1))
        self.assertEqual([tuple(p) for p in seed["slots"]], list(constants.STOP_SLOTS))
        self.assertEqual(seed["marks"], list(constants.STOP_MARKS))

    def test_切り替えのボタンは組の数だけ(self) -> None:
        html = self.page()
        buttons = re.findall(r'data-stop-to="(\d)"[^>]*>([^<]+)</button>', html)
        want = [(str(n), m) for n, m in enumerate(constants.STOP_MARKS, 1)]
        # 見出しは組ごとにあり、どれにも同じ並びの切り替えが付く
        self.assertTrue(buttons)
        self.assertEqual(len(buttons) % len(want), 0)
        for at in range(0, len(buttons), len(want)):
            self.assertEqual(buttons[at:at + len(want)], want)


class StopSumBrowserTests(EntryBrowserCase):
    def test_4組目に打つと行の停止のまとめに4組目の印と分が出る(self) -> None:
        page = self.open_entry()
        page.locator('[data-stop-to="4"]:visible').first.click()
        page.wait_for_function("document.querySelector('table.grid').dataset.stopShow === '4'")
        self.type_in(page, "S41", "A1")
        self.type_in(page, "TH41", "5")
        page.locator("#TH41").press("Tab")
        cell = page.locator('[data-stopsum-row="1"]')
        cell.locator('[data-stop-jump="4"]').wait_for(timeout=5000)
        self.assertEqual(cell.locator('[data-stop-jump="4"]').text_content(), "④")
        self.assertIn("作業停止④: A1 5分", cell.locator('[data-stop-jump="4"]').get_attribute("title"))
        self.assertIn("5分", cell.text_content())
        page.locator('[data-stop-to="1"]:visible').first.click()   # 1組目へ戻して、まとめの4組目から跳ぶ
        cell.locator('[data-stop-jump="4"]').click()
        page.wait_for_function("document.activeElement && document.activeElement.id === 'S41'",
                               timeout=3000)
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
