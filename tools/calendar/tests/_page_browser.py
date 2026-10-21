"""カレンダーの画面を**本物のブラウザ**で動かす試験の土台(段階1)。

    - 要求は Flask のテストクライアントへ回す(ポートを開かない。`http://calendar.test/`)
    - **答えを止められる**(`hold` に道の頭を入れる → `held` に溜まる → `release` で返す)
    - わざと断る(`refuse`)・試験が自分で返す(`pages` = HTML、`answers` = JSON)
    - 送った要求は `sent` に (方法, 道, 本文の JSON)。離れるときの合図(`sendBeacon`)は
      取り次ぎを通らないので、画面の中で受けて `beacons()` で読む
    - 画面の誤り(pageerror)は `errors`、確かめの窓(confirm・alert)の文言は `dialogs`

使う試験: `test_theme_browser`・`test_history_browser`・`test_background_browser` など。
"""
from __future__ import annotations

import json
import time
import unittest
from typing import Callable

from tests import _web
from tests._browser import chromium_or_skip

ORIGIN = "http://calendar.test"

#: 画面が自分で出す見張りの要求(心拍・名乗り・画面のエラー)。業務の書き込みではない
HOUSEKEEPING = ("/api/alive", "/api/screen/", "/api/client-log")

#: 離れるときの合図(`sendBeacon`)を画面の中で受け取っておく。本物は取り次ぎを通らず、
#: 試験の名前(calendar.test)を引きに行って console に失敗が出る
_BEACON_SCRIPT = """
window.__beacons = [];
navigator.sendBeacon = (url, data) => { window.__beacons.push({ url: String(url), data }); return true; };
"""


class PageBrowserCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.pw, cls.browser = chromium_or_skip()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.pw.stop()

    def setUp(self) -> None:
        super().setUp()
        from calendar_app import screen_lock

        _web.reset_sync()
        self.addCleanup(_web.reset_sync)
        self.addCleanup(screen_lock.reset)
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()
        self.errors: list[str] = []
        self.dialogs: list[str] = []
        #: 送られた要求 (方法, 道, 本文の JSON)。試験が答えた・断った・止めたものも入る
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
        """送られた要求を覚える(試験が自分で答えた・断った・止めたものも)。"""
        try:
            body = json.loads(req.post_data or "{}") if req.method == "POST" else {}
        except ValueError:
            body = {}
        self.sent.append((req.method, path, body if isinstance(body, dict) else {}))

    def _forward(self, route) -> None:
        req = route.request
        path = req.url.split(ORIGIN, 1)[1]
        headers = dict(_web.auth())
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

    def writes(self) -> list[tuple[str, str, dict]]:
        """業務の書き込み(見張りの要求を除いた GET 以外)。"""
        return [(m, p, b) for m, p, b in self.sent
                if m != "GET" and not p.startswith(HOUSEKEEPING)]

    def posts_to(self, prefix: str) -> list[dict]:
        return [b for m, p, b in self.sent if m == "POST" and p.startswith(prefix)]

    # -- 画面 ------------------------------------------------------------
    def open(self, path: str, ctx=None, *, wait_for: str = ".ribbon", prepare=None):
        """画面を開く。**開いた画面がこの端末のただ1枚**になる(前に開いた画面は名乗りを
        失う。試験では閉じた合図が届かないので、残ったままだと2枚目として断られる)。

        :param prepare: 開く前に画面へすること(時計を止める `page.clock` など)
        """
        from calendar_app import screen_lock

        screen_lock.reset()
        ctx = ctx or self.new_context()
        page = ctx.new_page()
        # 待つのは状態が変わるまで。変わらないなら長く待たない(壊れたときに早く分かる)
        page.set_default_timeout(8000)
        page.on("pageerror", lambda e: self.errors.append(str(e)))

        def answer(dialog) -> None:
            self.dialogs.append(dialog.message)
            dialog.accept()

        page.on("dialog", answer)
        if prepare:
            prepare(page)
        sep = "&" if "?" in path else "?"
        page.goto(f"{ORIGIN}{path}{sep}t={_web.TOKEN}", wait_until="load")
        if wait_for:
            page.wait_for_selector(wait_for)
        return page

    @staticmethod
    def beacons(page) -> list[dict]:
        """送った離れる合図(`sendBeacon`)の (宛先, 本文の JSON)。"""
        return page.evaluate("""async () => {
            const out = [];
            for (const b of window.__beacons) {
                const text = b.data && b.data.text ? await b.data.text() : String(b.data || '');
                let body = {};
                try { body = JSON.parse(text); } catch (e) { body = { raw: text }; }
                out.push({ url: b.url, body });
            }
            return out; }""")
