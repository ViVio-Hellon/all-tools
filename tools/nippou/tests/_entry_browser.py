"""日報入力を**本物のブラウザ**で打つ試験の土台(段階1)。

    - 要求は Flask のテストクライアントへ回す(ポートを開かない。`http://app.test/`)
    - **答えを止められる**(`hold` に道の頭を入れる → `held` に溜まる → `release` で返す)
    - サーバの時計は直の中ほど(その日の 10:00)に止める(直の残り5分は自動確定で画面が移るため)
    - 止めたまま終わった要求は閉じる前に片付ける(Playwright の CancelledError を出さない)

使う試験: `test_late_reply_browser`(遅れて届いた答え)・`test_entry_leave_browser`(打ちかけを置いてから出る)。
"""
from __future__ import annotations

import json
import time
import unittest
from typing import Callable, Optional

from tests._browser import chromium_or_skip
from tests._web import HAS_FLASK, HEADERS, SKIP_REASON, WebTestCase

ORIGIN = "http://app.test"


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class EntryBrowserCase(WebTestCase):
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
        self.ctx.route(f"{ORIGIN}/**", self._route)
        self.errors: list[str] = []
        #: 止める要求の道(先頭一致)。空なら全部そのまま通す
        self.hold: tuple[str, ...] = ()
        self.held: list = []
        #: 通した要求(道, 本文の JSON)。何が送られたかを見る
        self.sent: list[tuple[str, dict]] = []
        #: 試験が自分で返す道(`{道: HTML}`)。埋め込む親の画面など
        self.pages: dict[str, str] = {}
        #: わざと断る道(先頭一致 → HTTP の番号)。保存が失敗したときの画面を見る
        self.refuse: dict[str, int] = {}
        #: 画面が出した確かめの窓(confirm・alert)の文言。窓は「はい」で閉じる
        self.dialogs: list[str] = []
        self.addCleanup(self._drain)

    # -- 要求の取り次ぎ --------------------------------------------------
    def _route(self, route) -> None:
        path = route.request.url.split(ORIGIN, 1)[1]
        if path in self.pages:
            route.fulfill(status=200, body=self.pages[path],
                          headers={"content-type": "text/html; charset=utf-8"})
            return
        for prefix, status in self.refuse.items():
            if path.startswith(prefix):
                route.fulfill(status=status, body='{"error": {"code": "test", "message": "試験で断りました"}}',
                              headers={"content-type": "application/json"})
                return
        if self.hold and path.startswith(self.hold):
            self.held.append(route)                 # 返すのは試験が決める
            return
        self._forward(route)

    def _forward(self, route) -> None:
        req = route.request
        path = req.url.split(ORIGIN, 1)[1]
        try:
            body = json.loads(req.post_data or "{}") if req.method == "POST" else {}
        except ValueError:
            body = {}
        self.sent.append((path, body if isinstance(body, dict) else {}))
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

    def _drain(self) -> None:
        self.hold = ()
        while self.held:
            route = self.held.pop()
            try:
                route.abort()
            except Exception:                       # noqa: BLE001 - もう閉じた画面の要求
                pass

    def wait_until(self, page, ok: Callable[[], bool], what: str, timeout: float = 8.0) -> None:
        """Python 側の状態(`held`・`sent`)を待つ。待つあいだも要求は捌かれる。"""
        deadline = time.monotonic() + timeout
        while not ok():
            if time.monotonic() > deadline:
                self.fail(f"{what} になりません")
            page.wait_for_timeout(50)

    def wait_held(self, page, count: int, timeout: float = 8.0) -> None:
        self.wait_until(page, lambda: len(self.held) >= count,
                        f"止めた要求が {count} 本(いま {len(self.held)} 本)", timeout)

    def release(self, page, index: int = 0) -> None:
        """止めた要求を1本返し、画面が塗り終えるのを待つ。"""
        self._forward(self.held.pop(index))
        page.wait_for_timeout(400)

    def saves(self, *, draft: Optional[bool] = None) -> list[dict]:
        """送った保存(`/api/entry/save`)の本文。`draft` で打ちかけ/確定に絞る。"""
        out = [body for path, body in self.sent if path.startswith("/api/entry/save")]
        if draft is not None:
            out = [b for b in out if bool(b.get("draft")) is draft]
        return out

    # -- 画面 ------------------------------------------------------------
    def watch(self, page):
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        def answer(dialog) -> None:
            self.dialogs.append(dialog.message)
            dialog.accept()

        page.on("dialog", answer)
        return page

    def open_entry(self, path: str = "/"):
        page = self.watch(self.ctx.new_page())
        page.goto(f"{ORIGIN}{path}", wait_until="load")
        page.wait_for_selector("#grid-body")
        page.wait_for_timeout(600)
        self.name_worker(page)
        return page

    @staticmethod
    def name_worker(page) -> None:
        """作業者が決まっていないと表は伏せてある。名前を入れて開ける。"""
        if page.evaluate("document.body.classList.contains('needs-worker')"):
            page.fill("#hd-worker", "山田")
            page.dispatch_event("#hd-worker", "change")
            page.wait_for_function("!document.body.classList.contains('needs-worker')",
                                   timeout=5000)
        page.wait_for_timeout(300)

    @staticmethod
    def type_in(page, field: str, text: str, frame=None) -> None:
        """人と同じに打つ(焦点を当てて1文字ずつ)。離れはしない。"""
        box = (frame or page).locator(f"#{field}")
        box.click()
        box.press("Control+a")
        box.press_sequentially(text, delay=20)

    @staticmethod
    def value(page, field: str) -> str:
        return page.input_value(f"#{field}")
