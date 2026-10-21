"""看板: **どの画面も、本物のブラウザで開いて JS が転ばない** (段階1)

看板の画面の JS は、CI で一度も動いていませんでした(ブラウザの試験は1本で、それも
CI では飛ばされていた)。現場・倉庫・倉庫(見るだけ)の3つの見え方で各画面を開き、
画面の誤り(pageerror)と console の error が出ないことを確かめます。看板の画面は
決まった間隔で盤を読み直すので、1回ぶん待ってから見ます。

無い環境では飛ばします(CI では飛ばしません: ``ALLTOOLS_REQUIRE_BROWSER=1``)。
"""
from __future__ import annotations

import unittest

from kanban import config
from tests.test_web_app import HAS_FLASK, RouteTestBase

TOKEN = "test-token"


class _Smoke:
    """見え方ごとの試験の土台(`RouteTestBase` と組んで使う)。"""

    SCREENS: tuple[str, ...] = ("/board", "/settings")

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
        path = req.url.split("http://kanban.test", 1)[1]
        headers = {"X-Tool-Token": TOKEN}
        for name in ("content-type", "x-screen"):
            if req.headers.get(name):
                headers[name] = req.headers[name]
        res = self.client.open(path, method=req.method, headers=headers,
                               data=req.post_data_buffer)
        try:
            route.fulfill(status=res.status_code, body=res.get_data(),
                          headers={"content-type": res.headers.get("Content-Type", "text/plain")})
        finally:
            res.close()

    def test_どの画面も開いてJSが転ばない(self) -> None:
        ctx = self.browser.new_context(viewport={"width": 1400, "height": 900})
        self.addCleanup(ctx.close)
        ctx.route("http://kanban.test/**", self._forward)
        page = ctx.new_page()
        problems: list[str] = []
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}")
                if m.type == "error" else None)
        page.on("dialog", lambda d: d.dismiss())
        for path in self.SCREENS:
            with self.subTest(mode=self.MODE, path=path):
                problems.clear()
                res = page.goto(f"http://kanban.test{path}", wait_until="load")
                self.assertLess(res.status, 400, path)
                page.wait_for_timeout(1500)
                self.assertEqual(problems, [], f"{self.MODE} {path}")


@unittest.skipUnless(HAS_FLASK, "Flask が入っていない")
class SiteScreensTest(_Smoke, RouteTestBase):
    MODE = config.MODE_SITE
    LINE = "LVC"


@unittest.skipUnless(HAS_FLASK, "Flask が入っていない")
class WarehouseScreensTest(_Smoke, RouteTestBase):
    MODE = config.MODE_WAREHOUSE
    LINE = ""


@unittest.skipUnless(HAS_FLASK, "Flask が入っていない")
class WarehouseViewScreensTest(_Smoke, RouteTestBase):
    MODE = config.MODE_WAREHOUSE_VIEW
    LINE = ""


if __name__ == "__main__":
    unittest.main()
