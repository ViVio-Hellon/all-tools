"""デスクトップ版の入口(``bridge.py``)── ポートを使わずに画面を返す

外枠(Rust/Tauri)は ``bridge.py`` を子として起動し、標準入出力で要求を渡す。
ここでは外枠の代わりをして、次を確かめる:

- やりとりの形(見出し1行 + 本文)が往復で崩れない(日本語・バイナリ・大きい本文)
- **ソケットを1つも開かない**(開こうとしたら落ちるようにして起動する)
- 待機画面 → 準備完了 → 盤・設定・帳票・操作が返る
- トークン無し・知らない宛先は今までどおり断る
- 「終了」で ``quit`` を知らせ、標準入力を閉じればプロセスが終わる
- ブラウザ版とデスクトップ版が同時に動かない(錠)
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
        body = "看板コメント\n".encode("utf-8") + bytes(range(256))
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

        status, headers, data = bridge.call_wsgi(app, {
            "method": "POST", "path": "/api/echo", "query": "line=%E6%A9%9F%E5%81%B4",
            "headers": [["Content-Type", "application/json"], ["X-Tool-Token", "t"],
                        ["Host", "app.localhost"]]},
            json.dumps({"本文": 1}).encode("utf-8"))
        self.assertEqual(status, 200)
        self.assertIn(["Content-Type", "application/json"], headers)
        self.assertEqual(json.loads(data), {"q": "機側", "body": {"本文": 1},
                                            "host": "app.localhost", "token": "t"})

    def test_ファイルを送る本文もそのまま届く(self) -> None:
        app = flask.Flask("試し")

        @app.post("/api/upload")
        def upload():                                    # noqa: ANN202
            f = flask.request.files["file"]
            return flask.jsonify({"name": f.filename, "size": len(f.read())})

        boundary = "XyZ"
        payload = os.urandom(200_000)
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                f"filename=\"master.accdb\"\r\nContent-Type: application/octet-stream\r\n\r\n"
                ).encode() + payload + f"\r\n--{boundary}--\r\n".encode()
        status, _, data = bridge.call_wsgi(app, {
            "method": "POST", "path": "/api/upload",
            "headers": [["Content-Type", f"multipart/form-data; boundary={boundary}"]]}, body)
        self.assertEqual(status, 200, data)
        self.assertEqual(json.loads(data), {"name": "master.accdb", "size": len(payload)})


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
        (root / "share").mkdir()
        # 共有DB(看板マスタ)。起動時の取り込みも標準入出力のまま通ることを見る
        import sqlite3
        cols = ("[管理番号] NUMERIC, [資材] TEXT, [サイズ] TEXT, [欲] TEXT, [不] TEXT,"
                " [更新日] DATETIME, [発送] TEXT, [倉庫確認日時] DATETIME, [常設品] TEXT,"
                " [保留] TEXT, [注文中日時] DATETIME")
        db = sqlite3.connect(str(root / "share" / "看板マスタ.sqlite3"))
        db.execute(f"CREATE TABLE [看板_L1] ({cols})")
        db.executemany("INSERT INTO [看板_L1] VALUES (?, ?, ?, '〇', '', '', '', '', '〇', '', '')",
                       [(1, "外装紙", "2200:10"), (2, "テープ", "50")])
        db.execute("CREATE TABLE [Form状態管理] ([ライン名] TEXT, [状態] TEXT,"
                   " [更新日時] DATETIME, [ホスト名] TEXT)")
        db.commit()
        db.close()
        conf = root / "config.json"
        conf.write_text(json.dumps({
            "shared_db_path": str(root / "share" / "看板マスタ.sqlite3"),
            "sqlite_path": str(root / "local" / "kanban.sqlite3"),
            "log_dir": str(root / "logs"),
        }), encoding="utf-8")
        env = dict(os.environ,
                   KANBAN_LOCAL_DIR=str(root / "local"),
                   KANBAN_SETTINGS_DIR=str(root / "settings"),
                   KANBAN_LOGIN_ID="desk-test", KANBAN_PC_NAME="DESK-TEST-PC",
                   KANBAN_TOKEN=self.TOKEN,
                   PYTHONPATH=str(root / "nosock"))
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "bridge.py"), "--config", str(conf),
             "--mode", "site", "--line", "L1"],
            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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

    def wait_started(self) -> None:
        deadline = time.monotonic() + 60
        while not self.events and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.events[:1], [{"event": "started"}], self.err[-15:])

    def wait_ready(self) -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            head, body = self.request("GET", "/api/health")
            if head["status"] == 200 and json.loads(body)["ready"]:
                return
            time.sleep(0.1)
        self.fail(f"準備完了にならない。stderr: {self.err[-15:]}")

    def test_ポート無しで起動し画面を返し_終了で知らせて終わる(self) -> None:
        self.wait_started()
        self.wait_ready()

        health = json.loads(self.request("GET", "/api/health")[1])
        self.assertEqual(health["mode"], "site")
        self.assertEqual(self.request("GET", "/")[0]["status"], 302)   # 盤へ(外枠が移り先のページに替える)
        for path, query in (("/board", ""), ("/settings", ""), ("/static/js/app.js", ""),
                            ("/static/js/desktop.js", ""), ("/api/board", "line=L1"),
                            ("/api/report/available", "line=L1"),
                            ("/report/site", "line=L1")):
            with self.subTest(path=path):
                head, body = self.request("GET", path, query=query)
                self.assertEqual(head["status"], 200, body[:300])

        # 同時に来ても取り違えない(画面は自動更新・心拍・操作を同時に出す)
        results: list[int] = []
        threads = [threading.Thread(target=lambda: results.append(
            self.request("GET", "/api/status")[0]["status"])) for _ in range(12)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(results, [200] * 12)

        # 画面の持ち主の名乗り(心拍)も通る。自動終了の見張りは立てない(窓が閉じたら外枠が終わらせる)
        head, body = self.request("POST", "/api/alive", query="screen=s1", body=b"",
                                  headers=[["X-Screen", "s1"]])
        self.assertEqual(head["status"], 200, body)
        self.assertFalse(json.loads(body)["watching"])

        # 守りは今までどおり
        self.assertEqual(self.request("GET", "/api/status", token=False)[0]["status"], 401)
        self.assertEqual(self.request("GET", "/api/status", host="evil.example")[0]["status"], 400)

        head, body = self.request("POST", "/api/shutdown", body=b"{}",
                                  headers=[["Content-Type", "application/json"]])
        self.assertEqual(head["status"], 200, body)
        deadline = time.monotonic() + 10
        while {"event": "quit"} not in self.events and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIn({"event": "quit"}, self.events)
        self.proc.stdin.close()
        self.assertEqual(self.proc.wait(timeout=40), 0, self.err[-20:])
        self.assertFalse([line for line in self.err if "ソケットを開こうとしました" in line],
                         self.err[-20:])
        # 記録に「デスクトップ版」と残る(なぜなぜで、どちらで起きたか分かるように)
        records = list((self.root / "logs").rglob("*.jsonl"))
        text = "".join(p.read_text(encoding="utf-8") for p in records)
        self.assertIn("デスクトップ版", text)

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
            env = dict(os.environ, KANBAN_LOCAL_DIR=str(blocker),  # 作業用フォルダを作れない
                       KANBAN_SETTINGS_DIR=str(Path(tmp) / "settings"))
            done = subprocess.run([sys.executable, str(ROOT / "bridge.py"),
                                   "--config", str(Path(tmp) / "c.json")], env=env,
                                  input=b"", capture_output=True, timeout=60)
        self.assertEqual(done.returncode, 1, done.stderr[-500:])
        event = json.loads(done.stdout.decode("utf-8").splitlines()[0])
        self.assertEqual(event["event"], "fatal")
        self.assertIn("作業用フォルダ", event["message"])
        self.assertTrue(event["hint"])


class DesktopLockTests(unittest.TestCase):
    """ブラウザ版は、デスクトップ版の外枠が握る錠を見て起動しない。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._saved = os.environ.get("KANBAN_LOCAL_DIR")
        os.environ["KANBAN_LOCAL_DIR"] = tmp.name
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._saved is None:
            os.environ.pop("KANBAN_LOCAL_DIR", None)
        else:
            os.environ["KANBAN_LOCAL_DIR"] = self._saved

    def test_錠が無い_握られていない_握られている(self) -> None:
        import launch_guard

        self.assertFalse(launch_guard.desktop_running(), "錠のファイルが無い")
        path = launch_guard.desktop_lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
        self.assertFalse(launch_guard.desktop_running(), "ファイルだけ残っている(外枠は終わった)")

        holder = open(path, "a+b")
        self.addCleanup(holder.close)
        if os.name == "nt":                               # pragma: no cover - Windows
            import msvcrt
            holder.seek(0)
            msvcrt.locking(holder.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertTrue(launch_guard.desktop_running(), "外枠が握っている")

    def test_ブラウザ版が居なければ何もしない(self) -> None:
        import launch_guard

        self.assertEqual(launch_guard.stop_browser_instances(), "")


class VersionTests(unittest.TestCase):
    def test_外枠の版はアプリの版と同じ(self) -> None:
        """exe のプロパティに出る版と、画面の帯に出る版を食い違わせない。"""
        import re
        app = json.loads((ROOT / "config" / "app.json").read_text(encoding="utf-8"))["version"]
        conf = json.loads((ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
        cargo = re.search(r'^version = "([^"]+)"', (ROOT / "src-tauri" / "Cargo.toml")
                          .read_text(encoding="utf-8"), re.M).group(1)
        self.assertEqual((conf["version"], cargo), (app, app))

    def test_画面の宛先の名前と錠の名前は外枠と同じ(self) -> None:
        import launch_guard
        from kanban import app_config
        main_rs = (ROOT / "src-tauri" / "src" / "main.rs").read_text(encoding="utf-8")
        self.assertIn('const SCHEME: &str = "app";', main_rs)
        self.assertIn("app.localhost", app_config.BRIDGE_HOSTS)
        self.assertIn(f'join("{launch_guard.DESKTOP_LOCK_NAME}")', main_rs)
        self.assertIn('"KANBAN_LOCAL_DIR"', main_rs)


if __name__ == "__main__":                        # pragma: no cover
    unittest.main()
