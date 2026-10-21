"""点検表の画面を**本物のブラウザ**で動かす試験の土台(段階1)。

    - 要求は Flask のテストクライアントへ回す(ポートを開かない。`http://inspection.test/`)
    - 見本の点検表(`demo`)で動かすので、Excel・Word は使わない(`tests.test_web.WebTestBase`)
    - **答えを止められる**(`hold` に道の頭を入れる → `held` に溜まる → `release` で返す)
    - わざと断る(`refuse`)・試験が自分で返す(`pages` = HTML、`answers` = JSON)
    - 送られた要求は `sent` に (方法, 道, 本文の JSON)。試験が答えた・断った・止めたものも入る
    - 画面の誤り(pageerror)は `errors`

使う試験: `test_theme_browser`・`test_preview_browser`・`test_print_browser` など。
"""
from __future__ import annotations

import json
import time
from typing import Callable

from tests._browser import chromium_or_skip
from tests.test_web import AUTH, WebTestBase

ORIGIN = "http://inspection.test"

#: 離れる合図(`sendBeacon`)は取り次ぎを通らない。試験では送ったことにする
_BEACON_SCRIPT = "navigator.sendBeacon = () => true;"


class PageBrowserCase(WebTestBase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.pw, cls.browser = chromium_or_skip()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.pw.stop()

    def setUp(self) -> None:
        super().setUp()
        self.errors: list[str] = []
        self.sent: list[tuple[str, str, dict]] = []
        self.hold: tuple[str, ...] = ()
        self.held: list = []
        self.pages: dict[str, str] = {}
        self.answers: dict[str, tuple[int, dict]] = {}
        self.refuse: dict[str, int] = {}
        self.addCleanup(self._drain)

    def new_context(self, **kwargs):
        ctx = self.browser.new_context(viewport={"width": 1400, "height": 900}, **kwargs)
        self.addCleanup(ctx.close)
        ctx.route(f"{ORIGIN}/**", self._route)
        ctx.add_init_script(_BEACON_SCRIPT)
        return ctx

    # -- 要求の取り次ぎ --------------------------------------------------
    def _route(self, route) -> None:
        path = route.request.url.split(ORIGIN, 1)[1]
        bare = path.split("?", 1)[0]
        if bare in self.pages:
            route.fulfill(status=200, body=self.pages[bare],
                          headers={"content-type": "text/html; charset=utf-8"})
            return
        self._note(route.request, path)
        for prefix, (status, body) in self.answers.items():
            if path.startswith(prefix):
                route.fulfill(status=status, body=json.dumps(body, ensure_ascii=False),
                              headers={"content-type": "application/json"})
                return
        for prefix, status in self.refuse.items():
            if path.startswith(prefix):
                route.fulfill(status=status,
                              body='{"error": {"code": "test", "message": "試験で断りました"}}',
                              headers={"content-type": "application/json"})
                return
        if self.hold and path.startswith(self.hold):
            self.held.append(route)
            return
        self._forward(route)

    def _note(self, req, path: str) -> None:
        try:
            body = json.loads(req.post_data or "{}") if req.method == "POST" else {}
        except ValueError:
            body = {}
        self.sent.append((req.method, path, body if isinstance(body, dict) else {}))

    def _forward(self, route) -> None:
        req = route.request
        path = req.url.split(ORIGIN, 1)[1]
        headers = dict(AUTH)
        if req.headers.get("content-type"):
            headers["Content-Type"] = req.headers["content-type"]
        res = self.client.open(path, method=req.method, headers=headers,
                               data=req.post_data_buffer)
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

    def release(self, page, index: int = 0) -> None:
        self._forward(self.held.pop(index))
        page.wait_for_timeout(300)

    def wait_until(self, page, ok: Callable[[], bool], what: str, timeout: float = 8.0) -> None:
        """Python 側の状態(`held`・`sent`)を待つ。待つあいだも要求は捌かれる。"""
        deadline = time.monotonic() + timeout
        while not ok():
            if time.monotonic() > deadline:
                self.fail(f"{what} になりません")
            page.wait_for_timeout(50)

    def wait_held(self, page, count: int = 1) -> None:
        self.wait_until(page, lambda: len(self.held) >= count, f"止めた要求が {count} 本")

    def posts_to(self, prefix: str) -> list[dict]:
        return [b for m, p, b in self.sent if m == "POST" and p.startswith(prefix)]

    # -- 画面 ------------------------------------------------------------
    def open(self, path: str = "/", ctx=None, *, wait_list: bool = True, prepare=None):
        """画面を開く。`wait_list` なら点検表の一覧が描かれるまで待つ。"""
        ctx = ctx or self.new_context()
        page = ctx.new_page()
        # 待つのは状態が変わるまで。変わらないなら長く待たない(壊れたときに早く分かる)
        page.set_default_timeout(8000)
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.on("dialog", lambda d: d.accept())
        if prepare:
            prepare(page)
        page.goto(f"{ORIGIN}{path}", wait_until="load")
        if wait_list:
            page.wait_for_selector("#items .item", state="attached")
        return page

    def item(self, name_part: str) -> dict:
        """見本の一覧から、名前に `name_part` を含む点検表(サーバの形)。"""
        inv = self.get("/api/inventory").get_json()["inventory"]
        for c in inv["categories"]:
            for s in c["subcategories"]:
                for it in s["items"]:
                    if name_part in it["name"]:
                        return it
        raise AssertionError(f"見本に {name_part} が無い")
