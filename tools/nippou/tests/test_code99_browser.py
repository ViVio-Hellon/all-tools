"""集計管理: 「内訳番号99 の時間」の面は**管理者だけ**・開いたときに読む (本物のブラウザ)

    2. 99 の時間を数える表を管理者だけに出す(ライン別・直別・作業者別の合計と、
       月ごとの移り変わり。始めた日からの増減が分かる)

ここでは集計管理の画面を開いて押し、**出ている面・送った要求・表に出た数**を見ます:

    - 一般の端末には面が無い(要求も送らない)
    - 管理者モードの端末には面があり、**開くまで読まない**(控えをぜんぶ読むので)。開くと
      使い始めた月の1日からの表が出て、開始日の欄もそこへ戻る
    - 「4本ともCSVに出す」で4本書いた、と面の中に出る

数の細かい決まりは `tests/test_stop_other_99.py`。サーバの時計は 2026年8月31日 10:00 に止めます。
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._entry_browser import ORIGIN                     # noqa: E402
from tests._other_screen_browser import ScreenBrowserCase  # noqa: E402
from tests.test_stop_other_99 import shift_rows            # noqa: E402


class Code99PageTests(ScreenBrowserCase):
    def setUp(self) -> None:
        super().setUp()
        from tests.test_scenario_shift_flow import _Now
        _Now.value = datetime(2026, 8, 31, 10, 0)
        # 7月20日の2直(佐藤)と、8月3日の1直(山田)に 99 を 45分ずつ
        for day, shift, worker in (("2026年7月20日", "2直", "佐藤"),
                                   ("2026年8月3日", "1直", "山田")):
            header, details = shift_rows(worker=worker)
            for rec in (header, *details):
                rec.report_date, rec.shift = day, shift
            self.repo().save(header, details)

    def admin(self) -> None:
        """管理者モードを**サーバに**開けておく(`ScreenBrowserCase.admin`)。先に1回読んで
        直を覚えさせる ── 時計を止め直した直後だと「直が変わった」で管理者モードが外れる。"""
        self.get("/agg")
        super().admin()

    def open_agg(self):
        # 保存のある直の実績の確認(`shift_end.js`)へ連れて行かれないように、先に済ませる
        self.settle_review()
        page = self.watch(self.ctx.new_page())
        page.goto(f"{ORIGIN}/agg", wait_until="load")
        page.wait_for_selector("#push-log-presets [data-preset]")
        self.wait_until(page, lambda: any(p == "/api/agg/push-log" for p, _ in self.sent),
                        "開いたときの履歴の引き")
        return page

    def code99_sent(self, since: int = 0) -> list[dict]:
        return [b for p, b in self.sent[since:] if p == "/api/agg/code99"]

    @staticmethod
    def cells(page, key: str) -> list[list[str]]:
        return page.evaluate("""key => [...document.querySelectorAll(
            `[data-table="${key}"] tbody tr`)].map(tr => [...tr.cells].map(td => td.textContent))""",
                             key)

    def test_一般の端末には面が無い(self) -> None:
        page = self.open_agg()
        self.assertEqual(page.locator("#code99").count(), 0)
        self.assertNotIn("内訳番号99", page.inner_text("body"))
        self.assertEqual(self.code99_sent(), [])
        self.assertEqual(self.errors, [])

    def test_管理者には面があり_開くと読む(self) -> None:
        self.admin()
        page = self.open_agg()
        self.assertEqual(page.locator("#code99").count(), 1)
        page.wait_for_timeout(300)
        self.assertEqual(self.code99_sent(), [], "開く前に読んでいます")
        page.click("#code99 > summary")
        self.wait_until(page, lambda: bool(self.code99_sent()), "開いたときの読み")
        self.assertEqual(self.code99_sent()[-1]["start"], "")     # 始まりは空で頼む
        page.wait_for_function("""() => document.querySelectorAll(
            '[data-table="code99_months"] tbody tr').length === 2""")
        # 始まりは使い始めた月の1日へ戻る
        self.assertEqual(page.input_value("#code99-start"), "2026-07-01")
        self.assertEqual(page.input_value("#code99-end"), "2026-08-31")
        self.assertIn("2026-07-20(今日まで 42日)", page.inner_text("#code99-first"))
        self.assertEqual(self.cells(page, "code99_lines"),
                         [["L-1", "4", "90", "2", "2", "45.0", "2026-07-20", "42"]])
        self.assertEqual(self.cells(page, "code99_workers"),
                         [["佐藤", "2", "45", "1"], ["山田", "2", "45", "1"]])
        self.assertEqual([r[0] for r in self.cells(page, "code99_shifts")], ["1直", "2直"])
        self.assertEqual([(r[0], r[2], r[6]) for r in self.cells(page, "code99_months")],
                         [("2026-07", "45", ""), ("2026-08", "45", "0")])
        # 期間を選び直して「表示」 → その期間で引き直す
        mark = len(self.sent)
        page.fill("#code99-start", "2026-08-01")
        page.click("#code99-reload")
        self.wait_until(page, lambda: bool(self.code99_sent(mark)), "引き直し")
        self.assertEqual(self.code99_sent(mark)[-1]["start"], "2026-08-01")
        page.wait_for_function("""() => document.querySelectorAll(
            '[data-table="code99_months"] tbody tr').length === 1""")
        self.assertEqual(self.cells(page, "code99_workers"), [["山田", "2", "45", "1"]])
        self.assertEqual(self.errors, [])

    def test_CSVに4本(self) -> None:
        self.admin()
        page = self.open_agg()
        page.click("#code99 > summary")
        page.wait_for_function("""() => document.querySelectorAll(
            '[data-table="code99_lines"] tbody tr').length === 1""")
        page.click("#code99-csv")
        page.wait_for_function("() => !document.getElementById('code99-msg').hidden")
        text = page.inner_text("#code99-msg")
        for title in ("ライン別 1行", "直別 2行", "作業者別 2行", "月ごとの移り変わり 2行"):
            self.assertIn(title, text)
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
