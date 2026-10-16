"""組み立てに失敗した(待機画面のまま理由を出している)Python を、stop.bat の止め口で止められる。

現場から: 日報が「アプリを組み立てられませんでした」で止まったあと、stop.bat を実行しても
フォルダが消せず(Python が動いたまま)、タスクマネージャーで Python を切るしかなかった。
待機画面が `/api/shutdown` に答えていなかった(4 ツールとも)。
"""
from __future__ import annotations

import json
import socket
import unittest
import urllib.error
import urllib.request

import server as server_module


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class BootShutdownTest(unittest.TestCase):
    def setUp(self) -> None:
        self.port = _free_port()
        self.srv = server_module.AppServer(self.port)
        self.thread = server_module.run_in_background(self.srv)
        self.addCleanup(self._cleanup)
        self.srv.boot.mark_error("アプリを組み立てられませんでした: No module named 'x.services'")

    def _cleanup(self) -> None:
        self.srv.stop()
        self.thread.join(timeout=10)

    def post(self, body: dict, token: str | None) -> tuple[int, dict]:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/shutdown",
                                     data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json",
                                              **({"X-Tool-Token": token} if token is not None else {})})
        try:
            with urllib.request.urlopen(req, timeout=5) as res:
                return res.status, json.loads(res.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def test_failed_build_stops_on_request(self):
        for _ in range(50):
            try:
                status, _ = self.post({"check": True}, self.srv.token)
                break
            except (urllib.error.URLError, ConnectionError):
                import time
                time.sleep(0.1)
        self.assertEqual(status, 200)
        self.assertTrue(self.thread.is_alive(), "訊いただけ(check)で止まった")
        status, _ = self.post({}, "wrong-token")
        self.assertEqual(status, 403)
        self.assertTrue(self.thread.is_alive(), "トークンが違うのに止まった")
        status, body = self.post({"force": True}, self.srv.token)
        self.assertEqual((status, body.get("stopped")), (200, True), body)
        self.thread.join(timeout=10)
        self.assertFalse(self.thread.is_alive(), "止め口を呼んでも待ち受けが終わらない")


if __name__ == "__main__":
    unittest.main()
