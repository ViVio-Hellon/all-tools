"""「読めなかった」と「本当に無い」を分ける(入口・大設定 その2)

1.5.79 / 1.5.80 の点検の続き。読めなかったときは**書かない・作らない・前の値で入れ替えない**。

- 共有フォルダ・サーバに届かないと、Windows は `FileNotFoundError` を返す(WinError 53 / 67)。
  それだけで「無い」とみなすと、この端末の設定ファイルを空から書き直していた(置き場所・パスワードが消える)
- いまの配布設定(`日報複合ツール.json`)に届かないと「無い」とみなし、前の名前(`統合ツール.json`)の
  古い配布設定で、配布設定から入れた値を古い値に戻していた
- WAL の共有の DB を読むとき、開き直す一瞬に DB が見えなくなると、空の DB を共有に作っていた
- タブ表示権限の手元の写しを一瞬読めないと「まだ一度も読めていない」とみなし、すべてのタブを出していた
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests  # noqa: F401  (一時フォルダへ向ける)

from portal import app_config, distribution, rights_store, shared_db, user_settings
from tests import test_distribution as _dist

DIST = _dist.DIST
clear_terminal = _dist.clear_terminal


@contextlib.contextmanager
def unreachable(*paths: Path):
    """そのファイルのある**フォルダに届かない**ようにする(Windows で共有に届かないときと同じ:
    ファイルは `FileNotFoundError`、親フォルダの一覧は `OSError`)。"""
    files = {os.path.normcase(os.path.abspath(str(p))) for p in paths}
    dirs = {os.path.dirname(f) for f in files}
    real_read_text, real_stat, real_listdir = Path.read_text, Path.stat, os.listdir

    def norm(p) -> str:
        return os.path.normcase(os.path.abspath(os.fspath(p)))

    def read_text(self, *args, **kwargs):
        if norm(self) in files:
            raise FileNotFoundError(2, "ネットワーク パスが見つかりません", str(self))
        return real_read_text(self, *args, **kwargs)

    def stat(self, *args, **kwargs):
        if norm(self) in files:
            raise FileNotFoundError(2, "ネットワーク パスが見つかりません", str(self))
        return real_stat(self, *args, **kwargs)

    def listdir(path="."):
        if norm(path) in dirs:
            raise OSError(64, "指定されたネットワーク名は利用できなくなりました", str(path))
        return real_listdir(path)

    with mock.patch.object(Path, "read_text", read_text), mock.patch.object(Path, "stat", stat), \
            mock.patch.object(os, "listdir", listdir), \
            mock.patch.object(user_settings, "READ_RETRY_SEC", 0):
        yield


def attempt(work):
    try:
        work()
    except Exception as exc:                          # noqa: BLE001
        return exc
    return None


class ConfirmedMissingTests(unittest.TestCase):
    def test_親を一覧できて無いときだけ無いとする(self) -> None:
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        self.assertTrue(app_config.confirmed_missing(folder / "無い.json"))
        self.assertTrue(app_config.confirmed_missing(folder / "無いフォルダ" / "その下" / "x.json"),
                        "在る親までたどって一覧できれば無いと分かる")
        (folder / "ある.json").write_text("{}", encoding="utf-8")
        self.assertFalse(app_config.confirmed_missing(folder / "ある.json"), "一覧にあるなら読めないだけ")
        with unreachable(folder / "x.json"):
            self.assertFalse(app_config.confirmed_missing(folder / "x.json"), "親に届かないなら無いとは言えない")


class TerminalSettingsUnreachableTests(unittest.TestCase):
    """この端末の設定ファイルのフォルダに届かない(FileNotFoundError)。"""

    def setUp(self) -> None:
        clear_terminal()
        self.addCleanup(clear_terminal)
        user_settings.update({user_settings.KEY_THEME_DEFAULT: None})
        self.addCleanup(user_settings.update, {user_settings.KEY_THEME_DEFAULT: None})

    def test_届かないのを無いとみなして空から書き直さない(self) -> None:
        user_settings.update({user_settings.KEY_SHARED_DIR: r"\\srv\共有", user_settings.KEY_SHARED_NAME: "x.sqlite3"})
        target = user_settings.path()
        with unreachable(target):
            raised = attempt(lambda: user_settings.update({user_settings.KEY_THEME_DEFAULT: "dark"}))
            problem = user_settings.problem()
        data = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(data.get(user_settings.KEY_SHARED_DIR), r"\\srv\共有",
                         "届かないのを「無い」とみなし、空に足した1項目で上書きした")
        self.assertEqual(data.get(user_settings.KEY_SHARED_NAME), "x.sqlite3")
        self.assertNotIn(user_settings.KEY_THEME_DEFAULT, data)
        self.assertIsInstance(raised, user_settings.Unreadable)
        self.assertIn(str(target), problem)

    def test_本当に無いなら今までどおり空から書く(self) -> None:
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        with mock.patch.dict(os.environ, {"ALLTOOLS_SETTINGS_PATH": str(folder / "data" / "user_settings.json")}):
            self.assertEqual(user_settings.load_checked(), {})
            user_settings.update({user_settings.KEY_THEME_DEFAULT: "dark"})
            self.assertEqual(user_settings.load_checked(), {user_settings.KEY_THEME_DEFAULT: "dark"})


class OldNameDistributionTests(unittest.TestCase):
    """いまの配布設定に届かないとき、前の名前の古い配布設定で戻さない。"""

    def setUp(self) -> None:
        clear_terminal()
        shutil.rmtree(DIST, ignore_errors=True)
        self.addCleanup(clear_terminal)
        self.addCleanup(shutil.rmtree, DIST, True)
        DIST.mkdir(parents=True)

    def put(self, name: str, folder: str, created_at: str) -> None:
        (DIST / name).write_text(json.dumps({
            "format": 1, "created_at": created_at, "settings": {user_settings.KEY_SHARED_DIR: folder}},
            ensure_ascii=False), encoding="utf-8")

    def test_いまの配布設定に届かなければ_前の名前の古い値に戻さない(self) -> None:
        self.put(distribution.FILE_NAME, r"\\srv\新しい", "2026/10/28 09:00:00")
        self.assertEqual(distribution.apply_on_start().applied, ["共有の DB のフォルダ"])
        self.put(distribution.OLD_FILE_NAMES[0], r"\\srv\古い", "2025/01/01 09:00:00")
        with mock.patch.object(Path, "is_file", lambda self: False if self.name == distribution.FILE_NAME
                               else os.path.isfile(self)), \
                unreachable(distribution.file_path()):
            started = distribution.apply_on_start()
            again = distribution.reapply()
            problem = distribution.read_problem()
        self.assertEqual(user_settings.load()[user_settings.KEY_SHARED_DIR], r"\\srv\新しい",
                         "届かないのを「無い」とみなし、前の名前の古い配布設定で戻した")
        self.assertEqual(started.applied, [])
        self.assertFalse(again.ok)
        self.assertIn("読めませんでした", again.message)
        self.assertIn(distribution.FILE_NAME, problem)

    def test_いまの配布設定が本当に無ければ前の名前を読む(self) -> None:
        self.put(distribution.OLD_FILE_NAMES[0], r"\\srv\前の名前", "2025/01/01 09:00:00")
        self.assertEqual(distribution.apply_on_start().applied, ["共有の DB のフォルダ"])
        self.assertEqual(user_settings.load()[user_settings.KEY_SHARED_DIR], r"\\srv\前の名前")


class VanishWhileReopeningTests(unittest.TestCase):
    """WAL の共有の DB を読むとき、開き直す一瞬に見えなくなった(瞬断・差し替え)。"""

    def test_開き直すときに空のDBを作らない(self) -> None:
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        path = folder / shared_db.DEFAULT_NAME
        sqlite3.connect(path).close()
        shared_db.create_table(path)
        real_is_wal = shared_db._is_wal

        def vanish(conn):
            real_is_wal(conn)
            # 読み取り専用で開いたあと、開き直す前に見えなくなった。Windows は開いている
            # ファイルを消せないので、先に閉じてから消す(呼ぶ側も閉じてから開き直す)
            conn.close()
            path.unlink()
            return True

        with mock.patch.object(shared_db, "_is_wal", vanish):
            raised = attempt(lambda: shared_db.read_rows(path))
        self.assertFalse(path.exists(), "開き直すときに空の DB を共有に作った")
        self.assertIsInstance(raised, shared_db.SourceError, "空の DB を開いて「表がありません」と読まない")


class RightsCacheUnreadableTests(unittest.TestCase):
    """タブ表示権限の手元の写しを一瞬読めない(置き換えの最中・ロック)。"""

    def setUp(self) -> None:
        clear_terminal()
        self.addCleanup(clear_terminal)
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        db = folder / shared_db.DEFAULT_NAME
        sqlite3.connect(db).close()
        shared_db.create_table(db)
        shared_db.insert(db, {"PC名": "TEST-PC", "表示タブ": "看板"})
        user_settings.update({user_settings.KEY_SHARED_DIR: str(folder)})
        rights_store.cache_path().unlink(missing_ok=True)
        self.store = rights_store.Store()
        self.assertTrue(self.store.sync(force=True).changed)
        self.assertEqual(self.store.snapshot().state, rights_store.STATE_OK)

    def test_写しを一瞬読めなくても_すべてのタブを出さない(self) -> None:
        cache = rights_store.cache_path()
        real = Path.read_text

        def locked(self, *args, **kwargs):
            if self == cache:
                raise PermissionError(13, "ほかのプロセスが使用中です", str(self))
            return real(self, *args, **kwargs)

        with mock.patch.object(Path, "read_text", locked):
            snap = self.store.snapshot()
        self.assertEqual(snap.state, rights_store.STATE_OK,
                         "写しを読めないのを「まだ一度も読めていない」とみなし、すべてのタブを出した")
        self.assertEqual([r["表示タブ"] for r in snap.rows], ["看板"])

    def test_写しが本当に無いなら_まだ読めていない(self) -> None:
        rights_store.cache_path().unlink()
        self.assertEqual(self.store.snapshot().state, rights_store.STATE_NEVER)


if __name__ == "__main__":
    unittest.main()
