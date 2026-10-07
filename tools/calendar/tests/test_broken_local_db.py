"""作業用DB(手元の calendar.db)が壊れていても、起動ごと止まらない

壊れたファイルは**消さずに**脇へよけ(``<名前>.broken-<日時>``)、空の作業用DBで始める
(中身は取り込み元から取り込み直す)。「使用中」は壊れていないので、よけない。
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import start_app  # noqa: E402
from calendar_app import config, db  # noqa: E402


class BrokenLocalDbTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db = self.dir / "calendar.db"
        patcher = mock.patch.object(config, "sqlite_path", return_value=self.db)
        patcher.start()
        self.addCleanup(patcher.stop)

    def broken_files(self) -> list[str]:
        return sorted(p.name for p in self.dir.iterdir() if ".broken-" in p.name)

    def assert_fresh_db(self) -> None:
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertTrue(conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())
        finally:
            conn.close()

    def test_SQLiteのファイルでなければよけて空で始める(self) -> None:
        self.db.write_bytes(b"\x00garbage" * 500)
        start_app._open_local_db(db)
        self.assertEqual(len(self.broken_files()), 1)
        self.assert_fresh_db()

    def test_中が傷んでいてもよけて空で始める(self) -> None:
        db.connect().close()
        raw = bytearray(self.db.read_bytes())
        raw[100:4096] = b"\xff" * (4096 - 100)          # 1ページ目(表の定義)を傷める
        self.db.write_bytes(bytes(raw))
        start_app._open_local_db(db)
        self.assertEqual(len(self.broken_files()), 1)
        self.assert_fresh_db()

    def test_壊れていなければ触らない(self) -> None:
        db.connect().close()
        start_app._open_local_db(db)
        self.assertEqual(self.broken_files(), [])

    def test_使用中は壊れていないのでよけない(self) -> None:
        self.db.write_bytes(b"\x00garbage" * 500)
        with mock.patch.object(db, "connect", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertRaises(sqlite3.OperationalError):
                start_app._open_local_db(db)
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
        with mock.patch.object(db.sqlite3, "connect", spy):
            with self.assertRaises(sqlite3.DatabaseError):
                db.connect()
        self.assertEqual(len(opened), 1)
        with self.assertRaises(sqlite3.ProgrammingError):   # 閉じてある
            opened[0].execute("SELECT 1")

    def test_よけられなければ理由と置き場所を言う(self) -> None:
        self.db.write_bytes(b"\x00garbage" * 500)
        with mock.patch("os.replace", side_effect=PermissionError(13, "in use")):
            with self.assertRaises(RuntimeError) as caught:
                start_app._open_local_db(db)
        self.assertIn(str(self.db), str(caught.exception))
        self.assertIn("閉じてから", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
