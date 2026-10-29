"""置き場所に「届かない・読めない」のを「無い」と取り違えて、代わりを使わない(第2弾)

別ツールで、共有ファイルを読めなかったのを「表が無い」と取り違え、手元の実績を送り直して
重複が大量にできた事案を受けた点検の続き。ここで見るのは、**探す・確かめる**ところ:

* 参照パスのフォルダに届かないと「何も無いフォルダ」と見て、旧設定のフルパス(古い共有・
  手元の写し)へ移り、そこへ送っていた。マスタのフォルダに届かないと、保存用のフォルダに
  残っている古いマスタで名簿・アクセス権限を入れ替えていた
* この端末の設定ファイルを読めないまま配布設定を読み込み、ほかの設定ごと配布設定の値で
  書き直していた。読めない配布設定の代わりに、もう片方(大設定がまとめたファイル)の古い中身を
  使っていた
* 手元のDBが見えないとき、帯の表示が空のDBを作っていた(``sqlite3.connect`` は無ければ作る)
* 変換ツールが、出力先を確かめられないのを「無い」と見て書きにかかった・元を読めないのに
  出力先を消した

「無い」とするのは、親フォルダに届いて一覧を読め、その中に無いと確かめたときだけ。
届かない状態は ``os.scandir`` / ``os.listdir`` / ``os.stat`` に例外を差し込んで作る。
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from . import _web
from calendar_app import (
    config, distribution, master_admin, settings as user_settings, sources,
    sync_service,
)
from calendar_app.dbkit import source_db
from calendar_app.sync import notices


def _same(a, b) -> bool:
    try:
        return os.path.abspath(os.fspath(a)) == os.path.abspath(os.fspath(b))
    except TypeError:
        return False


@contextmanager
def unreachable(*folders: Path, error=PermissionError):
    """そのフォルダの一覧を読もうとすると断られる(共有フォルダに届かない・権限・瞬断)。"""
    real_scandir, real_listdir = os.scandir, os.listdir

    def blocked(path) -> bool:
        return any(_same(path, folder) for folder in folders)

    def fake_scandir(path="."):
        if blocked(path):
            raise error(13 if error is PermissionError else 2, "届きません", str(path))
        return real_scandir(path)

    def fake_listdir(path="."):
        if blocked(path):
            raise error(13 if error is PermissionError else 2, "届きません", str(path))
        return real_listdir(path)

    with mock.patch("os.scandir", fake_scandir), mock.patch("os.listdir", fake_listdir):
        yield


class _Settings(unittest.TestCase):
    """この試験だけの設定ファイル(settings.json)。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.settings_file = self.root / "home" / "settings.json"
        self.settings_file.parent.mkdir()
        patcher = mock.patch.object(config, "settings_path", lambda: self.settings_file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def stored(self) -> dict:
        return json.loads(self.settings_file.read_text(encoding="utf-8"))

    @contextmanager
    def settings_unreadable(self):
        """設定ファイルを開くと断られる(ほかの処理が掴んでいる・権限)。"""
        real = Path.open
        target = self.settings_file

        def refuse(path, *args, **kwargs):
            mode = args[0] if args else kwargs.get("mode", "r")
            if Path(path) == target and "w" not in mode:
                raise PermissionError(13, "Permission denied", str(path))
            return real(path, *args, **kwargs)

        with mock.patch.object(Path, "open", refuse), \
                mock.patch.object(user_settings, "READ_WAIT_SEC", 0):
            yield


class _Share(_Settings):
    """共有(保存用DBとマスタDB)と、旧設定のフルパスが指す古い写し。"""

    def setUp(self) -> None:
        super().setUp()
        _web.reset_sync()
        self.addCleanup(_web.reset_sync)
        self.share = self.root / "共有"
        self.share.mkdir()
        self.data_db = _web.make_source(self.share, config.SOURCE_FILE_DATA,
                                        _web.SOURCE_SCHEMA)
        self.master_db = _web.make_source(self.share, config.SOURCE_FILE_MASTER,
                                          _web.MASTER_SCHEMA)
        # 旧設定のフルパス(フォルダ方式の前)。いまは使っていない古い写し
        self.old = self.root / "古い写し"
        self.old.mkdir()
        self.legacy = _web.make_source(self.old, "連絡帳_前の.sqlite3", _web.SOURCE_SCHEMA)
        user_settings.update({
            user_settings.KEY_DATA_DB_DIR: str(self.share),
            user_settings.KEY_MASTER_DB_DIR: str(self.share),
            user_settings.KEY_ACCESS_DATA_PATH: str(self.legacy),
        })


# ---------------------------------------------------------------------------
# 1. 探す: 届かないフォルダを「無い」と見て、次の候補へ移らない
# ---------------------------------------------------------------------------
class FindTests(_Share):
    def test_届かない参照パスで旧設定のフルパスへ移らない(self) -> None:
        with unreachable(self.share):
            with self.assertRaises(source_db.SourceError) as caught:
                sources.find_data_db()
        self.assertIn("読めませんでした", str(caught.exception))

    def test_共有ごと見えないときも移らない(self) -> None:
        """フォルダが「無い」と言われても、親フォルダも読めなければ確かめられていない。"""
        with unreachable(self.share, error=FileNotFoundError), unreachable(self.root):
            with self.assertRaises(source_db.SourceError):
                sources.find_data_db()

    def test_本当に無いと確かめたら旧設定のフルパスを使う(self) -> None:
        self.data_db.unlink()
        self.master_db.unlink()
        self.assertEqual(sources.find_data_db(), self.legacy)

    def test_参照パスのフォルダごと無いと確かめたら次へ(self) -> None:
        user_settings.set_value(user_settings.KEY_DATA_DB_DIR, str(self.share / "無いフォルダ"))
        self.assertEqual(sources.find_data_db(), self.legacy)

    def test_旧設定のフルパスが見えないのを無いと言わない(self) -> None:
        self.data_db.unlink()
        self.master_db.unlink()
        self.legacy.unlink()
        with unreachable(self.old):
            with self.assertRaises(source_db.SourceError):
                sources.find_data_db()
        self.assertIsNone(sources.find_data_db())   # 届いて無いなら「無い」

    def test_マスタのフォルダに届かないとき保存用のフォルダの古いマスタを使わない(self) -> None:
        master_dir = self.root / "マスタ"
        master_dir.mkdir()
        new_master = _web.make_source(master_dir, config.SOURCE_FILE_MASTER, _web.MASTER_SCHEMA)
        user_settings.set_value(user_settings.KEY_MASTER_DB_DIR, str(master_dir))
        self.assertEqual(sources.find_master_db(), new_master)
        with unreachable(master_dir):
            with self.assertRaises(source_db.SourceError):
                sources.find_master_db()

    def test_マスタ管理は届かないのを見つかりませんと言わない(self) -> None:
        master_dir = self.root / "マスタ"
        master_dir.mkdir()
        _web.make_source(master_dir, config.SOURCE_FILE_MASTER, _web.MASTER_SCHEMA)
        user_settings.set_value(user_settings.KEY_MASTER_DB_DIR, str(master_dir))
        with unreachable(master_dir):
            ready = master_admin.readiness()
        self.assertFalse(ready.ok)
        self.assertIsNone(ready.path)
        self.assertIn("読めませんでした", ready.why)
        self.assertNotIn("見つかりません", ready.why)

    def test_設定ファイルを読めないのを参照パス未設定と取り違えない(self) -> None:
        with self.settings_unreadable():
            with self.assertRaises(source_db.SourceError):
                sources.find_data_db()
            with self.assertRaises(source_db.SourceError):
                sources.find_master_db()

    def test_起動の記録に読めませんと出す(self) -> None:
        with unreachable(self.share):
            text = sources.describe_sources()
        self.assertIn("読めません", text)
        self.assertNotIn(str(self.legacy), text)


# ---------------------------------------------------------------------------
# 2. 同期: 届かない・設定を読めない間は、送り先を替えない
# ---------------------------------------------------------------------------
class SyncServiceTests(_Share):
    def make_service(self) -> sync_service.SyncService:
        service = sync_service.SyncService()
        self.addCleanup(service.stop)
        return service

    def test_届かない間は送り先を替えない(self) -> None:
        service = self.make_service()
        self.assertEqual(service.path, str(self.data_db))
        with unreachable(self.share):
            service.reload()
            self.assertEqual(service.path, str(self.data_db))
            self.assertEqual(service._auto.master_path, str(self.master_db))

    def test_起動のときに届かなければ_見えた時点で探し直す(self) -> None:
        with unreachable(self.share):
            service = self.make_service()
            self.assertEqual(service.path, "")
            self.assertTrue(service.configured)
            self.assertIn("読めませんでした", service.status().message)
        self.assertTrue(service._ensure())
        self.assertEqual(service.path, str(self.data_db))

    def test_マスタのフォルダだけ届かないときはいまのマスタのまま(self) -> None:
        master_dir = self.root / "マスタ"
        master_dir.mkdir()
        new_master = _web.make_source(master_dir, config.SOURCE_FILE_MASTER, _web.MASTER_SCHEMA)
        user_settings.set_value(user_settings.KEY_MASTER_DB_DIR, str(master_dir))
        service = self.make_service()
        self.assertEqual(service._auto.master_path, str(new_master))
        with unreachable(master_dir):
            service.reload()
        self.assertEqual(service.path, str(self.data_db))
        self.assertEqual(service._auto.master_path, str(new_master))

    def test_設定ファイルを読めないのを参照パス未設定と取り違えない(self) -> None:
        service = self.make_service()
        self.assertEqual(service.path, str(self.data_db))
        with self.settings_unreadable():
            service.reload()
            self.assertEqual(service.path, str(self.data_db))
            self.assertTrue(service.configured)


# ---------------------------------------------------------------------------
# 3. 配布設定: 設定を読めないまま書き直さない・読めないほうの代わりを使わない
# ---------------------------------------------------------------------------
class DistributionTests(_Settings):
    MINE = {
        user_settings.KEY_MY_LINE: "L-1",
        user_settings.KEY_DATA_DB_DIR: r"\\srv\この端末で決めた",
        user_settings.KEY_LOG_DIR: r"\\srv\logs",
    }

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(distribution, "DIR", self.root / "tool" / "配布設定")
        patcher.start()
        self.addCleanup(patcher.stop)
        distribution.DIR.mkdir(parents=True)
        self.bundle = {"format": distribution.FORMAT, "created_at": "2026/10/01 08:00:00",
                       "settings": {user_settings.KEY_DATA_DB_DIR: r"\\srv\配布",
                                    user_settings.KEY_MY_LINE: "コイル",
                                    user_settings.KEY_AUTO_SYNC: False}}
        distribution.settings_path().write_text(
            json.dumps(self.bundle, ensure_ascii=False), encoding="utf-8")
        self.settings_file.write_text(json.dumps(self.MINE, ensure_ascii=False),
                                      encoding="utf-8")
        # 大設定がまとめたファイルは、試験ごとに置き場所を替える(本物を読まない)
        env = mock.patch.dict(os.environ,
                              {"ALLTOOLS_DISTRIBUTION_DIR": str(self.root / "一式の配布設定")})
        env.start()
        self.addCleanup(env.stop)

    def test_設定を読めない起動で配布設定で上書きしない(self) -> None:
        with self.settings_unreadable():
            result = distribution.apply_on_start()
        self.assertFalse(result.ok)
        self.assertEqual(self.stored(), self.MINE)

    def test_設定を読めないときは配布設定を書き出さない(self) -> None:
        before = distribution.settings_path().read_text(encoding="utf-8")
        items = [k for k, _, default in distribution.ITEMS if default]
        with self.settings_unreadable():
            result = distribution.export("", items, admin=True, keep_previous=True)
        self.assertFalse(result.ok)
        self.assertIn("読めませんでした", result.message)
        self.assertEqual(distribution.settings_path().read_text(encoding="utf-8"), before)

    def test_読めない配布設定の代わりに_まとめたファイルの古い中身を読まない(self) -> None:
        whole = self.root / "一式の配布設定"
        whole.mkdir()
        older = dict(self.bundle, created_at="2026/01/01 08:00:00",
                     settings={user_settings.KEY_DATA_DB_DIR: r"\\srv\古い配布"})
        (whole / distribution.INTEGRATED_FILE).write_text(
            json.dumps({"tools": {distribution.TOOL_ID: older}}, ensure_ascii=False),
            encoding="utf-8")
        real = Path.read_text
        own = distribution.settings_path()

        def refuse(path, *args, **kwargs):
            if Path(path) == own:
                raise PermissionError(13, "Permission denied", str(path))
            return real(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", refuse), \
                mock.patch.object(distribution, "_guard", lambda *a: None):
            result = distribution.reapply("")
            summary = distribution.summary()
        self.assertFalse(result.ok)
        self.assertEqual(self.stored(), self.MINE)
        self.assertFalse(summary["exists"])
        self.assertIn("読めませんでした", summary["unreadable"])

    def test_途中で切れた配布設定を無いものとして扱わない(self) -> None:
        distribution.settings_path().write_text('{"format": 1, "settings": {', encoding="utf-8")
        with self.assertRaises(OSError):
            distribution.read()
        self.assertFalse(distribution.apply_on_start().ok)
        self.assertEqual(self.stored(), self.MINE)

    def test_配布設定が本当に無いならいままでどおり(self) -> None:
        distribution.settings_path().unlink()
        distribution.DIR.rmdir()
        self.assertIsNone(distribution.read())
        self.assertTrue(distribution.apply_on_start().ok)
        self.assertEqual(self.stored(), self.MINE)


# ---------------------------------------------------------------------------
# 4. 手元のDB: 帯の表示が空のDBを作らない
# ---------------------------------------------------------------------------
class NoticesTests(unittest.TestCase):
    def test_手元のDBが見えないとき空のDBを作らない(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "calendar.db"
            self.assertEqual(notices.pending_at(str(path)), [])
            self.assertFalse(path.exists())

    def test_あれば読める(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "calendar.db"
            from calendar_app import db

            conn = db.connect(path)
            try:
                notices.remember(conn, [{"日付": "2026/08/03", "登録内容": "山田"}])
            finally:
                conn.close()
            self.assertEqual(len(notices.pending_at(str(path))), 1)


# ---------------------------------------------------------------------------
# 5. 変換ツール: 出力先を確かめられないのに書かない・元を読めないのに消さない
# ---------------------------------------------------------------------------
def _load_tool():
    import importlib.util

    tool = Path(__file__).resolve().parent.parent / "tools" / "convert_accdb.py"
    spec = importlib.util.spec_from_file_location("convert_accdb_unreadable", tool)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SAMPLE = Path(__file__).parent / "data" / "sample.accdb"


class ConvertTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = _load_tool()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        # 動いている共有DB(1行入っている)
        self.target = _web.make_source(self.folder, config.SOURCE_FILE_DATA, _web.SOURCE_SCHEMA)
        conn = sqlite3.connect(self.target)
        try:
            with conn:
                conn.execute(f'INSERT INTO "{config.TABLE_DATA}" ("ID","登録内容") VALUES (1,"いまの行")')
        finally:
            conn.close()
        self.before = self.target.read_bytes()

    @unittest.skipUnless(SAMPLE.exists(), f"サンプル DB がありません: {SAMPLE}")
    def test_出力先を確かめられないときは書かない(self) -> None:
        real_stat = os.stat
        target = self.target

        def vanished(path, *args, **kwargs):
            if _same(path, target):
                raise FileNotFoundError(2, "一瞬見えない", str(path))
            return real_stat(path, *args, **kwargs)

        with mock.patch("os.stat", vanished), unreachable(self.folder):
            with self.assertRaises(SystemExit) as caught:
                self.tool.convert(SAMPLE, self.target)
        self.assertIn("確かめられません", str(caught.exception))
        self.assertEqual(self.target.read_bytes(), self.before)

    def test_元を読めないときは_forceでも出力先を消さない(self) -> None:
        broken = self.folder / "壊れた.accdb"
        broken.write_bytes(b"\x00" * 10)
        with self.assertRaises(BaseException):
            self.tool.convert(broken, self.target, force=True)
        self.assertTrue(self.target.exists())
        self.assertEqual(self.target.read_bytes(), self.before)
        self.assertEqual([p.name for p in self.folder.iterdir() if "変換中" in p.name], [])

    @unittest.skipUnless(SAMPLE.exists(), f"サンプル DB がありません: {SAMPLE}")
    def test_書いている途中のファイルは取り込み元として拾われない(self) -> None:
        seen: list[list[str]] = []
        real = self.tool.convert_table

        def watch(reader, out, table):
            seen.append([p.name for p in source_db.list_source_files_checked(self.folder)])
            return real(reader, out, table)

        with mock.patch.object(self.tool, "convert_table", watch):
            self.tool.convert(SAMPLE, self.target, force=True)
        self.assertTrue(seen)
        for names in seen:
            self.assertEqual(names, [config.SOURCE_FILE_DATA])


# ---------------------------------------------------------------------------
# 6. 終わるとき: 送信待ちを数えられないのを「0件」と読まない
# ---------------------------------------------------------------------------
class UnsentAtExitTests(unittest.TestCase):
    def setUp(self) -> None:
        _web.with_source(self)
        self.addCleanup(sync_service.remember_exit, 0)

    @contextmanager
    def cannot_count(self):
        import calendar_app.sync as sync_package

        def locked(conn):
            raise sqlite3.OperationalError("database is locked")

        with mock.patch.object(sync_package, "total_pending_count", locked):
            yield

    def test_数えられないときは残っているものとして粘る(self) -> None:
        with self.cannot_count():
            self.assertGreater(sync_service.unsent_count(), 0)

    def test_数えられないときは未送信を残して終わった覚えを消さない(self) -> None:
        sync_service.remember_exit(3)
        with self.cannot_count():
            sync_service.remember_exit(0)
        self.assertEqual(sync_service.last_unsent_exit()[0], 3)
        sync_service.remember_exit(0)               # 数えられて0件なら消える
        self.assertEqual(sync_service.last_unsent_exit(), (0, ""))


# ---------------------------------------------------------------------------
# 7. 画面: 設定ファイルを読めないのを「思わぬエラー」にしない
# ---------------------------------------------------------------------------
class ReplyTests(_Settings):
    def test_設定を読めないときは読めませんと返す(self) -> None:
        client = _web.make_client()
        self.settings_file.write_text(json.dumps({"PCライン": "L-1"}), encoding="utf-8")
        with self.settings_unreadable():
            response = client.post("/api/settings/auto-sync", headers=_web.auth(),
                                   json={"interval": 30})
        self.assertEqual(response.status_code, 503)
        body = response.get_json()
        self.assertEqual(body["error"]["code"], "unreadable")
        self.assertIn("何も書き換えていません", body["error"]["message"])
        self.assertEqual(self.stored(), {"PCライン": "L-1"})


if __name__ == "__main__":
    unittest.main()
