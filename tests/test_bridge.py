"""入口のデスクトップ版の入口(`bridge.py`)── ポートを使わずに画面を返す

外枠(Rust/Tauri)は `bridge.py` を子として起動し、標準入出力で要求を渡す。
ここでは外枠の代わりをして、次を確かめる:

- やりとりの形(見出し1行 + 本文)が往復で崩れない(日本語・バイナリ)
- **ソケットを1つも開かない**(開こうとしたら落ちるようにして起動する)
- 画面・タブ・大設定が返る。外枠の宛先(Host: app.localhost)を受け付ける
- 「終了」で `quit` を知らせ、標準入力を閉じればプロセスが終わる
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
import unittest
from pathlib import Path

import tests  # noqa: F401  (一時フォルダへ向ける)

from portal import bridge_proto

ROOT = Path(__file__).resolve().parent.parent


class FrameTests(unittest.TestCase):
    def roundtrip(self, head: dict, body: bytes) -> tuple[dict, bytes]:
        out = io.BytesIO()
        bridge_proto.FrameWriter(out).write(head, body)
        out.seek(0)
        return bridge_proto.read_frame(out)

    def test_日本語とバイナリの本文が崩れない(self) -> None:
        body = "タブ表示権限\n".encode("utf-8") + bytes(range(256))
        head, got = self.roundtrip({"id": 1, "status": 200, "headers": [["X-名前", "値"]]}, body)
        self.assertEqual(got, body)
        self.assertEqual(head["headers"], [["X-名前", "値"]])

    def test_知らせは本文なし_終わりはNone_途中で切れたら例外(self) -> None:
        head, body = self.roundtrip({"event": "started"}, b"")
        self.assertEqual((head, body), ({"event": "started"}, b""))
        self.assertIsNone(bridge_proto.read_frame(io.BytesIO(b"")))
        with self.assertRaises(EOFError):
            bridge_proto.read_frame(io.BytesIO(b'{"id": 1, "len": 10}\nabc'))
        with self.assertRaises(ValueError):
            bridge_proto.read_frame(io.BytesIO(b'{"id": 1, "len": -1}\n'))


class BridgeProcessTests(unittest.TestCase):
    """本物のプロセスを起こして、外枠の代わりに要求を渡す。"""

    def setUp(self) -> None:
        # ソケットを開こうとしたら落ちる Python で起こす(待ち受けていない証拠)
        launcher = textwrap.dedent("""
            import runpy, socket, sys
            class _No(socket.socket):
                def __init__(self, *a, **k):
                    raise RuntimeError("ソケットを開こうとしました")
            socket.socket = _No
            sys.argv = [sys.argv[1]]
            runpy.run_path(sys.argv[0], run_name="__main__")
        """)
        env = dict(os.environ, ALLTOOLS_TOKEN="tok", PYTHONIOENCODING="utf-8")
        self.proc = subprocess.Popen([sys.executable, "-c", launcher, str(ROOT / "bridge.py")],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, env=env, cwd=str(ROOT))
        self.addCleanup(self._close)
        self.err: list[str] = []
        threading.Thread(target=self._drain, daemon=True).start()
        self.events: list[dict] = []
        self.replies: dict[int, tuple[dict, bytes]] = {}
        self.lock = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()
        self.next_id = 1

    def _close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=10)
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except OSError:
                pass

    def _drain(self) -> None:
        for line in self.proc.stderr:
            self.err.append(line.decode("utf-8", "replace").rstrip())

    def _read(self) -> None:
        while True:
            try:
                frame = bridge_proto.read_frame(self.proc.stdout)
            except (ValueError, EOFError, OSError):
                return
            if frame is None:
                return
            head, body = frame
            with self.lock:
                if "event" in head:
                    self.events.append(head)
                else:
                    self.replies[head["id"]] = (head, body)
                self.lock.notify_all()

    def wait_event(self, name: str, timeout: float = 30) -> dict:
        deadline = time.monotonic() + timeout
        with self.lock:
            while time.monotonic() < deadline:
                for event in self.events:
                    if event["event"] == name:
                        return event
                self.lock.wait(0.2)
        self.fail(f"{name} が来ませんでした: {self.events} / {self.err[-20:]}")

    def call(self, method: str, path: str, *, body: bytes = b"", headers=None, host="app.localhost"):
        rid = self.next_id
        self.next_id += 1
        hdrs = [["Host", host], ["X-Tool-Token", "tok"]] + (headers or [])
        head = {"id": rid, "method": method, "path": path, "query": "", "headers": hdrs, "len": len(body)}
        self.proc.stdin.write(json.dumps(head, ensure_ascii=False).encode("utf-8") + b"\n" + body)
        self.proc.stdin.flush()
        deadline = time.monotonic() + 30
        with self.lock:
            while rid not in self.replies and time.monotonic() < deadline:
                self.lock.wait(0.2)
        self.assertIn(rid, self.replies, f"応答がありません: {path} / {self.err[-20:]}")
        reply, data = self.replies.pop(rid)
        return reply["status"], dict((k.lower(), v) for k, v in reply["headers"]), data

    def test_ポート無しで画面とタブを返し_終了で知らせて終わる(self) -> None:
        self.wait_event("started")
        status, headers, page = self.call("GET", "/")
        self.assertEqual(status, 200, page[:300])
        self.assertIn('"edition": "desktop"', page.decode("utf-8"))
        status, _, body = self.call("GET", "/api/tabs")
        self.assertEqual(status, 200, body)
        tabs = json.loads(body)
        self.assertEqual(tabs["identity"]["pc_name"], "TEST-PC")
        status, _, body = self.call("GET", "/api/settings")
        self.assertEqual(status, 200)
        # 守りは生きている
        self.assertEqual(self.call("GET", "/api/health", host="evil.example")[0], 400)
        # 終了 → quit → 外枠が標準入力を閉じる → 終わる
        status, _, body = self.call("POST", "/api/shutdown", body=b"{}",
                                    headers=[["Content-Type", "application/json"]])
        self.assertEqual(status, 200, body)
        self.wait_event("quit", timeout=10)
        self.proc.stdin.close()
        self.assertEqual(self.proc.wait(timeout=20), 0, "\n".join(self.err[-30:]))
        self.assertFalse(any("ソケットを開こうとしました" in line for line in self.err), self.err[-20:])

    def test_外枠が先に閉じても終わる(self) -> None:
        self.wait_event("started")
        self.proc.stdin.close()
        self.assertEqual(self.proc.wait(timeout=20), 0, "\n".join(self.err[-30:]))


if __name__ == "__main__":
    unittest.main()
