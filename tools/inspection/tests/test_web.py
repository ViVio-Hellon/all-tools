"""画面とAPI(Flask の test_client で、サーバを立てずに確かめる)

- 守り: Host 検証・起動トークン・同一オリジン・CORS を返さない・控えの方針
- 形: 断りは `{"error": {"code", "message"}}`、状態は HTTP ステータスで分ける
- 業務: 一覧 → 選択 → プレビュー → 印刷 → 状態、印刷中の終了は断る
"""
from __future__ import annotations

import os
import time
import unittest

from tests.helpers import make_tree, temp_dir

try:
    import flask  # noqa: F401
    HAS_WEB = True
except ImportError:                              # pragma: no cover
    HAS_WEB = False

TOKEN = "test-token"
AUTH = {"X-Tool-Token": TOKEN}


@unittest.skipUnless(HAS_WEB, "Flask が入っていないためスキップ")
class WebTestBase(unittest.TestCase):
    def setUp(self) -> None:
        from app import create_app
        from app.routes import health
        from core import idle_exit

        idle_exit.reset()
        self.addCleanup(idle_exit.reset)
        original = health._shutdown_hook
        self.addCleanup(health.set_shutdown_hook, original)
        health.set_shutdown_hook(None)

        self.app = create_app(token=TOKEN, port=8799, options={"demo": True})
        self.app.config["TESTING"] = True
        self.biz = self.app.extensions["inspection"]
        self.biz.excel.backend.print_delay = 0.05
        self.biz.excel.backend.preview_delay = 0
        self.biz.inspection.start_scan("test")
        self.assertTrue(self.biz.inspection.wait(10))
        self.app.config["READY"] = True
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.biz.printing.wait_idle(10)

    def get(self, path, **kw):
        return self.client.get(path, headers={**AUTH, **kw.pop("headers", {})}, **kw)

    def post(self, path, json=None, **kw):
        return self.client.post(path, json=json or {}, headers={**AUTH, **kw.pop("headers", {})}, **kw)

    def item_ids(self, n: int):
        inv = self.get("/api/inventory").get_json()["inventory"]
        ids = [it["id"] for c in inv["categories"] for s in c["subcategories"] for it in s["items"]]
        return ids[:n]


class SecurityTests(WebTestBase):
    def test_トークンが無ければ断る(self) -> None:
        res = self.client.get("/api/inventory")
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.get_json()["error"]["code"], "bad_token")

    def test_トークンはクエリでも受ける(self) -> None:
        """`<img src>` はヘッダを付けられない(プレビュー画像)。"""
        self.assertEqual(self.client.get(f"/api/inventory?t={TOKEN}").status_code, 200)

    def test_非ASCIIのトークンでも500にしない(self) -> None:
        self.assertEqual(self.client.get("/api/inventory", headers={"X-Tool-Token": "点検"}).status_code, 401)

    def test_healthと心拍はトークン無しで答える(self) -> None:
        self.assertEqual(self.client.get("/api/health").status_code, 200)
        self.assertEqual(self.client.post("/api/alive", json={}).status_code, 200)

    def test_別のHostは断る(self) -> None:
        """DNSリバインディング対策。"""
        res = self.client.get("/api/health", headers={"Host": "evil.example:8799"})
        self.assertEqual(res.status_code, 400)

    def test_別オリジンからの要求は断る(self) -> None:
        res = self.get("/api/inventory", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(res.status_code, 403)

    def test_CORSヘッダを返さない(self) -> None:
        res = self.get("/api/inventory", headers={"Origin": "http://evil.example"})
        self.assertNotIn("Access-Control-Allow-Origin", res.headers)

    def test_APIは控えさせない_静的ファイルは版付きなら控える(self) -> None:
        self.assertEqual(self.get("/api/inventory").headers["Cache-Control"], "no-store")
        res = self.client.get("/static/css/tokens.css?v=1.0.0")
        self.assertIn("immutable", res.headers["Cache-Control"])
        res.close()


class PageTests(WebTestBase):
    def test_準備中は待機画面(self) -> None:
        self.app.config["READY"] = False
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn("起動しています", body)

    def test_準備ができたら点検表の画面(self) -> None:
        res = self.client.get("/")
        body = res.get_data(as_text=True)
        self.assertEqual(res.status_code, 200)
        for marker in ('id="cat-tabs"', 'id="sel-list"', 'id="btn-print"', 'id="preview-dialog"',
                       'id="settings-dialog"', 'id="offline"', 'id="quit"', "模擬モード"):
            self.assertIn(marker, body)
        self.assertIn(TOKEN, body)
        self.assertIn('data-theme="light"', body, "既定はライト(日報複合ツールの4ツールでそろえる)")
        self.assertIn("css/inspection.css?v=", body, "静的ファイルに版が付いていない")

    def test_明暗を選んでいなければ大設定の既定を描く前に当てる(self) -> None:
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn('localStorage.getItem("inspection.theme_default")', body)
        self.assertIn('themeChosen: false', body)

    def test_版がどこでも読める(self) -> None:
        """ダイアログだけを写した画面写しでも、どの版かが分かること。"""
        from core import app_config
        body = self.client.get("/").get_data(as_text=True)
        label = app_config.version_label()
        self.assertIn(f"<title>{self.app.config['DISPLAY_NAME']} {label}</title>", body)
        self.assertIn(f'<span class="ver" title="このアプリの版">{label}</span>', body)
        # 帯・プレビュー・印刷・フォルダ設定・終了した画面
        self.assertGreaterEqual(body.count(label), 6)
        self.assertEqual(self.client.get("/api/health").get_json()["version"], app_config.version())

    def test_healthの形(self) -> None:
        health = self.client.get("/api/health").get_json()
        for key in ("app_id", "version", "app_root", "pid", "ready", "stage", "stage_key",
                    "startup_error", "uptime_sec", "job"):
            self.assertIn(key, health)
        self.assertTrue(health["ready"])


class InventoryTests(WebTestBase):
    def test_一覧はVBA版と同じ規則で並ぶ(self) -> None:
        data = self.get("/api/inventory?wait=1").get_json()
        inv = data["inventory"]
        self.assertTrue(inv["root_exists"])
        self.assertGreater(inv["listed_files"], 0)
        self.assertEqual(data["settings"]["root_folder_source"], "demo")
        names = [c["name"] for c in inv["categories"]]
        self.assertEqual(names, sorted(names, key=str.casefold))
        for c in inv["categories"]:
            for s in c["subcategories"]:
                for it in s["items"]:
                    self.assertNotIn("path", it, "サーバのフルパスを画面へ出さない")

    def test_再読込(self) -> None:
        res = self.post("/api/inventory/refresh")
        self.assertEqual(res.status_code, 200)
        self.biz.inspection.wait(10)
        self.assertEqual(self.get("/api/inventory/status").get_json()["scan"]["state"], "done")


class PreviewTests(WebTestBase):
    def test_プレビュー画像を作って返す(self) -> None:
        item = self.item_ids(1)[0]
        data = self.post("/api/preview", {"id": item}).get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["range"], "A1:P40")
        res = self.client.get(f"{data['image_url']}?t={TOKEN}")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.mimetype.startswith("image/"))
        self.assertIn("max-age", res.headers["Cache-Control"])
        again = self.post("/api/preview", {"id": item}).get_json()
        self.assertTrue(again["cached"], "2回目は控えから出す")

    def test_断り(self) -> None:
        self.assertEqual(self.post("/api/preview", {}).status_code, 400)
        res = self.post("/api/preview", {"id": "no-such-item"})
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["error"]["code"], "ITEM_NOT_FOUND")
        self.assertEqual(self.client.get(f"/api/preview/image/{'0' * 40}?t={TOKEN}").status_code, 404)


class MultiLineWebTests(WebTestBase):
    def test_置かれた版をhealthで返し_画面に知らせの枠がある(self):
        from core import app_config
        health = self.client.get("/api/health").get_json()
        self.assertEqual(health["version_on_disk"], app_config.version())
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="updated"', body)
        self.assertIn(f'version: "{app_config.version()}"', body)

    def test_印刷はどの画面から始めたかを持つ(self):
        ids = self.item_ids(2)
        res = self.post("/api/print", {"ids": ids, "copies": 1}, headers={"X-Screen-Id": "line1"})
        self.assertEqual(res.get_json()["job"]["screen_id"], "line1")
        self.assertTrue(self.biz.printing.wait_idle(10))


class SpeedTests(WebTestBase):
    def test_Excelの先回り起動は待たせない(self):
        res = self.post("/api/excel/warm")
        self.assertEqual(res.status_code, 202)
        self.assertFalse(res.get_json()["started"], "模擬の Excel では何もしない")

    def test_一覧があれば確認の終わりを待たない(self):
        """前回の控え・再読込の途中でも、いまの一覧をすぐ返す。"""
        import threading
        gate = threading.Event()
        original = self.biz.inspection._scan_worker

        def slow_scan():
            gate.wait(5)
            original()
        self.biz.inspection._scan_worker = slow_scan
        self.addCleanup(gate.set)
        self.assertTrue(self.biz.inspection.start_scan("試験"))
        started = time.time()
        data = self.get("/api/inventory?wait=2").get_json()
        self.assertLess(time.time() - started, 1.0)
        self.assertEqual(data["scan"]["state"], "scanning")
        self.assertIsNotNone(data["inventory"])
        gate.set()
        self.assertTrue(self.biz.inspection.wait(10))


class PrintTests(WebTestBase):
    def test_選んだ順に印刷して結果を返す(self) -> None:
        ids = list(reversed(self.item_ids(3)))
        res = self.post("/api/print", {"ids": ids, "copies": "2"})
        self.assertEqual(res.status_code, 202)
        self.assertTrue(res.get_json()["job"]["active"])
        self.assertTrue(self.biz.printing.wait_idle(10))
        job = self.get("/api/print/status").get_json()["job"]
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["copies"], 2)
        self.assertEqual([i["id"] for i in job["items"]], ids)

    def test_入力の誤りと業務の断りを分ける(self) -> None:
        ids = self.item_ids(1)
        cases = [({"ids": "x"}, 400, "BAD_REQUEST"),
                 ({"ids": ids, "copies": "abc"}, 400, "INVALID_COPIES"),
                 ({"ids": [], "copies": 1}, 422, "NO_SELECTION"),
                 ({"ids": ["unknown"], "copies": 1}, 409, "ITEM_NOT_FOUND")]
        for body, status, code in cases:
            with self.subTest(code=code):
                res = self.post("/api/print", body)
                self.assertEqual(res.status_code, status)
                err = res.get_json()["error"]
                self.assertEqual(err["code"], code)
                self.assertTrue(err["message"])

    def test_印刷中は二重に受けず_終了も断る(self) -> None:
        self.biz.excel.backend.print_delay = 0.3
        ids = self.item_ids(3)
        self.assertEqual(self.post("/api/print", {"ids": ids, "copies": 1}).status_code, 202)
        res = self.post("/api/print", {"ids": ids[:1], "copies": 1})
        self.assertEqual((res.status_code, res.get_json()["error"]["code"]), (409, "PRINT_RUNNING"))
        res = self.post("/api/shutdown", {})
        self.assertEqual(res.status_code, 409)
        body = res.get_json()
        self.assertEqual((body["reason"], body["running"]), ("busy", ["印刷"]))
        self.assertIn("job", self.client.get("/api/health").get_json())
        self.assertEqual(self.client.get("/api/health").get_json()["job"]["label"], "印刷")
        cancel = self.post("/api/print/cancel").get_json()["job"]
        self.assertTrue(cancel["cancel_requested"])
        self.assertTrue(self.biz.printing.wait_idle(10))
        self.assertEqual(self.get("/api/print/status").get_json()["job"]["state"], "cancelled")

    def test_止め方が無ければ501(self) -> None:
        self.assertEqual(self.post("/api/shutdown", {}).status_code, 501)

    def test_止め方があれば止める(self) -> None:
        from app.routes import health
        called = []
        health.set_shutdown_hook(lambda: called.append(1))
        res = self.post("/api/shutdown", {})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["stopped"])
        deadline = time.time() + 3
        while not called and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(called, [1])


class SettingsTests(WebTestBase):
    def test_テーマを保存する(self) -> None:
        self.assertFalse(self.get("/api/settings").get_json()["settings"]["dark_mode"], "既定はライト")
        self.post("/api/settings", {"dark_mode": True})
        self.assertTrue(self.get("/api/settings").get_json()["settings"]["dark_mode"])
        self.assertIn('data-theme="dark"', self.client.get("/").get_data(as_text=True))
        self.post("/api/settings", {"dark_mode": False})
        self.assertFalse(self.get("/api/settings").get_json()["settings"]["dark_mode"])
        self.assertIn('data-theme="light"', self.client.get("/").get_data(as_text=True))

    def test_模擬モードではフォルダを変えられない(self) -> None:
        res = self.post("/api/settings", {"root_folder": "/tmp"})
        self.assertEqual((res.status_code, res.get_json()["error"]["code"]), (422, "DEMO_MODE"))

    def test_フォルダ参照はExcelの名前とフォルダだけ返す(self) -> None:
        root = self.biz.settings.effective_root_folder()
        data = self.get(f"/api/fs/list?path={root}").get_json()
        self.assertTrue(data["readable"])
        self.assertGreater(len(data["dirs"]), 0)
        sub = data["dirs"][0]["path"]
        inner = self.get(f"/api/fs/list?path={sub}").get_json()
        self.assertTrue(inner["exists"])

    def test_見えないフォルダは理由を返す(self) -> None:
        data = self.get("/api/fs/list?path=/no/such/folder").get_json()
        self.assertFalse(data["exists"])
        self.assertTrue(data["message"])

    def test_診断情報(self) -> None:
        data = self.get("/api/diagnostics").get_json()
        self.assertEqual(data["excel"]["backend"], "dummy")
        self.assertTrue(data["app"]["demo"])


@unittest.skipUnless(HAS_WEB, "Flask が入っていないためスキップ")
class FolderSettingTests(unittest.TestCase):
    """模擬モードでない場合のフォルダ設定(VBA版 ShowFolderSettings)。"""

    def setUp(self) -> None:
        from app import create_app
        from app.business import InspectionBusiness
        from core import idle_exit
        idle_exit.reset()
        self.addCleanup(idle_exit.reset)
        self._tmp = temp_dir()
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.join(self._tmp.name, "点検表")
        make_tree(self.root, ["梱包/日常点検/梱包_日常点検_台車点検.xlsx",
                              "梱包/日常点検/命名規則外.xlsx"])
        biz = InspectionBusiness({})
        self.app = create_app(token=TOKEN, port=8798, business=biz)
        self.app.config["READY"] = True
        self.client = self.app.test_client()
        self.biz = biz
        self.addCleanup(lambda: biz.settings.update(reset_root_folder=True))

    def test_存在するフォルダだけ保存して読み直す(self) -> None:
        res = self.client.post("/api/settings", json={"root_folder": "/no/such/folder"}, headers=AUTH)
        self.assertEqual((res.status_code, res.get_json()["error"]["code"]), (422, "FOLDER_NOT_FOUND"))
        res = self.client.post("/api/settings", json={"root_folder": ""}, headers=AUTH)
        self.assertEqual(res.status_code, 400)
        res = self.client.post("/api/settings", json={"root_folder": f'"{self.root}"'}, headers=AUTH)
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertTrue(body["rescan"])
        self.assertEqual(body["settings"]["root_folder_source"], "user")
        self.biz.inspection.wait(10)
        inv = self.client.get("/api/inventory?wait=2", headers=AUTH).get_json()["inventory"]
        self.assertEqual(inv["listed_files"], 1)
        self.assertEqual(inv["unmatched_files"], 1)
        res = self.client.post("/api/settings", json={"reset_root_folder": True}, headers=AUTH)
        self.assertEqual(res.get_json()["settings"]["root_folder_source"], "config")


if __name__ == "__main__":
    unittest.main()
