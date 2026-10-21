"""直の締めくくり: **画面を出るときは、必ず答えを残す** (本物のブラウザ。段階1)

前は字を探していました(`test_shift_closing` の LeaveAlwaysAnswersTests ── `location.href` の
数と、ack より後ろにあるか)。ここでは全画面の確認を本当に開き、出口を押して確かめます:

    - 「直しに戻る」も「確認しました」も、**覚えさせて(ack)から**日報入力へ出る
    - 覚えられなかったら出ない(出た先で1分タイマーに連れ戻されるので)

詰まりの正体は画面のほうでした ── 「直しに戻る」だけ ack を飛ばして出ていたので、戻った先で
1分タイマーに連れ戻されていました(「一瞬だけ画面が切り替わるがすぐ実績画面に戻され何もできない」)。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._entry_browser import ORIGIN, EntryBrowserCase  # noqa: E402

ACK = "/api/graph/review/ack"
#: 打ってある1ページ。直の終わりのぶん(最終時間・休憩)が空なので「直しに戻る」になる
SHEET = {"rows": {"1": {"LOT": "N7131T0", "KZ": "08", "KH": "00", "SZ": "10", "SH": "00",
                        "CON": "100", "WEI": "1000"}},
         "header": {"worker": "山田"}, "checks": {}}


class LeaveTests(EntryBrowserCase):
    def open_review(self):
        page = self.watch(self.ctx.new_page())
        page.goto(f"{ORIGIN}/graph/review", wait_until="load")
        page.wait_for_selector("#review-act")
        # なぜ出ているか(と機会)を訊く答えまで待つ
        self.wait_until(page, lambda: any(p == "/api/graph/review" for p, _ in self.sent),
                        "確認の理由を訊く要求")
        page.wait_for_timeout(300)
        return page

    def leave_by(self, page, button: str) -> list[str]:
        mark = len(self.sent)
        page.click(button)
        page.wait_for_function("location.pathname === '/'", timeout=8000)
        page.wait_for_selector("#grid-body")
        return [p for p, _ in self.sent[mark:]]

    def test_直しに戻るは覚えてから日報入力へ出る(self) -> None:
        self.post("/api/entry/save", SHEET)
        page = self.open_review()
        self.assertEqual(page.get_attribute("#review-act", "data-action"), "fix")    # 直しに戻る
        paths = self.leave_by(page, "#review-act")
        self.assertIn(ACK, paths, "覚えずに出ています(戻った先で連れ戻されます)")
        self.assertLess(paths.index(ACK), paths.index("/"), "覚える前に画面を出ています")
        self.assertEqual(self.errors, [])

    def test_確認しましたも覚えてから出る(self) -> None:
        page = self.open_review()                               # 1ページも無い → 確認しました
        self.assertIn("確認しました", page.text_content("#review-act"))
        paths = self.leave_by(page, "#review-act")
        self.assertIn(ACK, paths)
        self.assertLess(paths.index(ACK), paths.index("/"))

    def test_覚えられなければ出ない(self) -> None:
        self.post("/api/entry/save", SHEET)
        page = self.open_review()
        self.refuse[ACK] = 500
        page.click("#review-act")
        page.wait_for_timeout(1000)
        self.assertEqual(page.evaluate("location.pathname"), "/graph/review",
                         "覚えられなかったのに出ています(出た先で連れ戻されます)")


if __name__ == "__main__":
    unittest.main()
