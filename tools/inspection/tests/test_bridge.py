"""デスクトップ版の入口(`bridge.py`)── ポートを使わずに画面を返す

外枠(Rust/Tauri)は `bridge.py` を子として起動し、標準入出力で要求を渡す。
ここでは外枠の代わりをして、次を確かめる:

- やりとりの形(見出し1行 + 本文)が往復で崩れない(日本語・バイナリ・大きい本文)
- **ソケットを1つも開かない**(開こうとしたら落ちるようにして起動する)
- 待機画面 → 準備完了 → 一覧・プレビュー(画像)・印刷が返る
- トークン無し・知らない宛先は今までどおり断る
- 「終了」で `quit` を知らせ、標準入力を閉じればプロセスが終わる
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import tests.helpers  # noqa: F401  (ローカル領域を一時フォルダへ)

ROOT = Path(__file__).resolve().parent.parent
import bridge  # noqa: E402

try:
    import flask  # noqa: F401
    HAS_WEB = True
except ImportError:                              # pragma: no cover
    HAS_WEB = False


class FrameTests(unittest.TestCase):
    def roundtrip(self, head: dict, body: bytes) -> tuple[dict, bytes]:
        out = io.BytesIO()
        bridge.FrameWriter(out).write(head, body)
        out.seek(0)
        return bridge.read_frame(out)

    def test_日本語とバイナリの本文が崩れない(self) -> None:
        body = "点検表\n".encode("utf-8") + bytes(range(256))
        head, got = self.roundtrip({"id": 3, "status": 200, "headers": [["X", "あ"]]}, body)
        self.assertEqual(got, body)
        self.assertEqual(head["len"], len(body))
        self.assertEqual(head["headers"], [["X", "あ"]])

    def test_大きい本文(self) -> None:
        body = os.urandom(3 * 1024 * 1024)
        self.assertEqual(self.roundtrip({"id": 1}, body)[1], body)

    def test_知らせは本文なし(self) -> None:
        out = io.BytesIO()
        bridge.FrameWriter(out).event("quit")
        self.assertEqual(json.loads(out.getvalue()), {"event": "quit"})

    def test_終わりはNone_途中で切れたら例外(self) -> None:
        self.assertIsNone(bridge.read_frame(io.BytesIO(b"")))
        with self.assertRaises(EOFError):
            bridge.read_frame(io.BytesIO(b'{"id": 1, "len": 10}\nabc'))
        with self.assertRaises(ValueError):
            bridge.read_frame(io.BytesIO(b'{"id": 1, "len": -1}\n'))


@unittest.skipUnless(HAS_WEB, "flask が無い")
class CallWsgiTests(unittest.TestCase):
    def test_方法_経路_問い合わせ_本文_見出しが届き応答が返る(self) -> None:
        app = flask.Flask("試し")

        @app.post("/api/echo")
        def echo():                                      # noqa: ANN202
            return flask.jsonify({"q": flask.request.args.get("a"),
                                  "body": flask.request.get_json(),
                                  "host": flask.request.host,
                                  "token": flask.request.headers.get("X-Tool-Token")})

        status, headers, data = bridge.call_wsgi(app, {
            "method": "POST", "path": "/api/echo", "query": "a=%E3%81%82",
            "headers": [["Content-Type", "application/json"], ["X-Tool-Token", "t"],
                        ["Host", "app.localhost"]]},
            json.dumps({"部数": 2}).encode("utf-8"))
        self.assertEqual(status, 200)
        self.assertIn(["Content-Type", "application/json"], headers)
        self.assertEqual(json.loads(data), {"q": "あ", "body": {"部数": 2},
                                            "host": "app.localhost", "token": "t"})


@unittest.skipUnless(HAS_WEB, "flask が無い")
class BridgeProcessTests(unittest.TestCase):
    """本物の子プロセスとして起動し、外枠の代わりに要求を投げる。"""

    TOKEN = "desk-test-token"

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "nosock").mkdir()
        # **ソケットを開こうとしたら落ちる**ようにして起動する
        (root / "nosock" / "sitecustomize.py").write_text(
            "import socket\n"
            "class _No(socket.socket):\n"
            "    def __init__(self, *a, **k):\n"
            "        raise RuntimeError('ソケットを開こうとしました')\n"
            "socket.socket = _No\n", encoding="utf-8")
        env = dict(os.environ, INSPECTION_LOCAL_DIR=str(root / "local"),
                   INSPECTION_DISTRIBUTION_DIR=str(root / "配布設定"),
                   INSPECTION_TOKEN=self.TOKEN, INSPECTION_DEMO="1",
                   PYTHONPATH=str(root / "nosock"), PYTHONIOENCODING="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", str(ROOT / "bridge.py")], env=env, cwd=str(ROOT),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self._kill)
        self.err: list[str] = []
        threading.Thread(target=lambda: self.err.extend(
            self.proc.stderr.read().decode("utf-8", "replace").splitlines()), daemon=True).start()
        self.events: list[dict] = []
        self.replies: dict[int, tuple[dict, bytes]] = {}
        self.cond = threading.Condition()
        self.next_id = 0
        self.send_lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()
        self.wait_event("started")

    def _kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except OSError:
                pass

    def _read(self) -> None:
        while True:
            try:
                frame = bridge.read_frame(self.proc.stdout)
            except (ValueError, EOFError, OSError):
                frame = None
            with self.cond:
                if frame is None:
                    self.events.append({"event": "<closed>"})
                    self.cond.notify_all()
                    return
                head, body = frame
                if "event" in head:
                    self.events.append(head)
                else:
                    self.replies[head["id"]] = (head, body)
                self.cond.notify_all()

    def wait_event(self, name: str, timeout: float = 60) -> dict:
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                for e in self.events:
                    if e["event"] == name:
                        return e
                if any(e["event"] in ("<closed>", "fatal") for e in self.events) or time.monotonic() > deadline:
                    self.fail(f"{name} が来ません: {self.events}\n" + "\n".join(self.err[-30:]))
                self.cond.wait(0.2)

    def call(self, method: str, path: str, *, query: str = "", body: bytes = b"",
             token: bool = True, host: str = "app.localhost", json_body=None):
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
        headers = [["Host", host]]
        if token:
            headers.append(["X-Tool-Token", self.TOKEN])
        if body:
            headers.append(["Content-Type", "application/json"])
        with self.send_lock:
            self.next_id += 1
            rid = self.next_id
            self.proc.stdin.write(json.dumps({"id": rid, "method": method, "path": path, "query": query,
                                              "headers": headers, "len": len(body)}).encode() + b"\n" + body)
            self.proc.stdin.flush()
        deadline = time.monotonic() + 60
        with self.cond:
            while rid not in self.replies:
                if time.monotonic() > deadline:
                    self.fail(f"応答がありません: {path}\n" + "\n".join(self.err[-30:]))
                self.cond.wait(0.2)
            head, data = self.replies.pop(rid)
        return head["status"], dict((k.lower(), v) for k, v in head["headers"]), data

    def wait_ready(self) -> dict:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            status, _, data = self.call("GET", "/api/health")
            body = json.loads(data)
            if body.get("ready"):
                return body
            time.sleep(0.2)
        self.fail("準備が終わりません")

    def test_ポートを開かずに_画面_一覧_プレビュー_印刷が通り_終了で終わる(self) -> None:
        health = self.wait_ready()
        self.assertEqual(health["edition"], "desktop")
        self.assertEqual(health["port"], 0)

        status, headers, page = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["content-type"])
        self.assertIn(self.TOKEN.encode(), page)                  # 画面がトークンを持つ

        status, _, data = self.call("GET", "/api/inventory")
        self.assertEqual(status, 200)
        inv = json.loads(data)["inventory"]
        item = inv["categories"][0]["subcategories"][0]["items"][0]

        status, _, data = self.call("POST", "/api/preview", json_body={"id": item["id"]})
        self.assertEqual(status, 200, data)
        image_url = json.loads(data)["image_url"]
        status, headers, image = self.call("GET", image_url, query=f"t={self.TOKEN}", token=False)
        self.assertEqual(status, 200)
        self.assertTrue(image.startswith(b"<svg") or image[:4] == b"\x89PNG", image[:20])

        status, _, data = self.call("POST", "/api/print", json_body={"ids": [item["id"]], "copies": 1})
        self.assertEqual(status, 202, data)
        deadline = time.monotonic() + 30
        while json.loads(self.call("GET", "/api/print/status")[2])["job"]["active"]:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.2)

        status, _, css = self.call("GET", "/static/css/tokens.css", query="v=1")
        self.assertEqual(status, 200)

        # 守りは今までどおり
        self.assertEqual(self.call("GET", "/api/inventory", token=False)[0], 401)
        self.assertEqual(self.call("GET", "/api/health", host="evil.example")[0], 400)

        # 画面の「終了」→ quit を知らせる → 外枠が標準入力を閉じる → 終わる
        status, _, data = self.call("POST", "/api/shutdown", json_body={})
        self.assertEqual(status, 200, data)
        self.wait_event("quit", timeout=15)
        self.proc.stdin.close()
        self.assertEqual(self.proc.wait(timeout=20), 0, "\n".join(self.err[-30:]))
        self.assertFalse(any("ソケットを開こうとしました" in line for line in self.err), self.err[-20:])

    def test_外枠が先に閉じても終わる(self) -> None:
        self.wait_ready()
        self.proc.stdin.close()
        self.assertEqual(self.proc.wait(timeout=20), 0, "\n".join(self.err[-30:]))


# 統合ツールの一式(このツールは `tools/inspection/` にある)
INTEGRATED = ROOT.parent.parent


class IntegratedShellTest(unittest.TestCase):
    """統合ツールの外枠(Rust/Tauri)・入口と、名前・置き場所が揃っているか。"""

    def entry(self) -> dict:
        tools = json.loads((INTEGRATED / "config" / "tools.json").read_text(encoding="utf-8"))
        return next(t for t in tools["tools"] if t["id"] == "inspection")

    def test_統合ツールの一覧に載っていて_置き場所と環境変数の頭が揃っている(self) -> None:
        from core import app_config
        entry = self.entry()
        self.assertEqual((INTEGRATED / entry["dir"]).resolve(), ROOT.resolve())
        # 外枠は <頭>_TOKEN / <頭>_PYTHON / <頭>_LOCAL_DIR を使う
        self.assertEqual(entry["env_prefix"], "INSPECTION")
        self.assertEqual(entry["demo_env"], "INSPECTION_DEMO")
        self.assertEqual(entry["local_dir_name"], app_config.load()["local_dir_name"])

    def test_外枠が渡す宛先名を受け付ける(self) -> None:
        from core import app_config
        relay = (INTEGRATED / "src-tauri" / "src" / "relay.rs").read_text(encoding="utf-8")
        self.assertIn('pub const TOOL_HOST: &str = "app.localhost";', relay)
        self.assertIn("app.localhost", app_config.BRIDGE_HOSTS)

    def test_窓を前に出すexeは一式のフォルダの直下(self) -> None:
        from unittest import mock

        from core import instance_guard
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"ALLTOOLS_ROOT": tmp}):
                (Path(tmp) / "AllTools.exe").write_bytes(b"")
                self.assertEqual(instance_guard.desktop_exe(), str(Path(tmp) / "AllTools.exe"))
                # 日本語の名前の exe があれば、そちら(配るときの名前)
                (Path(tmp) / "統合ツール.exe").write_bytes(b"")
                self.assertEqual(instance_guard.desktop_exe(), str(Path(tmp) / "統合ツール.exe"))


class BridgeGuardTest(unittest.TestCase):
    """ブラウザ版とは同時に動かさない(後から開いたほうが止まる)。"""

    def setUp(self) -> None:
        from core import instance_guard
        instance_guard.release_all()
        self.addCleanup(instance_guard.release_all)

    def test_ブラウザ版が先に動いていればデスクトップ版は起動しない(self) -> None:
        import start_app
        from core import instance_guard
        self.assertTrue(instance_guard.claim(instance_guard.BROWSER).ok)
        with self.assertRaises(start_app.StartupError) as ctx:
            start_app.start_bridge(server_factory=lambda *a: self.fail("起こしてはいけない"))
        self.assertIn("ブラウザ版", str(ctx.exception))
        self.assertIn("もう一度開く", ctx.exception.hint)


if __name__ == "__main__":
    unittest.main()
