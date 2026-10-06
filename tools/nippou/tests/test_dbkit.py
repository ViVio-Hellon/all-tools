"""dbkit(汎用DB接続レイヤー)の単体テスト。

このマシンはLinuxなので、`access_odbc`のうちWindows専用の実接続経路
(ctypes+odbc32.dll)は検証できない。ここでは、
    - どの環境でも動く純粋関数(SQLリテラル組み立て・エラー判定等)
    - `is_available()`がLinuxでFalseを返すこと、その先のAPIが
      例外/Noneで安全にフォールバックすること
    - sqlite_toolkit / outbox_sync のSQLite側ロジック全体
を対象にする。
"""
import sqlite3
import sys
import unittest
from unittest import mock
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dbkit import access_odbc, outbox_sync, sqlite_toolkit


class AccessOdbcQuoteTests(unittest.TestCase):
    def test_quote_none_and_bool(self) -> None:
        self.assertEqual(access_odbc.quote(None), "NULL")
        self.assertEqual(access_odbc.quote(True), "True")
        self.assertEqual(access_odbc.quote(False), "False")

    def test_quote_numbers(self) -> None:
        self.assertEqual(access_odbc.quote(12), "12")
        self.assertEqual(access_odbc.quote(1.5), "1.5")

    def test_quote_date_and_datetime(self) -> None:
        self.assertEqual(access_odbc.quote(date(2026, 8, 5)), "#2026-08-05#")
        self.assertEqual(
            access_odbc.quote(datetime(2026, 8, 5, 9, 30, 0)),
            "#2026-08-05 09:30:00#")

    def test_quote_string_escapes_single_quote(self) -> None:
        self.assertEqual(access_odbc.quote("O'Brien"), "'O''Brien'")

    def test_quote_string_replaces_newlines_and_tabs(self) -> None:
        self.assertEqual(access_odbc.quote("a\nb\tc"), "'a b c'")

    def test_quote_identifier(self) -> None:
        self.assertEqual(access_odbc.quote_identifier("報告日"), "[報告日]")

    def test_quote_identifier_rejects_closing_bracket(self) -> None:
        with self.assertRaises(access_odbc.AccessError):
            access_odbc.quote_identifier("bad]name")

    def test_build_insert(self) -> None:
        sql = access_odbc.build_insert("T", {"a": 1, "b": "x"})
        self.assertEqual(sql, "INSERT INTO [T] ([a], [b]) VALUES (1, 'x')")

    def test_build_update_with_where(self) -> None:
        sql = access_odbc.build_update("T", {"a": 1}, {"id": 5})
        self.assertEqual(sql, "UPDATE [T] SET [a] = 1 WHERE [id] = 5")

    def test_build_update_without_where(self) -> None:
        sql = access_odbc.build_update("T", {"a": 1}, {})
        self.assertEqual(sql, "UPDATE [T] SET [a] = 1")


class AccessOdbcErrorClassificationTests(unittest.TestCase):
    def test_is_lock_error_detects_english_and_japanese(self) -> None:
        self.assertTrue(access_odbc.is_lock_error("Could not lock the table"))
        self.assertTrue(access_odbc.is_lock_error("他のユーザーがロックしています"))
        self.assertFalse(access_odbc.is_lock_error("column not found"))

    def test_is_duplicate_error(self) -> None:
        self.assertTrue(access_odbc.is_duplicate_error("Duplicate key value"))
        self.assertTrue(access_odbc.is_duplicate_error("重複した値です"))
        self.assertFalse(access_odbc.is_duplicate_error("timeout"))

    def test_is_already_exists_error(self) -> None:
        self.assertTrue(access_odbc.is_already_exists_error("column already exists"))
        self.assertTrue(access_odbc.is_already_exists_error("既に存在します"))
        self.assertFalse(access_odbc.is_already_exists_error("syntax error"))


class AccessOdbcAvailabilityTests(unittest.TestCase):
    """Windows以外で安全にフォールバックすることを確認する。

    Windows の上で流しても同じことを確かめられるよう、`sys.platform` を
    Windows以外(linux)に見せかけて試す。
    """

    def setUp(self) -> None:
        patcher = mock.patch.object(access_odbc.sys, "platform", "linux")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_is_available_false_on_linux(self) -> None:
        self.assertFalse(access_odbc.is_available())

    def test_connect_raises_access_error(self) -> None:
        with self.assertRaises(access_odbc.AccessError):
            access_odbc.connect(Path("dummy.accdb"))

    def test_find_access_driver_returns_none(self) -> None:
        self.assertIsNone(access_odbc.find_access_driver())

    def test_diagnose_reports_platform_without_raising(self) -> None:
        report = access_odbc.diagnose()
        self.assertIn("Windowsではない", report)

    def test_connection_string_contains_driver_and_path(self) -> None:
        cs = access_odbc.connection_string(Path("x.accdb"), driver="Foo")
        self.assertIn("Driver={Foo}", cs)
        self.assertIn("x.accdb", cs)


class _FakeOdbcHandle:
    """`access_odbc.AccessConnection._odbc`(ctypes DLLハンドル)の代役。

    Windows実機なしで `AccessConnection.transaction()` の呼び出し順序
    (autocommit off -> ブロック実行 -> commit/rollback -> autocommit on)
    を検証するためのテストダブル。
    """

    def __init__(self, *, fail_set_attr: bool = False, fail_end_tran: bool = False) -> None:
        self.calls: list[tuple] = []
        self._fail_set_attr = fail_set_attr
        self._fail_end_tran = fail_end_tran

    def SQLSetConnectAttr(self, dbc, attr, value, str_len):
        self.calls.append(("SQLSetConnectAttr", attr, getattr(value, "value", value)))
        return access_odbc.SQL_ERROR if self._fail_set_attr else access_odbc.SQL_SUCCESS

    def SQLEndTran(self, handle_type, handle, completion_type):
        self.calls.append(("SQLEndTran", completion_type))
        return access_odbc.SQL_ERROR if self._fail_end_tran else access_odbc.SQL_SUCCESS

    def SQLGetDiagRecW(self, *args):
        # 失敗の詳細を読む。代役なので詳細は無い(呼び出し順の記録には入れない)
        return access_odbc.SQL_NO_DATA


def _make_fake_access_connection(fake_odbc: "_FakeOdbcHandle") -> "access_odbc.AccessConnection":
    """`__init__`(Windows専用の実接続)を経由せずインスタンスを組み立てる。"""
    import ctypes

    conn = object.__new__(access_odbc.AccessConnection)
    conn.path = Path("fake.accdb")
    conn.read_only = False
    conn._odbc = fake_odbc
    conn._dbc = ctypes.c_void_p(1)
    conn._env = ctypes.c_void_p(0)
    return conn


class AccessConnectionTransactionTests(unittest.TestCase):
    def test_successful_block_commits_and_restores_autocommit(self) -> None:
        fake = _FakeOdbcHandle()
        conn = _make_fake_access_connection(fake)

        with conn.transaction():
            pass

        kinds = [c[0] for c in fake.calls]
        self.assertEqual(kinds, ["SQLSetConnectAttr", "SQLEndTran", "SQLSetConnectAttr"])
        # ctypes.c_void_p(0).value is None (NULL pointer), not 0 -- that's
        # SQL_AUTOCOMMIT_OFF here.
        self.assertIsNone(fake.calls[0][2])
        self.assertEqual(fake.calls[1][1], access_odbc.SQL_COMMIT)
        self.assertEqual(fake.calls[2][2], access_odbc.SQL_AUTOCOMMIT_ON)

    def test_exception_inside_block_rolls_back_and_propagates(self) -> None:
        fake = _FakeOdbcHandle()
        conn = _make_fake_access_connection(fake)

        with self.assertRaises(ValueError):
            with conn.transaction():
                raise ValueError("boom")

        kinds = [c[0] for c in fake.calls]
        self.assertEqual(kinds, ["SQLSetConnectAttr", "SQLEndTran", "SQLSetConnectAttr"])
        self.assertEqual(fake.calls[1][1], access_odbc.SQL_ROLLBACK)
        self.assertEqual(fake.calls[2][2], access_odbc.SQL_AUTOCOMMIT_ON)

    def test_begin_failure_raises_access_error_without_running_block(self) -> None:
        fake = _FakeOdbcHandle(fail_set_attr=True)
        conn = _make_fake_access_connection(fake)
        ran_block = False

        with self.assertRaises(access_odbc.AccessError):
            with conn.transaction():
                ran_block = True  # pragma: no cover - should not run

        self.assertFalse(ran_block)

    def test_commit_failure_rolls_back_and_raises(self) -> None:
        fake = _FakeOdbcHandle(fail_end_tran=True)
        conn = _make_fake_access_connection(fake)

        with self.assertRaises(access_odbc.AccessError):
            with conn.transaction():
                pass

        # コミット失敗直後にロールバックも試みている
        end_tran_completions = [c[1] for c in fake.calls if c[0] == "SQLEndTran"]
        self.assertIn(access_odbc.SQL_COMMIT, end_tran_completions)
        self.assertIn(access_odbc.SQL_ROLLBACK, end_tran_completions)


class SqliteToolkitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE T (id INTEGER PRIMARY KEY, name TEXT)")
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_is_lock_error_true_for_operational_error(self) -> None:
        exc = sqlite3.OperationalError("database is locked")
        self.assertTrue(sqlite_toolkit.is_lock_error(exc))

    def test_is_lock_error_false_for_other_errors(self) -> None:
        exc = sqlite3.OperationalError("no such table: X")
        self.assertFalse(sqlite_toolkit.is_lock_error(exc))

    def test_is_lock_error_false_for_non_sqlite_exception(self) -> None:
        self.assertFalse(sqlite_toolkit.is_lock_error(ValueError("database is locked")))

    def test_enable_wal_does_not_raise_on_memory_db(self) -> None:
        sqlite_toolkit.enable_wal(self.conn)

    def test_execute_with_retry_insert_success(self) -> None:
        result = sqlite_toolkit.execute_with_retry(
            self.conn, "INSERT INTO T (id, name) VALUES (?, ?)", (1, "a"))
        self.assertTrue(result.ok)
        self.assertEqual(result.rowcount, 1)

    def test_execute_with_retry_returns_error_on_bad_sql(self) -> None:
        result = sqlite_toolkit.execute_with_retry(self.conn, "INSERT INTO NoSuchTable VALUES (1)")
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.error)

    def test_fetch_all_and_fetch_one(self) -> None:
        self.conn.execute("INSERT INTO T (id, name) VALUES (1, 'a'), (2, 'b')")
        self.conn.commit()
        rows = sqlite_toolkit.fetch_all(self.conn, "SELECT * FROM T ORDER BY id")
        self.assertEqual(len(rows), 2)
        one = sqlite_toolkit.fetch_one(self.conn, "SELECT * FROM T WHERE id = ?", (2,))
        self.assertEqual(one["name"], "b")

    def test_fetch_all_returns_empty_list_for_no_rows(self) -> None:
        rows = sqlite_toolkit.fetch_all(self.conn, "SELECT * FROM T")
        self.assertEqual(rows, [])

    def test_fetch_all_returns_none_on_error(self) -> None:
        rows = sqlite_toolkit.fetch_all(self.conn, "SELECT * FROM NoSuchTable")
        self.assertIsNone(rows)

    def test_update_record_success(self) -> None:
        self.conn.execute("INSERT INTO T (id, name) VALUES (1, 'a')")
        self.conn.commit()
        result = sqlite_toolkit.update_record(self.conn, "T", "id", 1, {"name": "z"})
        self.assertTrue(result.ok)
        self.assertEqual(result.reason, "success")
        row = self.conn.execute("SELECT name FROM T WHERE id = 1").fetchone()
        self.assertEqual(row["name"], "z")

    def test_update_record_not_found(self) -> None:
        result = sqlite_toolkit.update_record(self.conn, "T", "id", 999, {"name": "z"})
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "not_found")

    def test_insert_record(self) -> None:
        result = sqlite_toolkit.insert_record(self.conn, "T", {"id": 5, "name": "e"})
        self.assertTrue(result.ok)
        row = self.conn.execute("SELECT name FROM T WHERE id = 5").fetchone()
        self.assertEqual(row["name"], "e")

    def test_now_db_string_format(self) -> None:
        s = sqlite_toolkit.now_db_string()
        datetime.strptime(s, "%Y-%m-%d %H:%M:%S")

    def test_sanitize_for_db_strips_and_collapses_newlines(self) -> None:
        self.assertEqual(sqlite_toolkit.sanitize_for_db("  a\r\nb\tc  "), "a b c")


class OutboxSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE Src (行ID INTEGER PRIMARY KEY, 値 TEXT)")
        self.conn.commit()
        self.spec = outbox_sync.WriteBackSpec(
            sqlite_table="Src", access_table="Dst", key_column="行ID")

    def tearDown(self) -> None:
        self.conn.close()

    def _insert(self, row_id: int, value: str) -> None:
        self.conn.execute("INSERT INTO Src (行ID, 値) VALUES (?, ?)", (row_id, value))
        self.conn.commit()

    def test_pending_rows_before_and_after_mark_synced(self) -> None:
        self._insert(1, "a")
        pending = outbox_sync.pending_rows(self.conn, self.spec)
        self.assertEqual(len(pending), 1)
        outbox_sync.mark_synced(self.conn, self.spec, [1])
        pending = outbox_sync.pending_rows(self.conn, self.spec)
        self.assertEqual(pending, [])

    def test_claim_rows_assigns_op_ids_and_does_not_double_claim(self) -> None:
        self._insert(1, "a")
        self._insert(2, "b")
        rows, op_ids = outbox_sync.claim_rows(self.conn, self.spec)
        self.assertEqual(len(rows), 2)
        self.assertEqual(set(op_ids), {1, 2})

        # 予約済みなので、済にする前にもう一度呼んでも拾わない
        rows_again, op_ids_again = outbox_sync.claim_rows(self.conn, self.spec)
        self.assertEqual(rows_again, [])
        self.assertEqual(op_ids_again, {})

    def test_release_claim_allows_reclaim_with_new_op_id(self) -> None:
        self._insert(1, "a")
        _, op_ids = outbox_sync.claim_rows(self.conn, self.spec)
        first_op_id = op_ids[1]
        outbox_sync.release_claim(self.conn, self.spec, [1])

        rows, op_ids_2 = outbox_sync.claim_rows(self.conn, self.spec)
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(op_ids_2[1], first_op_id)

    def test_unsent_tables_reports_only_specs_with_pending_rows(self) -> None:
        self._insert(1, "a")
        other = outbox_sync.WriteBackSpec(
            sqlite_table="Src", access_table="Dst2", key_column="行ID")
        remaining = outbox_sync.unsent_tables(self.conn, [self.spec, other])
        self.assertEqual(remaining, {"Src": 1})
        outbox_sync.mark_synced(self.conn, self.spec, [1])
        remaining = outbox_sync.unsent_tables(self.conn, [self.spec])
        self.assertEqual(remaining, {})


class _FakeAccessConnection:
    """`AccessConnection`の代わり(Windows実機なしでwrite_backを検証する)。

    `values` には主キー(行ID)は含まれない(Access側はオートナンバー
    前提のため)ので、判定は業務列(値)で行う。
    """

    def __init__(self, path: str = "fake.accdb", *, fail_values: set[str] | None = None,
                 duplicate_values: set[str] | None = None) -> None:
        self.path = path
        self.executed_ddl: list[str] = []
        self.inserted: list[tuple[str, dict]] = []
        self._fail_values = fail_values or set()
        self._duplicate_values = duplicate_values or set()

    def execute(self, sql: str) -> None:
        self.executed_ddl.append(sql)

    def insert(self, table: str, values: dict) -> None:
        value = values.get("値")
        if value in self._duplicate_values:
            raise access_odbc.AccessError("Duplicate key value violates unique index")
        if value in self._fail_values:
            raise access_odbc.AccessError("column not found")
        self.inserted.append((table, dict(values)))


class WriteBackTests(unittest.TestCase):
    def setUp(self) -> None:
        outbox_sync._op_id_column_cache.clear()
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE Src (行ID INTEGER PRIMARY KEY, 値 TEXT)")
        self.conn.commit()
        self.spec = outbox_sync.WriteBackSpec(
            sqlite_table="Src", access_table="Dst", key_column="行ID")

    def tearDown(self) -> None:
        self.conn.close()
        outbox_sync._op_id_column_cache.clear()

    def _insert(self, row_id: int, value: str) -> None:
        self.conn.execute("INSERT INTO Src (行ID, 値) VALUES (?, ?)", (row_id, value))
        self.conn.commit()

    def test_write_back_sends_all_pending_rows(self) -> None:
        self._insert(1, "a")
        self._insert(2, "b")
        access = _FakeAccessConnection()
        result = outbox_sync.write_back(self.conn, access, [self.spec])
        self.assertTrue(result.ok)
        self.assertEqual(result.sent, {"Src": 2})
        self.assertEqual(len(access.inserted), 2)
        # 送信ID列(既定名)が values に含まれている
        self.assertIn(outbox_sync.DEFAULT_OP_ID_COLUMN, access.inserted[0][1])
        # 送信済みなので二回目は何も送らない
        result2 = outbox_sync.write_back(self.conn, access, [self.spec])
        self.assertEqual(result2.sent, {})

    def test_write_back_treats_duplicate_as_success(self) -> None:
        self._insert(1, "a")
        access = _FakeAccessConnection(duplicate_values={"a"})
        result = outbox_sync.write_back(self.conn, access, [self.spec])
        self.assertTrue(result.ok)
        self.assertEqual(result.sent, {"Src": 1})
        pending = outbox_sync.pending_rows(self.conn, self.spec)
        self.assertEqual(pending, [])

    def test_write_back_records_error_and_allows_reclaim(self) -> None:
        self._insert(1, "a")
        access = _FakeAccessConnection(fail_values={"a"})
        result = outbox_sync.write_back(self.conn, access, [self.spec])
        self.assertFalse(result.ok)
        self.assertEqual(len(result.errors), 1)
        # 予約は外れているので、まだ未送信として残る
        pending = outbox_sync.pending_rows(self.conn, self.spec)
        self.assertEqual(len(pending), 1)

    def test_write_back_without_op_id_guard_omits_op_id_column(self) -> None:
        self._insert(1, "a")
        spec = outbox_sync.WriteBackSpec(
            sqlite_table="Src", access_table="Dst", key_column="行ID",
            use_op_id_guard=False)
        access = _FakeAccessConnection()
        result = outbox_sync.write_back(self.conn, access, [spec])
        self.assertTrue(result.ok)
        self.assertNotIn(outbox_sync.DEFAULT_OP_ID_COLUMN, access.inserted[0][1])
        # DDLも一切呼ばれていない
        self.assertEqual(access.executed_ddl, [])


if __name__ == "__main__":
    unittest.main()
