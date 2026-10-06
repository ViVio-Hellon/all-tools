"""デスクトップ版の入口(``bridge.py``)── ポートを使わずに画面を返す

外枠(統合ツールの Rust/Tauri)は ``bridge.py`` を子として起動し、標準入出力で
要求を渡す。ここでは外枠の代わりをして、次を確かめる:

- やりとりの形(見出し1行 + 本文)が往復で崩れない(日本語・バイナリ・大きい本文)
- **ソケットを1つも開かない**(開こうとしたら落ちるようにして起動する)
- 待機画面 → 準備完了 → 日報入力・記録・設定・紙・静的ファイルが返る
- トークン無し・知らない宛先は今までどおり断る
- 「終わってよいか」を訊くだけなら止まらない。「終了」で ``quit`` を知らせ、
  標準入力を閉じればプロセスが終わる
- **応答を返してから**後片付け(共有の控えへの写し)をする
- ブラウザ版とデスクトップ版が同時に動かない(後から開いたほうが止まる)
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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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
        body = "日報の紙\n".encode("utf-8") + bytes(range(256))
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
            return flask.jsonify({"q": flask.request.args.get("line"),
                                  "body": flask.request.get_json(),
                                  "host": flask.request.host,
                                  "token": flask.request.headers.get("X-Tool-Token")})

        status, headers, data, close = bridge.call_wsgi(app, {
            "method": "POST", "path": "/api/echo", "query": "line=%E6%A9%9F%E5%81%B4",
            "headers": [["Content-Type", "application/json"], ["X-Tool-Token", "t"],
                        ["Host", "app.localhost"]]},
            json.dumps({"本文": 1}).encode("utf-8"))
        close()
        self.assertEqual(status, 200)
        self.assertIn(["Content-Type", "application/json"], headers)
        self.assertEqual(json.loads(data), {"q": "機側", "body": {"本文": 1},
                                            "host": "app.localhost", "token": "t"})

    def test_後片付けは応答のあとで呼ぶ(self) -> None:
        """日報は応答を返してから共有の控えへ写す(`call_on_close`)。

        後片付けを応答の前に呼ぶと、保存のたびに共有フォルダの写しを待たされる。
        """
        app = flask.Flask("試し")
        done: list[str] = []

        @app.post("/api/save")
        def save():                                      # noqa: ANN202
            response = flask.jsonify({"saved": True})
            response.call_on_close(lambda: done.append("控えへ写した"))
            return response

        status, _, data, close = bridge.call_wsgi(app, {"method": "POST", "path": "/api/save"}, b"")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data), {"saved": True})
        self.assertEqual(done, [], "応答を受け取った時点では、まだ写していない")
        close()
        self.assertEqual(done, ["控えへ写した"])


@unittest.skipUnless(HAS_WEB, "flask が無い")
class BridgeProcessTests(unittest.TestCase):
    """本物の子プロセスとして起動し、外枠の代わりに要求を投げる。"""

    TOKEN = "desk-test-token"

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.root = root
        (root / "nosock").mkdir()
        # **ソケットを開こうとしたら落ちる**ようにして起動する
        (root / "nosock" / "sitecustomize.py").write_text(
            "import socket\n"
            "class _No(socket.socket):\n"
            "    def __init__(self, *a, **k):\n"
            "        raise RuntimeError('ソケットを開こうとしました')\n"
            "socket.socket = _No\n", encoding="utf-8")
        self.env = dict(os.environ,
                        NIPPOU_LOCAL_DIR=str(root / "local"),
                        NIPPOU_APP_DIR=str(root / "app"),
                        NIPPOU_GW_REF_DIR=str(root / "ref"),
                        NIPPOU_DIST_DIR=str(root / "配布設定"),
                        NIPPOU_ACCESS_DIR=str(root / "share"),
                        NIPPOU_TOKEN=self.TOKEN,
                        PYTHONPATH=str(root / "nosock"))
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "bridge.py")], cwd=str(ROOT),
            env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self._kill)
        self.err: list[str] = []
        threading.Thread(target=lambda: self.err.extend(
            self.proc.stderr.read().decode("utf-8", "replace").splitlines()),
            daemon=True).start()
        self.events: list[dict] = []
        self.replies: dict[int, tuple[dict, bytes]] = {}
        self.lock = threading.Lock()
        self.next_id = 0
        threading.Thread(target=self._read, daemon=True).start()

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
                return
            if frame is None:
                return
            head, body = frame
            if "event" in head:
                self.events.append(head)
            else:
                self.replies[head["id"]] = (head, body)

    def request(self, method: str, path: str, *, query: str = "", body: bytes = b"",
                headers=None, token: bool = True, host: str = "app.localhost",
                timeout: float = 30.0) -> tuple[dict, bytes]:
        with self.lock:
            self.next_id += 1
            i = self.next_id
            h = [["Host", host]] + ([["X-Tool-Token", self.TOKEN]] if token else [])
            h += headers or []
            head = {"id": i, "method": method, "path": path, "query": query,
                    "headers": h, "len": len(body)}
            self.proc.stdin.write(json.dumps(head).encode("utf-8") + b"\n" + body)
            self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while i not in self.replies:
            if time.monotonic() > deadline:
                self.fail(f"{path} の応答が来ない。stderr: {self.err[-15:]}")
            time.sleep(0.01)
        return self.replies.pop(i)

    def post_json(self, path: str, payload: dict) -> tuple[dict, bytes]:
        return self.request("POST", path, body=json.dumps(payload).encode("utf-8"),
                            headers=[["Content-Type", "application/json"]])

    def wait_started(self) -> None:
        deadline = time.monotonic() + 60
        while not self.events and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.events[:1], [{"event": "started"}], self.err[-15:])

    def wait_ready(self) -> dict:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            head, body = self.request("GET", "/api/health")
            if head["status"] == 200 and json.loads(body).get("ready"):
                return json.loads(body)
            time.sleep(0.1)
        self.fail(f"準備完了にならない。stderr: {self.err[-15:]}")

    def test_ポート無しで起動し画面を返し_終了で知らせて終わる(self) -> None:
        self.wait_started()
        health = self.wait_ready()
        self.assertEqual(health["app_id"], "nlm.nippou-tool")

        # 画面(ページ)・静的ファイル(版が道に入った /sv/ と昔の /static/)・業務の API・紙
        page = self.request("GET", "/")
        self.assertEqual(page[0]["status"], 200, page[1][:300])
        html = page[1].decode("utf-8")
        self.assertIn("window.APP", html)
        stamp = json.loads(self.request("GET", "/api/health")[1]).get("static_stamp") or "x"
        for path, query in (("/records", ""), ("/settings", ""), ("/agg", ""), ("/vc", ""),
                            ("/static/js/app.js", ""), ("/static/js/desktop.js", ""),
                            (f"/sv/{stamp}/js/desktop.js", ""), ("/api/settings/state", ""),
                            ("/api/shift/key", ""), ("/api/vc/state", "")):
            with self.subTest(path=path):
                head, body = self.request("GET", path, query=query)
                self.assertEqual(head["status"], 200, body[:300])
        # 紙(トークンが要る経路)。データが無い日は紙の 404 ページが返る ── 経路は通っている
        head, body = self.request("GET", "/report/nippou",
                                  query="report_date=2026-10-01&line=L-1&shift=1&page=all")
        self.assertIn(head["status"], (200, 404), body[:300])
        self.assertIn("<", body.decode("utf-8"))

        # 同時に来ても取り違えない(画面は心拍・タブの合図・進み具合を同時に出す)
        results: list[int] = []
        threads = [threading.Thread(target=lambda: results.append(
            self.request("GET", "/api/health")[0]["status"])) for _ in range(12)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(results, [200] * 12)

        # 自動終了の見張りは立てない(窓が閉じたら外枠が終わらせる)
        head, body = self.post_json("/api/alive", {"tab": "t1", "page": "entry"})
        self.assertEqual(head["status"], 200, body)

        # 守りは今までどおり
        self.assertEqual(self.request("GET", "/api/entry/state", token=False)[0]["status"], 401)
        self.assertEqual(self.request("GET", "/api/entry/state", host="evil.example")[0]["status"], 400)

        # 訊くだけなら止まらない(統合ツールの外枠が先に全ツールへ訊く)
        head, body = self.post_json("/api/shutdown", {"check": True})
        self.assertEqual(head["status"], 200, body)
        self.assertEqual(json.loads(body), {"stopped": False, "can_stop": True})
        time.sleep(0.8)
        self.assertNotIn({"event": "quit"}, self.events)
        self.assertEqual(self.request("GET", "/api/health")[0]["status"], 200)

        head, body = self.post_json("/api/shutdown", {})
        self.assertEqual(head["status"], 200, body)
        deadline = time.monotonic() + 10
        while {"event": "quit"} not in self.events and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIn({"event": "quit"}, self.events)
        self.proc.stdin.close()
        self.assertEqual(self.proc.wait(timeout=40), 0, self.err[-20:])
        self.assertFalse([line for line in self.err if "ソケットを開こうとしました" in line],
                         self.err[-20:])

    def test_外枠が先に閉じたら終わる(self) -> None:
        self.wait_started()
        self.wait_ready()
        self.proc.stdin.close()
        self.assertEqual(self.proc.wait(timeout=40), 0, self.err[-20:])


@unittest.skipUnless(HAS_WEB, "flask が無い")
class FatalTests(unittest.TestCase):
    def test_起動できないときは理由を知らせる(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "ファイル"
            blocker.write_text("x", encoding="utf-8")
            env = dict(os.environ, NIPPOU_LOCAL_DIR=str(blocker),  # 作業用フォルダを作れない
                       NIPPOU_APP_DIR=str(Path(tmp) / "app"),
                       NIPPOU_DIST_DIR=str(Path(tmp) / "配布設定"))
            done = subprocess.run([sys.executable, str(ROOT / "bridge.py")], cwd=str(ROOT),
                                  env=env, input=b"", capture_output=True, timeout=60)
        self.assertEqual(done.returncode, 1, done.stderr[-500:])
        event = json.loads(done.stdout.decode("utf-8").splitlines()[0])
        self.assertEqual(event["event"], "fatal")
        self.assertIn("作業用フォルダ", event["message"])
        self.assertTrue(event["hint"])


class DesktopLockTests(unittest.TestCase):
    """ブラウザ版とデスクトップ版は同時に動かない(後から開いたほうが止まる)。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self._saved = {k: os.environ.get(k) for k in ("NIPPOU_LOCAL_DIR", "NIPPOU_APP_DIR")}
        os.environ["NIPPOU_LOCAL_DIR"] = str(self.tmp / "local")
        os.environ["NIPPOU_APP_DIR"] = str(self.tmp / "app")
        self.addCleanup(self._restore)
        import launch_guard
        self.guard = launch_guard
        self.addCleanup(launch_guard.release_desktop_lock)

    def _restore(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_錠が無い_握られていない_握られている(self) -> None:
        guard = self.guard
        self.assertFalse(guard.desktop_running(), "錠のファイルが無い")
        path = guard.desktop_lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
        self.assertFalse(guard.desktop_running(), "ファイルだけ残っている(デスクトップ版は終わった)")

        # 別のプロセスが握っている(デスクトップ版の bridge.py の代わり)
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import sys, time; sys.path.insert(0, sys.argv[1]); import launch_guard;"
             "assert launch_guard.hold_desktop_lock(); print('held', flush=True); time.sleep(30)",
             str(ROOT)],
            env=dict(os.environ), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), b"held", holder.stderr.read(2000) if holder.poll() else b"")
        self.assertTrue(guard.desktop_running(), "デスクトップ版が握っている")
        self.assertFalse(guard.hold_desktop_lock(), "2つ目のデスクトップ版は握れない")
        holder.kill()
        holder.wait(timeout=10)
        self.assertFalse(guard.desktop_running(), "終われば OS が外す")
        self.assertTrue(guard.hold_desktop_lock())

    def test_ブラウザ版の印_生きていなければ居ないものとする(self) -> None:
        guard = self.guard
        self.assertIsNone(guard.browser_running(), "印が無い")
        guard.write_lock(guard.LockInfo(app_id="nlm.nippou-tool", pid=99999999, port=0,
                                        url="", started_at=time.time()))
        self.assertIsNone(guard.browser_running(), "PID が居ない(落ちた印)")

    def test_ブラウザ版の起動はデスクトップ版が居れば止まる(self) -> None:
        import start_app

        self.assertTrue(self.guard.hold_desktop_lock())
        with self.assertRaises(start_app.StartupError) as caught:
            start_app.start(open_browser=False)
        self.assertIn("デスクトップ版", str(caught.exception))
        self.assertIn("統合ツール", caught.exception.hint)

    def test_デスクトップ版の起動はブラウザ版が居れば止まる(self) -> None:
        import start_app

        guard = self.guard
        # 動いているブラウザ版の代わり: 自分とは別の生きているプロセスの印
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)",
                                  "start_app.py"])
        self.addCleanup(other.kill)
        guard.write_lock(guard.LockInfo(app_id="nlm.nippou-tool", pid=other.pid, port=0,
                                        url="", started_at=time.time(),
                                        app_root=str(ROOT)))
        with self.assertRaises(start_app.StartupError) as caught:
            start_app.start_bridge(token="x", server_factory=lambda token: None)
        self.assertIn("ブラウザ版", str(caught.exception))
        self.assertFalse(guard.desktop_running(), "止まったほうは錠を握らない")


class WiringTests(unittest.TestCase):
    """画面の別窓・フォルダ選択が、デスクトップ版では外枠へ頼む作りになっている。"""

    def test_紙と早見表は外枠の別窓で開く(self) -> None:
        js = ROOT / "app" / "static" / "js"
        for name in ("views/entry.js", "views/records.js", "views/gw.js"):
            text = (js / name).read_text(encoding="utf-8")
            with self.subTest(name=name):
                self.assertIn('from "../desktop.js"', text)
                self.assertNotIn('window.open(tokenUrl(', text)
        vc = (js / "views" / "vc.js").read_text(encoding="utf-8")
        self.assertIn('label: "vc-quick"', vc)
        desktop = (js / "desktop.js").read_text(encoding="utf-8")
        self.assertIn('invoke("open_window"', desktop)
        self.assertIn('invoke("pick_path"', desktop)
        self.assertIn("desktop.install()", (js / "app.js").read_text(encoding="utf-8"))
        self.assertIn('desktop.pickPath("folder"',
                      (js / "views" / "settings.js").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
