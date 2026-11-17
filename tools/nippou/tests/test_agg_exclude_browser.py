"""集計から除く作業者: 本物のブラウザで時間・停止を打てない(v4.52.2)

    テーブル登録し、除いた人で入力テストをしてみたが作業時間も停止時間も打てますけど？
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class AutoTimeBrowserTests(WebTestCase):
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
        from nippou.config import SETTINGS
        from nippou.services import agg_exclude
        from tests.test_seq_entry import BrowserTests

        agg_exclude.reset()
        self.addCleanup(agg_exclude.reset)
        master = SETTINGS.gw_material_master_path
        master.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(master)
        conn.execute(agg_exclude.SCHEMA)
        conn.execute(f'INSERT INTO "{agg_exclude.TABLE}" ("作業者","理由","有効") VALUES ("山田","試験",1)')
        conn.commit()
        conn.close()
        self.ctx = self.browser.new_context(viewport={"width": 1400, "height": 900})
        self.addCleanup(self.ctx.close)
        self.ctx.route("http://app.test/**", lambda route: BrowserTests._forward(self, route))
        self.errors: list[str] = []

    def open(self):
        page = self.ctx.new_page()
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.goto("http://app.test/", wait_until="load")
        page.wait_for_selector("#grid-body")
        page.wait_for_timeout(600)
        return page

    def choose(self, page, name: str) -> None:
        if page.evaluate("document.body.classList.contains('needs-worker')"):
            page.fill("#hd-worker", name)
            page.dispatch_event("#hd-worker", "change")
            page.wait_for_function("!document.body.classList.contains('needs-worker')", timeout=5000)
        else:
            page.fill("#hd-worker", name)
            page.dispatch_event("#hd-worker", "change")
        page.wait_for_timeout(800)

    def typed(self, page, field: str, text: str) -> str:
        page.focus(f"#{field}")
        page.keyboard.type(text)
        page.keyboard.press("Tab")
        page.wait_for_timeout(300)
        return page.input_value(f"#{field}")

    def test_除く人を選んだら時間と停止を打てない(self) -> None:
        page = self.open()
        self.choose(page, "山田")
        self.assertTrue(page.is_visible("#auto-time-note"), "案内が出ていない")
        for field in ("KZ1", "KH1", "SZ1", "SH1", "S1", "TH1", "SS1", "THS1"):
            with self.subTest(field=field):
                self.assertEqual(self.typed(page, field, "12"), "", f"{field} に打てた")
        # ロット・枚数はふつうに打てる
        self.assertEqual(self.typed(page, "MAI1", "5"), "5")
        # 開き直しても打てない
        page.reload(wait_until="load")
        page.wait_for_selector("#grid-body")
        page.wait_for_timeout(800)
        self.assertEqual(self.typed(page, "KZ1", "07"), "", "開き直したら打てた")
        self.assertEqual(self.errors, [])

    def test_ふつうの人は打てる(self) -> None:
        page = self.open()
        self.choose(page, "佐藤")
        self.assertFalse(page.is_visible("#auto-time-note"))
        self.assertEqual(self.typed(page, "KZ1", "07"), "07")
