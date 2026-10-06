"""dbkit.sqlite_toolkit のテスト。"""

from __future__ import annotations

import sqlite3
import unittest

from dbkit import sqlite_toolkit as tk


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT NOT NULL, count INTEGER NOT NULL)"
    )
    conn.execute("INSERT INTO t (id, name, count) VALUES (1, 'a', 10)")
    conn.commit()
    return conn


class IsLockErrorTest(unittest.TestCase):
    def test_recognizes_lock_messages(self):
        exc = sqlite3.OperationalError("database is locked")
        self.assertTrue(tk.is_lock_error(exc))

    def test_recognizes_busy(self):
        exc = sqlite3.OperationalError("SQLITE_BUSY: table busy")
        self.assertTrue(tk.is_lock_error(exc))

    def test_other_operational_errors_are_not_lock_errors(self):
        exc = sqlite3.OperationalError("no such table: x")
        self.assertFalse(tk.is_lock_error(exc))

    def test_non_sqlite_exception_is_not_lock_error(self):
        self.assertFalse(tk.is_lock_error(ValueError("locked")))


class EnableWalTest(unittest.TestCase):
    def test_memory_db_cannot_use_wal_but_does_not_raise(self):
        conn = make_conn()
        # :memory: DBは WAL 非対応(常に memory journal)。エラーにならず
        # 実際に有効になったモードを返すことだけ確認する
        mode = tk.enable_wal(conn)
        self.assertIsInstance(mode, str)

    def test_file_db_can_use_wal(self):
        import shutil
        import tempfile
        from pathlib import Path

        d = Path(tempfile.mkdtemp(prefix="dbkit_wal_"))
        try:
            conn = sqlite3.connect(str(d / "t.sqlite3"))
            mode = tk.enable_wal(conn)
            self.assertEqual(mode, "wal")
            conn.close()
        finally:
            shutil.rmtree(d, ignore_errors=True)


class ExecuteWithRetryTest(unittest.TestCase):
    def test_success(self):
        conn = make_conn()
        result = tk.execute_with_retry(
            conn, "UPDATE t SET count = count + 1 WHERE id = ?", (1,)
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.rowcount, 1)
        self.assertEqual(conn.execute("SELECT count FROM t WHERE id=1").fetchone()[0], 11)

    def test_syntax_error_is_not_retried_and_reports_error(self):
        conn = make_conn()
        result = tk.execute_with_retry(conn, "UPDATE t SET nosuch = 1 WHERE id = 1")
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.error)

    def test_lock_error_is_retried_then_succeeds(self):
        # 2つ目の接続で書き込みロックを取得し、一定時間後に解放することで
        # リトライ経路(is_lock_error -> sleep -> 再試行)を検証する。
        # sqlite3接続は作成したスレッドでしか使えないため、ロックの取得も
        # 解放も同じバックグラウンドスレッド内で行う
        import shutil
        import tempfile
        import threading
        import time
        from pathlib import Path

        d = Path(tempfile.mkdtemp(prefix="dbkit_retry_"))
        try:
            path = str(d / "t.sqlite3")
            setup = sqlite3.connect(path, timeout=1)
            setup.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, count INTEGER)")
            setup.execute("INSERT INTO t VALUES (1, 0)")
            setup.commit()
            setup.close()

            release = threading.Event()
            acquired = threading.Event()

            def hold_lock() -> None:
                blocker = sqlite3.connect(path, timeout=1)
                blocker.execute("BEGIN IMMEDIATE")
                blocker.execute("UPDATE t SET count = 99 WHERE id = 1")
                acquired.set()
                release.wait(timeout=5)
                blocker.commit()
                blocker.close()

            holder = threading.Thread(target=hold_lock, daemon=True)
            holder.start()
            acquired.wait(timeout=5)

            def release_after_delay() -> None:
                time.sleep(0.3)
                release.set()

            threading.Thread(target=release_after_delay, daemon=True).start()

            conn = sqlite3.connect(path, timeout=0.05)
            result = tk.execute_with_retry(
                conn, "UPDATE t SET count = count + 1 WHERE id = 1",
                max_retry=5, retry_wait_sec=0.2,
            )
            holder.join(timeout=5)
            self.assertTrue(result.ok, result.error)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class FetchTest(unittest.TestCase):
    def test_fetch_all_returns_rows(self):
        conn = make_conn()
        rows = tk.fetch_all(conn, "SELECT * FROM t")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "a")

    def test_fetch_all_empty_result_is_empty_list_not_none(self):
        conn = make_conn()
        rows = tk.fetch_all(conn, "SELECT * FROM t WHERE id = 999")
        self.assertEqual(rows, [])

    def test_fetch_all_query_failure_returns_none(self):
        conn = make_conn()
        rows = tk.fetch_all(conn, "SELECT * FROM nosuch")
        self.assertIsNone(rows)

    def test_fetch_one(self):
        conn = make_conn()
        row = tk.fetch_one(conn, "SELECT * FROM t WHERE id = ?", (1,))
        self.assertEqual(row["name"], "a")

    def test_fetch_one_no_match_is_none(self):
        conn = make_conn()
        self.assertIsNone(tk.fetch_one(conn, "SELECT * FROM t WHERE id = 999"))


class UpdateRecordTest(unittest.TestCase):
    def test_update_existing_record(self):
        conn = make_conn()
        result = tk.update_record(conn, "t", "id", 1, {"name": "b"})
        self.assertTrue(result.ok)
        self.assertEqual(result.reason, "success")
        self.assertEqual(conn.execute("SELECT name FROM t WHERE id=1").fetchone()[0], "b")

    def test_update_missing_record_is_not_found(self):
        conn = make_conn()
        result = tk.update_record(conn, "t", "id", 999, {"name": "b"})
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "not_found")

    def test_update_with_custom_where_for_optimistic_lock(self):
        conn = make_conn()
        conn.execute("ALTER TABLE t ADD COLUMN rev INTEGER NOT NULL DEFAULT 1")
        conn.commit()
        # rev が一致しない場合は not_found として弾かれる(楽観ロック)
        result = tk.update_record(
            conn, "t", "id", 1, {"name": "b"},
            where_clause="[id] = ? AND [rev] = ?", where_params=(1, 999),
        )
        self.assertEqual(result.reason, "not_found")


class InsertRecordTest(unittest.TestCase):
    def test_insert(self):
        conn = make_conn()
        result = tk.insert_record(conn, "t", {"id": 2, "name": "c", "count": 0})
        self.assertTrue(result.ok)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM t").fetchone()[0], 2)


class UtilityTest(unittest.TestCase):
    def test_now_db_string_format(self):
        import re

        value = tk.now_db_string()
        self.assertRegex(value, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_sanitize_for_db(self):
        self.assertEqual(tk.sanitize_for_db("a\tb\r\nc\n"), "a b c")


if __name__ == "__main__":
    unittest.main()
