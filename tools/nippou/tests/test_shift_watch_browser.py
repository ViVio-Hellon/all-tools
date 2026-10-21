"""1分ごとの見張りと塗り直し: **打ちかけごと連れて行かない・変えたら塗り直す** (本物のブラウザ。段階1)

前は字を探していました(`test_entry_leave` の「見張りは打ちかけごと連れて行かない」・
`test_stale_screen` の `KnownCasesTests`)。ここでは見張り(`shift_end.js` の `tick`)を
本当に回し、サーバの答えは試験が決めて、**画面が引き直したか・どこへ移ったか・
打った値が残ったか**を見ます:

    実績の確認画面へ連れて行く
        日報入力 … 打ちかけを置いてから移る(確かめの道 `go` を通る)
        確かめを持たない画面(記録を見る など)… 打ちかけがあれば連れて行かず、知らせる
    残り5分の自動確定が走ったら塗り直す
        確定(`ran`)でも拾い(`swept`)でも塗り直す ── 片方だけだと古いまま残る側ができる
        打っている最中(焦点が欄にある)は見送る
        確かめを持たない画面で打ちかけがあれば見送る(日報入力は置いてから引き直す)
    共有へ保存(設定)・前の直を引き継ぐ(日報入力)のあとは塗り直す
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._entry_browser import ORIGIN  # noqa: E402
from tests.test_entry_shell_browser import ShellCase  # noqa: E402

DUE = {"due": True, "stage": "warn", "reason": "直の終わりです"}
RAN = {"ran": True, "swept": [], "message": "打ってあるぶんを確定しました"}
SWEPT = {"ran": False, "swept": [{"report_date": "前の日", "shift": "3直"}],
         "message": "締まっていなかった直を確定しました"}


class WatchCase(ShellCase):
    def open_page(self, path: str, ready: str):
        page = self.watch(self.ctx.new_page())
        page.goto(f"{ORIGIN}{path}", wait_until="load")
        page.wait_for_selector(ready)
        # 開いたときの1回目の見張りまで済ませる(このあとの答えは試験が決める。設定は
        # 確認画面へ連れて行かない画面なので、3つ目は訊かない ── 2つ目の確定で見る)
        self.wait_until(page, lambda: "/api/entry/close" in self.paths(), "1回目の見張り")
        page.wait_for_timeout(300)
        return page

    def toasts(self, page) -> list[str]:
        return page.evaluate("[...document.querySelectorAll('.toast__text')].map(t => t.textContent)")

    def refreshed(self, since: int, path: str) -> bool:
        """`since` のあとに、いまの画面を取り直したか(`nav.refresh` は同じ道を GET する)。"""
        return path in self.paths(since)

    def tick_and_settle(self, page) -> int:
        """見張りを1回まわし、3つ目(確認画面へ)の答えまで待つ。まわす前の `sent` の長さを返す。"""
        mark = len(self.sent)
        self.tick(page)
        self.wait_sent(page, mark, "/api/graph/review", "見張りの3つ目(確認画面へ)")
        page.wait_for_timeout(600)
        return mark


class ReviewWatchTests(WatchCase):
    """実績の確認画面へ連れて行く。**打ちかけごと連れて行かない。**"""

    def test_日報入力は打ちかけを置いてから確認画面へ移る(self) -> None:
        page = self.open_entry()
        self.wait_until(page, lambda: "/api/graph/review" in self.paths(), "1回目の見張り")
        self.type_in(page, "ZAI1", "52S- R")
        self.answers["/api/graph/review"] = (200, DUE)
        mark = len(self.sent)
        self.tick(page)
        page.wait_for_function("location.pathname === '/graph/review'", timeout=8000)
        at = self.paths(mark).index("/graph/review")
        draft = self.first(mark, self.is_draft)
        self.assertIsNotNone(draft, f"打ちかけを置かずに連れて行きました: {self.paths(mark)}")
        self.assertLess(draft, at)
        self.assertEqual(self.sent[mark + draft][1]["rows"]["1"].get("ZAI"), "52S- R")
        self.assertEqual(self.errors, [])

    def test_確かめを持たない画面は打ちかけがあれば連れて行かずに知らせる(self) -> None:
        page = self.open_page("/records", "#lot-no")
        self.type_in(page, "lot-no", "N71")
        page.mouse.click(5, 5)                         # 焦点は外す(打っている最中の見送りに頼らない)
        self.answers["/api/graph/review"] = (200, DUE)
        self.tick_and_settle(page)
        self.assertEqual(page.evaluate("location.pathname"), "/records", "打ちかけごと連れて行きました")
        self.assertEqual(self.value(page, "lot-no"), "N71")
        self.assertTrue(any("直の実績の確認があります" in t for t in self.toasts(page)), self.toasts(page))

    def test_打ちかけが無ければ確認画面へ移る(self) -> None:
        """対照: 同じ画面でも、打った欄が無ければ連れて行く。"""
        page = self.open_page("/records", "#lot-no")
        self.answers["/api/graph/review"] = (200, DUE)
        self.tick(page)
        page.wait_for_function("location.pathname === '/graph/review'", timeout=8000)


class RepaintTests(WatchCase):
    """**変えたら塗り直す。** 画面は HTML が描かれた時点の答えを持っているので。"""

    def setUp(self) -> None:
        super().setUp()
        self.answers["/api/graph/review"] = (200, {"due": False})

    def test_確定でも拾いでも塗り直す(self) -> None:
        page = self.open_page("/records", "#lot-no")
        for name, answer in (("確定", RAN), ("拾い", SWEPT)):
            with self.subTest(name):
                self.answers["/api/entry/close"] = (200, answer)
                mark = self.tick_and_settle(page)
                self.wait_until(page, lambda: self.refreshed(mark, "/records"), f"{name}のあとの塗り直し")
                self.assertTrue(any(answer["message"] in t for t in self.toasts(page)))
                page.wait_for_selector("#lot-no")

    def test_何も走らなければ塗り直さない(self) -> None:
        page = self.open_page("/records", "#lot-no")
        self.answers["/api/entry/close"] = (200, {"ran": False, "swept": []})
        mark = self.tick_and_settle(page)
        self.assertFalse(self.refreshed(mark, "/records"))

    def test_打っている最中は塗り直さない(self) -> None:
        """焦点が欄にあるあいだは見送る(塗り直しは本文を差し替えるので、打ちかけが消える)。"""
        page = self.open_page("/records", "#lot-no")
        page.focus("#lot-no")                          # 打ちはじめ(まだ1文字も打っていない)
        self.answers["/api/entry/close"] = (200, RAN)
        mark = self.tick_and_settle(page)
        self.assertFalse(self.refreshed(mark, "/records"), "打っている最中に塗り直しました")

    def test_確かめを持たない画面は打ちかけがあれば塗り直さない(self) -> None:
        page = self.open_page("/records", "#lot-no")
        self.type_in(page, "lot-no", "N71")
        page.mouse.click(5, 5)
        self.answers["/api/entry/close"] = (200, RAN)
        mark = self.tick_and_settle(page)
        self.assertFalse(self.refreshed(mark, "/records"), "打ちかけのある画面を塗り直しました")
        self.assertEqual(self.value(page, "lot-no"), "N71")

    def test_日報入力は打ちかけを置いてから塗り直す(self) -> None:
        page = self.open_entry()
        self.wait_until(page, lambda: "/api/graph/review" in self.paths(), "1回目の見張り")
        self.hold = ("/api/entry/state",)               # 離れた答えの自動保存を先に走らせない
        self.type_in(page, "ZAI1", "52S- R")
        page.mouse.click(5, 5)
        self.answers["/api/entry/close"] = (200, RAN)
        mark = len(self.sent)
        self.tick(page)
        self.wait_until(page, lambda: self.refreshed(mark, "/"), "確定のあとの塗り直し")
        draft = self.first(mark, self.is_draft)
        self.assertIsNotNone(draft, f"打ちかけを置かずに塗り直しました: {self.paths(mark)}")
        self.assertLess(draft, self.paths(mark).index("/"))
        self.hold = ()
        page.wait_for_selector("#grid-body")
        page.wait_for_timeout(600)
        self.assertEqual(self.value(page, "ZAI1"), "52S- R")

    def test_共有へ保存したら設定画面を塗り直す(self) -> None:
        """「共有へ保存: 1件」のすぐ横で「共有へ未送信 1直ぶん」が減らないままだった。"""
        self.answers["/api/settings/push/plan"] = (200, {"confirm_needed": False})
        self.answers["/api/settings/push"] = (200, {"message": "共有へ保存: 1件"})
        page = self.open_page("/settings", "#push")
        mark = len(self.sent)
        page.dispatch_event("#push", "click")
        at = self.wait_sent(page, mark, "/api/settings/push")
        self.wait_until(page, lambda: "/settings" in self.paths(mark + at), "共有へ保存のあとの塗り直し")
        self.assertEqual(self.errors, [])

    def test_前の直を引き継いだら塗り直す(self) -> None:
        """「前の直が共有へ出ていません / 入力は始められません」が出たままにしない。"""
        self.answers["/api/entry/handover"] = (200, {"message": "引き継ぎました"})
        page = self.open_entry()
        mark = len(self.sent)
        # 引き継ぐボタンは、前の直が残っているときだけ見える。ここでは押した後だけを見る
        page.dispatch_event("#handover-take", "click")
        at = self.wait_sent(page, mark, "/api/entry/handover")
        self.assertEqual(self.sent[mark + at][1].get("worker"), "山田")
        self.wait_until(page, lambda: "/" in self.paths(mark + at), "引き継いだあとの塗り直し")
        page.wait_for_selector("#grid-body")
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
