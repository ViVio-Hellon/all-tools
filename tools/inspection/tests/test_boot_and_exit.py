"""起動をいちばん先に見せる / 終わるときは確実に終わる(python-web-tools と同じ守り)

1. 待機画面は Flask を読み込む前に出す(`boot_server`)。外部への要求を出さない
2. 待ち受けを開き直さずに本体へ引き継ぐ(`Handover`)。トークンは変わらない
3. 止めたら終わる(つないだままの接続があっても)
4. タブを閉じたら終わる。**裏に回ったタブ・スリープ明けでは終わらない**
"""
from __future__ import annotations

import json
import socket
import unittest
from unittest import mock

import tests.helpers  # noqa: F401  (ローカル領域を一時フォルダへ)
from core import app_config, boot_screen, idle_exit

try:
    import flask  # noqa: F401
    import waitress  # noqa: F401
    HAS_WEB = True
except ImportError:                              # pragma: no cover
    HAS_WEB = False
_SKIP = "Flask / waitress が入っていないためスキップ"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _KeepShutdownHook:
    """`build()` は止め方を `app.routes.health` のモジュール変数へ差し込む。戻す。"""

    def keep_shutdown_hook(self) -> None:
        from app.routes import health
        original = health._shutdown_hook
        self.addCleanup(health.set_shutdown_hook, original)


class BootScreenTests(unittest.TestCase):
    def page(self) -> str:
        return boot_screen.render(display_name="点検表 選択・印刷", version_label="VER9.9.9",
                                  token="tok", app_id=app_config.app_id(), poll_ms=300,
                                  home_url="/?t=tok", log_dir="C:\\logs")

    def test_外部への要求を1つも出さない(self) -> None:
        html = self.page()
        self.assertNotRegex(html, r"(src|href)\s*=\s*[\"']https?://")
        self.assertNotIn("@import", html)
        self.assertNotRegex(html, r"\{\{[A-Z_]+\}\}", "差し込み忘れがあります")

    def test_名前と版と段が出る(self) -> None:
        html = self.page()
        self.assertIn("点検表 選択・印刷", html)
        self.assertIn("VER9.9.9", html)
        for key in ("env", "prepare", "scan", "done"):
            self.assertIn(key, html)

    def test_色はtokensから来る(self) -> None:
        self.assertIn("--bar-a", self.page())


@unittest.skipUnless(HAS_WEB, _SKIP)
class BootAppTests(unittest.TestCase):
    def setUp(self) -> None:
        from boot_server import BootApp
        self.app = BootApp(8713, "tok")

    def call(self, path: str):
        captured = {}

        def start(status, headers):
            captured["status"] = status
            captured["headers"] = dict(headers)

        body = b"".join(self.app({"PATH_INFO": path, "REQUEST_METHOD": "GET"}, start))
        return captured["status"], captured["headers"], body.decode("utf-8")

    def test_待機画面を返して残さない(self) -> None:
        status, headers, body = self.call("/")
        self.assertEqual(status, "200 OK")
        self.assertIn("起動しています", body)
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_healthは本体と同じ形で多重起動の判定に使える(self) -> None:
        import launch_guard
        health = json.loads(self.call("/api/health")[2])
        for key in ("app_id", "version", "port", "pid", "ready", "stage", "stage_key", "startup_error"):
            self.assertIn(key, health)
        self.assertFalse(health["ready"])
        self.assertTrue(launch_guard.is_our_app(health))

    def test_業務の要求にはまだと答える(self) -> None:
        status, _, body = self.call("/api/inventory")
        self.assertTrue(status.startswith("503"))
        self.assertEqual(json.loads(body)["error"]["code"], "starting")

    def test_段と失敗は外から差し替えられる(self) -> None:
        self.app.mark_stage("点検表フォルダを確認中", "scan")
        self.app.mark_error("試験用のエラー")
        health = json.loads(self.call("/api/health")[2])
        self.assertEqual((health["stage"], health["stage_key"]), ("点検表フォルダを確認中", "scan"))
        self.assertEqual(health["startup_error"], "試験用のエラー")


@unittest.skipUnless(HAS_WEB, _SKIP)
class BuildAndStopTests(_KeepShutdownHook, unittest.TestCase):
    def setUp(self) -> None:
        self.keep_shutdown_hook()
        idle_exit.reset()
        self.addCleanup(idle_exit.reset)

    def test_組み立ての前後でトークンと段を引き継ぐ(self) -> None:
        import server as server_module
        srv = server_module.AppServer(_free_port(), token="t", options={"demo": True})
        self.assertIsNone(srv.app)
        srv.mark_stage("点検表フォルダを確認中", "scan")
        srv.build()
        self.assertEqual(srv.app.config["TOKEN"], "t")
        self.assertEqual(srv.app.config["STAGE"], "点検表フォルダを確認中")

    def test_接続を張ったままでも止まる(self) -> None:
        """waitress の輪は keep-alive の接続が残ると回り続ける(プロセスが残る)。"""
        import server as server_module
        port = _free_port()
        srv = server_module.AppServer(port, token="t", options={"demo": True})
        srv.build()
        thread = server_module.run_in_background(srv)
        self.assertTrue(server_module.wait_until_listening(port, timeout=10))
        held = []
        for _ in range(3):
            sock = socket.create_connection(("127.0.0.1", port), timeout=3)
            sock.sendall(b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: keep-alive\r\n\r\n")
            sock.recv(4096)
            held.append(sock)
        self.addCleanup(lambda: [s.close() for s in held])
        srv.stop()
        srv.stop()                               # 2回止めても安全
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertTrue(srv.stop_requested)


class HardExitTests(unittest.TestCase):
    def test_上限を過ぎたら落とす(self) -> None:
        import start_app
        srv = mock.Mock(stop_requested=True)
        thread = mock.Mock()
        thread.is_alive.return_value = True
        with mock.patch.object(start_app, "_hard_exit") as hard, \
             mock.patch.object(start_app, "EXIT_WAIT_SEC", 0.01):
            start_app._hold_until_stopped(srv, thread)
        hard.assert_called_once()

    def test_終わったなら落とさない(self) -> None:
        import start_app
        srv = mock.Mock(stop_requested=True)
        thread = mock.Mock()
        thread.is_alive.side_effect = [True, False]
        with mock.patch.object(start_app, "_hard_exit") as hard, \
             mock.patch.object(start_app, "EXIT_WAIT_SEC", 0.01):
            start_app._hold_until_stopped(srv, thread)
        hard.assert_not_called()


@unittest.skipUnless(HAS_WEB, _SKIP)
class RequestIsPresenceTests(unittest.TestCase):
    """要求が来ている = 誰かが見ている。ただし見張りの問い合わせは数えない。"""

    def setUp(self) -> None:
        from app import create_app
        idle_exit.reset()
        self.addCleanup(idle_exit.reset)
        self.watch = idle_exit.install(lambda: None, lambda: False)
        app = create_app(token="t", port=8715, options={"demo": True})
        app.config["TESTING"] = True
        self.client = app.test_client()

    def closed_long_ago(self):
        self.watch.grace_sec = 0.0
        return self.watch.overdue()

    def test_画面からのAPIは在席の合図になる(self) -> None:
        self.watch.beat("A", visible=True)
        self.watch.leaving("A")
        self.client.get("/api/inventory/status", headers={"X-Tool-Token": "t", "X-Screen-Id": "A"})
        self.assertIsNone(self.closed_long_ago())

    def test_番号の無いhealthは在席にしない(self) -> None:
        """2回目の起動の判定や stop.bat --status が、閉じたアプリを生かし続けない。"""
        self.watch.beat("A", visible=True)
        self.watch.leaving("A")
        self.client.get("/api/health")
        self.assertIsNotNone(self.closed_long_ago())

    def test_心拍の口は閉じた合図を取り消さない(self) -> None:
        self.watch.beat("A", visible=True)
        self.client.post("/api/alive", json={"leaving": True, "screen_id": "A"})
        self.assertIsNotNone(self.closed_long_ago())

    def test_裏に回った合図は心拍の口で受ける(self) -> None:
        self.client.post("/api/alive", json={"visible": False, "screen_id": "B"})
        self.watch.idle_sec = 0.0
        self.assertIsNone(self.watch.overdue())


class IdleWatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stopped = []
        self.busy = False
        idle_exit.reset()
        self.addCleanup(idle_exit.reset)

    def watch(self, **kw) -> idle_exit.IdleWatch:
        return idle_exit.IdleWatch(lambda: self.stopped.append(1), lambda: self.busy, **kw)

    def test_1度も繋がっていなければ落とさない(self) -> None:
        """`--no-browser` で立てておく使い方を巻き添えにしない。"""
        self.assertIsNone(self.watch(idle_sec=0.0).overdue())

    def test_裏に回った画面は心拍が途切れても落とさない(self) -> None:
        w = self.watch(idle_sec=0.0)
        w.beat("A", visible=True)
        self.assertIsNotNone(w.overdue(), "前提: 表なら心拍の途切れで落ちる")
        w.hidden("A")
        self.assertIsNone(w.overdue())

    def test_裏のままスリープ明けに気づいても裏のまま(self) -> None:
        w = self.watch(idle_sec=0.0)
        w.hidden("A")
        w.resumed("A", gap_sec=3600, visible=False)
        self.assertIsNone(w.overdue())

    def test_閉じた合図のあとに裏の合図が届いても閉じたまま(self) -> None:
        w = self.watch(idle_sec=60.0, grace_sec=0.0)
        w.beat("A", visible=True)
        w.leaving("A")
        w.hidden("A")
        self.assertIn("閉じ", w.overdue())

    def test_2つのうち1つを閉じても落とさない(self) -> None:
        w = self.watch(idle_sec=60.0, grace_sec=0.0)
        w.beat("A", visible=True)
        w.beat("B", visible=True)
        w.leaving("A")
        self.assertIsNone(w.overdue())
        w.leaving("B")
        self.assertIn("閉じ", w.overdue())

    def test_再読込なら猶予のうちに戻って取り消される(self) -> None:
        w = self.watch(idle_sec=60.0, grace_sec=60.0)
        w.beat("A", visible=True)
        w.leaving("A")
        self.assertIsNone(w.overdue())
        w.beat("A", visible=True)
        w.grace_sec = 0.0
        self.assertIsNone(w.overdue())

    def test_スリープから戻ったら待ち直す(self) -> None:
        w = self.watch(idle_sec=60.0, sleep_gap_sec=30.0)
        w.beat("A", visible=True)
        w._screens["A"].seen -= 3600
        self.assertIsNotNone(w.overdue(), "前提: 1時間心拍が無い")
        self.assertTrue(w.check_sleep(3600))
        self.assertIsNone(w.overdue(), "スリープ明けは画面が戻るのを待つ")

    def test_印刷中は落とさない(self) -> None:
        w = self.watch(idle_sec=0.0, grace_sec=0.0, tick_sec=0.01)
        w.beat("A", visible=True)
        w.leaving("A")
        self.busy = True
        w.start()
        import time
        time.sleep(0.1)
        self.assertEqual(self.stopped, [], "印刷中なのに止めた")
        self.busy = False
        time.sleep(0.2)
        w.cancel()
        self.assertEqual(self.stopped, [1])


if __name__ == "__main__":
    unittest.main()
