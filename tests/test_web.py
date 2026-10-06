"""入口の画面と API(`portal/web.py`)

- 守り: Host(127.0.0.1 / localhost、デスクトップ版は外枠の宛先も)・別のページからの要求・起動トークン
- `/` は大きなタブの画面。デスクトップ版は各ツールの宛先(独自の宛先)を画面に渡す
- 大設定: 管理者パスワードで鍵を開けてから書く。2人が同時に直したら後の人に読み直しを頼む
- 終了: 訊くだけ(check)・止める
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import unittest
from pathlib import Path

import tests  # noqa: F401  (一時フォルダへ向ける)

from portal import admin_password, app_config, shared_db, user_settings, web
from portal.rights_store import store

SHARE = Path(os.environ["ALLTOOLS_SHARED_DB_DIR"])
TOKEN = "test-token"


def fresh_db() -> Path:
    path = SHARE / shared_db.DEFAULT_NAME
    if path.exists():
        path.unlink()
    sqlite3.connect(path).close()
    return path


class Base(unittest.TestCase):
    bridge = False

    def setUp(self) -> None:
        self.db = fresh_db()
        store.sync(force=True)
        admin_password.session.lock()
        user_settings.update({user_settings.KEY_ADMIN_PASSWORD: None})
        self.app = web.create_app(token=TOKEN, bridge=self.bridge)
        self.client = self.app.test_client()
        self.host = "app.localhost" if self.bridge else "127.0.0.1:8700"

    def get(self, path: str, *, token: bool = True, **kw):
        headers = {"X-Tool-Token": TOKEN} if token else {}
        headers.update(kw.pop("headers", {}))
        return self.client.get(path, headers=headers, base_url=f"http://{kw.pop('host', self.host)}", **kw)

    def post(self, path: str, body=None, *, token: bool = True, **kw):
        headers = {"X-Tool-Token": TOKEN} if token else {}
        headers.update(kw.pop("headers", {}))
        return self.client.post(path, json=body if body is not None else {}, headers=headers,
                                base_url=f"http://{kw.pop('host', self.host)}", **kw)

    def unlock(self) -> None:
        res = self.post("/api/settings/auth", {"password": "nisk"})
        self.assertEqual(res.status_code, 200, res.get_json())


class GuardTests(Base):
    def test_知らない宛先は断る(self) -> None:
        self.assertEqual(self.get("/api/health", host="evil.example").status_code, 400)
        self.assertEqual(self.get("/api/health", host="app.localhost").status_code, 400,
                         "ブラウザ版では外枠の宛先は使わない")
        self.assertEqual(self.get("/api/health", host="localhost:8700").status_code, 200)

    def test_APIは起動トークンが要る_体調と心拍は要らない(self) -> None:
        self.assertEqual(self.get("/api/tabs", token=False).status_code, 401)
        self.assertEqual(self.get("/api/tabs", headers={"X-Tool-Token": "違う"}, token=False).status_code, 401)
        self.assertEqual(self.get("/api/tabs").status_code, 200)
        self.assertEqual(self.get("/api/health", token=False).status_code, 200)
        self.assertEqual(self.post("/api/alive", token=False).status_code, 200)

    def test_別のページからのAPIは断る(self) -> None:
        res = self.get("/api/tabs", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(self.get("/api/tabs", headers={"Sec-Fetch-Site": "same-origin"}).status_code, 200)

    def test_守りの見出し(self) -> None:
        res = self.get("/", token=False)
        self.assertEqual(res.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(res.headers["Content-Security-Policy"], "frame-ancestors 'self'")
        self.assertNotIn("Access-Control-Allow-Origin", res.headers)
        self.assertEqual(res.headers["Cache-Control"], "no-store")


class PageTests(Base):
    def shell(self) -> dict:
        res = self.get("/", token=False)
        self.assertEqual(res.status_code, 200)
        text = res.get_data(as_text=True)
        found = re.search(r"window\.SHELL = (\{.*?\});</script>", text)
        self.assertIsNotNone(found, text[:500])
        return json.loads(found.group(1))

    def test_ブラウザ版の画面(self) -> None:
        shell = self.shell()
        self.assertEqual(shell["edition"], "browser")
        self.assertEqual(shell["token"], TOKEN)
        self.assertEqual([t["id"] for t in shell["tools"]], ["nippou", "kanban", "calendar", "inspection"])
        self.assertTrue(all(t["origin"] == "" for t in shell["tools"]))


class DesktopPageTests(Base):
    bridge = True

    def test_デスクトップ版は各ツールの独自の宛先を渡す(self) -> None:
        res = self.get("/", token=False)
        text = res.get_data(as_text=True)
        shell = json.loads(re.search(r"window\.SHELL = (\{.*?\});</script>", text).group(1))
        self.assertEqual(shell["edition"], "desktop")
        origins = {t["id"]: t["origin"] for t in shell["tools"]}
        for tool_id, origin in origins.items():
            self.assertEqual(origin, app_config.origin_of(tool_id))
        self.assertEqual(self.get("/api/health", host="app.localhost").status_code, 200)

    def test_ブラウザ版だけの操作は断る(self) -> None:
        res = self.post("/api/tools/kanban/open")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error"]["code"], "not_browser")


class TabsTests(Base):
    def test_表が無ければ全部出す_登録が無い端末は大設定だけ(self) -> None:
        body = self.get("/api/tabs").get_json()
        self.assertEqual([t["id"] for t in body["tabs"]], ["nippou", "kanban", "calendar", "inspection"])
        self.assertEqual(body["decision"]["source"], "unconfigured")
        self.assertEqual(body["identity"]["pc_name"], "TEST-PC")
        shared_db.create_table(self.db)
        shared_db.insert(self.db, {"PC名": "OTHER-PC", "表示タブ": "日報"})
        store.sync(force=True)
        body = self.get("/api/tabs").get_json()
        self.assertEqual(body["tabs"], [])
        self.assertEqual(body["decision"]["source"], "unregistered")

    def test_この端末の行で決まる(self) -> None:
        shared_db.create_table(self.db)
        shared_db.insert(self.db, {"PC名": "test-pc", "表示タブ": "カレンダー, 日報", "既定タブ": "カレンダー"})
        store.sync(force=True)
        body = self.get("/api/tabs").get_json()
        self.assertEqual([t["id"] for t in body["tabs"]], ["nippou", "calendar"])
        self.assertEqual(body["default_tab"], "calendar")


class SettingsTests(Base):
    def test_鍵_違うパスワード_開ける_掛ける(self) -> None:
        self.assertFalse(self.get("/api/settings").get_json()["admin"])
        res = self.post("/api/settings/auth", {"password": "違う"})
        self.assertEqual(res.status_code, 403)
        self.unlock()
        self.assertTrue(self.get("/api/settings").get_json()["admin"])
        self.post("/api/settings/auth", {"lock": True})
        self.assertFalse(self.get("/api/settings").get_json()["admin"])

    def test_鍵が掛かっていれば書けない(self) -> None:
        for path, body in (("/api/rights/create-table", {}),
                           ("/api/rights/add", {"pc_name": "X", "tab_ids": ["nippou"]}),
                           ("/api/settings/location", {"folder": "", "name": ""})):
            with self.subTest(path=path):
                res = self.post(path, body)
                self.assertEqual(res.status_code, 403)
                self.assertEqual(res.get_json()["error"]["code"], "locked")

    def test_表を作り_足し_直し_消す(self) -> None:
        self.unlock()
        self.assertEqual(self.post("/api/rights/create-table").status_code, 200)
        res = self.post("/api/rights/add", {"login_id": "", "pc_name": "TEST-PC",
                                            "tab_ids": ["kanban", "nippou"], "default_tab": "kanban",
                                            "enabled": True, "note": "試し"})
        self.assertEqual(res.status_code, 200, res.get_json())
        view = res.get_json()
        self.assertEqual(view["decision"]["tabs"], ["nippou", "kanban"], "書いたら写しも読み直す")
        rule = view["rules"][0]
        self.assertEqual((rule["tabs"], rule["default_tab"]), ("日報, 看板", "看板"))
        # 2人が同時に直した: 後の人(古い was)は断られる
        was = {k: rule[k] for k in ("login_id", "pc_name", "tabs", "default_tab", "enabled", "note")}
        first = self.post("/api/rights/save", {**was, "key": rule["key"], "was": was,
                                               "tab_ids": ["nippou", "kanban", "calendar", "inspection"]})
        self.assertEqual(first.status_code, 200, first.get_json())
        self.assertEqual(first.get_json()["rules"][0]["tabs"], "すべて")
        second = self.post("/api/rights/save", {**was, "key": rule["key"], "was": was, "tab_ids": ["nippou"]})
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.get_json()["error"]["code"], "stale_row")
        now = first.get_json()["rules"][0]
        now_was = {k: now[k] for k in ("login_id", "pc_name", "tabs", "default_tab", "enabled", "note")}
        gone = self.post("/api/rights/delete", {"key": now["key"], "was": now_was})
        self.assertEqual(gone.status_code, 200, gone.get_json())
        self.assertEqual(gone.get_json()["rules"], [])

    def test_IDもPC名も空の行は作れない(self) -> None:
        self.unlock()
        self.post("/api/rights/create-table")
        res = self.post("/api/rights/add", {"login_id": " ", "pc_name": "", "tab_ids": ["nippou"]})
        self.assertEqual(res.status_code, 400)
        self.assertIn("両方空", res.get_json()["error"]["message"])

    def test_置き場所を変える_ファイル名にフォルダは入れない(self) -> None:
        self.unlock()
        res = self.post("/api/settings/location", {"folder": str(SHARE), "name": "a/b.sqlite3"})
        self.assertEqual(res.status_code, 400)
        try:
            res = self.post("/api/settings/location", {"folder": str(SHARE), "name": "別.sqlite3"})
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.get_json()["source"]["origin"], "大設定")
            self.assertTrue(res.get_json()["source"]["path"].endswith("別.sqlite3"))
        finally:
            self.post("/api/settings/location", {"folder": "", "name": ""})
        self.assertEqual(self.get("/api/settings").get_json()["source"]["origin"], "環境変数")

    def test_パスワードを変える(self) -> None:
        res = self.post("/api/settings/password", {"current": "nisk", "new": "abc", "confirm": "abc"})
        self.assertEqual(res.status_code, 422, "4文字以上")
        res = self.post("/api/settings/password", {"current": "nisk", "new": "abcd", "confirm": "abcd"})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertTrue(admin_password.is_custom())
        self.assertEqual(self.post("/api/settings/auth", {"password": "nisk"}).status_code, 403)
        self.assertEqual(self.post("/api/settings/auth", {"password": "abcd"}).status_code, 200)
        stored = user_settings.get(user_settings.KEY_ADMIN_PASSWORD)
        self.assertTrue(stored.startswith("pbkdf2$"), "平文で持たない")
        self.assertNotIn("abcd", stored)


class ShutdownTests(Base):
    def tearDown(self) -> None:
        web.set_shutdown_hook(None)

    def test_訊くだけ_と_止める(self) -> None:
        res = self.post("/api/shutdown", {"check": True})
        self.assertEqual(res.get_json(), {"stopped": False, "can_stop": True})
        self.assertEqual(self.post("/api/shutdown").status_code, 501, "止める鉤が無いプロセス")
        called = []
        web.set_shutdown_hook(lambda: called.append(True))
        res = self.post("/api/shutdown")
        self.assertTrue(res.get_json()["stopped"])
        deadline = time.monotonic() + 5
        while not called and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(called, "応答を返してから止める")


class ClientLogTests(Base):
    def test_画面で起きたことをログに残す(self) -> None:
        self.post("/api/log/client", {"level": "error", "message": "x is not defined\n次の行",
                                      "where": "shell.js:10"})
        self.post("/api/log/client", {"message": "日報 の画面が出ました"})
        logs = sorted(Path(os.environ["ALLTOOLS_LOCAL_DIR"], "logs").glob("portal_*.log"))
        text = logs[-1].read_text(encoding="utf-8")
        self.assertIn("ERROR [portal.web] 画面のエラー: x is not defined 次の行(shell.js:10)", text)
        self.assertIn("画面: 日報 の画面が出ました", text)


if __name__ == "__main__":
    unittest.main()
