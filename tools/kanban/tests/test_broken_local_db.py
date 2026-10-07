"""作業用DB(手元の kanban.sqlite3)が壊れていても、起動ごと止まらない

壊れたファイルは**消さずに**脇へよけ(``<名前>.broken-<日時>``)、空の作業用DBで始める
(看板は共有DBから取り込み直す)。古い版の設定から引き継いだ置き場所でも同じ。
「使用中」は壊れていないので、よけない。
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import start_app
from kanban import config
from kanban.db import store as store_mod


class BrokenLocalDbTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db = self.dir / "kanban.sqlite3"
        self.cfg = config.Config()
        self.cfg.sqlite_path = str(self.db)

    def open(self):
        store = start_app._open_store(self.cfg)
        self.addCleanup(store.close)
        return store

    def broken_files(self) -> list[str]:
        return sorted(p.name for p in self.dir.iterdir() if ".broken-" in p.name)

    def test_SQLiteのファイルでなければよけて空で始める(self) -> None:
        self.db.write_bytes(b"\x00garbage" * 500)
        store = self.open()
        self.assertEqual(len(self.broken_files()), 1)
        self.assertEqual(store.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_中が傷んでいてもよけて空で始める(self) -> None:
        self.open().close()
        raw = bytearray(self.db.read_bytes())
        raw[100:4096] = b"\xff" * (4096 - 100)          # 1ページ目(表の定義)を傷める
        self.db.write_bytes(bytes(raw))
        store = self.open()
        self.assertEqual(len([n for n in self.broken_files() if not n.endswith(store_mod.SIDECARS)]), 1)
        self.assertEqual(store.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_壊れていなければ触らない(self) -> None:
        self.open().close()
        self.open()
        self.assertEqual(self.broken_files(), [])

    def test_使用中は壊れていないのでよけない(self) -> None:
        self.db.write_bytes(b"\x00garbage" * 500)
        with mock.patch.object(start_app, "_store_at", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertRaises(sqlite3.OperationalError):
                start_app._open_store(self.cfg)
        self.assertEqual(self.broken_files(), [])

    def test_開けなかった接続は閉じてから投げる(self) -> None:
        """開いたまま投げると、Windows ではよけられない(名前を変えられない)。"""
        self.db.write_bytes(b"not a database" * 100)
        opened = []
        real = sqlite3.connect

        def spy(*args, **kwargs):
            conn = real(*args, **kwargs)
            opened.append(conn)
            return conn
        with mock.patch.object(store_mod.sqlite3, "connect", spy):
            with self.assertRaises(sqlite3.DatabaseError):
                start_app._store_at(str(self.db), self.cfg)
        self.assertTrue(opened)
        for conn in opened:
            with self.assertRaises(sqlite3.ProgrammingError):   # 閉じてある
                conn.execute("SELECT 1")

    def test_よけられなければ理由と置き場所を言う(self) -> None:
        self.db.write_bytes(b"\x00garbage" * 500)
        with mock.patch("os.replace", side_effect=PermissionError(13, "in use")):
            with self.assertRaises(start_app.StartupError) as caught:
                start_app._open_store(self.cfg)
        self.assertIn(str(self.db), str(caught.exception))
        self.assertIn("閉じてから", caught.exception.hint)


if __name__ == "__main__":
    unittest.main()
