"""点検表: **画面を本物のブラウザで開いて、JS が転ばない** (段階1)

点検表の画面の JS は試験で一度も動いていませんでした(字を探す試験だけ)。画面を開いて
一覧が出るのを待ち、画面の誤り(pageerror)と console の error が出ないことを確かめます。
見本の点検表(`demo`)で動かすので、Excel・Word は使いません。

無い環境では飛ばします(CI では飛ばしません: ``ALLTOOLS_REQUIRE_BROWSER=1``)。
"""
from __future__ import annotations

import unittest

from tests.test_web import AUTH, HAS_WEB, WebTestBase


@unittest.skipUnless(HAS_WEB, "Flask が入っていないためスキップ")
class ScreenSmokeTest(WebTestBase):
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
        path = req.url.split("http://inspection.test", 1)[1]
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

    def test_画面を開いて一覧が出て_JSが転ばない(self) -> None:
        ctx = self.browser.new_context(viewport={"width": 1400, "height": 900})
        self.addCleanup(ctx.close)
        ctx.route("http://inspection.test/**", self._forward)
        # 離れるときの合図(`sendBeacon`)は取り次ぎを通らない。試験では送らない
        ctx.add_init_script("navigator.sendBeacon = () => true;")
        page = ctx.new_page()
        problems: list[str] = []
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}")
                if m.type == "error" and not str(m.location.get("url", "")).endswith("/favicon.ico")
                else None)
        page.on("dialog", lambda d: d.dismiss())
        res = page.goto("http://inspection.test/", wait_until="load")
        self.assertLess(res.status, 400)
        page.wait_for_timeout(1500)
        # 見本の点検表の名前が一覧に出ている(一覧を描く JS まで動いた)
        names = [item["name"] for c in self.get("/api/inventory").get_json()["inventory"]["categories"]
                 for s in c["subcategories"] for item in s["items"]]
        self.assertTrue(names)
        page.wait_for_function("(n) => document.body.innerText.includes(n)", arg=names[0],
                               timeout=8000)
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
