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
        # 全部にチェックしても、名前で並べていた行は名前のまま(「すべて」は後で足すツールにも効く)
        self.assertEqual(first.get_json()["rules"][0]["tabs"], "日報, 看板, カレンダー, 点検表")
        second = self.post("/api/rights/save", {**was, "key": rule["key"], "was": was, "tab_ids": ["nippou"]})
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.get_json()["error"]["code"], "stale_row")
        # 断るときは読み直した表も返す(画面は打った値を残して、行の「いま」だけ差し替える)
        self.assertEqual(second.get_json()["rules"][0]["tabs"], "日報, 看板, カレンダー, 点検表")
        now = first.get_json()["rules"][0]
        now_was = {k: now[k] for k in ("login_id", "pc_name", "tabs", "default_tab", "enabled", "note")}
        gone = self.post("/api/rights/delete", {"key": now["key"], "was": now_was})
        self.assertEqual(gone.status_code, 200, gone.get_json())
        self.assertEqual(gone.get_json()["rules"], [])

    def test_知らない語と既定タブは備考だけ直しても残る(self) -> None:
        self.unlock()
        self.post("/api/rights/create-table")
        db = sqlite3.connect(self.db)
        db.execute('INSERT INTO "タブ表示権限" ("ログインID", "表示タブ", "既定タブ", "有効", "備考") '
                   "VALUES ('future', '日報, 在庫', '在庫', 1, 'もと')")
        db.commit()
        db.close()
        rule = self.post("/api/rights/sync").get_json()["rules"][0]
        self.assertEqual(rule["unknown"], ["在庫"])
        was = {k: rule[k] for k in ("login_id", "pc_name", "tabs", "default_tab", "enabled", "note")}
        # 画面は知っているツールのチェック(日報)と、知らない既定タブをそのまま送る
        res = self.post("/api/rights/save", {"key": rule["key"], "was": was, "login_id": "future", "pc_name": "",
                                             "tab_ids": rule["tab_ids"], "default_tab": "在庫",
                                             "enabled": True, "note": "備考だけ直した"})
        self.assertEqual(res.status_code, 200, res.get_json())
        now = res.get_json()["rules"][0]
        self.assertEqual((now["tabs"], now["default_tab"], now["note"]), ("日報, 在庫", "在庫", "備考だけ直した"))

    def test_断られたら写しも読み直す_もう一度で通る(self) -> None:
        """409 の前に写しを読み直していなかったので、読み直しても古い was のまま何度でも 409 だった。"""
        self.unlock()
        self.post("/api/rights/create-table")
        rule = self.post("/api/rights/add", {"pc_name": "P1", "tab_ids": ["nippou"], "note": "もと"}).get_json()["rules"][0]
        was = {k: rule[k] for k in ("login_id", "pc_name", "tabs", "default_tab", "enabled", "note")}
        db = sqlite3.connect(self.db)
        db.execute('UPDATE "タブ表示権限" SET "備考" = \'ほかの端末\'')
        db.commit()
        db.close()
        mine = {"key": rule["key"], "login_id": "", "pc_name": "P1", "tab_ids": ["nippou"], "note": "わたしの"}
        res = self.post("/api/rights/save", {**mine, "was": was})
        self.assertEqual(res.status_code, 409)
        body = res.get_json()
        self.assertEqual(body["error"]["code"], "stale_row")
        fresh = body["rules"][0]
        self.assertEqual(fresh["note"], "ほかの端末", "断るときに読み直した表を返す")
        self.assertEqual(self.get("/api/settings").get_json()["rules"][0]["note"], "ほかの端末")
        fresh_was = {k: fresh[k] for k in ("login_id", "pc_name", "tabs", "default_tab", "enabled", "note")}
        again = self.post("/api/rights/save", {**mine, "was": fresh_was})
        self.assertEqual(again.status_code, 200, again.get_json())
        self.assertEqual(again.get_json()["rules"][0]["note"], "わたしの")

    def test_置き場所の欄には大設定で決めた値だけを渡す(self) -> None:
        """効いている既定(環境変数・既定)を欄に入れると、「変える」で大設定の値として書かれていた。"""
        source = self.get("/api/settings").get_json()["source"]
        self.assertEqual((source["set_folder"], source["set_name"]), ("", ""))
        self.assertEqual(source["folder"], str(SHARE), "効いている場所は別に渡す")
        self.assertEqual(source["default_folder"], str(SHARE), "空ならこれ(環境変数)")
        self.unlock()
        try:
            res = self.post("/api/settings/location", {"folder": "", "name": "別.sqlite3"})
            source = res.get_json()["source"]
            self.assertEqual((source["set_folder"], source["set_name"]), ("", "別.sqlite3"))
            self.assertIsNone(user_settings.get(user_settings.KEY_SHARED_DIR))
        finally:
            self.post("/api/settings/location", {"folder": "", "name": ""})

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

    def test_途中の処理があれば訊く_問いかけを重ねない(self) -> None:
        """ツールの断りが問いかけで終わっていれば「それでも終了しますか?」を足さない。"""
        class Busy:
            def __init__(self, lines):
                self.lines = lines

            def busy(self):
                return self.lines

        self.addCleanup(web.set_browser_tools, None)
        web.set_browser_tools(Busy(["点検表: 印刷の途中です。中断して終了しますか?"]))
        res = self.post("/api/shutdown")
        self.assertEqual(res.status_code, 409)
        message = res.get_json()["message"]
        self.assertEqual(message.count("?"), 1, message)

        web.set_browser_tools(Busy(["看板: 書き戻しの途中です"]))
        message = self.post("/api/shutdown").get_json()["message"]
        self.assertIn("書き戻しの途中です", message)
        self.assertTrue(message.endswith("それでも終了しますか?"), message)


class CloseAskTests(Base):
    """外から(ランチャー・stop.bat)の停止は、開いている画面に打ちかけを置いてもらってから。"""

    class Idle:
        def busy(self):
            return []

        def stop_all(self, force=False):
            pass

    def setUp(self) -> None:
        super().setUp()
        web.close_ask.__init__()
        self.called = []
        web.set_shutdown_hook(lambda: self.called.append(True))
        web.set_browser_tools(self.Idle())
        self.addCleanup(web.set_browser_tools, None)
        self.addCleanup(web.set_shutdown_hook, None)
        self.addCleanup(web.close_ask.__init__)
        self._wait = web.CloseAsk.WAIT_SEC
        web.CloseAsk.WAIT_SEC = 0.3
        self.addCleanup(setattr, web.CloseAsk, "WAIT_SEC", self._wait)

    def stopped(self) -> bool:
        deadline = time.monotonic() + 3
        while not self.called and time.monotonic() < deadline:
            time.sleep(0.05)
        return bool(self.called)

    def test_画面が無ければすぐ止める(self) -> None:
        res = self.post("/api/shutdown")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(self.stopped())

    def test_画面が居れば頼む_置けたら入口が自分で止まる(self) -> None:
        self.post("/api/alive", {"page": "A"}, token=False)
        res = self.post("/api/shutdown")
        self.assertEqual(res.status_code, 409, "返事を待ちきれなければ「頼んでいます」")
        self.assertEqual(res.get_json()["reason"], "asking")
        self.assertFalse(self.called, "画面が置き終える前に止めない")
        seq = self.get("/api/close-ask?page=A").get_json()["seq"]
        self.assertGreater(seq, 0)
        self.assertTrue(self.post("/api/close-answer", {"page": "A", "seq": seq, "state": "working"}).get_json()["ok"])
        self.assertFalse(self.called)
        # 続けて頼まれても、同じ頼み(番号)を使い回す
        self.assertEqual(self.post("/api/shutdown").get_json()["reason"], "asking")
        self.assertEqual(self.get("/api/close-ask?page=A").get_json()["seq"], seq)
        body = self.post("/api/close-answer", {"page": "A", "seq": seq, "state": "ok"}).get_json()
        self.assertTrue(body["stopping"])
        self.assertTrue(self.stopped())
        self.assertEqual(self.get("/api/close-ask?page=A").get_json()["seq"], 0, "もう頼まない")

    def test_待っているあいだに置けたらその場で止める(self) -> None:
        web.CloseAsk.WAIT_SEC = 3.0
        self.post("/api/alive", {"page": "A"}, token=False)

        def answer():
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                seq = web.close_ask.pending("A")
                if seq:
                    web.close_ask.answer("A", seq, "ok")
                    return
                time.sleep(0.02)

        import threading
        threading.Thread(target=answer).start()
        res = self.post("/api/shutdown")
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertTrue(self.stopped())

    def test_閉じないを選んだら止めない(self) -> None:
        self.post("/api/alive", {"page": "A"}, token=False)
        self.post("/api/shutdown")
        seq = self.get("/api/close-ask?page=A").get_json()["seq"]
        body = self.post("/api/close-answer", {"page": "A", "seq": seq, "state": "refused"}).get_json()
        self.assertFalse(body["stopping"])
        res = self.post("/api/shutdown")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["reason"], "asking",
                         "片付けてからもう一度止めにきたら、前の「閉じない」で断らずに訊き直す")
        again = self.get("/api/close-ask?page=A").get_json()["seq"]
        self.assertGreater(again, seq)
        time.sleep(0.6)
        self.assertFalse(self.called)
        self.assertTrue(self.post("/api/close-answer", {"page": "A", "seq": again, "state": "ok"}).get_json()["stopping"])
        self.assertTrue(self.stopped())

    def test_返事を待つあいだに閉じないを選んだら_その場で断る(self) -> None:
        web.CloseAsk.WAIT_SEC = 3.0
        self.post("/api/alive", {"page": "A"}, token=False)

        def answer():
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                seq = web.close_ask.pending("A")
                if seq:
                    web.close_ask.answer("A", seq, "refused")
                    return
                time.sleep(0.02)

        import threading
        threading.Thread(target=answer).start()
        res = self.post("/api/shutdown")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["reason"], "refused")
        self.assertFalse(self.called)

    def test_閉じた画面には頼まない_終了ボタンと強制は頼まない(self) -> None:
        self.post("/api/alive", {"page": "A"}, token=False)
        self.post("/api/alive", {"page": "A", "leaving": True}, token=False)
        self.get("/api/close-ask?page=A")      # 閉じる間際に出た問い合わせが後から着いても
        self.assertEqual(self.post("/api/shutdown").status_code, 200, "閉じた画面を待たない")
        self.assertTrue(self.stopped())
        self.called.clear()
        web.close_ask.__init__()
        self.post("/api/alive", {"page": "B"}, token=False)
        self.assertEqual(self.post("/api/shutdown", {"screens_ready": True}).status_code, 200,
                         "画面の「終了」ボタンは自分で置いてから頼む")
        self.assertTrue(self.stopped())
        self.called.clear()
        self.assertEqual(self.post("/api/shutdown", {"force": True}).status_code, 200)
        self.assertTrue(self.stopped())

    def test_置けたあとでも途中の処理があれば止めない(self) -> None:
        class Busy(self.Idle):
            def busy(self):
                return ["看板: 書き戻しの途中です"]

        self.post("/api/alive", {"page": "A"}, token=False)
        self.post("/api/shutdown")
        seq = self.get("/api/close-ask?page=A").get_json()["seq"]
        web.set_browser_tools(Busy())
        res = self.post("/api/close-answer", {"page": "A", "seq": seq, "state": "ok"})
        self.assertEqual(res.status_code, 409)
        time.sleep(0.6)
        self.assertFalse(self.called)

    def test_起動確認はランチャーが照合に使うものを返す(self) -> None:
        body = self.get("/api/health", token=False).get_json()
        self.assertEqual(body["app_id"], "nlm.all-tools")
        self.assertTrue(body["ready"])
        self.assertEqual(Path(body["app_root"]), Path(web.PORTAL_DIR).parent)
        self.assertEqual(body["display_name"], "統合ツール")


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
