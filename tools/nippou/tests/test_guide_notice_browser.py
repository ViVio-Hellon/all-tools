"""案内表示を本物のブラウザで (v4.47.0)

画面を開くと(1分ごとの見張りの1回目)、機側・2直の始まりの案内が画面いっぱいに出る。
押すまで閉じない(Esc でも閉じない)。名前を選ぶまで「点検済み」は押せない。押すと閉じ、
同じ直ではもう出ない。
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._other_screen_browser import ScreenBrowserCase  # noqa: E402


class GuideNoticeBrowserTests(ScreenBrowserCase):
    terminal_line = "機側"

    def setUp(self) -> None:
        super().setUp()
        from nippou.config import SETTINGS
        from nippou.logic.staff import StaffMember
        from nippou.services import guide_notice
        from app.routes import staff

        master = SETTINGS.transmission_master_path
        master.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(master)
        conn.execute("CREATE TABLE 時間用 (直 TEXT, 開始 TEXT, 終了 TEXT)")
        conn.commit()
        conn.close()
        guide_notice.reset()
        self.addCleanup(guide_notice.reset)
        real = guide_notice.due_now
        when = datetime(2026, 11, 9, 15, 1)
        for patcher in (
                mock.patch.object(guide_notice, "due_now",
                                  side_effect=lambda repo, calc, line, now=None, **kw:
                                  real(repo, calc, line, when, **kw)),
                mock.patch.object(staff.staff_master, "load_staff_members",
                                  return_value=[StaffMember("山田", "A"), StaffMember("佐藤", "A")])):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_大きく出て_名前を選んで押すまで閉じない(self) -> None:
        page = self.open_screen("/graph", "main#main")
        page.wait_for_selector("#guide-notice.gnote--large", timeout=8000)
        text = page.inner_text("#guide-notice")
        self.assertIn("計量器の点検を行ってください", text)
        self.assertIn("点検済みですか？", text)
        ok = "#guide-notice .gnote__ok"
        self.assertTrue(page.is_disabled(ok), "名前を選ぶまで押せない")
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        self.assertTrue(page.is_visible("#guide-notice"), "Esc でも閉じない")
        page.click('#guide-notice .gnote__name[data-name="山田"]')
        self.assertFalse(page.is_disabled(ok))
        page.click(ok)
        page.wait_for_selector("#guide-notice", state="detached", timeout=5000)
        acks = self.posts("/api/notice/ack")
        self.assertEqual((acks[0]["id"], acks[0]["worker"]), ("計量器の点検", "山田"))
        # 開き直しても、同じ直ではもう出ない
        again = self.open_screen("/graph", "main#main", settle=1500)
        self.assertFalse(again.is_visible("#guide-notice"))
        self.assertEqual(self.errors, [])

    def test_名簿に無い人は打って押せる(self) -> None:
        page = self.open_screen("/graph", "main#main")
        page.wait_for_selector("#guide-notice", timeout=8000)
        page.fill("#guide-notice .gnote__typed", "応援の田中")
        page.click("#guide-notice .gnote__ok")
        page.wait_for_selector("#guide-notice", state="detached", timeout=5000)
        self.assertEqual(self.posts("/api/notice/ack")[0]["worker"], "応援の田中")
