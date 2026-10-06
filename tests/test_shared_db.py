"""共有の DB(タブ表示権限の表)を読む・書く(`portal/shared_db.py`)と、手元の写し(`rights_store.py`)

- 置き場所は 大設定 > 環境変数 > 既定(python-web-tools と同じ DB)
- UNC は SQLite が読める URI(`file:////サーバ/…`)にする
- 書くときは短い取引で、**読んだときと同じ行か**を確かめてから(2人が同時に直しても消し合わない)
- アクセス権限など、同じ DB のほかの表には触らない
- 共有に届かない日は、前回読めた手元の写しで決める
"""
from __future__ import annotations

import os
import sqlite3
import unittest
from pathlib import Path

import tests

from portal import catalog as catalog_mod
from portal import rights_store, shared_db, user_settings
from portal.identity import Identity

SHARE = Path(os.environ["ALLTOOLS_SHARED_DB_DIR"])
CATALOG = catalog_mod.load()


def fresh_db(name: str = shared_db.DEFAULT_NAME) -> Path:
    """python-web-tools の DB の代わり(アクセス権限の表だけがある)。"""
    path = SHARE / name
    if path.exists():
        path.unlink()
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE "アクセス権限" ("管理番号" INTEGER PRIMARY KEY, "ログインID" TEXT, "PC名" TEXT, "権限" TEXT)')
    db.execute("INSERT INTO \"アクセス権限\" (\"ログインID\", \"PC名\", \"権限\") VALUES ('', 'LINE1-PC', 'mode:field')")
    db.commit()
    db.close()
    return path


class LocationTests(unittest.TestCase):
    def tearDown(self) -> None:
        user_settings.update({user_settings.KEY_SHARED_DIR: None, user_settings.KEY_SHARED_NAME: None})

    def test_大設定_環境変数_既定の順(self) -> None:
        loc = shared_db.location()
        self.assertEqual((loc.folder, loc.name, loc.origin), (str(SHARE), shared_db.DEFAULT_NAME, "環境変数"))
        user_settings.update({user_settings.KEY_SHARED_DIR: r"\\srv\共有", user_settings.KEY_SHARED_NAME: "別.sqlite3"})
        loc = shared_db.location()
        self.assertEqual((loc.folder, loc.name, loc.origin), (r"\\srv\共有", "別.sqlite3", "大設定"))
        saved = os.environ.pop("ALLTOOLS_SHARED_DB_DIR")
        try:
            user_settings.update({user_settings.KEY_SHARED_DIR: None, user_settings.KEY_SHARED_NAME: None})
            loc = shared_db.location()
            self.assertEqual((loc.folder, loc.origin), (shared_db.DEFAULT_DIR, "既定"))
            self.assertIn("梱包課", loc.folder)
        finally:
            os.environ["ALLTOOLS_SHARED_DB_DIR"] = saved

    def test_UNCはSQLiteが読めるURIにする(self) -> None:
        uri = shared_db.to_uri(Path(r"\\nlmfangyshrd\各課共有\梱包資材マスタ.sqlite3"), read_only=True)
        self.assertTrue(uri.startswith("file:////nlmfangyshrd/"), uri)
        self.assertTrue(uri.endswith("?mode=ro"))
        self.assertNotIn("\\", uri)


class TableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = fresh_db()

    def test_表が無ければNone_作ると空_ほかの表には触らない(self) -> None:
        self.assertIsNone(shared_db.read_rows(self.path))
        self.assertTrue(shared_db.create_table(self.path))
        self.assertFalse(shared_db.create_table(self.path), "あれば何もしない")
        self.assertEqual(shared_db.read_rows(self.path), [])
        db = sqlite3.connect(self.path)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM "アクセス権限"').fetchone()[0], 1)
        cols = [r[1] for r in db.execute('PRAGMA table_info("タブ表示権限")')]
        db.close()
        self.assertEqual(cols, [c for c, _ in shared_db.COLUMNS])

    def test_足す_直す_消す(self) -> None:
        shared_db.create_table(self.path)
        key = shared_db.insert(self.path, {"ログインID": "", "PC名": " LINE1-PC ", "表示タブ": "日報, 看板",
                                           "既定タブ": "日報", "有効": True, "備考": "1ライン"})
        rows = shared_db.read_rows(self.path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][shared_db.ROW_KEY], key)
        self.assertEqual(rows[0]["PC名"], "LINE1-PC", "前後の空白は落とす")
        self.assertEqual(rows[0]["有効"], 1)
        was = {k: rows[0][k] for k in shared_db.EDITABLE}
        shared_db.update(self.path, key, {**was, "表示タブ": "すべて"}, was)
        self.assertEqual(shared_db.read_rows(self.path)[0]["表示タブ"], "すべて")
        # 読んだときと違う行(ほかの人が先に直した)は直さない・消さない
        with self.assertRaises(shared_db.SourceError) as caught:
            shared_db.update(self.path, key, {**was, "備考": "x"}, was)
        self.assertEqual(caught.exception.code, "stale_row")
        with self.assertRaises(shared_db.SourceError):
            shared_db.delete(self.path, key, was)
        now = {k: shared_db.read_rows(self.path)[0][k] for k in shared_db.EDITABLE}
        shared_db.delete(self.path, key, now)
        self.assertEqual(shared_db.read_rows(self.path), [])
        # もう無い行を直そうとした
        with self.assertRaises(shared_db.SourceError) as caught:
            shared_db.update(self.path, key, now, now)
        self.assertEqual(caught.exception.code, "no_row")

    def test_表が無いのに足そうとした(self) -> None:
        with self.assertRaises(shared_db.SourceError) as caught:
            shared_db.insert(self.path, {"PC名": "X"})
        self.assertEqual(caught.exception.code, "no_table")

    def test_ファイルが無い(self) -> None:
        missing = SHARE / "無い.sqlite3"
        self.assertFalse(shared_db.reachable(missing))
        with self.assertRaises(shared_db.SourceError) as caught:
            shared_db.read_rows(missing)
        self.assertEqual(caught.exception.code, "no_source")


class StoreTests(unittest.TestCase):
    """共有から読み直し、届かない日は手元の写しで決める。"""

    def setUp(self) -> None:
        self.path = fresh_db()
        rights_store.cache_path().unlink(missing_ok=True)
        self.store = rights_store.Store()
        self.me = Identity("tester", "TEST-PC")

    def test_読めない_表が無い_読めたで決め方が変わる(self) -> None:
        self.path.unlink()
        result = self.store.sync()
        self.assertFalse(result.reached)
        self.assertEqual(self.store.decide(self.me, CATALOG).source, "unreadable")

        fresh_db()
        result = self.store.sync()
        self.assertTrue(result.reached)
        self.assertEqual(result.state, rights_store.STATE_MISSING)
        self.assertEqual(self.store.decide(self.me, CATALOG).source, "unconfigured")

        shared_db.create_table(self.path)
        shared_db.insert(self.path, {"PC名": "test-pc", "表示タブ": "点検表"})
        result = self.store.sync()
        self.assertTrue(result.changed)
        decision = self.store.decide(self.me, CATALOG)
        self.assertEqual((decision.tabs, decision.source), (["inspection"], "rows"))

    def test_変わっていなければ読まない(self) -> None:
        shared_db.create_table(self.path)
        self.assertTrue(self.store.sync().changed)
        again = self.store.sync()
        self.assertTrue(again.reached)
        self.assertFalse(again.changed)
        self.assertTrue(self.store.sync(force=True).changed, "読み直すを押したときは読む")

    def test_共有に届かない日は前回の写しで決める(self) -> None:
        shared_db.create_table(self.path)
        shared_db.insert(self.path, {"PC名": "TEST-PC", "表示タブ": "看板"})
        self.store.sync()
        self.path.rename(self.path.with_suffix(".bak"))
        try:
            result = self.store.sync()
            self.assertFalse(result.reached)
            self.assertIn("届きません", result.error)
            self.assertEqual(self.store.decide(self.me, CATALOG).tabs, ["kanban"])
        finally:
            self.path.with_suffix(".bak").rename(self.path)

    def test_置き場所を変えたら前の場所の写しでは決めない(self) -> None:
        shared_db.create_table(self.path)
        shared_db.insert(self.path, {"PC名": "TEST-PC", "表示タブ": "看板"})
        self.store.sync()
        user_settings.update({user_settings.KEY_SHARED_NAME: "別.sqlite3"})
        try:
            self.assertEqual(self.store.snapshot().state, rights_store.STATE_NEVER)
            self.assertEqual(self.store.decide(self.me, CATALOG).source, "unreadable")
        finally:
            user_settings.update({user_settings.KEY_SHARED_NAME: None})


if __name__ == "__main__":
    unittest.main()
