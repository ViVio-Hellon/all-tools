"""起動の途中に開いた画面へ、起動し終えたら戻る (v4.46.9)

起動の途中に「GW計算」(`/gw`)などを開くと待機画面が出て、起動し終えたら日報入力(`/`)へ
連れて行っていた(Windows CI で GW の画面を開いたはずの試験が日報入力にいて落ちた)。
待機画面が出たのが `/gw` なら、`/gw` へ戻る。`/` で出たときはこれまでどおり既定の行き先へ。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou import boot_screen  # noqa: E402
from tests._browser import chromium_or_skip  # noqa: E402

BASE = "http://boot.test"


class BootReturnTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.pw, cls.browser = chromium_or_skip()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.pw.stop()

    def landed(self, asked: str) -> str:
        """待機画面を `asked` で出し、起動し終えたあとに移った先の道。"""
        page = self.browser.new_page()
        self.addCleanup(page.close)
        boot = boot_screen.render(display_name="日報", version_label="VER0", token="tok",
                                  app_id="nippou", poll_ms=150, home_url=asked)
        seen: list[str] = []

        def handle(route) -> None:
            path = route.request.url[len(BASE):].split("?")[0]
            if path == "/api/health":
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"ready": True, "app_id": "nippou", "home_url": "/"}))
            elif path == asked and not seen:
                seen.append(path)
                route.fulfill(status=200, content_type="text/html", body=boot)
            else:
                seen.append(path)
                route.fulfill(status=200, content_type="text/html", body="<p id=done>ok</p>")

        page.route(f"{BASE}/**", handle)
        page.goto(f"{BASE}{asked}")
        page.wait_for_selector("#done", timeout=10000)
        return seen[-1]

    def test_GWを開いて待ったらGWへ戻る(self) -> None:
        self.assertEqual(self.landed("/gw"), "/gw")

    def test_入口で待ったら既定の行き先へ(self) -> None:
        self.assertEqual(self.landed("/"), "/")


if __name__ == "__main__":
    unittest.main()
