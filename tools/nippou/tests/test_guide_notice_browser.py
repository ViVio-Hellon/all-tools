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


class CarriedBrowserTests(ScreenBrowserCase):
    """2直に押されなかった案内は3直で「確認されていません」を添えて出る。記録を見るに一覧。"""
    terminal_line = "機側"

    def setUp(self) -> None:
        super().setUp()
        from nippou import config, user_settings
        from nippou.config import SETTINGS
        from nippou.logic.staff import StaffMember
        from nippou.services import guide_notice
        from app.routes import staff

        user_settings.save_many({config.KEY_REPORT_OUT_DIR: str(self.tmp / "出力先")})
        master = SETTINGS.transmission_master_path
        master.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(master)
        conn.execute("CREATE TABLE 時間用 (直 TEXT, 開始 TEXT, 終了 TEXT)")
        conn.commit()
        conn.close()
        guide_notice.reset()
        self.addCleanup(guide_notice.reset)
        when = datetime(2026, 11, 9, 23, 30)                # 3直。2直は誰も押していない
        for name in ("due_now", "checks"):
            real = getattr(guide_notice, name)
            patcher = mock.patch.object(
                guide_notice, name,
                side_effect=lambda repo, calc, line, now=None, _real=real, **kw:
                _real(repo, calc, line, now=when, **kw))
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(staff.staff_master, "load_staff_members",
                                    return_value=[StaffMember("山田", "A")])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_3直で2直のぶんを出し_押せば記録を見るに押した直が出る(self) -> None:
        page = self.open_screen("/records", "#records-table")
        page.wait_for_selector("#guide-notice .gnote__carried", timeout=8000)
        self.assertIn("2直 に確認されていません", page.inner_text("#guide-notice .gnote__carried"))
        page.wait_for_selector("#notice-table tbody tr.row-bad", timeout=8000)
        self.assertIn("押されていません", page.inner_text("#notice-table"))
        page.click('#guide-notice .gnote__name[data-name="山田"]')
        page.click("#guide-notice .gnote__ok")
        page.wait_for_selector("#guide-notice", state="detached", timeout=5000)
        again = self.open_screen("/records", "#records-table", settle=800)
        again.wait_for_function(
            "document.querySelector('#notice-table')?.innerText.includes('山田')", timeout=8000)
        row = again.inner_text("#notice-table tbody tr")
        self.assertIn("2直", row)
        self.assertIn("3直", row, "押した直は3直")
        self.assertEqual(self.errors, [])


from tests.test_master_browser import MasterBrowserCase  # noqa: E402


class MasterHintBrowserTests(MasterBrowserCase):
    """マスタ管理で案内表示の行を足すとき、選べる値と説明が欄の下に出る(色: 注意 / 警告 / 案内)。"""
    TABLES = {"案内表示": (("ID", "有効", "ライン", "直", "タイミング", "時刻", "分", "表示文",
                          "確認の文", "ボタン", "大きさ", "色", "名前を選ぶ", "過ぎても出す",
                          "開始日", "終了日", "並び"), [])}

    def test_色の欄に選べる値が出る(self) -> None:
        self.unlock()
        page = self.open_master()
        self.choose(page, "案内表示")
        page.click("#tbl-add")
        page.wait_for_selector("#row-modal:not([hidden])")
        hint = page.inner_text('#row-fields [data-hint-for="色"]')
        self.assertIn("選べる値: 注意 / 警告 / 案内", hint)
        self.assertIn("警告 = 赤", hint)
        self.assertEqual(page.eval_on_selector_all('#choices-色 option', "(o) => o.map((x) => x.value)"),
                         ["注意", "警告", "案内"])
        self.assertEqual(self.errors, [])
