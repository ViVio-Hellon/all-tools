"""案内表示を入口の画面で(本物のブラウザ ── 1.5.104)

日報のタブを出していない PC(タブ表示権限で看板だけ)でも、入口が案内を画面いっぱいに出す。
押すまで閉じない。名前を選ぶまで押せない。押すと閉じる。大設定の「案内表示」で行を直すとき、
欄の下に選べる値(色: 注意 / 警告 / 案内)が出る。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from unittest import mock

from portal import guide_notice as rule
from portal import guide_store, user_settings
from tests.test_settings_browser import SettingsCase


class NoticeShellTests(SettingsCase):
    def setUp(self) -> None:
        super().setUp()
        guide_store.reset()
        self.addCleanup(guide_store.reset)
        user_settings.update({guide_store.KEY_LINE: "機側"})
        self.addCleanup(user_settings.update, {guide_store.KEY_LINE: None})
        local = guide_store.app_config.local_dir("data") / "guide_notice.sqlite3"
        if local.exists():
            local.unlink()
        path = guide_store.shared_path()
        guide_store.ensure_table(path)
        conn = sqlite3.connect(path)
        conn.execute('CREATE TABLE IF NOT EXISTS "班員名簿" ("名前" TEXT, "班" TEXT)')
        conn.executemany('INSERT INTO "班員名簿" VALUES (?, ?)', [("山田", "A"), ("佐藤", "A")])
        conn.commit()
        conn.close()
        guide_store.reset()
        guide_store.first_seen(rule.parse([rule.FIRST_ROW]), datetime(2026, 11, 9, 6, 0))
        real = guide_store.due_now
        patcher = mock.patch.object(guide_store, "due_now",
                                    side_effect=lambda *a, **kw: real(*a, **{**kw, "now": datetime(2026, 11, 9, 15, 1)}))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_日報のタブが無いPCでも出て_名前を選んで押すまで閉じない(self) -> None:
        # この端末は看板だけ(日報のタブを出していない)
        self.unlock()
        self.post("/api/rights/create-table")
        res = self.post("/api/rights/add", {"login_id": "", "pc_name": "TEST-PC", "tab_ids": ["kanban"]})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.post("/api/settings/auth", {"lock": True})
        page = self.open_shell(first="kanban")
        self.assertEqual(page.locator("#tab-nippou").count(), 0, "日報のタブは出ていない")
        page.wait_for_selector("#guide-notice.gnote--large", timeout=8000)
        self.assertIn("計量器の点検を行ってください", page.inner_text("#guide-notice"))
        ok = "#guide-notice .gnote__ok"
        self.assertTrue(page.is_disabled(ok), "名前を選ぶまで押せない")
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        self.assertTrue(page.is_visible("#guide-notice"), "Esc でも閉じない")
        page.click('#guide-notice .gnote__name[data-name="山田"]')
        page.click(ok)
        page.wait_for_selector("#guide-notice", state="detached", timeout=5000)
        self.assertEqual(self.errors, [])

    def test_大設定で行を直すとき選べる値が出る(self) -> None:
        # この試験は案内を出さない(ラインを決めない ── 出ていると押すまで大設定にさわれない)
        user_settings.update({guide_store.KEY_LINE: None})
        guide_store.reset()
        page = self.open_settings()
        self.unlock_in_page(page)
        page.wait_for_selector("#notice-grid tbody tr[data-key]", timeout=8000)
        page.click("#notice-add")
        page.wait_for_selector("#notice-dialog[open]")
        hint = page.inner_text('#notice-fields [data-hint-for="色"]')
        self.assertIn("選べる値: 注意 / 警告 / 案内", hint)
        page.fill('#notice-fields [data-col="ID"]', "試し")
        page.fill('#notice-fields [data-col="表示文"]', "試しの案内")
        page.fill('#notice-fields [data-col="色"]', "紫")
        page.click("#notice-save")
        page.wait_for_selector("#notice-form-error:not([hidden])", timeout=5000)
        self.assertIn("注意 ・ 警告 ・ 案内", page.inner_text("#notice-form-error"))
        page.fill('#notice-fields [data-col="色"]', "警告")
        page.click("#notice-save")
        page.wait_for_selector("#notice-dialog", state="hidden", timeout=5000)
        self.assertIn("試し", page.inner_text("#notice-grid"))
        self.assertEqual(self.errors, [])
