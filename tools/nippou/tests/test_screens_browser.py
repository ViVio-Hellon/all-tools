"""日報: **どの画面も、本物のブラウザで開いて JS が転ばない** (段階1)

日報入力のほかの画面(集計・グラフ・記録・設定・梱包資材・VC計算・標準作業時間)は、
画面の JS が試験で一度も動いていませんでした(字を探す試験だけ)。JS が読み込みで
転ぶと、画面は出ているのに**ボタンが全部効かない**(`nav.js` の「この画面の操作を
読み込めませんでした」)。ここでは各画面を開いて:

    - 画面の誤り(pageerror)・console の error が出ない
    - 「操作を読み込めませんでした」の札(`.msg--wire`)が出ない
    - 左の並びで次の画面へ移れる(画面の差し替え `nav.go` を通る)

サーバの時計は直の中ほどに止めます。無い環境では飛ばします(CI では飛ばしません)。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._browser import chromium_or_skip  # noqa: E402
from tests._web import HAS_FLASK, HEADERS, SKIP_REASON, WebTestCase  # noqa: E402

#: 開いて確かめる画面(左の並びにあるもの + 確認画面)
SCREENS = ("/", "/records", "/agg", "/graph", "/graph/review", "/gw", "/vc",
           "/standard-time", "/settings")


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class ScreenSmokeTests(WebTestCase):
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
        self.ctx.route("http://app.test/**", self._forward)

    def _forward(self, route) -> None:
        req = route.request
        path = req.url.split("http://app.test", 1)[1]
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

    def open(self, path: str):
        page = self.ctx.new_page()
        problems: list[str] = []
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}")
                if m.type == "error" else None)
        page.on("dialog", lambda d: d.dismiss())
        page.goto(f"http://app.test{path}", wait_until="load")
        page.wait_for_selector("main#main")
        page.wait_for_timeout(800)                    # 画面のモジュールが繋がるまで
        return page, problems

    def assert_clean(self, page, problems: list[str], where: str) -> None:
        wire = page.query_selector(".msg--wire")
        self.assertIsNone(wire, f"{where}: {wire.text_content() if wire else ''}")
        self.assertEqual(problems, [], where)

    def test_どの画面も開いてJSが転ばない(self) -> None:
        """**1つのタブで**順に開く(読み直しと同じ)。

        タブを閉じても閉じる合図はこの試験の取り次ぎを通らないので、別のタブで開くと
        前のタブが「打てるタブ」のまま残り、新しいタブの見張り(自動確定)が断られる。
        """
        page, problems = self.open(SCREENS[0])
        self.assert_clean(page, problems, SCREENS[0])
        for path in SCREENS[1:]:
            with self.subTest(path=path):
                problems.clear()
                page.goto(f"http://app.test{path}", wait_until="load")
                page.wait_for_selector("main#main")
                page.wait_for_timeout(800)
                self.assert_clean(page, problems, path)

    def test_左の並びで画面を渡り歩いても転ばない(self) -> None:
        """画面の差し替え(`nav.go`)を通る道。前の画面の後始末と次の画面の繋ぎ込みを順に。"""
        page, problems = self.open("/records")
        for path in ("/agg", "/graph", "/gw", "/vc", "/standard-time", "/settings", "/records"):
            link = page.query_selector(f'nav.rail a[href="{path}"]')
            if link is None:
                continue                              # 権限で出ていない画面
            link.click()
            page.wait_for_function(f"location.pathname === {path!r}", timeout=8000)
            page.wait_for_timeout(600)
            self.assert_clean(page, problems, f"→ {path}")


if __name__ == "__main__":
    unittest.main()
