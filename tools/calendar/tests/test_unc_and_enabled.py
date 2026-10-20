"""共有フォルダ(UNC)の開き方と、アクセス権限「有効」の読み方 (VER4.2.2)

1. UNC(``\\\\サーバ\\共有\\…``)の URI が ``file://サーバ/…`` になり、sqlite がサーバ名を URI の
   ホストとして断っていた。ふだんの開き方は素のパスへ逃げて通っていたが、控え(`backup`)は
   整合の取れないファイルの写しに落ちていた → ``file:////サーバ/…``(入口・日報と同じ形)
2. 「有効」列の「FALSE」「off」「×」「no」を、カレンダーだけ有効と読んでいた(看板・python-web-tools
   は無効)→ 看板と同じ読み方(`access_control.parse_enabled`)
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path, PureWindowsPath
from unittest import mock

from calendar_app import access_control
from calendar_app.dbkit import source_db


class UncUriTests(unittest.TestCase):
    def test_UNCはホストの無いURIにする(self) -> None:
        uri = source_db.to_uri(PureWindowsPath(r"\\nlmfangyshrd\共有\x.sqlite3"))
        self.assertTrue(uri.startswith("file:////nlmfangyshrd/"), uri)
        with self.assertRaises(sqlite3.Error) as caught:
            sqlite3.connect(uri + "?mode=ro", uri=True).execute("SELECT 1")
        self.assertNotIn("authority", str(caught.exception), "URI としては読める(ファイルが無いだけ)")

    def test_ドライブと普通のパスは今までどおり(self) -> None:
        self.assertEqual(source_db.to_uri(PureWindowsPath(r"C:\a\b.sqlite3")), "file:/C:/a/b.sqlite3")
        self.assertEqual(source_db.to_uri(Path("/tmp/a.sqlite3")), "file:/tmp/a.sqlite3")


@unittest.skipUnless(os.name == "posix", "// で始まるパスが同じファイルを指すのは posix だけ")
class UncOpenTests(unittest.TestCase):
    """posix では ``//tmp/x`` が ``/tmp/x`` と同じファイル。UNC の形のパスで本物を開いて確かめる。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db = self.dir / "共有.sqlite3"
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE t (a)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        conn.close()
        self.unc = Path("/" + str(self.db))
        self.assertTrue(str(self.unc).startswith("//"))
        source_db.reset_backup_memory()
        self.addCleanup(source_db.reset_backup_memory)

    def test_URIで開ける(self) -> None:
        with source_db.connect(self.unc) as source:
            self.assertEqual(source.way, source_db.WAY_URI)

    def test_控えは一貫した写しで取る(self) -> None:
        target = self.dir / "控え.sqlite3"
        self.assertTrue(source_db._backup_consistent(self.unc, target))
        conn = sqlite3.connect(target)
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        conn.close()
        # ファイルの丸写しに落ちない
        with mock.patch.object(shutil, "copyfile", side_effect=AssertionError("丸写しに落ちた")):
            self.assertIsNotNone(source_db.backup(self.unc, self.dir / "控え", min_interval_sec=0))


class EnabledWordsTests(unittest.TestCase):
    TABLE = {None: True, "": True, 0: False, 1: True, -1: True, "0": False, "1": True,
             "TRUE": True, "FALSE": False, "False": False, " false ": False, "off": False,
             "OFF": False, "no": False, "N": False, "×": False, "x": False, "X": False,
             "無効": False, "有効": True, "はい": True}

    def test_看板と同じ読み方(self) -> None:
        for value, want in self.TABLE.items():
            with self.subTest(value=value):
                self.assertEqual(access_control.parse_enabled(value), want)
                self.assertEqual(access_control._enabled(value), want)

    def test_共有の表から写すとき_FALSEやoffやバツは無効として写す(self) -> None:
        from calendar_app import db, importer
        from tests.test_access_control import OTHER_TOOL_SCHEMA

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "共有.sqlite3"
        conn = sqlite3.connect(path)
        conn.execute(OTHER_TOOL_SCHEMA)
        conn.executemany('INSERT INTO "アクセス権限" ("ログインID","PC名","権限","有効") VALUES (?,?,?,?)',
                         [("a", "", "L1", "FALSE"), ("b", "", "L2", "off"), ("c", "", "L3", "×"),
                          ("d", "", "L4", "1"), ("e", "", "L5", None)])
        conn.commit()
        conn.close()
        local = db.connect(":memory:")
        self.addCleanup(local.close)
        importer.import_master_table(local, path, "アクセス権限")
        rows = dict(local.execute('SELECT "権限","有効" FROM "アクセス権限"').fetchall())
        self.assertEqual(rows, {"L1": 0, "L2": 0, "L3": 0, "L4": 1, "L5": 1})


if __name__ == "__main__":
    unittest.main()
