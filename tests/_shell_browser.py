"""大きなタブの画面(`portal/static/js/shell.js`・`settings.js`・`embed.js`)を**本物のブラウザ**で動かす土台(段階1)。

    - 入口の要求は Flask のテストクライアントへ回す(ポートを開かない。`http://portal.test/`)
    - 各ツールの画面は**偽のツールの画面**(`http://<ツール>.test/`)。試験が中身を決める
      (`tool_script` に台本を足す・`embed` で外枠が差し込む台本 `embed.js` を入れる)
    - デスクトップ版(`bridge = True`)は**偽の外枠**: `window.__TAURI_INTERNALS__.invoke` を置き、
      頼まれた命令を `invokes` に残す(答えは `answers` で決める)。外枠が呼ぶ口は `window.__shell`
    - **答えを止められる**(`hold` に道の頭(`$` で終わればその道だけ)→ `held` に溜まる → `release`)。`hold_answered` に
      入れた道は、着いた時点の答えを作っておき、返すのだけ遅らせる(遅れて届いた古い答え)
    - `events` に起きた順で残す: 入口への要求(`"portal"`)・外枠への頼み(`"invoke"`)・
      ツールの画面が受けた合図(`"tool"`)・`sendBeacon`(`"beacon"`)
    - 確かめの窓(confirm・alert)は `dialogs` に文言を残し、`answers_for_dialogs` の順に答える
      (無ければ「はい」)

使う試験: `test_shell_close_browser`・`test_shell_frames_browser`・`test_settings_browser`。
"""
from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from typing import Callable, Optional

from tests.test_web import TOKEN, Base

ROOT = Path(__file__).resolve().parent.parent
ORIGIN = "http://portal.test"
EMBED = (ROOT / "portal" / "static" / "js" / "embed.js").read_text(encoding="utf-8")


def tool_origin(tool_id: str) -> str:
    return f"http://{tool_id}.test"


#: 偽のツールの画面。`{embed}` に外枠が差し込む台本、`{script}` に試験ごとの台本が入る
TOOL_PAGE = """<!doctype html><html lang="ja"><head><meta charset="utf-8"><title>{title}</title>{embed}
<style>body{{margin:0;font:14px sans-serif}} #zone{{position:absolute;left:40px;top:120px;width:300px;height:200px;
border:2px dashed #888}}</style></head>
<body><h1>{tool}</h1>
<input id="typed" value=""> <input id="given" value="サーバの値">
<div id="zone">ここへ落とす</div>
<script>
window.__got = [];
window.addEventListener("message", (e) => {{
  const d = e.data;
  if (!d || typeof d.type !== "string") return;
  window.__got.push(d);
  if (window.__note) window.__note("tool", {{ tool: "{tool}", type: d.type, seq: d.seq, theme: d.theme }});
}});
const zone = document.getElementById("zone");
zone.addEventListener("dragover", (e) => e.preventDefault());
zone.addEventListener("drop", async (e) => {{
  e.preventDefault();
  const files = [...e.dataTransfer.files];
  window.__dropped = await Promise.all(files.map(async (f) => [f.name, await f.text()]));
}});
{script}
</script></body></html>"""

#: 偽の外枠(デスクトップ版の大きなタブの画面にだけ置く)と、`sendBeacon` の記録
INIT_SCRIPT = """
(() => {
  const note = (kind, data) => {
    if (window.__quiet) return;                     // 試験の後片付け(閉じる)で残さない
    try { window.__note && window.__note(kind, data); } catch (e) {}
  };
  try {
    navigator.sendBeacon = (url, blob) => {
      note("beacon", { url: String(url), parts: blob && blob.__parts ? blob.__parts.join("") : "" });
      return true;
    };
    const RealBlob = window.Blob;
    window.Blob = function (parts, options) { const b = new RealBlob(parts, options); b.__parts = parts; return b; };
    window.Blob.prototype = RealBlob.prototype;
  } catch (e) {}
  if (location.origin !== "%(origin)s") {
    // ツールの画面: 画面の要求が**何の形で**送り出されたか(WebView2 は FormData のファイルを落とす)
    const send = window.fetch;
    window.fetch = function (input, init) {
      const body = init && init.body;
      note("fetch", { url: String(input), body: body ? Object.prototype.toString.call(body) : "" });
      return send.apply(this, arguments);
    };
    return;
  }
  if (!%(desktop)s) return;
  window.__TAURI_INTERNALS__ = {
    invoke(cmd, args, options) {
      const plain = args instanceof ArrayBuffer || ArrayBuffer.isView(args) ? null : (args === undefined ? null : args);
      note("invoke", { cmd, args: plain });
      if (cmd === "shell_status") return Promise.resolve([]);
      if (cmd === "shell_dropped_file") return Promise.resolve(new TextEncoder().encode("落とした中身").buffer);
      return Promise.resolve(null);
    },
  };
})();
"""


class FakeTools:
    """ブラウザ版の各ツールを起こす係の代わり(起こしたことにして、偽のツールの画面の宛先を返す)。"""

    def __init__(self) -> None:
        #: id → `/api/tools/status` の答え(無ければ「動いている」)
        self.states: dict[str, dict] = {}
        self.busy_labels: list[str] = []

    def open(self, tool) -> str:
        return f"{tool_origin(tool.id)}/"

    def status(self, ids=None) -> list:
        return [{"id": i, "running": True, **self.states.get(i, {})} for i in (ids or [])]

    def busy(self) -> list:
        return list(self.busy_labels)

    def stop_all(self, force: bool = False) -> None:
        pass


class ShellBrowserCase(Base):
    """大きなタブの画面を開く土台。`bridge = True` でデスクトップ版(偽の外枠)。"""

    @classmethod
    def setUpClass(cls) -> None:
        from tests._browser import chromium_or_skip

        cls.pw, cls.browser = chromium_or_skip()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.pw.stop()

    def setUp(self) -> None:
        super().setUp()
        from portal import catalog as catalog_mod, web

        self.catalog = catalog_mod.load()
        self.tools = FakeTools()
        if not self.bridge:
            web.set_browser_tools(self.tools)
        self.addCleanup(web.set_browser_tools, None)
        self.stopped: list[bool] = []
        web.set_shutdown_hook(lambda: self.stopped.append(True))
        self.addCleanup(web.set_shutdown_hook, None)
        web.close_ask.__init__()
        self.addCleanup(web.close_ask.__init__)
        #: 入口への要求(方法, 道, 本文)
        self.sent: list[tuple[str, str, dict]] = []
        self.events: list[tuple[str, dict]] = []
        self.hold: tuple[str, ...] = ()
        self.hold_answered: tuple[str, ...] = ()
        self.held: list = []
        self.dialogs: list[str] = []
        self.answers_for_dialogs: list[bool] = []
        #: ツールの id → 偽の画面に足す台本
        self.tool_script: dict[str, str] = {}
        #: 偽のツールの画面に外枠の台本(embed.js)を入れるか(デスクトップ版の画面)
        self.embed = bool(self.bridge)
        #: ツールの画面を返した回数(読み直したか)
        self.tool_loads: dict[str, int] = {}
        self.errors: list[str] = []
        self.ctx = self.browser.new_context(viewport={"width": 1400, "height": 900})
        self.addCleanup(self.ctx.close)
        self.ctx.expose_binding("__note", lambda source, kind, data: self.events.append((kind, data)))
        self.ctx.add_init_script(INIT_SCRIPT % {"origin": ORIGIN, "desktop": "true" if self.bridge else "false"})
        self.ctx.route(f"{ORIGIN}/**", self._route)
        for tool in self.catalog.tools:
            self.ctx.route(f"{tool_origin(tool.id)}/**", self._tool_route)
        self.addCleanup(self._drain)
        self.addCleanup(self._quiet)

    def _quiet(self) -> None:
        """閉じる前に、画面から記録へ書かせない(閉じたあとに書きに来て Playwright が騒ぐ)。"""
        for page in self.ctx.pages:
            for frame in page.frames:
                try:
                    frame.evaluate("window.__quiet = true")
                except Exception:                   # noqa: BLE001 - もう閉じた枠
                    pass

    # -- 入口への要求 ------------------------------------------------------
    def _route(self, route) -> None:
        req = route.request
        path = req.url.split(ORIGIN, 1)[1]
        try:
            body = json.loads(req.post_data or "{}") if req.method == "POST" else {}
        except ValueError:
            body = {}
        body = body if isinstance(body, dict) else {}
        self.sent.append((req.method, path, body))
        if path.startswith("/api/"):
            self.events.append(("portal", {"path": path, "body": body}))
        if self._matches(path, self.hold_answered):
            self.held.append((route, self._answer(route)))
            return
        if self._matches(path, self.hold):
            self.held.append((route, None))
            return
        self._fulfill(route, self._answer(route))

    @staticmethod
    def _matches(path: str, patterns: tuple[str, ...]) -> bool:
        """道の頭が合うか。`$` で終わるものはその道だけ(`/api/settings$` は `/api/settings/auth` に合わない)。"""
        return any(path == p[:-1] if p.endswith("$") else path.startswith(p) for p in patterns)

    def _answer(self, route) -> tuple[int, bytes, str]:
        req = route.request
        path = req.url.split(ORIGIN, 1)[1]
        headers = {"X-Tool-Token": TOKEN}
        if req.headers.get("content-type"):
            headers["Content-Type"] = req.headers["content-type"]
        res = self.client.open(path, method=req.method, headers=headers, data=req.post_data_buffer,
                               base_url=f"http://{self.host}")
        try:
            data = res.get_data()
            ctype = res.headers.get("Content-Type", "text/plain")
        finally:
            res.close()
        if path.split("?")[0] == "/" and self.bridge:
            # デスクトップ版の各ツールの宛先(独自の宛先)を、偽のツールの画面の宛先へ
            from portal import app_config

            text = data.decode("utf-8")
            for tool in self.catalog.tools:
                text = text.replace(f'"origin": "{app_config.origin_of(tool.scheme)}"',
                                    f'"origin": "{tool_origin(tool.id)}"')
            data = text.encode("utf-8")
        return res.status_code, data, ctype

    @staticmethod
    def _fulfill(route, answer) -> None:
        status, data, ctype = answer
        try:
            route.fulfill(status=status, body=data, headers={"content-type": ctype})
        except Exception:                           # noqa: BLE001 - もう閉じた画面の要求
            pass

    def release(self, page, index: int = 0) -> None:
        """止めた要求を1本返す(`hold_answered` なら止めたときの答えを)。"""
        route, answer = self.held.pop(index)
        self._fulfill(route, answer or self._answer(route))
        page.wait_for_timeout(300)

    def _drain(self) -> None:
        self.hold = ()
        self.hold_answered = ()
        while self.held:
            route, _ = self.held.pop()
            try:
                route.abort()
            except Exception:                       # noqa: BLE001
                pass

    # -- 偽のツールの画面 ----------------------------------------------------
    def _tool_route(self, route) -> None:
        url = route.request.url
        tool_id = url.split("://", 1)[1].split(".test", 1)[0]
        path = url.split(".test", 1)[1]
        if route.request.method != "GET" or path.split("?")[0] != "/":
            # 画面からの要求(ファイルを送る など)は、届いたものを残して空で答える
            self.events.append(("tool-request", {"tool": tool_id, "path": path,
                                                 "type": route.request.headers.get("content-type", ""),
                                                 "body": route.request.post_data_buffer or b""}))
            route.fulfill(status=200, body='{"ok": true}', headers={"content-type": "application/json"})
            return
        self.tool_loads[tool_id] = self.tool_loads.get(tool_id, 0) + 1
        embed = ""
        if self.embed:
            script = EMBED.replace('"__ALLTOOLS_TOOL__"', json.dumps(tool_id)).replace(
                '"__ALLTOOLS_SHELL__"', json.dumps(ORIGIN))
            embed = f"<script>{script}</script>"
        html = TOOL_PAGE.format(title=f"{tool_id} の画面", tool=tool_id, embed=embed,
                                script=self.tool_script.get(tool_id, ""))
        route.fulfill(status=200, body=html, headers={"content-type": "text/html; charset=utf-8"})

    # -- 画面 ------------------------------------------------------------
    def open_shell(self, *, first: str = "kanban", wait_tools: tuple[str, ...] = ()):
        """大きなタブの画面を開く。`first` のタブを先に出す(前に開いたタブとして覚えさせる)。"""
        page = self.ctx.new_page()
        page.on("pageerror", lambda e: self.errors.append(str(e)))

        def answer(dialog) -> None:
            self.dialogs.append(dialog.message)
            if self.answers_for_dialogs and not self.answers_for_dialogs.pop(0):
                dialog.dismiss()
            else:
                dialog.accept()

        page.on("dialog", answer)
        page.add_init_script(f"try {{ localStorage.setItem('alltools.lastTab', {json.dumps(first)}); }} catch (e) {{}}")
        page.goto(f"{ORIGIN}/", wait_until="load")
        page.wait_for_selector(f"#tab-{first}[aria-selected=true]")
        for tool_id in (first, *wait_tools):
            if tool_id != "settings":
                self.tool_frame(page, tool_id)
        return page

    def tool_frame(self, page, tool_id: str, timeout: float = 8.0):
        """そのツールの枠(読み込み終えたもの)。"""
        deadline = time.monotonic() + timeout
        while True:
            for frame in page.frames:
                if frame.url.startswith(tool_origin(tool_id)):
                    try:
                        if frame.evaluate("Array.isArray(window.__got)"):
                            return frame
                    except Exception:               # noqa: BLE001 - 読み込みの途中
                        pass
            if time.monotonic() > deadline:
                self.fail(f"{tool_id} の画面が出ません")
            page.wait_for_timeout(50)

    def wait_until(self, page, ok: Callable[[], bool], what: str, timeout: float = 8.0) -> None:
        deadline = time.monotonic() + timeout
        while not ok():
            if time.monotonic() > deadline:
                self.fail(f"{what} になりません")
            page.wait_for_timeout(50)

    def kinds(self, kind: str, **match) -> list[dict]:
        """`events` のうち、その種類で `match` に合うもの。"""
        return [d for k, d in self.events if k == kind and all(d.get(a) == b for a, b in match.items())]

    def invokes(self, cmd: str) -> list[Optional[dict]]:
        return [d.get("args") for d in self.kinds("invoke", cmd=cmd)]

    def posts(self, path: str) -> list[dict]:
        return [b for m, p, b in self.sent if m == "POST" and p.split("?")[0] == path]

    def toasts(self, page) -> str:
        return page.evaluate("[...document.querySelectorAll('#toasts .toast')].map(t => t.textContent).join('\\n')")


class DesktopShellBrowserCase(ShellBrowserCase):
    bridge = True


if __name__ == "__main__":
    unittest.main()
