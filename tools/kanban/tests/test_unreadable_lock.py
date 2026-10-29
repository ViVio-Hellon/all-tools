"""共有を「読めなかった」のを「無い」と取り違えない(第2弾)── 長いロック・無いファイルを作らない・代わりの場所

* 共有DBが**長くロックされたまま**のとき、1 文ごとに待ち続けない(残りは「ロック中」で返し、次の回に送る)。
  ロック中は「読めない」と同じに扱う: 手元の看板を消さない・「表が無い」と言わない・送った印を付けない・
  諦めの回数に数えない
* 書く・確かめるために開くとき、**無いファイルを作らない**(素のパスの ``sqlite3.connect`` は空の DB を作る)
* 写しの途中で断られた(ロック・届きかけで切れた)写しを残さない。届かない・掴まれているのを
  「見つかりません」と言わない
* 看板履歴.sqlite3 に届かないのを「まだ無い」として、看板マスタの以前の記録で集計しない
* 表の中身の入れ替え: 読めない表を「0 行」と見せない・読めないまま入れ替えに進まない
* 配布設定: 日報複合ツールのまとめたファイルを読めない回に、看板のフォルダの古い配布設定を読み込まない
* 道具(accdb_to_sqlite / check_shared_lock): 変換の途中で共有DBを消さない・運用のファイルに試験の表を作らない
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kanban import config, distribution
from kanban import table_refresh as tr
from kanban.db import history, shared as shared_module, sync
from kanban.db.shared import SharedDb, SharedDbError, kanban_tables_in, _is_usable_sqlite
from kanban.db.store import MAX_SYNC_ATTEMPTS, Store
from kanban.domain import models
from kanban.presenters import stats

from test_distribution import DistributionTestBase
from test_table_refresh import Fixture as RefreshFixture
from test_unreadable_shared import _make_shared

TOOLS = Path(__file__).resolve().parent.parent / "tools"


def _deny(paths, real=os.stat):
    """``paths`` だけ「アクセスが拒否されました」(掴まれている・権限・共有フォルダの不調)にする ``os.stat``。"""
    denied = {os.fspath(p) for p in paths}

    def stat(path, *args, **kwargs):
        if os.fspath(path) in denied:
            raise PermissionError(13, "アクセスが拒否されました", os.fspath(path))
        return real(path, *args, **kwargs)

    return stat


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="kanban_unreadable2_"))
        self.store = Store(str(self.dir / "local.sqlite3"), host_name="PC-1")
        self.store.ensure_schema()
        self.shared = self.dir / "看板マスタ.sqlite3"
        _make_shared(self.shared)
        self.gateway = SharedDb(str(self.shared), cache_dir=str(self.dir / "cache"),
                                busy_timeout_ms=50, max_retry=1)
        self.lockers: list[sqlite3.Connection] = []
        # ロックの待ち(1, 2, 3 秒)は待たない
        patcher = mock.patch.object(shared_module.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        for conn in self.lockers:
            conn.close()
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def release_locks(self) -> None:
        for conn in self.lockers:
            conn.execute("ROLLBACK")
            conn.close()
        self.lockers.clear()

    def hold_lock(self, path: Path) -> None:
        """ほかの PC が書いている最中(``BEGIN EXCLUSIVE`` を握ったまま)。"""
        conn = sqlite3.connect(str(path), isolation_level=None)
        conn.execute("BEGIN EXCLUSIVE")
        self.lockers.append(conn)


# ---------------------------------------------------------------------------
# 長いロック
# ---------------------------------------------------------------------------
class LongLockTest(Fixture):
    def setUp(self) -> None:
        super().setUp()
        sync.import_all(self.store, self.gateway, lines=["LVC"])
        self.store.apply_transition("LVC", "1", models.order_button_changes, operation="order")
        self.store.apply_transition("LVC", "2", models.order_button_changes, operation="order")

    def test_a_lock_that_does_not_clear_is_not_waited_for_statement_by_statement(self):
        """ロックが解けないとき、残りの文を 1 つずつ待たない(100 文なら 1 時間を超えていた)。"""
        self.hold_lock(self.shared)
        statements = [("UPDATE [看板_LVC] SET [欲] = ? WHERE [管理番号] = ?", ["〇", str(n)]) for n in range(5)]
        real = SharedDb._execute_one
        with mock.patch.object(SharedDb, "_execute_one", autospec=True, side_effect=real) as tried:
            results = self.gateway.execute(statements)
        self.assertEqual(tried.call_count, 1, "ロックが解けないのに、残りの文も 1 つずつ待った")
        self.assertEqual(len(results), 5)
        self.assertTrue(all(not r.ok and r.is_lock_conflict for r in results),
                        [r.error_message for r in results])

    def test_export_under_a_long_lock_keeps_everything_for_the_next_round(self):
        self.hold_lock(self.shared)
        for _ in range(MAX_SYNC_ATTEMPTS + 1):
            result = sync.export_pending(self.store, self.gateway)
            self.assertEqual(result.succeeded, 0)
        self.assertEqual(self.store.sync_failures(), [], "ロック中なだけで「要確認」(諦め)にした")
        self.assertEqual(sorted(r["mgmt_no"] for r in self.store.pending_items()), ["1", "2"])
        self.assertEqual(sorted(i.mgmt_no for i in self.store.items("LVC")), ["1", "2"])

        # 出来事(看板履歴)もロック中は送った印を付けない
        target = history.HistoryDb(str(self.dir / config.HISTORY_DB_NAME), cache_dir=str(self.dir / "cache"),
                                   busy_timeout_ms=50, max_retry=1)
        self.assertTrue(sync.ensure_history_table(target).ok)
        unsent = self.store.unsent_event_count()
        self.assertGreater(unsent, 0)
        self.hold_lock(Path(target.path))
        exporter = sync.Exporter(self.store, self.gateway, interval_sec=0, history_gateway=target)
        exporter.run_once()
        self.assertEqual(self.store.unsent_event_count(), unsent, "ロック中なのに送れたことにした")

        self.release_locks()
        self.gateway.forget_copy()
        target.forget_copy()
        exporter.run_once()
        self.assertEqual(self.store.pending_count(), 0)
        self.assertEqual(self.store.unsent_event_count(), 0)
        self.assertEqual(history.row_count(target), unsent)

    def test_an_import_that_is_refused_while_copying_keeps_the_board(self):
        """写している最中に断られた(Windows でほかの PC が掴んでいる)回は、読めなかったと扱う。"""
        self.store.set_meta("last_import_at", "2000/01/01 00:00:00")
        cache = self.dir / "cache"
        before = set(os.listdir(cache))

        def refused(_self, _source, target):
            Path(target).write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)   # 途中まで
            raise PermissionError(33, "プロセスはファイルにアクセスできません。別のプロセスがファイルの一部をロックしています")

        self.gateway.forget_copy()
        with mock.patch.object(SharedDb, "_copy_files", refused):
            result = sync.import_all(self.store, self.gateway, lines=["LVC"])
        self.assertFalse(result.lines[0].ok)
        self.assertNotEqual(result.lines[0].message, sync.TABLE_MISSING)
        self.assertEqual(self.store.get_meta("missing_line_tables", ""), "")
        self.assertEqual(self.store.get_meta("last_import_at", ""), "2000/01/01 00:00:00")
        self.assertEqual(sorted(i.mgmt_no for i in self.store.items("LVC")), ["1", "2"])
        self.assertLessEqual(set(os.listdir(cache)), before, "途中まで写した写しを残した")


# ---------------------------------------------------------------------------
# 届かない・掴まれているのを「見つかりません」と言わない
# ---------------------------------------------------------------------------
class CannotCheckTest(Fixture):
    def test_a_shared_db_that_cannot_be_checked_is_not_called_missing(self):
        with mock.patch("os.stat", _deny([self.shared])):
            with self.assertRaises(SharedDbError) as caught:
                self.gateway.table_names()
        text = str(caught.exception)
        self.assertNotIn("見つかりません", text)
        self.assertIn("確かめられません", text)
        self.assertTrue(sync.is_transient_failure(text), "一時的な失敗として扱わない(諦めの回数に数える)")

    def test_a_refused_stat_while_copying_is_a_shared_db_error(self):
        """確かめた直後に掴まれた。生の PermissionError のまま上げない(マスタ管理・集計が 500 になる)。"""
        with mock.patch.object(SharedDb, "_require_exists"), mock.patch("os.stat", _deny([self.shared])):
            with self.assertRaises(SharedDbError):
                self.gateway.local_copy()


# ---------------------------------------------------------------------------
# 無いファイルを作らない
# ---------------------------------------------------------------------------
class DoNotCreateTest(Fixture):
    def test_writing_does_not_create_a_shared_db_that_vanished(self):
        """確かめた直後に共有DBが見えなくなった(差し替えの最中・一瞬切れた)。そこへ空の DB を作らない。"""
        gone = self.dir / "消えた看板マスタ.sqlite3"
        gateway = SharedDb(str(gone), cache_dir=str(self.dir / "cache"))
        with mock.patch.object(SharedDb, "_require_exists"):
            with self.assertRaises(SharedDbError):
                gateway.execute([("CREATE TABLE IF NOT EXISTS [看板履歴] ([ID] TEXT)", [])])
            with self.assertRaises(SharedDbError):
                with gateway.transaction():
                    pass
        self.assertFalse(gone.exists(), "見えなくなった場所へ空の DB を作った")

    def test_checking_a_file_does_not_create_it(self):
        gone = self.dir / "無い.sqlite3"
        self.assertFalse(_is_usable_sqlite(gone), "無いファイルを「sqlite3 として開ける」と答えた")
        with self.assertRaises(sqlite3.Error):
            kanban_tables_in(gone)
        self.assertFalse(gone.exists(), "確かめるだけのつもりで空の DB を作った")

    def test_copying_old_history_does_not_create_the_history_file(self):
        c = sqlite3.connect(str(self.shared))
        c.execute("CREATE TABLE [看板履歴] ([ID] TEXT, [日時] TEXT)")
        c.execute("INSERT INTO [看板履歴] VALUES ('a', '2026/10/01 09:00:00')")
        c.commit()
        c.close()
        target = self.dir / config.HISTORY_DB_NAME
        with self.assertRaises(SharedDbError):
            history.copy_from_shared(self.gateway, history.HistoryDb(str(target), cache_dir=str(self.dir / "c2")))
        self.assertFalse(target.exists(), "看板履歴.sqlite3 が見えない回に空のファイルを作った")


# ---------------------------------------------------------------------------
# 集計: 看板履歴に届かないのを「まだ無い」にしない
# ---------------------------------------------------------------------------
class StatsTest(Fixture):
    def setUp(self) -> None:
        super().setUp()
        c = sqlite3.connect(str(self.shared))
        c.execute("CREATE TABLE [看板履歴] ([ID] TEXT, [日時] TEXT, [ライン] TEXT, [管理番号] TEXT, [出来事] TEXT)")
        c.execute("INSERT INTO [看板履歴] VALUES ('old', '2026/01/01 09:00:00', 'LVC', '1', '出した')")
        c.commit()
        c.close()
        self.history = history.HistoryDb(str(self.dir / config.HISTORY_DB_NAME), cache_dir=str(self.dir / "cache"))
        self.assertTrue(sync.ensure_history_table(self.history).ok)

    def test_unreachable_history_is_not_replaced_by_the_old_place(self):
        with mock.patch("os.stat", _deny([self.history.path])):
            data = stats.load(self.gateway, self.history)
        self.assertEqual(data.history_rows, [], "看板履歴に届かない回に、看板マスタの以前の記録で集計した")
        self.assertNotEqual(data.history_path, str(self.shared))
        self.assertIn("看板履歴を読めません", data.why)

    def test_history_really_missing_still_reads_the_old_place(self):
        os.remove(self.history.path)
        data = stats.load(self.gateway, self.history)
        self.assertEqual([r["ID"] for r in data.history_rows], ["old"])

    def test_a_line_that_cannot_be_read_does_not_vanish_from_the_totals(self):
        real = SharedDb.read_table

        def read_table(db, name, **kwargs):
            if name == "看板_LVC":
                raise SharedDbError("看板_LVC を読めませんでした: database is locked")
            return real(db, name, **kwargs)

        with mock.patch.object(SharedDb, "read_table", read_table):
            data = stats.load(self.gateway, self.history)
        self.assertFalse(data.available, "読めなかったラインを抜いた集計を「読めた」として出した")
        self.assertIn("読めませんでした", data.why)


# ---------------------------------------------------------------------------
# 表の中身の入れ替え
# ---------------------------------------------------------------------------
class RefreshUnreadableTest(RefreshFixture):
    def test_plan_does_not_show_zero_rows_for_a_table_it_could_not_read(self):
        with mock.patch.object(SharedDb, "select", side_effect=SharedDbError("共有DBを読めませんでした: locked")):
            p = tr.plan(str(self.src), self.shared)
        self.assertFalse(p.ok, "読めなかった表を「いま 0 行」と見せた")
        self.assertEqual(p.candidates, [])
        self.assertIn("読めませんでした", p.message)

    def test_plan_and_refresh_answer_when_columns_cannot_be_read(self):
        unreadable = SharedDbError("看板_LVC の列を読めませんでした: database is locked")
        with mock.patch.object(SharedDb, "column_names", side_effect=unreadable):
            p = tr.plan(str(self.src), self.shared)
            r = tr.refresh(str(self.src), self.shared, ["看板_LVC"], self.backup)
        self.assertFalse(p.ok)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, tr.REFUSE_READ)
        self.assertEqual(len(self.rows("看板_LVC")), 4, "読めないまま入れ替えた")

    def test_a_locked_shared_db_is_left_as_it_was(self):
        locker = sqlite3.connect(str(self.shared_path), isolation_level=None)
        try:
            locker.execute("BEGIN IMMEDIATE")          # ほかの PC が書いている最中(読むのはできる)
            self.shared.busy_timeout_ms = 50
            r = tr.refresh(str(self.src), self.shared, ["看板_LVC"], self.backup)
        finally:
            locker.execute("ROLLBACK")
            locker.close()
        self.assertFalse(r.ok)
        self.assertIn("書いている最中", r.message)
        self.assertEqual(len(self.rows("看板_LVC")), 4)


# ---------------------------------------------------------------------------
# 配布設定: まとめたファイルを読めない回に、古いほうで上書きしない
# ---------------------------------------------------------------------------
class DistributionUnreadableTest(DistributionTestBase):
    ENV = DistributionTestBase.ENV + ("ALLTOOLS_DISTRIBUTION_DIR",)

    def setUp(self) -> None:
        super().setUp()
        self.whole_dir = self.dir / "一式" / "配布設定"
        os.environ["ALLTOOLS_DISTRIBUTION_DIR"] = str(self.whole_dir)

    def put_whole(self, settings: dict, created_at: str) -> Path:
        self.whole_dir.mkdir(parents=True, exist_ok=True)
        path = self.whole_dir / distribution.INTEGRATED_FILE
        path.write_text(json.dumps({
            "format": 1, "created_at": created_at, "settings": {},
            "tools": {"kanban": {"format": 1, "created_at": created_at, "settings": settings}}},
            ensure_ascii=False), encoding="utf-8")
        return path

    def test_an_unreadable_combined_file_does_not_bring_back_the_older_folder(self):
        # 看板のフォルダには古い配布設定(古い共有DB)、まとめたファイルには新しいもの
        self.configure_source()                    # 古い共有DB(\\server\看板マスタ.sqlite3)
        exported = self.export_default()
        self.assertTrue(exported.ok, exported.message)
        whole = self.put_whole({"shared_db_path": r"\\server\新しい\看板マスタ.sqlite3"}, "2999/01/01 00:00:00")

        self.use_terminal("配った先")
        distribution.apply_on_start()
        self.assertEqual(config.load_config().shared_db_path, r"\\server\新しい\看板マスタ.sqlite3")

        for broken in ("denied", "half"):
            with self.subTest(broken):
                if broken == "denied":
                    with mock.patch("os.stat", _deny([whole])):
                        result = distribution.apply_on_start()
                else:
                    text = whole.read_text(encoding="utf-8")
                    whole.write_text(text[: len(text) // 2], encoding="utf-8")   # 書きかけ
                    result = distribution.apply_on_start()
                self.assertFalse(result.ok)
                self.assertIn("読めませんでした", result.message)
                self.assertEqual(config.load_config().shared_db_path, r"\\server\新しい\看板マスタ.sqlite3",
                                 "まとめたファイルを読めない回に、看板のフォルダの古い接続先へ戻した")

    def test_reapply_refuses_when_the_folder_cannot_be_read(self):
        self.configure_source()
        self.assertTrue(self.export_default().ok)
        self.use_terminal("配った先")
        self.configure(line="LVC")
        with mock.patch("os.stat", _deny([distribution.settings_path(self.folder)])):
            result = distribution.reapply()
        self.assertFalse(result.ok)
        self.assertIn("読めませんでした", result.message)
        self.assertNotIn("置かれていません", result.message, "読めないのを「配布設定が無い」と言った")
        self.assertEqual(config.load_config().line, "LVC")


# ---------------------------------------------------------------------------
# 道具
# ---------------------------------------------------------------------------
def _load_tool(name: str):
    spec = importlib.util.spec_from_file_location(f"_kanban_tool_{name}", TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _FakeAccess:
    def __init__(self, *_a, **_k) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def table_names(self):
        return ["看板_LVC", "Form状態管理"]


class ConvertToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="kanban_convert_"))
        self.tool = _load_tool("accdb_to_sqlite")
        self.out = self.dir / "看板マスタ.sqlite3"
        _make_shared(self.out)

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_conversion_that_fails_halfway_keeps_the_shared_db(self):
        """--force の変換が途中で落ちても、いまの共有DBを消さない(ほかの PC から「無い」に見せない)。"""
        def read_table(_path, name):
            raise RuntimeError(f"{name} を読み切れませんでした")

        with mock.patch.object(self.tool.filereader, "AccdbReader", _FakeAccess), \
                mock.patch.object(self.tool.filereader, "read_table", read_table):
            with self.assertRaises(RuntimeError):
                self.tool.convert("x.accdb", str(self.out), force=True)
        self.assertTrue(self.out.exists(), "変換が落ちたら共有DBが無くなった")
        self.assertEqual(kanban_tables_in(self.out)[0], ["看板_LVC"])
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), [self.out.name], "書きかけのファイルを残した")

    def test_an_output_that_cannot_be_checked_is_not_written(self):
        target = self.dir / "届かない" / "看板マスタ.sqlite3"
        with mock.patch("os.stat", _deny([target])), \
                mock.patch.object(self.tool.filereader, "AccdbReader", _FakeAccess), \
                contextlib.redirect_stderr(io.StringIO()) as said:
            code = self.tool.convert("x.accdb", str(target))
        self.assertIn("確かめられません", said.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse((self.dir / "届かない").exists(), "確かめられない出力先にフォルダを作った")


class LockToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="kanban_locktool2_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_tool(self, path: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(TOOLS / "check_shared_lock.py"), "--path", str(path),
             "--workers", "1", "--seconds", "0.2"],
            capture_output=True, text=True, encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=60)

    def test_a_real_db_is_not_used_for_the_test(self):
        real = self.dir / "_locktest_本物.sqlite3"
        _make_shared(real)
        proc = self.run_tool(real)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("試験用ではない", proc.stderr)
        self.assertNotIn("lock_test_counter", kanban_tables_in(real)[1], "看板マスタに試験の表を作った")

    def test_production_names_and_missing_folders_are_refused(self):
        for name in ("看板マスタ.sqlite3", "看板履歴.sqlite3", "梱包資材マスタ.sqlite3"):
            with self.subTest(name):
                proc = self.run_tool(self.dir / name)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("同名", proc.stderr)
        proc = self.run_tool(self.dir / "届かない" / "_locktest.sqlite3")
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse((self.dir / "届かない").exists(), "見えないフォルダを作った")


if __name__ == "__main__":
    unittest.main()
