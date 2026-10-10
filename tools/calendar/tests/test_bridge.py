"""デスクトップ版の入口(``bridge.py``)── ポートを使わずに画面を返す

外枠(Rust/Tauri)は ``bridge.py`` を子として起動し、標準入出力で要求を渡す。
ここでは外枠の代わりをして、次を確かめる:

- やりとりの形(見出し1行 + 本文)が往復で崩れない(日本語・バイナリ・大きい本文)
- **ソケットを1つも開かない**(開こうとしたら落ちるようにして起動する)
- 待機画面 → 準備完了 → 画面・操作・印刷が返る。同時に来ても取り違えない
- トークン無し・知らない宛先は今までどおり断る
- 「終了」で ``quit`` を知らせ、標準入力を閉じればプロセスが終わる
- **未送信を抱えたまま終わらない**(終わる前に送り切る。送れなければ覚える)
- 起動できないときは理由(``fatal``)を外枠へ渡す
- 日報複合ツールの外枠と Python で、宛先と環境変数の名前が食い違わない
- ブラウザ版とは同時に動かさない(後から開いたほうが止まる)
"""
from __future__ import annotations

import io
import json
import os
import re
import sqlite3
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
        body = "休み管理\n".encode("utf-8") + bytes(range(256))
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
            json.dumps({"ライン": "コイル"}).encode("utf-8"))
        self.assertEqual(status, 200)
        self.assertIn(["Content-Type", "application/json"], headers)
        self.assertEqual(json.loads(data), {"q": "あ", "body": {"ライン": "コイル"},
                                            "host": "app.localhost", "token": "t"})


def _no_socket_dir(root: Path) -> Path:
    """**ソケットを開こうとしたら落ちる**ようにする(``sitecustomize``)。"""
    folder = root / "nosock"
    folder.mkdir()
    (folder / "sitecustomize.py").write_text(
        "import socket\n"
        "class _No(socket.socket):\n"
        "    def __init__(self, *a, **k):\n"
        "        raise RuntimeError('ソケットを開こうとしました')\n"
        "socket.socket = _No\n", encoding="utf-8")
    return folder


class BridgeProcess:
    """本物の子プロセスとして起動し、外枠の代わりに要求を投げる。"""

    TOKEN = "desk-test-token"

    def __init__(self, case: unittest.TestCase, env: dict) -> None:
        self.case = case
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "bridge.py")], env=env, cwd=str(ROOT),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        case.addCleanup(self.kill)
        self.err: list[str] = []
        threading.Thread(target=lambda: self.err.extend(
            self.proc.stderr.read().decode("utf-8", "replace").splitlines()),
            daemon=True).start()
        self.events: list[dict] = []
        self.replies: dict[int, tuple[dict, bytes]] = {}
        self.lock = threading.Lock()
        self.next_id = 0
        threading.Thread(target=self._read, daemon=True).start()

    def kill(self) -> None:
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

    def request(self, method: str, path: str, *, body: bytes = b"", query: str = "",
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
                self.case.fail(f"{path} の応答が来ない。stderr: {self.err[-15:]}")
            time.sleep(0.01)
        return self.replies.pop(i)

    def post(self, path: str, data: dict) -> tuple[dict, bytes]:
        return self.request("POST", path, body=json.dumps(data).encode("utf-8"),
                            headers=[["Content-Type", "application/json"]])

    def wait_event(self, name: str, timeout: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for event in self.events:
                if event.get("event") == name:
                    return event
            time.sleep(0.01)
        self.case.fail(f"{name} が来ない。events={self.events} stderr: {self.err[-15:]}")

    def wait_ready(self) -> None:
        self.wait_event("started")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            head, body = self.request("GET", "/api/health")
            if head["status"] == 200 and json.loads(body)["ready"]:
                return
            time.sleep(0.1)
        self.case.fail(f"準備完了にならない。stderr: {self.err[-15:]}")


@unittest.skipUnless(HAS_WEB, "flask が無い")
class BridgeProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.env = dict(os.environ,
                        CALENDAR_HOME=str(self.root / "home"),
                        CALENDAR_LOCAL_DIR=str(self.root / "local"),
                        CALENDAR_PC_NAME="PC-DESK",
                        CALENDAR_TOKEN=BridgeProcess.TOKEN,
                        PYTHONPATH=str(_no_socket_dir(self.root)))

    def log_text(self) -> str:
        return "\n".join(p.read_text(encoding="utf-8", errors="replace")
                         for p in sorted((self.root / "local" / "logs").glob("calendar_*.log")))

    def test_ポート無しで起動し画面と操作を返し_終了で知らせて終わる(self) -> None:
        b = BridgeProcess(self, self.env)
        b.wait_event("started")
        self.assertEqual(b.events[:1], [{"event": "started"}], b.err[-15:])
        b.wait_ready()

        head, _ = b.request("GET", "/")
        self.assertEqual(head["status"], 302)
        for path, query in (("/calendar", ""), ("/settings", ""), ("/history", ""),
                            ("/print", f"year=2026&month=9&t={b.TOKEN}"),
                            ("/static/js/app.js", ""), ("/static/js/desktop.js", "")):
            with self.subTest(path=path):
                self.assertEqual(b.request("GET", path, query=query)[0]["status"], 200)

        # 印刷の窓には「印刷する」「閉じる」があり、閉じるは外枠に頼める
        _, page = b.request("GET", "/print", query=f"year=2026&month=9&t={b.TOKEN}")
        self.assertIn(b'id="do-print"', page)
        self.assertIn(b"close_window", page)

        # 業務の API(設定・カレンダー)。操作の記録も残る
        head, body = b.request("GET", "/api/calendar", query="year=2026&month=9")
        self.assertEqual(head["status"], 200, body[:300])
        self.assertEqual(json.loads(body)["year"], 2026)
        head, _ = b.post("/api/screen/claim", {"screen_id": "s1", "force": False})
        self.assertEqual(head["status"], 200)

        # 同時に来ても取り違えない(画面は見張り・心拍・操作を同時に出す)
        results: list[int] = []
        threads = [threading.Thread(target=lambda: results.append(
            b.request("GET", "/api/sync")[0]["status"])) for _ in range(12)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(results, [200] * 12)

        # 画面が送る形(Origin つき)。**Host は画面の宛先と同じ名前で届く**
        # (外枠の `page_host`)。Windows の WebView2 は Origin が http://app.localhost
        def claim(host: str) -> int:
            return b.request("POST", "/api/screen/claim", host=host,
                             body=b'{"screen_id": "s1"}',
                             headers=[["Content-Type", "application/json"],
                                      ["Origin", "http://app.localhost"]])[0]["status"]
        self.assertEqual(claim("app.localhost"), 200)
        # 外枠が URI の名前(localhost)を渡すと、別のページ扱いで断られる
        # ── Windows で全操作が断られた原因。外枠がこれを渡さないことを Rust の試験で見る
        self.assertEqual(claim("localhost"), 403)

        # 守りは今までどおり
        self.assertEqual(b.request("GET", "/api/settings", token=False)[0]["status"], 403)
        self.assertEqual(b.request("GET", "/api/settings", host="evil.example")[0]["status"], 403)

        head, body = b.post("/api/shutdown", {})
        self.assertEqual(head["status"], 200, body)
        b.wait_event("quit", timeout=20)
        b.proc.stdin.close()
        self.assertEqual(b.proc.wait(timeout=20), 0)
        self.assertFalse([line for line in b.err if "ソケットを開こうとしました" in line],
                         b.err[-20:])
        # 画面からの操作がログに残る(外枠 → Python → 外枠が1周した証拠)
        self.assertIn("操作 POST /api/screen/claim → 200", self.log_text())
        self.assertIn("ポートは使いません", self.log_text())

    def test_外枠が先に閉じたら終わる(self) -> None:
        b = BridgeProcess(self, self.env)
        b.wait_ready()
        b.proc.stdin.close()
        self.assertEqual(b.proc.wait(timeout=20), 0)

    def test_登録は共有へ届いてから終わる(self) -> None:
        """取り込み元が見えている端末で登録し、すぐ終了しても入力は失われない。"""
        from calendar_app import config
        from tests import _web

        share = self.root / "share"
        share.mkdir()
        source = _web.make_source(share, config.SOURCE_FILE_DATA, _web.SOURCE_SCHEMA)
        master = _web.make_source(share, config.SOURCE_FILE_MASTER, _web.MASTER_SCHEMA)
        # **閉じる。** ``with sqlite3.connect()`` は確定するだけで閉じない。
        # 開いたままだと Windows では後片付けで消せない(ファイルの錠)
        conn = sqlite3.connect(master)
        try:
            conn.execute(f'INSERT INTO "{config.TABLE_MEMBER}" VALUES '
                         "('1', '試験', 'A', '試験太郎', 'しけん', 'コイル')")
            conn.commit()
        finally:
            conn.close()
        (self.root / "home").mkdir()
        (self.root / "home" / "settings.json").write_text(json.dumps({
            "保存用DBフォルダ": str(share), "PCライン": "コイル",
            "自動同期": True, "自動同期間隔秒": 3600}, ensure_ascii=False), encoding="utf-8")
        b = BridgeProcess(self, self.env)
        b.wait_ready()
        # 起動直後の取り込みで班員名簿が入るのを待つ(宛先の一覧に出る)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            _, body = b.request("GET", "/api/comment-targets")
            if "コイル" in body.decode("utf-8"):
                break
            time.sleep(0.2)
        head, body = b.post("/api/comment", {
            "date": "2026/10/05", "line": "コイル", "group": "A", "text": "デスクトップ版から"})
        self.assertEqual(head["status"], 200, body[:300])

        # 同期の途中なら 409(画面・外枠は「中断して終了しますか」と訊く)。
        # ここでは中断させず、終わるのを待ってから頼み直す
        deadline = time.monotonic() + 30
        while True:
            head, body = b.post("/api/shutdown", {})
            if head["status"] != 409 or time.monotonic() > deadline:
                break
            time.sleep(0.3)
        self.assertEqual(head["status"], 200, body[:300])
        b.wait_event("quit", timeout=30)
        b.proc.stdin.close()
        self.assertEqual(b.proc.wait(timeout=20), 0)
        remote = sqlite3.connect(source)
        try:
            texts = [r[0] for r in remote.execute(f'SELECT "登録内容" FROM "{config.TABLE_DATA}"')]
        finally:
            remote.close()
        self.assertIn("デスクトップ版から", texts)
        self.assertNotIn("を残して終わります", self.log_text())


class SendBeforeExitTests(unittest.TestCase):
    """窓を閉じたら終わるので、**終わる前に送り切る**(ブラウザ版の粘りの代わり)。"""

    def run_with(self, counts: list[int], *, timeout: float = 2.0):
        from unittest import mock

        from calendar_app import sync_service

        sent: list[bool] = []
        remembered: list[int] = []
        service = mock.Mock()
        service.is_busy.return_value = False
        service.sync_now.side_effect = lambda receive: sent.append(receive)
        values = iter(counts)
        last = [counts[-1]]

        def count() -> int:
            try:
                last[0] = next(values)
            except StopIteration:
                pass
            return last[0]

        with mock.patch.object(sync_service, "get_service", return_value=service), \
                mock.patch.object(sync_service, "unsent_count", side_effect=count), \
                mock.patch.object(sync_service, "remember_exit", side_effect=remembered.append):
            left = sync_service.send_before_exit(timeout)
        return left, sent, remembered

    def test_送れたら0を覚える(self) -> None:
        left, sent, remembered = self.run_with([2, 0])
        self.assertEqual(left, 0)
        self.assertEqual(sent, [False])                 # 送るだけ(取り込みはしない)
        self.assertEqual(remembered, [0])

    def test_届かなければ待ち続けずに残りを覚える(self) -> None:
        start = time.monotonic()
        left, sent, remembered = self.run_with([3, 3], timeout=30)
        self.assertLess(time.monotonic() - start, 5)    # 減らないなら粘らない
        self.assertEqual(left, 3)
        self.assertEqual(remembered, [3])

    def test_未送信が無ければ送らない(self) -> None:
        left, sent, remembered = self.run_with([0])
        self.assertEqual((left, sent, remembered), (0, [], [0]))


@unittest.skipUnless(HAS_WEB, "flask が無い")
class FatalTests(unittest.TestCase):
    def test_起動できないときは理由を知らせる(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "ファイル"
            blocker.write_text("x", encoding="utf-8")
            env = dict(os.environ, CALENDAR_LOCAL_DIR=str(blocker),   # 作業用フォルダを作れない
                       CALENDAR_HOME=str(blocker / "home"))
            done = subprocess.run([sys.executable, str(ROOT / "bridge.py")], env=env,
                                  input=b"", capture_output=True, timeout=60, cwd=str(ROOT))
        self.assertEqual(done.returncode, 1, done.stderr[-500:])
        event = json.loads(done.stdout.decode("utf-8").splitlines()[0])
        self.assertEqual(event["event"], "fatal")
        self.assertTrue(event["message"])
        self.assertIn("hint", event)


# 日報複合ツールの一式(このツールは ``tools/calendar/`` にある)
INTEGRATED = ROOT.parent.parent


class IntegratedShellTests(unittest.TestCase):
    """日報複合ツールの外枠(Rust/Tauri)・入口と、名前・置き場所が食い違わない。"""

    def entry(self) -> dict:
        tools = json.loads((INTEGRATED / "config" / "tools.json").read_text(encoding="utf-8"))
        return next(t for t in tools["tools"] if t["id"] == "calendar")

    def test_日報複合ツールの一覧に載っていて_置き場所と環境変数の頭が揃っている(self) -> None:
        """外枠が渡す名前と Python が読む名前がずれると、黙って既定で動く。"""
        from calendar_app import app_config
        entry = self.entry()
        self.assertEqual((INTEGRATED / entry["dir"]).resolve(), ROOT.resolve())
        # 外枠は <頭>_TOKEN / <頭>_PYTHON / <頭>_LOCAL_DIR を使う
        self.assertEqual(entry["env_prefix"], "CALENDAR")
        self.assertIn("CALENDAR_TOKEN", (ROOT / "bridge.py").read_text(encoding="utf-8"))
        self.assertEqual(entry["local_dir_name"], app_config.load()["local_dir_name"])

    def test_外枠が渡す宛先名を受け付ける(self) -> None:
        from calendar_app import app_config
        relay = (INTEGRATED / "src-tauri" / "src" / "relay.rs").read_text(encoding="utf-8")
        self.assertIn('pub const TOOL_HOST: &str = "app.localhost";', relay)
        self.assertIn("app.localhost", app_config.BRIDGE_HOSTS)


class DesktopGuardTests(unittest.TestCase):
    """ブラウザ版とデスクトップ版(日報複合ツールの窓)を同時に動かさない。後から開いたほうが止まる。"""

    def setUp(self) -> None:
        import launch_guard
        self.guard = launch_guard
        launch_guard.release_desktop_lock()
        self.addCleanup(launch_guard.release_desktop_lock)

    def test_錠が無い_握った_ほかのプロセスからも動いていると見える(self) -> None:
        self.assertFalse(self.guard.desktop_running())
        self.assertTrue(self.guard.hold_desktop_lock(wait_sec=0))
        self.assertTrue(self.guard.desktop_running(), "自分が握っている")
        probe = ("import sys; sys.path.insert(0, sys.argv[1]); import launch_guard; "
                 "print(launch_guard.desktop_running())")
        done = subprocess.run([sys.executable, "-c", probe, str(ROOT)], capture_output=True,
                              text=True, timeout=60, cwd=str(ROOT))
        self.assertEqual(done.stdout.strip().splitlines()[-1], "True", done.stderr[-500:])
        self.guard.release_desktop_lock()
        self.assertFalse(self.guard.desktop_running(), "放せば動いていない")

    def test_デスクトップ版が動いていればブラウザ版は起動しない(self) -> None:
        import start_app
        self.assertTrue(self.guard.hold_desktop_lock(wait_sec=0))
        with self.assertRaises(start_app.StartupError) as ctx:
            start_app.start(open_browser=False)
        self.assertIn("日報複合ツールの窓", str(ctx.exception))
        self.assertFalse(self.guard.browser_running(), "入口(instance.lock)は放してある")

    def test_ブラウザ版が動いていればデスクトップ版は起動しない(self) -> None:
        import start_app
        browser = self.guard.StartupLock()           # ブラウザ版は動いているあいだ入口を握る
        self.assertTrue(browser.acquire())
        self.addCleanup(browser.release)
        self.assertTrue(self.guard.browser_running())
        with self.assertRaises(start_app.StartupError) as ctx:
            start_app.start_bridge(server_factory=lambda token: self.fail("起こしてはいけない"))
        self.assertIn("ブラウザ版", str(ctx.exception))
        self.assertIn("もう一度開く", ctx.exception.hint)
        self.assertFalse(self.guard.desktop_running(), "断ったら錠を放す")


if __name__ == "__main__":                        # pragma: no cover
    unittest.main()
