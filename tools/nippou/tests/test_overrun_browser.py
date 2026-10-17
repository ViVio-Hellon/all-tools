"""標準時間の画面: 標準を大きく超過した作業が分かる (v4.37.0)

標準の1.5倍以上かつ10分以上長い作業を、標準の表では「大きく超過(件)」と行の色、同条件の作業では
「大きく超過」の列と行の色・「大きく超過した作業だけ」の絞り込みで出す。本物のブラウザで確かめる。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.models import DetailRecord, HeaderRecord  # noqa: E402
from nippou.services import standard_time as svc  # noqa: E402
from nippou.services import summary  # noqa: E402
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402

TEAMS = {"山田": "A", "鈴木": "A"}


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class OverrunBrowserTests(WebTestCase):
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
        from nippou import config, user_settings
        from nippou.config import SETTINGS
        share = self.tmp / "共有"
        share.mkdir()
        user_settings.save_many({config.KEY_ACCESS_DIR: str(share)})
        patcher = patch.object(svc, "load_teams", return_value=dict(TEAMS))
        patcher.start()
        self.addCleanup(patcher.stop)
        repo = self.repo()
        key = dict(report_date="2026年9月1日", line="L-1", shift="1直", page=1)
        # (梱包数, 人数, 作業時間) ── 60・70・80分と、180分の1件(標準70分の2.5倍 → 大きく超過)
        rows = [(4, 2, 60), (4, 2, 70), (4, 2, 80), (4, 2, 180)]
        repo.save(HeaderRecord(**key, worker="山田"), [
            DetailRecord(**key, row_no=i + 1, lot=f"N71{i}T0", siz="8.000×1528.0×3053.0",
                         tut=str(t), hit=str(h), tim=str(m), con="40",
                         others1="H176", others2="ｼﾔ-ｼ", others4="1P0001")
            for i, (t, h, m) in enumerate(rows)])
        summary.refresh_shift(repo, "2026年9月1日", "L-1", "1直")
        svc.after_push(repo, [("2026年9月1日", "L-1", "1直", 1)], SETTINGS.standard_time_db_path)

    def open_page(self):
        from tests.test_seq_entry import BrowserTests
        ctx = self.browser.new_context(viewport={"width": 1400, "height": 900})
        self.addCleanup(ctx.close)
        ctx.route("http://app.test/**", lambda route: BrowserTests._forward(self, route))
        # 直の終わりの確認画面へ連れて行く見張りは、いまの時刻しだい。この試験では止めておく
        ctx.route("http://app.test/api/graph/review", lambda route: route.fulfill(
            status=200, body='{"due": false}', headers={"content-type": "application/json"}))
        page = ctx.new_page()
        self.errors: list[str] = []
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.on("dialog", lambda d: d.accept())
        page.goto("http://app.test/standard-time", wait_until="load")
        return page

    def test_標準の表と同条件の作業に出る(self) -> None:
        page = self.open_page()
        page.click("#standard-reload")
        std = '[data-table="standard_time"] table.list'
        page.wait_for_selector(f"{std} tbody tr.is-over", timeout=5000)
        heads = page.eval_on_selector_all(f"{std} thead th", "els => els.map(e => e.textContent.trim())")
        self.assertIn("大きく超過(件)", heads)
        at = heads.index("大きく超過(件)")
        marked = page.eval_on_selector_all(
            f"{std} tbody tr.is-over", f"trs => trs.map(t => t.children[{at}].textContent.trim())")
        self.assertTrue(marked and all(v == "1" for v in marked), marked)

        # 抽出 → 同条件の作業。180分の1件だけに印
        page.locator(f"{std} tbody tr.is-over button").first.click()
        works = '[data-table="works"] table.list'
        page.wait_for_selector(f"{works} tbody tr", timeout=5000)
        self.assertEqual(page.locator(f"{works} tbody tr").count(), 4)
        self.assertEqual(page.locator(f"{works} tbody tr.is-over").count(), 1)
        self.assertIn("大きく超過(標準の", page.text_content(f"{works} tbody tr.is-over"))

        # 「大きく超過した作業だけ」
        page.check("#works-overrun")
        page.wait_for_function(
            f"document.querySelectorAll('{works} tbody tr').length === 1", timeout=5000)
        self.assertIn("180", page.text_content(f"{works} tbody tr"))
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
