"""看板の画面を**本物のブラウザ**で動かす試験の土台(段階1。日報の `_entry_browser.py` と同じ作り)。

    - 要求は Flask のテストクライアントへ回す(ポートを開かない。``http://kanban.test/``)
    - **通した要求を覚える**(``sent`` に (方法, 道, 本文の JSON))── 何が送られたかを見る
    - **答えを試験が決める**(``answers`` に道の頭 → ``(番号, 本文)`` か、要求を受けてそれを返す関数)
    - **試験が自分で返す画面**(``pages`` に ``{道: HTML}``)。埋め込む親の画面・起動待機画面など
    - **答えを止められる**(``hold`` に道の頭 → ``held`` に溜まる → ``release`` で返す)
    - 画面が出した確かめの窓(画面の中の窓。``js/dialog.js``)の文言は ``dialogs``
      (``accept_dialogs`` が True なら決める・False ならやめる・None なら答えずに置く)。
      **ブラウザ標準の窓(confirm・alert)が出たら落とす**(``tests/_dialogs.py``)
    - 流れて消える知らせ(トースト)も覚えておく(``toasts``)。見る前に消えても取りこぼさない

看板の画面は決まった間隔で盤・心拍・接続を読み直すので、決まった時間では待たず、
画面の状態(``wait_for_function``)か ``sent`` を待ちます。

無い環境では飛ばします(CI では飛ばしません: ``ALLTOOLS_REQUIRE_BROWSER=1``)。
"""
from __future__ import annotations

import json
import time
from typing import Callable, Union

from tests._dialogs import answer_dialogs, native_guard
from tests.test_web_app import RouteTestBase

ORIGIN = "http://kanban.test"
TOKEN = "test-token"

#: 出たトーストの文言を覚える(2 秒ほどで消えるので、出た時点で拾う)
TOAST_SPY = """
  window.__toasts = [];
  new MutationObserver((list) => {
    for (const m of list) for (const n of m.addedNodes) {
      if (n.nodeType === 1 && n.classList && n.classList.contains('toast')) {
        window.__toasts.push(n.textContent);
      }
    }
  }).observe(document, { childList: true, subtree: true });
"""

Answer = Union[tuple, Callable[[object], tuple]]


class KanbanBrowserCase(RouteTestBase):
    """`RouteTestBase`(手元の SQLite・Flask のアプリ)に本物のブラウザを足したもの。"""

    #: 確かめの窓に「決める」と答えるか。False なら「やめる」、None なら答えずに置く
    accept_dialogs: "bool | None" = True

    @classmethod
    def setUpClass(cls) -> None:
        from tests._browser import chromium_or_skip

        super().setUpClass()
        cls.pw, cls.browser = chromium_or_skip()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.pw.stop()
        super().tearDownClass()

    def setUp(self) -> None:
        super().setUp()
        from kanban import idle_exit, screen

        # 画面の持ち主・心拍はプロセスに 1 つ。前の試験の画面を持ち越さない
        # (持ち越すと「この画面では操作できません」の覆いが出る)
        for part in (screen, idle_exit):
            part.reset()
            self.addCleanup(part.reset)
        self.ctx =self.browser.new_context(viewport={"width": 1400, "height": 900})
        self.addCleanup(self.ctx.close)
        self.ctx.set_default_timeout(10000)
        self.ctx.add_init_script(TOAST_SPY)
        self.ctx.route(f"{ORIGIN}/**", self._route)
        self.errors: list[str] = []
        self.sent: list[tuple[str, str, dict]] = []
        self.answers: dict[str, Answer] = {}
        self.pages: dict[str, str] = {}
        self.hold: tuple[str, ...] = ()
        self.held: list = []
        self.dialogs: list[str] = []
        self.native_dialogs: list[str] = []
        answer_dialogs(self.ctx, self._answer_dialog)
        self.addCleanup(lambda: self.assertEqual(
            self.native_dialogs, [], "ブラウザ標準の確かめの窓が出た(出しているあいだ心拍が止まる)"))
        self.addCleanup(self._drain)

    def _answer_dialog(self, text: str):
        self.dialogs.append(text)
        return self.accept_dialogs

    # -- 要求の取り次ぎ --------------------------------------------------
    def _route(self, route) -> None:
        req = route.request
        path = req.url.split(ORIGIN, 1)[1]
        if path in self.pages:
            route.fulfill(status=200, body=self.pages[path],
                          headers={"content-type": "text/html; charset=utf-8"})
            return
        for prefix, answer in self.answers.items():
            if path.startswith(prefix):
                status, body = answer(req) if callable(answer) else answer
                route.fulfill(status=status, body=json.dumps(body, ensure_ascii=False),
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
        except (ValueError, UnicodeDecodeError):
            body = {}
        self.sent.append((req.method, path, body if isinstance(body, dict) else {}))
        # 起動トークンは、試験が開いた画面(外枠が ?t= を付けて開くのの代わり)にだけ足す。
        # 画面の JS が出す要求は、画面が付けたもの(X-Tool-Token・リンクの ?t=)のまま渡す
        # ── ここで足すと、トークンを付け忘れた要求まで通ってしまう
        headers = {"X-Tool-Token": TOKEN} if req.is_navigation_request() else {}
        for name, value in req.headers.items():
            # 名札(X-Screen)・管理者パスワード(X-Admin-Password)なども画面が付けたまま渡す
            low = name.lower()
            if low == "content-type" or low.startswith("x-"):
                headers[name] = value
        res = self.client.open(path, method=req.method, headers=headers,
                               data=req.post_data_buffer)
        try:
            out = {"content-type": res.headers.get("Content-Type", "text/plain")}
            if res.headers.get("Content-Disposition"):
                out["content-disposition"] = res.headers["Content-Disposition"]
            route.fulfill(status=res.status_code, body=res.get_data(), headers=out)
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

    def posted(self, prefix: str) -> list[dict]:
        """送った POST のうち、道が ``prefix`` で始まるものの本文。"""
        return [body for method, path, body in self.sent
                if method == "POST" and path.startswith(prefix)]

    def wait_until(self, page, ok: Callable[[], bool], what: str, timeout: float = 8.0) -> None:
        """Python 側の状態(``sent``・``held``)を待つ。待つあいだも要求は捌かれる。"""
        deadline = time.monotonic() + timeout
        while not ok():
            if time.monotonic() > deadline:
                self.fail(f"{what} になりません")
            page.wait_for_timeout(50)

    def wait_held(self, page, count: int = 1, timeout: float = 8.0) -> None:
        self.wait_until(page, lambda: len(self.held) >= count,
                        f"止めた要求が {count} 本(いま {len(self.held)} 本)", timeout)

    # -- 画面 ------------------------------------------------------------
    def watch(self, page):
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        native_guard(page, self.native_dialogs)
        return page

    def open(self, path: str, page=None):
        """開いて、読み込み終わるまで待つ。"""
        page = page or self.watch(self.ctx.new_page())
        page.goto(f"{ORIGIN}{path}", wait_until="load")
        return page

    def open_settings(self, tab: str = ""):
        """設定画面を開き、配線が済むまで待つ。``tab`` の面へ移る。

        面の選び(``aria-selected``)はサーバも描くので、配線の印にならない。``start()`` の
        途中で JS が入れる「記録」の期間(``#trace-from``)を印にする(同じ一続きの処理なので、
        入っていれば ``start()`` は終わっている)。
        """
        page = self.open("/settings")
        page.wait_for_function("(document.getElementById('trace-from') || {}).value")
        if tab:
            page.click(f"#tab-{tab}")
            page.wait_for_selector(f"#panel-{tab}:not([hidden])")
        return page

    @staticmethod
    def toasts(page) -> list[str]:
        return page.evaluate("window.__toasts || []")

    def wait_toast(self, page, part: str, timeout: float = 8.0) -> None:
        self.wait_until(page, lambda: any(part in t for t in self.toasts(page)),
                        f"「{part}」の知らせが出た状態(いま {self.toasts(page)})", timeout)

    @staticmethod
    def visible(page, selector: str) -> bool:
        return page.locator(selector).is_visible()

    @staticmethod
    def text(page, selector: str) -> str:
        return page.locator(selector).inner_text()

    def answer_password(self, page, password: str = "秘密") -> None:
        """「管理者パスワード」の覆いが出たら入れて実行する(決めていない端末は既定の値で開く)。"""
        page.wait_for_selector("#pw:not([hidden])")
        page.fill("#pw-input", password)
        page.click("#pw-ok")
        page.wait_for_selector("#pw", state="hidden")
