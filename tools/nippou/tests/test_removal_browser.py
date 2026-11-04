"""直を消す・テストモード: 画面を**本物のブラウザ**で押す

    - テストモード: 「この端末と配布」で管理者パスワードを入れて入る → どの画面にも帯 →
      帯の「テストモードを抜ける」で抜ける
    - 直を消す: 「取り込みと直し」でテストデータを探す → まとめて選ぶ → 画面の中の窓で確かめて
      手元から消す →「共有の日報データからも消しますか？」→ はい / いいえ
      (**confirm は出さない** ── デスクトップ版の窓では出ないので)

環境変数 `NIPPOU_SHOT_DIR` があれば、そこへ画面写真を残す(報告用)。
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._other_screen_browser import ScreenBrowserCase  # noqa: E402

DAY = "2026年8月3日"


class RemovalBrowserCase(ScreenBrowserCase):
    def setUp(self) -> None:
        super().setUp()
        from nippou import config, user_settings
        from nippou.services import local_backup

        self.share = self.tmp / "共有"
        self.share.mkdir()
        user_settings.save_many({config.KEY_ACCESS_DIR: str(self.share)})
        local_backup.reset()
        self.addCleanup(local_backup.reset)
        self.settle_review()

    def password(self) -> str:
        from nippou.config import SETTINGS

        return SETTINGS.admin_password

    def shot(self, page, name: str) -> None:
        folder = os.environ.get("NIPPOU_SHOT_DIR")
        if folder:
            Path(folder).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(folder) / f"{name}.png"), full_page=False)

    def save(self, shift: str, page: int = 1, n: int = 2, worker: str = "山田") -> None:
        from nippou.db.models import DetailRecord, HeaderRecord

        key = dict(report_date=DAY, line="L-1", shift=shift, page=page)
        self.repo().save(HeaderRecord(**key, worker=worker),
                         [DetailRecord(**key, row_no=i, lot=f"T{i}", ken="10", con="10",
                                       kz="8", kh="00", sz="9", sh="00")
                          for i in range(1, n + 1)])


class QuickEntryBrowserTests(RemovalBrowserCase):
    """「そんなモードないです」── 面の奥のカードは**見つからない**。設定を開いた最初の
    画面(既定の面)の一番上の「すぐ使う」から、押すだけで目の前に出ること。"""

    def test_すぐ使うからテストモードと直を消すへ飛べる(self) -> None:
        page = self.open_screen("/settings", "#quick-tools")
        for button, card in (("#quick-removal", "#removal"), ("#quick-test-mode", "#test-mode")):
            with self.subTest(button=button):
                page.evaluate("document.querySelector('main#main')?.scrollTo(0, 0)")
                page.evaluate("window.scrollTo(0, 0)")
                self.assertTrue(page.locator(button).is_visible(), "最初の画面に入口が見える")
                page.click(button)
                page.wait_for_timeout(400)
                self.assertTrue(page.locator(card).is_visible())
                top = page.evaluate(f"document.querySelector('{card}').getBoundingClientRect().top")
                height = page.evaluate("innerHeight")
                self.assertTrue(0 <= top < height / 2, f"{card} が画面の上半分にある: {top}/{height}")
        self.assertEqual(self.dialogs, [])
        self.assertEqual(self.errors, [])

    def test_リンクで来てもそのカードを出す(self) -> None:
        page = self.open_screen("/settings?tab=data#removal", "#removal")
        page.wait_for_timeout(400)
        top = page.evaluate("document.querySelector('#removal').getBoundingClientRect().top")
        self.assertTrue(0 <= top < page.evaluate("innerHeight") / 2, top)


class TestModeBrowserTests(RemovalBrowserCase):
    def test_パスワードで入ると帯が出て帯から抜けられる(self) -> None:
        page = self.open_screen("/settings?tab=terminal", "#test-mode-enter")
        self.assertEqual(page.locator("#test-band").count(), 0)
        page.fill("#test-mode-pw", "ちがう")
        page.click("#test-mode-enter")
        page.wait_for_selector("#test-mode-note:not([hidden])")
        self.assertIn("パスワードが違います", page.inner_text("#test-mode-note"))
        page.fill("#test-mode-pw", self.password())
        page.click("#test-mode-enter")
        page.wait_for_selector("#test-band", timeout=8000)
        band = page.inner_text("#test-band")
        self.assertIn("テストモード(この間に保存した直はテストデータです)", band)
        self.shot(page, "テストモードの帯")
        # 画面を移っても帯は残る(外枠の側)
        page.click('nav.rail a[href="/records"]')
        page.wait_for_selector("#main")
        page.wait_for_timeout(800)
        self.assertTrue(page.locator("#test-band").is_visible())
        page.click("#test-band-leave")
        page.wait_for_selector("#test-band", state="detached", timeout=8000)
        self.assertFalse(self.get("/api/test-mode").get_json()["active"])
        self.assertEqual(self.dialogs, [])
        self.assertEqual(self.errors, [])


class RemovalBrowserTests(RemovalBrowserCase):
    def setUp(self) -> None:
        super().setUp()
        from nippou.access_bridge import pusher

        self.save("1直")
        self.post("/api/test-mode", {"enable": True, "password": self.password()})
        self.save("2直", n=3)
        self.save("2直", page=2, n=1)
        self.post("/api/test-mode", {"enable": False})
        pusher.push_pending(self.repo(), self.share / "日報データ.sqlite3")
        self.admin()

    def shared_shifts(self) -> list[str]:
        conn = sqlite3.connect(str(self.share / "日報データ.sqlite3"))
        try:
            return sorted(r[0] for r in conn.execute('SELECT "直" FROM "T_日報ヘッダー_L-1"'))
        finally:
            conn.close()

    def find_tests(self):
        page = self.open_screen("/settings?tab=data", "#removal-find")
        page.select_option("#removal-kind", "test")
        page.click("#removal-find")
        page.wait_for_selector("#removal-rows tr", timeout=8000)
        return page

    def test_テストデータをまとめて選んで手元と共有から消す(self) -> None:
        page = self.find_tests()
        rows = page.locator("#removal-rows tr")
        self.assertEqual(rows.count(), 2)
        self.assertIn("テスト", rows.first.inner_text())
        page.click("#removal-pick-test")
        self.assertIn("2ページを選んでいます", page.inner_text("#removal-picked"))
        page.click("#removal-delete")
        page.wait_for_selector("#removal-confirm:not([hidden])")
        self.assertIn("2ページ", page.inner_text("#removal-confirm"))
        self.shot(page, "直を消す_確かめの窓")
        page.click("#removal-confirm-yes")
        page.wait_for_selector("#removal-ask:not([hidden])", timeout=8000)
        self.assertIn("共有の日報データからも消しますか？", page.inner_text("#removal-ask"))
        self.shot(page, "直を消す_共有からも消しますか")
        self.assertEqual(self.shared_shifts(), ["1直", "2直", "2直"])
        page.click("#removal-ask-yes")
        page.wait_for_selector("#removal-note:not([hidden])")
        page.wait_for_timeout(300)
        self.assertIn("共有の日報データからも 2ページを消しました", page.inner_text("#removal-note"))
        self.assertEqual(self.shared_shifts(), ["1直"])
        self.assertEqual(set(self.repo().list_keys()), {(DAY, "L-1", "1直", 1)})
        self.assertEqual(self.dialogs, [], "confirm を出しています(デスクトップ版で使えない)")
        self.assertEqual(self.errors, [])

    def test_いいえなら手元だけ(self) -> None:
        page = self.find_tests()
        page.check("#removal-all")
        page.click("#removal-delete")
        page.click("#removal-confirm-yes")
        page.wait_for_selector("#removal-ask:not([hidden])", timeout=8000)
        page.click("#removal-ask-no")
        page.wait_for_selector("#removal-note:not([hidden])")
        page.wait_for_timeout(300)
        self.assertIn("共有からは消しませんでした", page.inner_text("#removal-note"))
        self.assertEqual(self.shared_shifts(), ["1直", "2直", "2直"])
        self.assertEqual(set(self.repo().list_keys()), {(DAY, "L-1", "1直", 1)})
        self.assertEqual(self.dialogs, [])

    def test_共有に届かなければ一覧に残りあとで消せる(self) -> None:
        page = self.find_tests()
        page.click("#removal-pick-test")
        page.click("#removal-delete")
        page.click("#removal-confirm-yes")
        page.wait_for_selector("#removal-ask:not([hidden])", timeout=8000)
        self.refuse["/api/settings/removal/shared"] = 503      # 共有に届かなかった、とする
        page.click("#removal-ask-yes")
        page.wait_for_selector("#removal-note:not([hidden])")
        self.refuse.clear()
        # 答えはサーバに届いていないので、記録は「答えていない」のまま一覧に残る
        page.click("#removal-find")
        page.wait_for_selector("#removal-open:not([hidden])", timeout=8000)
        self.assertEqual(page.locator("#removal-open-rows tr").count(), 2)
        self.shot(page, "直を消す_共有からまだ消していない")
        page.check("#removal-open-all")
        page.click("#removal-open-shared")
        page.wait_for_selector("#removal-open", state="hidden", timeout=8000)
        self.assertEqual(self.shared_shifts(), ["1直"])

    def test_管理者モードでなければ消す釦は押せない(self) -> None:
        self.post("/api/settings/admin", {"enable": False})
        page = self.find_tests()
        self.assertTrue(page.locator("#removal-delete").is_disabled())


if __name__ == "__main__":
    import unittest

    unittest.main()
