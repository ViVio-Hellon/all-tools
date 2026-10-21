"""入口: **大設定の画面を本物のブラウザで開いて、JS が転ばない** (段階1)

入口の画面の JS は試験で一度も動いていませんでした(字を探す試験だけ)。画面を開き、
画面の誤り(pageerror)と console の error が出ないことを確かめます。説明書の画面
(`/static/manual/…`)も開きます。

無い環境では飛ばします(CI では飛ばしません: ``ALLTOOLS_REQUIRE_BROWSER=1``)。
"""
from __future__ import annotations

import unittest

from tests.test_web import TOKEN, Base

SCREENS = ("/", "/static/manual/index.html", "/static/manual/portal.html")


class _FakeTools:
    """ブラウザ版の各ツールを起こす係の代わり(起こしたことにして、空の画面を返す)。"""

    def open(self, tool) -> str:
        return "about:blank"

    def status(self, ids=None) -> list:
        return []


class ScreenSmokeTest(Base):
    def setUp(self) -> None:
        super().setUp()
        # 大設定の画面は開くと最初のツールを起こしに行く(`shell.js`)。その係を差し込む
        from portal import web

        web.set_browser_tools(_FakeTools())
        self.addCleanup(web.set_browser_tools, None)

    @classmethod
    def setUpClass(cls) -> None:
        from tests._browser import chromium_or_skip

        cls.pw, cls.browser = chromium_or_skip()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.pw.stop()

    def _forward(self, route) -> None:
        req = route.request
        path = req.url.split("http://portal.test", 1)[1]
        headers = {"X-Tool-Token": TOKEN}
        if req.headers.get("content-type"):
            headers["Content-Type"] = req.headers["content-type"]
        res = self.client.open(path, method=req.method, headers=headers,
                               data=req.post_data_buffer, base_url=f"http://{self.host}")
        try:
            route.fulfill(status=res.status_code, body=res.get_data(),
                          headers={"content-type": res.headers.get("Content-Type", "text/plain")})
        finally:
            res.close()

    def test_画面を開いてJSが転ばない(self) -> None:
        ctx = self.browser.new_context(viewport={"width": 1400, "height": 900})
        self.addCleanup(ctx.close)
        ctx.route("http://portal.test/**", self._forward)
        # 離れるときの合図(`sendBeacon`)は取り次ぎを通らない。試験では送らない
        ctx.add_init_script("navigator.sendBeacon = () => true;")
        page = ctx.new_page()
        problems: list[str] = []
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}")
                if m.type == "error" and not str(m.location.get("url", "")).endswith("/favicon.ico")
                else None)
        page.on("dialog", lambda d: d.dismiss())
        for path in SCREENS:
            with self.subTest(path=path):
                problems.clear()
                res = page.goto(f"http://portal.test{path}", wait_until="load")
                self.assertLess(res.status, 400, path)
                page.wait_for_timeout(1200)
                self.assertEqual(problems, [], path)


if __name__ == "__main__":
    unittest.main()
