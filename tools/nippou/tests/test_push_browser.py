"""共有へ保存: 画面から送るものを**本物のブラウザ**で確かめる (段階1)

前は `views/entry.js`・`views/settings.js`・`views/agg.js` の字を探して「仕組みが入っていること」
だけを見ていました(`test_push_history`・`test_push_scope`・`test_fix_route` の一部)。ここでは
画面で押して、送った要求と画面の動きを見ます:

    - 押した人は**欄に出ている作業者**(共有保存の履歴の「押した作業者」)
    - 送る範囲を添える: 日報入力の「共有へ保存」= いまの直・「過去分を共有へ送る」= 前の直、
      設定の「共有へ保存(いまの直)」「過去分を共有へ送る」も同じ
    - 断られたら「先に次を直してください」に**そこを開くボタン**を並べ、押せば開く。
      開いたばかりの空の紙(打ちかけを置くと「まだ1行も打っていません」と断られる)でも止まらない
    - 集計の「共有保存の履歴」に、直の終わりに対していつ押したかのグラフが出る
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._other_screen_browser import ScreenBrowserCase  # noqa: E402

#: 共有へ出せない間違い(梱包数が検入枚数を超える)が残った前の直
OLD = dict(report_date="2026年9月15日", line="L-1", shift="1直", page=1)


class PushBrowserCase(ScreenBrowserCase):
    def setUp(self) -> None:
        super().setUp()
        from nippou import config, user_settings

        share = self.tmp / "共有"
        share.mkdir()
        user_settings.save_many({config.KEY_ACCESS_DIR: str(share),
                                 config.KEY_REPORT_OUT_DIR: str(self.tmp / "出力")})
        self.post("/api/entry/line", {"line": "L-1"})

    def pushes(self, since: int = 0) -> list[dict]:
        return [b for p, b in self.sent[since:] if p.split("?")[0] == "/api/settings/push"]

    def press(self, page, selector: str) -> dict:
        mark = len(self.sent)
        page.click(selector)
        self.wait_until(page, lambda: self.pushes(mark), f"{selector} で送る要求")
        page.wait_for_timeout(300)
        return self.pushes(mark)[0]

    def leave_old_error(self) -> None:
        from nippou.db.models import DetailRecord, HeaderRecord

        self.repo().save(HeaderRecord(**OLD, worker="前の人"),
                         [DetailRecord(**OLD, row_no=1, lot="H573E52", ken="15", mai="10", tut="2",
                                       kz="08", kh="00", sz="09", sh="00")])
        self.settle_review()


class ScopeTests(PushBrowserCase):
    """送れたかどうかではなく、**何を添えて頼んだか**を見る(断られても頼みは出ている)。"""

    def test_日報入力の共有へ保存は欄の作業者といまの直を添える(self) -> None:
        page = self.open_entry()
        page.fill("#hd-worker", "高橋")
        page.dispatch_event("#hd-worker", "change")
        body = self.press(page, "#push-shared")
        self.assertEqual(body.get("worker"), "高橋", "押した人が欄の作業者になっていません")
        self.assertEqual(body.get("scope"), "current")
        self.assertEqual(self.errors, [])

    def test_過去分を共有へ送るは前の直だけを頼む(self) -> None:
        self.leave_old_error()
        page = self.open_screen("/", "#handover-push")   # 前の直が片付くまで表は伏せてある
        self.assertEqual(self.press(page, "#handover-push").get("scope"), "previous")

    def test_設定の2つのボタンもそれぞれの範囲を添える(self) -> None:
        page = self.open_screen("/settings?tab=output", "#push")
        self.assertEqual(self.press(page, "#push").get("scope"), "current")
        page.wait_for_timeout(500)
        self.assertEqual(self.press(page, "#push-prev").get("scope"), "previous")
        self.assertEqual(self.errors, [])


class FixRouteTests(PushBrowserCase):
    def test_断られたらそこを開くボタンを並べ_空の紙からでも開ける(self) -> None:
        self.leave_old_error()
        self.admin()                                   # ほかの日の直を開くのは管理者モード
        page = self.open_screen("/", "#handover-push")   # いまの直は開いたばかり(1行も打っていない)
        page.click("#handover-push")
        go = page.locator("#entry-skip-blockers li button")
        go.first.wait_for(timeout=8000)
        self.assertIn("2026年9月15日 1直 1ページ目を開く", go.first.inner_text())
        self.assertIn("2026年9月15日 L-1 1直", page.inner_text("#entry-skip-blockers"))
        mark = len(self.sent)
        go.first.click()
        self.wait_until(page, lambda: "/api/settings/recall" in self.paths(mark),
                        "開く要求(空の紙で止まっています)")
        saves = [p for p in self.paths(mark) if p.startswith("/api/entry/save")]
        self.assertTrue(saves, "移る前に打ちかけを置いていません")
        page.wait_for_function(
            "document.getElementById('sheet-key')?.dataset.openedDate === '2026年9月15日'", timeout=8000)
        self.assertEqual(page.evaluate("document.getElementById('sheet-key').dataset.openedShift"), "1直")


class PushLogCardTests(PushBrowserCase):
    def test_集計の共有保存の履歴にいつ押したかのグラフが出る(self) -> None:
        from tests.test_auto_export import payload

        self.assertTrue(self.post("/api/entry/save", payload()).get_json()["saved"])
        with patch("nippou.services.shift_check.run_pending", return_value=[]):
            self.assertEqual(self.post("/api/settings/push", {}).status_code, 200)
        page = self.open_screen("/agg", "#push-log")
        page.fill("#push-log-start", "2000-01-01")
        page.fill("#push-log-end", "2099-12-31")
        page.click("#push-log-reload")
        page.wait_for_selector("#push-log-chart svg", timeout=8000)
        self.assertIn("直の終わりに対して", page.inner_text("#push-log-chart h3"))
        self.assertGreater(page.locator("#push-log-chart svg circle, #push-log-chart svg rect").count(), 0)
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    import unittest

    unittest.main()
