"""日報入力: **遅れて届いた答え・追い越された答えで、打った欄が戻らない** (本物のブラウザ)

「打った値が戻る・消える」は何度も直してきた不具合です(README に「消え」117回)。
画面は要求ごとに「頼んだときの打った番号」(`editSeq`)を覚え、答えを塗るときに
それより後に打った欄は塗らない(`paint(..., { since })`)。この試験は**答えをわざと
止めて**、そのあいだに打ち、止めた答えを返したり順番を入れ替えたりして確かめます。

    欄を離れた答え(`/api/entry/state`)を止めて、別の欄を打つ
    2つの答えを逆の順に返す(追い越し)
    ロット№の引き(`/api/entry/lot`)を止めて、同じ行のほかの欄を打つ
    保存(`/api/entry/save`)を止めて、打つ

サーバの時計は直の中ほどに止めます(直の残り5分は自動確定で画面が移るため)。
本物のブラウザ(Playwright)で通します。無い環境では飛ばします(CI では飛ばしません)。
"""
from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._browser import chromium_or_skip  # noqa: E402
from tests._web import HAS_FLASK, HEADERS, SKIP_REASON, WebTestCase  # noqa: E402


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class LateReplyTests(WebTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.pw, cls.browser = chromium_or_skip()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.pw.stop()

    def setUp(self) -> None:
        super().setUp()
        from datetime import date, datetime, time as clock_time
        from tests.test_scenario_shift_flow import _Now, install_clock

        _Now.value = datetime.combine(date.today(), clock_time(10, 0))
        install_clock(self)
        self.ctx = self.browser.new_context(viewport={"width": 1400, "height": 900})
        self.addCleanup(self.ctx.close)
        self.ctx.route("http://app.test/**", self._route)
        self.errors: list[str] = []
        #: 止める要求の道(先頭一致)。空なら全部そのまま通す
        self.hold: tuple[str, ...] = ()
        self.held: list = []
        # 止めたまま終わった要求は**閉じる前に片付ける**(放っておくと Playwright が待ちを
        # 取り消して CancelledError を出す)。後片付けは逆順なので ctx.close より先に走る
        self.addCleanup(self._drain)

    def _drain(self) -> None:
        self.hold = ()
        while self.held:
            route = self.held.pop()
            try:
                route.abort()
            except Exception:                       # noqa: BLE001 - もう閉じた画面の要求
                pass

    # -- 要求の取り次ぎ --------------------------------------------------
    def _route(self, route) -> None:
        path = route.request.url.split("http://app.test", 1)[1]
        if self.hold and path.startswith(self.hold):
            self.held.append(route)                 # 返すのは試験が決める
            return
        self._forward(route)

    def _forward(self, route) -> None:
        req = route.request
        path = req.url.split("http://app.test", 1)[1]
        headers = dict(HEADERS)
        if req.headers.get("x-tab"):
            headers["X-Tab"] = req.headers["x-tab"]
        res = self.client.open(path, method=req.method, headers=headers,
                               data=req.post_data_buffer,
                               content_type=req.headers.get("content-type"))
        try:
            route.fulfill(status=res.status_code, body=res.get_data(),
                          headers={"content-type": res.headers.get("Content-Type", "text/plain")})
        finally:
            res.close()

    def wait_held(self, page, count: int, timeout: float = 8.0) -> None:
        deadline = time.monotonic() + timeout
        while len(self.held) < count:
            if time.monotonic() > deadline:
                self.fail(f"止めた要求が {count} 本になりません(いま {len(self.held)} 本)")
            page.wait_for_timeout(50)

    def release(self, page, index: int = 0) -> None:
        """止めた要求を1本返し、画面が塗り終えるのを待つ。"""
        self._forward(self.held.pop(index))
        page.wait_for_timeout(400)

    # -- 画面 ------------------------------------------------------------
    def open_entry(self):
        page = self.ctx.new_page()
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.on("dialog", lambda d: d.accept())
        page.goto("http://app.test/", wait_until="load")
        page.wait_for_selector("#grid-body")
        page.wait_for_timeout(600)
        if page.evaluate("document.body.classList.contains('needs-worker')"):
            page.fill("#hd-worker", "山田")
            page.dispatch_event("#hd-worker", "change")
            page.wait_for_function("!document.body.classList.contains('needs-worker')",
                                   timeout=5000)
        page.wait_for_timeout(300)
        return page

    @staticmethod
    def type_in(page, field: str, text: str) -> None:
        """人と同じに打つ(焦点を当てて1文字ずつ)。離れはしない。"""
        box = page.locator(f"#{field}")
        box.click()
        box.press("Control+a")
        box.press_sequentially(text, delay=20)

    @staticmethod
    def value(page, field: str) -> str:
        return page.input_value(f"#{field}")

    # -- 試験 ------------------------------------------------------------
    def test_離れた答えが遅れても_そのあいだに打った欄は戻らない(self) -> None:
        page = self.open_entry()
        self.hold = ("/api/entry/state",)
        self.type_in(page, "MAI1", "10")
        page.locator("#MAI1").press("Tab")           # 離れる → 答えを止める
        self.wait_held(page, 1)
        self.type_in(page, "HIT1", "3")              # 答えを待つあいだに打つ
        self.hold = ()
        self.release(page)
        self.assertEqual(self.value(page, "HIT1"), "3")
        self.assertEqual(self.value(page, "MAI1"), "10")
        self.assertEqual(self.errors, [])

    def test_追い越した古い答えで新しい欄が戻らない(self) -> None:
        page = self.open_entry()
        self.hold = ("/api/entry/state",)
        self.type_in(page, "MAI1", "10")
        page.locator("#MAI1").press("Tab")           # 1本目(HIT1 が空のときの答え)
        self.wait_held(page, 1)
        self.type_in(page, "HIT1", "3")
        page.locator("#HIT1").press("Tab")           # 2本目
        self.wait_held(page, 2)
        self.hold = ()
        self.release(page, 1)                        # 新しいほうを先に返す
        self.release(page, 0)                        # 古いほうが後から届く
        self.assertEqual(self.value(page, "HIT1"), "3")
        self.assertEqual(self.value(page, "MAI1"), "10")
        self.assertEqual(self.errors, [])

    def test_ロットを引くあいだに同じ行に打った欄は戻らない(self) -> None:
        """ロットの答えは**焦点のある行も書き換える**(`forceRow`)。それでも後から打った欄は守る。"""
        page = self.open_entry()
        self.hold = ("/api/entry/lot",)
        self.type_in(page, "LOT1", "N7131T0")
        page.locator("#LOT1").press("Tab")
        self.wait_held(page, 1)
        self.hold = ()                               # 離れた答え(state)は通す
        self.type_in(page, "HIT1", "4")
        self.release(page)
        self.assertEqual(self.value(page, "HIT1"), "4")
        self.assertEqual(self.value(page, "LOT1"), "N7131T0")
        self.assertEqual(self.errors, [])

    def test_保存を待つあいだに打った欄は戻らない(self) -> None:
        page = self.open_entry()
        self.type_in(page, "MAI1", "10")
        page.locator("#MAI1").press("Tab")
        page.wait_for_timeout(600)                   # 離れた答えまで済ませる
        self.hold = ("/api/entry/save",)
        page.click("#save")
        deadline = time.monotonic() + 8
        while not any(json.loads(r.request.post_data or "{}").get("silent") is not True
                      for r in self.held):
            if time.monotonic() > deadline:
                self.fail("保存(確定)の要求が来ません")
            page.wait_for_timeout(50)
        self.type_in(page, "HIT1", "5")              # 保存を待つあいだに打つ
        # **焦点も外す**(打っている欄は別の守りで書き換えないので、それに頼らずに確かめる)
        page.locator("#HIT1").press("Tab")
        page.wait_for_timeout(600)                   # 離れた答え(止めていない)を塗り終える
        self.hold = ()
        while self.held:
            self.release(page)
        self.assertEqual(self.value(page, "HIT1"), "5")
        self.assertEqual(self.value(page, "MAI1"), "10")
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
