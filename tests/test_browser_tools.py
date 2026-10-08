"""ブラウザ版(予備)で、各ツールのブラウザ版を大きなタブに並べる(`portal/browser_tools.py`)

ツールのブラウザ版の代わりに、小さな待ち受けを立てて確かめる:
- ツールの印(`<ツールの手元の領域>/runtime/*.lock`)から番号とトークンを読み、枠の宛先を作る
- 印があっても、待ち受けが自分(app_id)と答えなければ使わない
- 終了の前に「終わってよいか」を訊き、途中の処理があれば理由を集める
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import tests  # noqa: F401  (一時フォルダへ向ける)

from portal import browser_tools, catalog as catalog_mod

CATALOG = catalog_mod.load()
KANBAN = CATALOG.by_id("kanban")


class FakeTool(BaseHTTPRequestHandler):
    """ツールのブラウザ版の代わり(体調と終了だけ答える)。"""

    app_id = KANBAN.app_id()
    busy = False
    calls: list = []

    def log_message(self, *args) -> None:  # noqa: D401 - 黙らせる
        pass

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/health":
            self._send(200, {"app_id": self.app_id, "version": "9.9.9"})
        else:
            self._send(404, {})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).calls.append((self.path, body, self.headers.get("X-Tool-Token")))
        if self.path == "/api/shutdown":
            if type(self).busy and not body.get("force"):
                self._send(409, {"stopped": False, "busy": "書き戻しの途中です"})
            elif body.get("check"):
                self._send(200, {"stopped": False, "can_stop": True})
            else:
                self._send(200, {"stopped": True})
        else:
            self._send(404, {})


class BrowserToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.local = Path(tempfile.mkdtemp(prefix="alltools_kanban_local_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.local, ignore_errors=True))
        self._saved = os.environ.get(KANBAN.local_dir_env)
        os.environ[KANBAN.local_dir_env] = str(self.local)
        self.addCleanup(self._restore)
        FakeTool.busy = False
        FakeTool.calls = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeTool)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def _restore(self) -> None:
        if self._saved is None:
            os.environ.pop(KANBAN.local_dir_env, None)
        else:
            os.environ[KANBAN.local_dir_env] = self._saved

    def write_lock(self, *, pid: int = 0, app_id: str = "", name: str = "site.lock") -> None:
        runtime = self.local / "runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / name).write_text(json.dumps({
            "app_id": app_id or KANBAN.app_id(), "pid": pid or os.getpid(), "port": self.port,
            "token": "tok-123"}), encoding="utf-8")

    def test_印と待ち受けから枠の宛先を作る(self) -> None:
        self.assertIsNone(browser_tools.find_running(KANBAN), "印が無い")
        # OS の錠だけのファイル(中身が無い)は読み飛ばす
        (self.local / "runtime").mkdir(parents=True)
        (self.local / "runtime" / "desktop.lock").write_bytes(b"")
        self.write_lock()
        running = browser_tools.find_running(KANBAN)
        self.assertIsNotNone(running)
        self.assertEqual(running.url, f"http://127.0.0.1:{self.port}/?t=tok-123")

    def test_待ち受けが自分と答えなければ使わない(self) -> None:
        self.write_lock(app_id="nlm.別のアプリ")
        self.assertIsNone(browser_tools.find_running(KANBAN))
        self.server.shutdown()
        self.write_lock()
        self.assertIsNone(browser_tools.find_running(KANBAN, check_pid=False), "待ち受けが居ない")

    def test_見張りは動いているかを返す(self) -> None:
        tools = browser_tools.BrowserTools(CATALOG)
        self.write_lock()
        status = {s["id"]: s["running"] for s in tools.status(["kanban", "nippou"])}
        self.assertEqual(status, {"kanban": True, "nippou": False})

    def test_答えないだけでは終わったとしない_印かPIDが消えたら終わった(self) -> None:
        """重い処理の途中で待ち受けが答えない ≠ 終わった。以前は2回答えないだけで枠を消していた。"""
        tools = browser_tools.BrowserTools(CATALOG)
        self.write_lock()
        self.assertEqual(tools.status(["kanban"]), [{"id": "kanban", "running": True, "ended": False}])
        self.server.shutdown()                     # 答えない(が、印の PID = この試験は生きている)
        self.assertEqual(tools.status(["kanban"]), [{"id": "kanban", "running": False, "ended": False}])
        dead = 2 ** 22 + 12345                     # 居ない PID
        self.write_lock(pid=dead)
        self.assertEqual(tools.status(["kanban"]), [{"id": "kanban", "running": False, "ended": True}])
        (self.local / "runtime" / "site.lock").unlink()
        self.assertEqual(tools.status(["kanban"]), [{"id": "kanban", "running": False, "ended": True}])

    def test_終了の前に訊き_途中の処理があれば理由を集める(self) -> None:
        tools = browser_tools.BrowserTools(CATALOG)
        self.write_lock()
        self.assertEqual(tools.busy(), [])
        FakeTool.busy = True
        busy = tools.busy()
        self.assertEqual(len(busy), 1)
        self.assertIn("看板", busy[0])
        self.assertIn("書き戻しの途中", busy[0])
        tools.stop_all(force=True)
        self.assertIn(("/api/shutdown", {"force": True}, "tok-123"), FakeTool.calls)


if __name__ == "__main__":
    unittest.main()
