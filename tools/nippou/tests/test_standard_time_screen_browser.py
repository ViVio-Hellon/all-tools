"""標準作業時間の画面 (本物のブラウザ。段階1)

前は字を探していました(`test_standard_time` の「別の画面になっている」── `standard_time.js` に
4つの道の字があるか)。ここでは画面を開いて、押して、**出たもの**を見ます:

    - 開くと標準の表を引き、出どころ(実績の行数)が出る
    - 同条件の作業: 条件を入れて「引く」と件数が出る。CSV を押すと書き出した知らせが出る
    - 過去ぶんを入れる: 押すと結果の知らせが出る
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import test_standard_time as st  # noqa: E402  共有へ渡した直と名簿の作り方を借りる
from tests._entry_browser import ORIGIN, EntryBrowserCase  # noqa: E402


class StandardTimeScreenTests(EntryBrowserCase):
    def setUp(self) -> None:
        super().setUp()
        from nippou import config, user_settings

        self.share = self.tmp / "共有"
        self.share.mkdir()
        user_settings.save_many({config.KEY_ACCESS_DIR: str(self.share),
                                 config.KEY_REPORT_OUT_DIR: str(self.tmp / "出力")})
        patcher = patch("nippou.services.standard_time.load_teams", return_value=dict(st.TEAMS))
        patcher.start()
        self.addCleanup(patcher.stop)
        st.WebTests.push(self, st.WebTests.save_shift(self))      # 1直を共有へ渡した(実績が1行)

    def posted(self, path: str) -> bool:
        return any(p == path for p, _ in self.sent)

    def test_開いて引いて書き出して過去ぶんを入れる(self) -> None:
        page = self.watch(self.ctx.new_page())
        page.goto(f"{ORIGIN}/standard-time", wait_until="load")
        self.wait_until(page, lambda: self.posted("/api/standard-time/standards"), "標準の表を引く")
        page.wait_for_function("document.getElementById('standard-source').textContent.includes('実績 1行')",
                               timeout=5000)

        page.click('[role="tab"][data-key="works"]')
        page.fill("#works-purpose", "H176")
        page.click("#works-run")
        page.wait_for_function("document.getElementById('works-summary').textContent.includes('1件')",
                               timeout=5000)
        self.assertTrue(self.posted("/api/standard-time/works"))
        page.click("#works-csv")
        page.wait_for_selector("#works-msg:not([hidden])", timeout=5000)
        self.assertTrue(self.posted("/api/standard-time/works/csv"))
        self.assertIn("H176", page.text_content("#works-msg"))

        page.click('[role="tab"][data-key="backfill"]')
        page.click("#backfill-run")
        page.wait_for_selector("#backfill-note:not([hidden])", timeout=5000)
        self.assertTrue(self.posted("/api/standard-time/backfill"))
        self.assertTrue(page.text_content("#backfill-note").strip())
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
