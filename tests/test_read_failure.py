"""「読めなかった」と「本当に無い/空」を分ける(入口・大設定)

ほかのツールで起きたこと:

    共有ファイルを読めなかった(ネットワークが一瞬切れた・ロック中・権限)のを「実績の表が無い」と
    取り違え、手元の実績を送り直していた。共有側に重複が大量にできた

入口・大設定にも同じ形の取り違えがあった。読めなかったときは**書かない・作らない・消さない・
既定に戻さない**(次の回にやり直す)。理由はログと画面に出す。

- この端末の設定ファイル(`user_settings.json`)を読めないと空とみなし、1項目だけ書くと
  共有の DB の置き場所・管理者パスワード・配布設定の覚えが消えた。起動時の配布設定の
  読み込みも「この端末に無い」と見て上書きした。既定のパスワードで鍵が開いた
- 設定を読めないと「置き場所を決めていない」とみなし、**既定の共有の DB** にタブ表示権限を
  書き、既定の DB の中身で手元の写しを置き換えた
- 確かめたあとで共有の DB が見えなくなる(瞬断・差し替え)と、**空の新しい DB を共有に作り**、
  「表がありません」と読んだり、新しい DB に表を作ったりした
- まとめた配布設定(`日報複合ツール.json`)・ツールのフォルダの配布設定を読めないと
  「4ツールの分が無い」とみなし、書き出し直すとまとめた分が消えた
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests  # noqa: F401  (一時フォルダへ向ける)

from portal import admin_password, distribution, rights_store, shared_db, user_settings
from portal.rights_store import store
from tests import test_distribution as _dist
from tests import test_web as _web

DIST = _dist.DIST
SHARE = _web.SHARE
clear_terminal = _dist.clear_terminal

NEW_PW = "line2026"


@contextlib.contextmanager
def unreadable(*paths: Path):
    """そのファイルを、**あるのに読めない**ようにする(ほかが使用中・権限)。"""
    targets = {str(Path(p)) for p in paths}
    original = Path.read_text

    def read_text(self, *args, **kwargs):
        if str(self) in targets:
            raise PermissionError(13, "ほかのプロセスが使用中です", str(self))
        return original(self, *args, **kwargs)

    with mock.patch.object(Path, "read_text", read_text), \
            mock.patch.object(user_settings, "READ_RETRY_SEC", 0, create=True):
        yield


def attempt(work):
    """やってみる。起きた例外(起きなければ None)を返す ── 先に「消していない」を確かめるため。"""
    try:
        work()
    except Exception as exc:                          # noqa: BLE001
        return exc
    return None


def settings_file() -> Path:
    return user_settings.path()


def make_db(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / shared_db.DEFAULT_NAME
    if path.exists():
        path.unlink()
    sqlite3.connect(path).close()
    shared_db.create_table(path)
    return path


def count_rows(path: Path) -> int:
    db = sqlite3.connect(path)
    try:
        return db.execute('SELECT COUNT(*) FROM "タブ表示権限"').fetchone()[0]
    finally:
        db.close()


class TerminalSettingsTests(unittest.TestCase):
    """この端末の設定ファイルを読めないとき。"""

    def setUp(self) -> None:
        clear_terminal()
        self.addCleanup(clear_terminal)
        user_settings.update({user_settings.KEY_THEME_DEFAULT: None})
        self.addCleanup(user_settings.update, {user_settings.KEY_THEME_DEFAULT: None})

    def test_読めないときは書かず_ほかの設定を消さない(self) -> None:
        user_settings.update({user_settings.KEY_SHARED_DIR: r"\\srv\共有", user_settings.KEY_SHARED_NAME: "x.sqlite3"})
        with unreadable(settings_file()):
            raised = attempt(lambda: user_settings.update({user_settings.KEY_THEME_DEFAULT: "dark"}))
        data = json.loads(settings_file().read_text(encoding="utf-8"))
        self.assertEqual(data.get(user_settings.KEY_SHARED_DIR), r"\\srv\共有", "読めなかったのに空とみなして上書きした")
        self.assertEqual(data.get(user_settings.KEY_SHARED_NAME), "x.sqlite3")
        self.assertNotIn(user_settings.KEY_THEME_DEFAULT, data, "読めないあいだは書かない")
        self.assertIsInstance(raised, user_settings.Unreadable)
        with unreadable(settings_file()):
            self.assertIn(str(settings_file()), user_settings.problem())
        self.assertEqual(user_settings.problem(), "")

    def test_無いときは今までどおり空から書く(self) -> None:
        with mock.patch.dict(os.environ, {"ALLTOOLS_SETTINGS_PATH": str(Path(tempfile.mkdtemp()) / "無い.json")}):
            self.assertEqual(user_settings.load(), {})
            user_settings.update({user_settings.KEY_THEME_DEFAULT: "dark"})
            self.assertEqual(user_settings.load(), {user_settings.KEY_THEME_DEFAULT: "dark"})

    def test_起動時に設定を読めなければ_配布設定で上書きしない(self) -> None:
        user_settings.update({user_settings.KEY_SHARED_DIR: r"\\srv\この端末で決めた"})
        self.assertTrue(admin_password.change(admin_password.DEFAULT_PASSWORD, NEW_PW, NEW_PW).ok)
        DIST.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: distribution.file_path().unlink(missing_ok=True))
        distribution.file_path().write_text(json.dumps({
            "format": 1, "created_at": "2026/10/28 09:00:00",
            "settings": {user_settings.KEY_SHARED_DIR: r"\\srv\配布設定の"}}, ensure_ascii=False), encoding="utf-8")
        with unreadable(settings_file()):
            result = distribution.apply_on_start()
            raised = attempt(distribution.reapply)
        data = user_settings.load()
        self.assertEqual(data.get(user_settings.KEY_SHARED_DIR), r"\\srv\この端末で決めた",
                         "読めなかったのを「この端末に無い」とみなして配布設定で上書きした")
        self.assertTrue(admin_password.verify(NEW_PW), "変えたパスワードを消した")
        self.assertNotIn(distribution.KEY_FROM_DIST, data)
        self.assertFalse(result.ok)
        self.assertEqual(result.applied, [])
        self.assertIsInstance(raised, user_settings.Unreadable)
        # 読めるようになれば今までどおり(この端末で決めた値はそのまま)
        self.assertEqual(distribution.apply_on_start().kept, ["共有の DB のフォルダ"])

    def test_書き出すときに読めなければ_既定のままと取り違えない(self) -> None:
        user_settings.update({user_settings.KEY_SHARED_DIR: r"\\srv\共有"})
        with unreadable(settings_file()):
            try:
                outcome = distribution.export()
            except Exception as exc:                  # noqa: BLE001
                outcome = exc
        self.assertNotEqual(getattr(outcome, "reason", None), "nothing",
                            "読めなかったのを「既定のままなので要りません」と出した")
        self.assertFalse(distribution.file_path().exists(), "読めなかったのに書き出した")
        self.assertIsInstance(outcome, user_settings.Unreadable)


class WebTests(_web.Base):
    """大設定の画面から。読めないときは理由を返して断る。"""

    def setUp(self) -> None:
        super().setUp()
        clear_terminal()
        self.addCleanup(clear_terminal)
        self.addCleanup(store.sync, force=True)

    def test_設定を読めなければ既定のパスワードで鍵が開かない(self) -> None:
        self.assertTrue(admin_password.change(admin_password.DEFAULT_PASSWORD, NEW_PW, NEW_PW).ok)
        with unreadable(settings_file()):
            res = self.post("/api/settings/auth", {"password": admin_password.DEFAULT_PASSWORD})
            view = self.get("/api/settings").get_json()
        self.assertFalse(admin_password.session.peek(), "変えた端末なのに既定のパスワードで鍵が開いた")
        self.assertEqual(res.status_code, 503, res.get_json())
        self.assertEqual(res.get_json()["error"]["code"], "settings_unreadable")
        self.assertIn("設定ファイルを読めません", res.get_json()["error"]["message"])
        self.assertIn("設定ファイルを読めません", view.get("settings_error", ""))
        self.assertEqual(self.post("/api/settings/auth", {"password": NEW_PW}).status_code, 200)

    def test_設定を読めなければ置き場所を変えず_ほかの設定も消さない(self) -> None:
        self.assertTrue(admin_password.change(admin_password.DEFAULT_PASSWORD, NEW_PW, NEW_PW).ok)
        self.assertEqual(self.post("/api/settings/auth", {"password": NEW_PW}).status_code, 200)
        with unreadable(settings_file()):
            moved = self.post("/api/settings/location", {"folder": r"\\srv\別", "name": ""})
            themed = self.post("/api/settings/theme", {"theme": "dark"})
        self.assertTrue(admin_password.verify(NEW_PW), "読めなかったのに上書きして、変えたパスワードを消した")
        self.assertIsNone(user_settings.get(user_settings.KEY_SHARED_DIR))
        self.assertIsNone(user_settings.get(user_settings.KEY_THEME_DEFAULT))
        self.assertEqual(moved.status_code, 503, moved.get_json())
        self.assertEqual(themed.status_code, 503)

    def test_設定を読めなければ既定の共有のDBに書かない(self) -> None:
        mine = make_db(Path(tempfile.mkdtemp()) / "大設定で決めた")
        shared_db.create_table(self.db)            # 既定の場所の DB(決めた場所ではない)
        user_settings.update({user_settings.KEY_SHARED_DIR: str(mine.parent)})
        store.sync(force=True)
        self.unlock()
        with unreadable(settings_file()):
            res = self.post("/api/rights/add", {"pc_name": "LINE1-PC", "tab_ids": ["nippou"]})
        self.assertEqual(count_rows(self.db), 0, "読めない設定を「決めていない」とみなし、既定の共有の DB に書いた")
        self.assertEqual(count_rows(mine), 0)
        self.assertEqual(res.status_code, 503, res.get_json())
        self.assertEqual(res.get_json()["error"]["code"], "settings_unreadable")
        self.assertEqual(self.post("/api/rights/add", {"pc_name": "LINE1-PC", "tab_ids": ["nippou"]}).status_code,
                         200)
        self.assertEqual(count_rows(mine), 1, "読めるようになれば決めた場所に書く")


class RightsCacheTests(unittest.TestCase):
    """手元の写し(タブ表示権限)。読めない設定で、既定の DB の中身に置き換えない。"""

    def setUp(self) -> None:
        clear_terminal()
        self.addCleanup(clear_terminal)
        default = SHARE / shared_db.DEFAULT_NAME
        if default.exists():
            default.unlink()
        sqlite3.connect(default).close()             # 既定の場所: 表の無い DB
        self.mine = make_db(Path(tempfile.mkdtemp()) / "大設定で決めた")
        shared_db.insert(self.mine, {"PC名": "TEST-PC", "表示タブ": "看板"})
        user_settings.update({user_settings.KEY_SHARED_DIR: str(self.mine.parent)})
        rights_store.cache_path().unlink(missing_ok=True)
        self.store = rights_store.Store()
        self.assertTrue(self.store.sync(force=True).changed)

    def test_設定を読めなければ読み直さず_写しを置き換えない(self) -> None:
        with unreadable(settings_file()):
            result = self.store.sync(force=True)
            during = self.store.snapshot()
        snap = self.store.snapshot()
        self.assertEqual(snap.source, str(self.mine), "既定の共有の DB の中身で手元の写しを置き換えた")
        self.assertEqual([r["表示タブ"] for r in snap.rows], ["看板"])
        self.assertEqual(during.state, rights_store.STATE_OK, "写しを「別の場所のもの」として捨てない")
        self.assertEqual([r["表示タブ"] for r in during.rows], ["看板"])
        self.assertFalse(result.reached)
        self.assertIn("設定ファイルを読めません", result.error)


class VanishedDbTests(unittest.TestCase):
    """確かめたあとで共有の DB が見えなくなった(瞬断・ほかのツールが差し替えている最中)。"""

    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp())
        self.path = self.folder / shared_db.DEFAULT_NAME
        original = Path.is_file
        target = str(self.path)

        def is_file(path):
            return True if str(path) == target else original(path)

        patcher = mock.patch.object(Path, "is_file", is_file)   # 確かめたときは見えていた
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_読むときに空のDBを作らない(self) -> None:
        raised = attempt(lambda: shared_db.read_rows(self.path))
        self.assertFalse(os.path.exists(self.path), "共有に空の DB を作った")
        self.assertIsInstance(raised, shared_db.SourceError, "「表がありません」と取り違えない")
        self.assertEqual(raised.code, "no_source")

    def test_表を作るときに新しいDBを作らない(self) -> None:
        raised = attempt(lambda: shared_db.create_table(self.path))
        self.assertFalse(os.path.exists(self.path), "共有に新しい DB を作って表を作った")
        self.assertIsInstance(raised, shared_db.SourceError)

    def test_足すときも作らない(self) -> None:
        raised = attempt(lambda: shared_db.insert(self.path, {"PC名": "X", "表示タブ": "日報"}))
        self.assertFalse(os.path.exists(self.path))
        self.assertIsInstance(raised, shared_db.SourceError)


class CombinedUnreadableTests(unittest.TestCase):
    """まとめた配布設定・ツールのフォルダの配布設定を読めないとき(4ツールの分を消さない)。"""

    setUp = _dist.CombinedFileTests.setUp
    put_tool = _dist.CombinedFileTests.put_tool
    export_all = _dist.CombinedFileTests.export_all

    def tools_in_file(self) -> list:
        return sorted(json.loads(distribution.file_path().read_text(encoding="utf-8")).get("tools") or {})

    def test_まとめたファイルを読めなければ書き出し直さない(self) -> None:
        user_settings.update({user_settings.KEY_SHARED_DIR: r"\\srv\共有"})
        self.export_all({"nippou": ({"gw_reference_dir": r"\\srv\参照"}, "2026-10-27 09:00")})
        self.assertEqual(self.tools_in_file(), ["nippou"])
        with unreadable(distribution.file_path()):
            result = distribution.export()
        self.assertFalse(result.ok)
        self.assertIn("読めない", result.message)
        self.assertEqual(self.tools_in_file(), ["nippou"], "読めなかったのに4ツールの分を消して書き出した")

    def test_ツールのフォルダの配布設定を読めなければ_前の分を残す(self) -> None:
        user_settings.update({user_settings.KEY_SHARED_DIR: r"\\srv\共有"})
        self.export_all({"nippou": ({"gw_reference_dir": r"\\srv\参照"}, "2026-10-27 09:00"),
                         "kanban": ({"shared_db_path": r"\\srv\1.sqlite3"}, "2026/10/27 09:00:00")})
        nippou = self.tmp / "tools" / "nippou" / "配布設定" / "設定.json"
        with unreadable(nippou):
            outcome = self.export_all({"kanban": ({"shared_db_path": r"\\srv\2.sqlite3"}, "2026/10/28 09:00:00")})
        data = json.loads(distribution.file_path().read_text(encoding="utf-8"))
        self.assertEqual(sorted(data["tools"]), ["kanban", "nippou"], "読めなかったツールの分を消した")
        self.assertEqual(data["tools"]["nippou"]["settings"], {"gw_reference_dir": r"\\srv\参照"})
        self.assertEqual(data["tools"]["kanban"]["settings"], {"shared_db_path": r"\\srv\2.sqlite3"})
        line = {r["id"]: r for r in outcome["results"]}["nippou"]["line"]
        self.assertIn("読めなかった", line)

    def test_ツールのフォルダに本当に無いなら今までどおり入れない(self) -> None:
        user_settings.update({user_settings.KEY_SHARED_DIR: r"\\srv\共有"})
        self.export_all({"kanban": ({"shared_db_path": r"\\srv\1.sqlite3"}, "2026/10/27 09:00:00")})
        self.assertEqual(self.tools_in_file(), ["kanban"])


if __name__ == "__main__":
    unittest.main()
