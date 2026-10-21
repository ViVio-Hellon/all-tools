"""日報の**どの画面でも**本物のブラウザで動かす土台(段階1。外枠・音・印刷・グラフなど)。

`EntryBrowserCase`(日報入力の土台)に、次を足しただけです:

    - **答えを試験が決める**(`answers` に道の頭 → `(番号, 本文)` か、要求を受けて
      それを返す関数)。サーバの答えを少し変えるだけなら `rewrite`(道の頭 → 本文を直す関数)
    - **通した要求を見出しごと覚える**(`requests` に (方法, 道, 見出し))── 誰も押していない
      通信の印(`X-Quiet`)やどの画面からか(`X-Screen`)を見る
    - **鳴らした音を覚える**(`played`)。`<audio>` の `play()` を差し替え、鳴らそうとした
      道を `window.__played` に積む(本当には鳴らさない。自動再生の制限にもかからない)
    - どの画面でも開ける `open_screen`(起動しきる `APP.booted` まで待つ)
"""
from __future__ import annotations

import json
from typing import Callable, Union

from tests._entry_browser import ORIGIN, EntryBrowserCase

#: `<audio>`/`new Audio()` の `play()` を差し替える(鳴らした道を覚えて、すぐ「鳴った」と返す)
PLAY_SPY = """
  window.__played = [];
  HTMLMediaElement.prototype.play = function () {
    window.__played.push(this.src || "");
    return Promise.resolve();
  };
"""

Answer = Union[tuple, Callable[[object], tuple]]


class ScreenBrowserCase(EntryBrowserCase):
    def setUp(self) -> None:
        super().setUp()
        #: 試験が返す答え(道の頭 → (番号, 本文の dict) か、要求 → それを返す関数)
        self.answers: dict[str, Answer] = {}
        #: サーバの答えを直してから返す(道の頭 → 本文の dict を直して返す関数)
        self.rewrite: dict[str, Callable[[dict], dict]] = {}
        #: 通した要求(方法, 道, 見出し)。試験が返したものも含む
        self.requests: list[tuple[str, str, dict]] = []
        self.ctx.add_init_script(PLAY_SPY)

    # -- 要求の取り次ぎ --------------------------------------------------
    def _route(self, route) -> None:
        req = route.request
        path = req.url.split(ORIGIN, 1)[1]
        self.requests.append((req.method, path, dict(req.headers)))
        for prefix, answer in self.answers.items():
            if path.startswith(prefix):
                status, body = answer(req) if callable(answer) else answer
                route.fulfill(status=status, body=json.dumps(body, ensure_ascii=False),
                              headers={"content-type": "application/json"})
                return
        for prefix, change in self.rewrite.items():
            if path.startswith(prefix) and not (self.hold and path.startswith(self.hold)):
                self._forward_changed(route, change)
                return
        super()._route(route)

    def _forward_changed(self, route, change) -> None:
        from tests._web import HEADERS

        req = route.request
        path = req.url.split(ORIGIN, 1)[1]
        headers = dict(HEADERS)
        if req.headers.get("x-tab"):
            headers["X-Tab"] = req.headers["x-tab"]
        res = self.client.open(path, method=req.method, headers=headers,
                               data=req.post_data_buffer,
                               content_type=req.headers.get("content-type"))
        try:
            body = change(res.get_json())
            route.fulfill(status=res.status_code, body=json.dumps(body, ensure_ascii=False),
                          headers={"content-type": "application/json"})
        finally:
            res.close()

    # -- 画面 ------------------------------------------------------------
    def open_screen(self, path: str, page=None):
        """開いて、起動しきるまで待つ(`app.js` が `APP.booted` を立てる)。"""
        page = page or self.watch(self.ctx.new_page())
        page.goto(f"{ORIGIN}{path}", wait_until="load")
        page.wait_for_function("window.APP && window.APP.booted === true", timeout=8000)
        page.wait_for_timeout(300)
        return page

    def asked(self, prefix: str, method: str = "") -> list[tuple[str, str, dict]]:
        """通した要求のうち、道が `prefix` で始まるもの。"""
        return [r for r in self.requests
                if r[1].startswith(prefix) and (not method or r[0] == method)]

    # -- 音 --------------------------------------------------------------
    @staticmethod
    def played(page) -> list[str]:
        """鳴らそうとした道(`/sound/<鍵>?t=…` など。無音の「触った」印は除く)。"""
        return [u.split(ORIGIN, 1)[-1] for u in page.evaluate("window.__played")
                if not u.startswith("data:")]

    def played_keys(self, page) -> list[str]:
        """鳴らそうとした出来事の鍵(`/sound/<鍵>` の鍵だけ)。"""
        from urllib.parse import unquote

        out = []
        for url in self.played(page):
            if url.startswith("/sound/") and not url.startswith("/sound/preview"):
                out.append(unquote(url[len("/sound/"):].split("?")[0]))
        return out

    def wait_played(self, page, key: str, timeout: float = 6.0) -> None:
        self.wait_until(page, lambda: key in self.played_keys(page),
                        f"「{key}」を鳴らした状態(いま {self.played_keys(page)})", timeout)
