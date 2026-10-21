"""カレンダー: **どの画面も、本物のブラウザで開いて JS が転ばない** (段階1)

カレンダーの画面の JS は試験で一度も動いていませんでした(印刷の余白の試験だけ)。
各画面を開き、画面の誤り(pageerror)と console の error が出ないことを確かめます。

無い環境では飛ばします(CI では飛ばしません: ``ALLTOOLS_REQUIRE_BROWSER=1``)。
"""
from __future__ import annotations

import unittest

from tests import _web

SCREENS = ("/calendar", "/history", "/settings", "/print")


class ScreenSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from tests._browser import chromium_or_skip

        cls.pw, cls.browser = chromium_or_skip()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.pw.stop()

    def setUp(self) -> None:
        _web.reset_sync()
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()

    def _forward(self, route) -> None:
        req = route.request
        path = req.url.split("http://calendar.test", 1)[1]
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

    def test_どの画面も開いてJSが転ばない(self) -> None:
        ctx = self.browser.new_context(viewport={"width": 1400, "height": 900})
        self.addCleanup(ctx.close)
        ctx.route("http://calendar.test/**", self._forward)
        # 離れるときの合図(`sendBeacon`)は取り次ぎ(`route`)を通らず、試験の名前
        # (calendar.test)を引きに行って console に失敗が出る。試験では送らない
        ctx.add_init_script("navigator.sendBeacon = () => true;")
        page = ctx.new_page()
        problems: list[str] = []
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        # ブラウザが自分で取りに行く `favicon.ico` は取り次ぎを通らない(印刷の画面は
        # アイコンを指定しないので既定の場所へ行く)。画面の誤りではないので数えない
        page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}")
                if m.type == "error" and not str(m.location.get("url", "")).endswith("/favicon.ico")
                else None)
        page.on("dialog", lambda d: d.dismiss())
        for path in SCREENS:
            with self.subTest(path=path):
                problems.clear()
                res = page.goto(f"http://calendar.test{path}", wait_until="load")
                self.assertLess(res.status, 400, path)
                page.wait_for_timeout(1200)
                self.assertEqual(problems, [], path)


if __name__ == "__main__":
    unittest.main()
