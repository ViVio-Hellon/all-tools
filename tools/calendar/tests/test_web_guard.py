"""守り (app/__init__.py の3つ) と、起動確認・停止

待ち受けは 127.0.0.1 だけなので LAN からは届かないが、
**同じPCで開いている別のWebページ**は要求を投げられる。3つで守る:

  1. 起動トークン      … 起動ごとに作る秘密
  2. Host の確認       … DNS リバインディング対策
  3. 同一オリジン確認  … 別ページからの更新要求を弾く
"""

from __future__ import annotations

import unittest

from . import _web


class TokenTests(unittest.TestCase):
    def setUp(self) -> None:
        _web.reset_sync()
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()

    def test_トークン無しは断る(self) -> None:
        res = self.client.get("/api/calendar")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json()["error"]["code"], "bad_token")

    def test_違うトークンは断る(self) -> None:
        res = self.client.get("/api/calendar", headers={"X-Tool-Token": "でたらめ"})
        self.assertEqual(res.status_code, 403)

    def test_クエリでも通る(self) -> None:
        """`<a href>` のようにヘッダを付けられない経路のため。"""
        res = self.client.get(f"/api/calendar?t={_web.TOKEN}")
        self.assertEqual(res.status_code, 200)

    def test_健康確認は素通し(self) -> None:
        """**まだトークンを知らない相手**が正当に問い合わせる場面がある
        (多重起動の判定・起動待機画面)。業務データは含まない。"""
        self.assertEqual(self.client.get("/api/health").status_code, 200)

    def test_心拍は素通し(self) -> None:
        """トークンを要求すると、切れた画面が黙って死んだ扱いになる。"""
        self.assertEqual(self.client.post("/api/alive").status_code, 200)

    def test_画面のHTMLは素通し(self) -> None:
        """アドレス欄から開く経路。中身は空の器で、業務データは API から取る。"""
        self.assertEqual(self.client.get("/calendar").status_code, 200)

    def test_業務データを返す応答は残さない(self) -> None:
        res = self.client.get("/api/calendar", headers=_web.auth())
        self.assertEqual(res.headers.get("Cache-Control"), "no-store")


class HostTests(unittest.TestCase):
    def setUp(self) -> None:
        _web.reset_sync()
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()

    def test_知らないHostは断る(self) -> None:
        """外部のDNSが 127.0.0.1 を指してここへ届く(DNSリバインディング)。"""
        res = self.client.get("/api/health", headers={"Host": "evil.example.com"})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json()["error"]["code"], "bad_host")

    def test_localhostは通る(self) -> None:
        res = self.client.get("/api/health", headers={"Host": "localhost:8730"})
        self.assertEqual(res.status_code, 200)


class OriginTests(unittest.TestCase):
    def setUp(self) -> None:
        _web.reset_sync()
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()

    def test_別オリジンからの更新は断る(self) -> None:
        res = self.client.post("/api/settings/line", json={"line": "コイル"},
                               headers={**_web.auth(),
                                        "Origin": "http://evil.example.com"})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json()["error"]["code"], "bad_origin")

    def test_同じオリジンなら通る(self) -> None:
        res = self.client.post("/api/settings/line",
                               json={"line": "コイル",
                                     "password": _web.ADMIN_PASSWORD},
                               headers={**_web.auth(),
                                        "Origin": "http://localhost"})
        self.assertEqual(res.status_code, 200)

    def test_読み取りはOriginを見ない(self) -> None:
        """GET は画面の読み込みでも来る。状態を変える要求だけ見る。"""
        res = self.client.get("/api/calendar",
                              headers={**_web.auth(),
                                       "Origin": "http://evil.example.com"})
        self.assertEqual(res.status_code, 200)


class HealthTests(unittest.TestCase):
    """起動確認 (基盤仕様書 2.3)。"""

    def setUp(self) -> None:
        _web.reset_sync()
        self.conn = _web.bind_db(self)

    def test_app_idを返す(self) -> None:
        """**同じポートに居る別のアプリを自分だと誤認しない**ための鍵。"""
        from calendar_app import app_config

        body = _web.json_of(_web.make_client().get("/api/health"))
        self.assertEqual(body["app_id"], app_config.app_id())
        self.assertEqual(body["version"], app_config.version())

    def test_どこの版が動いているか分かる(self) -> None:
        body = _web.json_of(_web.make_client().get("/api/health"))
        for key in ("app_root", "pid", "port", "ready", "stage", "stage_key"):
            self.assertIn(key, body)

    def test_準備中は待機画面(self) -> None:
        """ブラウザだけが先に開き、未完成の画面が出るのを防ぐ。"""
        client = _web.make_client(ready=False)
        text = client.get("/").get_data(as_text=True)
        self.assertIn("起動", text)
        self.assertIn("bootWatch", text)

    def test_準備できたら業務画面へ送る(self) -> None:
        res = _web.make_client(ready=True).get("/")
        self.assertEqual(res.status_code, 302)
        self.assertIn("/calendar", res.headers["Location"])


class ShutdownTests(unittest.TestCase):
    """安全な停止 (基盤仕様書 2.8)。"""

    def setUp(self) -> None:
        _web.reset_sync()
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()

    def test_トークンが要る(self) -> None:
        self.assertEqual(self.client.post("/api/shutdown").status_code, 403)

    def test_止め方が無ければ501(self) -> None:
        """サーバ抜きで組み立てた場合(テストなど)。"""
        res = self.client.post("/api/shutdown", json={}, headers=_web.auth())
        self.assertEqual(res.status_code, 501)

    def test_同期中は止めずに理由を返す(self) -> None:
        """送信の途中で落とすと、届いたかどうか分からないまま終わる。"""
        from app.routes import health
        from calendar_app import sync_service

        health.set_shutdown_hook(lambda: None)
        self.addCleanup(health.set_shutdown_hook, None)

        service = sync_service.get_service()
        service._running = True
        self.addCleanup(setattr, service, "_running", False)

        res = self.client.post("/api/shutdown", json={}, headers=_web.auth())
        self.assertEqual(res.status_code, 409)
        body = res.get_json()
        self.assertEqual(body["reason"], "busy")
        self.assertTrue(body["running"])

    def test_forceなら中断して止める(self) -> None:
        from app.routes import health
        from calendar_app import sync_service

        calls = []
        health.set_shutdown_hook(lambda: calls.append(1))
        self.addCleanup(health.set_shutdown_hook, None)

        service = sync_service.get_service()
        service._running = True
        self.addCleanup(setattr, service, "_running", False)

        res = self.client.post("/api/shutdown", json={"force": True},
                               headers=_web.auth())
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["stopped"])


if __name__ == "__main__":
    unittest.main()
