"""設定画面の API

tkinter 版で上部のボタンに散らばっていた
[ライン設定] [同期設定] [取り込み] [今すぐ同期] の移植先。

取り込み元が sqlite3 になったので、**取り込みの試験も本物のファイルで行う**。
Access のころは ODBC / サンプル .accdb 頼みで、環境しだいで飛ばしていた。
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from . import _web
from calendar_app import config, settings as user_settings


class SettingsViewTests(unittest.TestCase):
    def setUp(self) -> None:
        _web.reset_sync()
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()

    def test_設定一式が返る(self) -> None:
        body = _web.json_of(self.client.get("/api/settings", headers=_web.auth()))
        self.assertEqual(body["line_options"], list(config.ALL_LINE_NAMES))
        self.assertIn("version", body)
        self.assertIn("log_dir", body)

    def test_未設定は直すべきこととして出る(self) -> None:
        user_settings.save_my_line("")
        user_settings.set_value(user_settings.KEY_DATA_DB_DIR, "")
        user_settings.set_value(user_settings.KEY_MASTER_DB_DIR, "")
        body = _web.json_of(self.client.get("/api/settings", headers=_web.auth()))
        text = " ".join(body["problems"])
        self.assertIn("ライン", text)
        # **登録そのものができない状態**なので、はっきり言う
        self.assertIn("保存用DB", text)
        self.assertIn("マスタDB", text)

    def test_参照パスが返る(self) -> None:
        folder = _web.with_source(self, master=True)
        body = _web.json_of(self.client.get("/api/settings", headers=_web.auth()))
        self.assertEqual(body["data_db_dir"], str(folder))
        self.assertTrue(body["data_db_path"].endswith(config.SOURCE_FILE_DATA))
        self.assertTrue(body["master_db_path"].endswith(config.SOURCE_FILE_MASTER))


class LineTests(unittest.TestCase):
    def setUp(self) -> None:
        _web.reset_sync()
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()

    def test_保存できる(self) -> None:
        res = self.client.post("/api/settings/line",
                               json={"line": "コイル",
                                     "password": _web.ADMIN_PASSWORD},
                               headers=_web.auth())
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["my_line"], "コイル")
        self.assertEqual(user_settings.get_my_line(), "コイル")

    def test_一覧に無いラインは断る(self) -> None:
        res = self.client.post("/api/settings/line", json={"line": "存在しない"},
                               headers=_web.auth())
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"], "not_listed")


class PathTests(unittest.TestCase):
    """参照パス ── 取り込み元の**フォルダ**を決める。"""

    def setUp(self) -> None:
        _web.reset_sync()
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(user_settings.set_value, user_settings.KEY_DATA_DB_DIR, "")
        self.addCleanup(user_settings.set_value, user_settings.KEY_MASTER_DB_DIR, "")
        self.addCleanup(_web.reset_sync)

    def post(self, **body):
        # 参照パスの変更には管理者パスワードが要る。**関門そのものは
        # ``AdminPasswordTests`` で見る**ので、ここでは通した先を確かめる
        body.setdefault("password", _web.ADMIN_PASSWORD)
        return self.client.post("/api/settings/paths", json=body,
                                headers=_web.auth())

    def test_フォルダを保存できる(self) -> None:
        folder = Path(self.tmp.name)
        _web.make_source(folder, config.SOURCE_FILE_DATA, _web.SOURCE_SCHEMA)
        res = self.post(data_db_dir=str(folder))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(user_settings.data_db_dir_setting(), str(folder))

    def test_存在しないフォルダは断る(self) -> None:
        """設定できたつもりのまま送信待ちが溜まるのを防ぐ。"""
        res = self.post(data_db_dir=str(Path(self.tmp.name) / "無い"))
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"], "not_found")

    def test_ファイルを渡しても断る(self) -> None:
        """フォルダを持つ設計なので、ファイルは受けない。"""
        target = _web.make_source(Path(self.tmp.name), config.SOURCE_FILE_DATA,
                                  _web.SOURCE_SCHEMA)
        res = self.post(data_db_dir=str(target))
        self.assertEqual(res.status_code, 422)

    def test_空なら未設定に戻す(self) -> None:
        user_settings.set_value(user_settings.KEY_DATA_DB_DIR, "/somewhere")
        self.assertEqual(self.post(data_db_dir="").status_code, 200)
        self.assertEqual(user_settings.data_db_dir_setting(), "")

    def test_何も指定しなければ400(self) -> None:
        self.assertEqual(self.post().status_code, 400)

    def test_片方だけ送っても他方を消さない(self) -> None:
        """片方を直したいだけのときに、もう片方まで上書きしない。"""
        folder = Path(self.tmp.name)
        _web.make_source(folder, config.SOURCE_FILE_DATA, _web.SOURCE_SCHEMA)
        self.post(data_db_dir=str(folder), master_db_dir=str(folder))
        self.post(data_db_dir=str(folder))
        self.assertEqual(user_settings.master_db_dir_setting(), str(folder))

    def test_ファイル名が違っても拾う(self) -> None:
        """上流がファイル名を変えても動く ── フォルダで持つ理由。"""
        folder = Path(self.tmp.name)
        _web.make_source(folder, "連絡帳2026.sqlite3", _web.SOURCE_SCHEMA)
        self.post(data_db_dir=str(folder))
        body = _web.json_of(self.client.get("/api/settings", headers=_web.auth()))
        self.assertTrue(body["data_db_path"].endswith("連絡帳2026.sqlite3"))


class AutoSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        _web.reset_sync()
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()

    def post(self, **body):
        return self.client.post("/api/settings/auto-sync", json=body,
                                headers=_web.auth())

    def test_間隔を変えられる(self) -> None:
        self.assertEqual(self.post(interval=45).status_code, 200)
        self.assertEqual(user_settings.sync_interval(), 45)

    def test_短すぎる間隔は断る(self) -> None:
        """共有フォルダを叩き続けることになるので、下限を置く。"""
        res = self.post(interval=1)
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"], "out_of_range")

    def test_数字でなければ400(self) -> None:
        self.assertEqual(self.post(interval="はやく").status_code, 400)

    def test_入切できる(self) -> None:
        self.post(enabled=False)
        self.assertFalse(user_settings.auto_sync_enabled())
        self.post(enabled=True)
        self.assertTrue(user_settings.auto_sync_enabled())


class SyncStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        _web.reset_sync()
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()

    def test_状態が返る(self) -> None:
        body = _web.json_of(self.client.get("/api/sync", headers=_web.auth()))
        for key in ("state", "text", "pending", "busy", "configured", "offline"):
            self.assertIn(key, body)

    def test_未設定なら今すぐ同期を断る(self) -> None:
        user_settings.set_value(user_settings.KEY_DATA_DB_DIR, "")
        user_settings.set_value(user_settings.KEY_ACCESS_DATA_PATH, "")
        _web.reset_sync()
        res = self.client.post("/api/sync/now", headers=_web.auth())
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"], "not_configured")


class BrowseTests(unittest.TestCase):
    """サーバ側のフォルダ参照 (tkinter 版 filedialog の置き換え)。"""

    def setUp(self) -> None:
        _web.reset_sync()
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "共有").mkdir()
        _web.make_source(self.root, "連絡帳.sqlite3", _web.SOURCE_SCHEMA)
        (self.root / "メモ.txt").write_text("x", encoding="utf-8")

    def browse(self, path):
        return _web.json_of(self.client.post("/api/settings/browse",
                                             json={"path": str(path)},
                                             headers=_web.auth()))

    def test_フォルダと取り込み元だけ返る(self) -> None:
        body = self.browse(self.root)
        self.assertEqual([d["name"] for d in body["dirs"]], ["共有"])
        self.assertEqual([f["name"] for f in body["files"]], ["連絡帳.sqlite3"])

    def test_中身は返さない(self) -> None:
        """ここを「サーバの中を読む窓口」にしない。"""
        body = self.browse(self.root)
        for entry in body["dirs"] + body["files"]:
            self.assertEqual(set(entry), {"name", "path"})

    def test_無いフォルダは理由を返す(self) -> None:
        body = self.browse(self.root / "無い")
        self.assertFalse(body["exists"])
        self.assertIn("見つかりません", body["message"])

    def test_ファイルを渡したら親を開く(self) -> None:
        """利用者はたいていフルパスを貼り付ける。"""
        body = self.browse(self.root / "連絡帳.sqlite3")
        self.assertEqual(body["path"], str(self.root))


class ImportTests(unittest.TestCase):
    """取り込みは**参照パスから**。ファイルのパスは受け取らない。"""

    def setUp(self) -> None:
        _web.reset_sync()
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_参照パスが無ければ断る(self) -> None:
        res = self.client.post("/api/import", json={}, headers=_web.auth())
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"], "no_source")

    def test_対象の指定が変なら400(self) -> None:
        res = self.client.post("/api/import", json={"target": "なにか"},
                               headers=_web.auth())
        self.assertEqual(res.status_code, 400)

    def test_CSVから班員名簿を取り込める(self) -> None:
        """マスタDB が見られない端末のための逃げ道。"""
        csv = Path(self.tmp.name) / "班員名簿.csv"
        csv.write_text("管理番号,苗字,班,名前,読み,担当ライン\n"
                       "10,山田,B,山田太郎,ヤマダ,L-1\n", encoding="utf-8")
        res = self.client.post("/api/import/csv", json={"path": str(csv)},
                               headers=_web.auth())
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["member_count"], 1)

    def test_CSV以外は断る(self) -> None:
        other = Path(self.tmp.name) / "資料.xlsx"
        other.write_bytes(b"x")
        res = self.client.post("/api/import/csv", json={"path": str(other)},
                               headers=_web.auth())
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"], "bad_suffix")

    def test_無いCSVは断る(self) -> None:
        res = self.client.post("/api/import/csv",
                               json={"path": str(Path(self.tmp.name) / "無い.csv")},
                               headers=_web.auth())
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"], "not_found")

    def prepared_source(self) -> Path:
        """1件入った取り込み元を置き、参照パスに設定する。"""
        folder = _web.with_source(self, master=True)
        path = folder / config.SOURCE_FILE_DATA
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                f'INSERT INTO "{config.TABLE_DATA}" '
                '("日付","区分","登録内容","識別コード","班","ライン") '
                'VALUES (?,?,?,?,?,?)',
                ("2026/08/03", config.KUBUN_REST, "取り込まれる人", "01", "B", "L-1"))
            conn.commit()
        finally:
            conn.close()
        return folder

    def test_参照パスから取り込める(self) -> None:
        self.prepared_source()
        res = self.client.post("/api/import", json={"target": "data"},
                               headers=_web.auth())
        self.assertEqual(res.status_code, 200)

        # **入ったところまで見る。** 200 だけでは「空を取り込んだ」も通る
        rows = self.conn.execute(
            f'SELECT "登録内容" FROM "{config.TABLE_DATA}"').fetchall()
        self.assertEqual([r[0] for r in rows], ["取り込まれる人"])

    def test_未送信があれば確認を求める(self) -> None:
        """取り込みはテーブルを入れ替えるので、勝手に消さない。"""
        self.prepared_source()
        self.client.post("/api/import", json={"target": "data"}, headers=_web.auth())
        # 取り込み後に1件入力する = 未送信の状態を作る
        self.conn.execute(
            f'INSERT INTO "{config.TABLE_DATA}" '
            '("日付","区分","登録内容","識別コード") VALUES (?,?,?,?)',
            ("2026/08/03", config.KUBUN_REST, "未送信の人", "99"))
        self.conn.commit()

        res = self.client.post("/api/import", json={"target": "data"},
                               headers=_web.auth())
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["error"]["code"], "pending_changes")

        forced = self.client.post("/api/import",
                                  json={"target": "data", "force": True},
                                  headers=_web.auth())
        self.assertEqual(forced.status_code, 200)

    def test_マスタDBだけ取り込める(self) -> None:
        """班員名簿はマスタDBにある ── 保存用DBを触らずに入ること。"""
        folder = _web.with_source(self, master=True)
        conn = sqlite3.connect(folder / config.SOURCE_FILE_MASTER)
        try:
            conn.execute(
                f'INSERT INTO "{config.TABLE_MEMBER}" '
                '("管理番号","苗字","班","名前","読み","担当ライン") '
                'VALUES (?,?,?,?,?,?)',
                ("10", "山田", "B", "山田太郎", "ヤマダ", "L-1"))
            conn.commit()
        finally:
            conn.close()

        res = self.client.post("/api/import", json={"target": "master"},
                               headers=_web.auth())
        self.assertEqual(res.status_code, 200)
        rows = self.conn.execute(
            f'SELECT "名前" FROM "{config.TABLE_MEMBER}"').fetchall()
        self.assertEqual([r[0] for r in rows], ["山田太郎"])


if __name__ == "__main__":
    unittest.main()


class SkippedNoticeTests(unittest.TestCase):
    """送らなかった登録の知らせ(``/api/sync`` と ``/api/sync/skipped/ack``)。"""

    def setUp(self) -> None:
        import tempfile

        from calendar_app import config, db
        from calendar_app.sync import notices

        _web.reset_sync()
        self.addCleanup(_web.reset_sync)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "calendar.db"
        conn = db.connect(str(path))
        _web.bind_db(self, conn)
        original = config.sqlite_path
        config.sqlite_path = lambda: path
        self.addCleanup(setattr, config, "sqlite_path", original)
        notices.remember(conn, [{"日付": "2026/09/15", "登録内容": "佐藤次郎",
                                 "識別コード": "12", "直": "1"}])
        self.client = _web.make_client()

    def test_帯の問い合わせに載る(self) -> None:
        body = _web.json_of(self.client.get("/api/sync", headers=_web.auth()))
        self.assertEqual(body["skipped"]["count"], 1)
        self.assertIn("9月15日 佐藤次郎さんの休み(1直)", body["skipped"]["text"])

    def test_確かめたら消える(self) -> None:
        res = self.client.post("/api/sync/skipped/ack", headers=_web.auth())
        self.assertEqual(res.status_code, 200)
        self.assertEqual(_web.json_of(res)["skipped"]["count"], 0)


class StorageTests(unittest.TestCase):
    """設定画面の「ファイルの置き場所」。**この端末だけ / 全端末で共有**を分けて出す。"""

    def setUp(self) -> None:
        _web.reset_sync()
        self.addCleanup(_web.reset_sync)
        _web.bind_db(self)
        self.client = _web.make_client()

    def test_この端末だけのものと共有のものを分けて出す(self) -> None:
        body = _web.json_of(self.client.get("/api/settings", headers=_web.auth()))
        by_name = {item["name"]: item for item in body["storage"]}
        self.assertIn("この端末だけ", by_name["settings.json"]["group"])
        self.assertIn("この端末だけ", by_name["calendar.db"]["group"])
        self.assertIn("全端末で共有", by_name[config.SOURCE_FILE_DATA]["group"])
        self.assertIn("全端末で共有", by_name[config.SOURCE_FILE_MASTER]["group"])
        self.assertIn("アプリのフォルダ", by_name["配布設定"]["group"])
        # 実際の場所を出す
        self.assertEqual(by_name["settings.json"]["path"], str(config.settings_path()))

    def test_送信待ちを持つファイルは消さないと言う(self) -> None:
        body = _web.json_of(self.client.get("/api/settings", headers=_web.auth()))
        db_item = next(i for i in body["storage"] if i["name"] == "calendar.db")
        self.assertTrue(db_item["keep"])
        self.assertIn("送信待ち", db_item["note"])
