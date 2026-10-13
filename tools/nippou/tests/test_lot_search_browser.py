"""記録を見る「ロットを探す」を本物のブラウザで (v4.33.0)

    期間を絞ってロットNo検索したい場合に何を検索したらいいですか?

キーボードだけで使えること(ロット№を打って **Enter** で探す)と、当たった行の
「下見」が下の「中身の下見」に出て、当たった行に印が付くことを確かめる。
配線は `views/records.js` の中で、Python の試験では壊れても分からないので。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class LotSearchBrowserTests(WebTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from tests._browser import chromium_or_skip
        cls.pw, cls.browser = chromium_or_skip()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.pw.stop()

    def setUp(self) -> None:
        super().setUp()
        from tests.test_lot_search import save
        save(self.repo(), shift="1直", lots=("N7131T0", "L715C50"))
        save(self.repo(), shift="2直", worker="佐藤", lots=("X000000", "N7131T0"))
        # 直の実績の確認(`shift_end.js`)へ連れて行かれないように、先に確認を済ませる
        for _ in range(3):
            state = self.get("/api/graph/review").get_json()
            if not state.get("due"):
                break
            self.post("/api/graph/review/ack", {"stage": state.get("stage", "")})
        self.assertFalse(self.get("/api/graph/review").get_json().get("due"))

    def open_records(self):
        from tests.test_seq_entry import BrowserTests
        ctx = self.browser.new_context(viewport={"width": 1400, "height": 900})
        self.addCleanup(ctx.close)
        ctx.route("http://app.test/**", lambda route: BrowserTests._forward(self, route))
        page = ctx.new_page()
        self.errors: list[str] = []
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.on("dialog", lambda d: d.accept())
        page.goto("http://app.test/records", wait_until="load")
        page.wait_for_selector("#lot-card")
        page.wait_for_timeout(400)
        # 期間は試験の日付に合わせる(既定は今日から3か月)
        page.fill("#lot-start", "2026-09-01")
        page.fill("#lot-end", "2026-10-31")
        return page

    def test_Enterで探して_下見で当たった行に印が付く(self) -> None:
        page = self.open_records()
        page.fill("#lot-no", "ｎ７１")                    # 全角・小文字でも
        page.press("#lot-no", "Enter")
        page.wait_for_selector("#lot-rows tr button")
        rows = page.locator("#lot-rows tr")
        self.assertEqual(rows.count(), 2)
        # どの直・ページの何行目かが、表から読める
        self.assertEqual(page.locator("#lot-head th").nth(2).text_content(), "直")
        self.assertIn("1直", rows.nth(0).text_content())
        self.assertIn("2直", rows.nth(1).text_content())
        self.assertIn("N71 で始まるロット: 2行", page.text_content("#lot-note"))
        # 管理者モードではないので「呼び出す」は出ない
        self.assertEqual(rows.nth(0).locator("button", has_text="呼び出す").count(), 0)

        # 2直の行の「下見」── 下の欄へ入り、中身の下見に出て、当たった行(2行目)に印
        rows.nth(1).locator("button", has_text="下見").click()
        page.wait_for_selector("#preview tr.row-hit")
        self.assertEqual(page.input_value("#p-shift"), "2直")
        self.assertEqual(page.input_value("#p-page"), "1")
        hit = page.locator("#preview tr.row-hit")
        self.assertEqual(hit.count(), 1)
        self.assertIn("N7131T0", hit.text_content())
        self.assertEqual(hit.locator("td").first.text_content(), "2")
        self.assertFalse(page.is_disabled("#print"))     # このページの紙も押せる
        self.assertEqual(self.errors, [])

    def test_空欄でEnterなら断りを出す(self) -> None:
        page = self.open_records()
        page.press("#lot-no", "Enter")
        page.wait_for_selector("#lot-note:not([hidden])")
        self.assertIn("ロット№を入れてください", page.text_content("#lot-note"))
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
