"""access_bridge.odbc_backend と、access_backend設定による分岐のテスト。

実際のODBC接続(Windows専用)は使えないため、``dbkit.access_odbc.connect``
をこのモジュール内でだけ差し替えたフェイク接続で検証する。
"""
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dbkit import access_odbc
from nippou.access_bridge import importer, odbc_backend, pusher
from nippou.access_bridge.errors import ErrorKind
from nippou.access_bridge.runner import ScriptResult
from nippou.config import SETTINGS
from nippou.db.connection import connect
from nippou.db.models import DetailRecord, HeaderRecord
from nippou.db.repository import NippouRepository


class _FakeOdbcConnection:
    def __init__(self, *, query_rows=None, fail_query: Exception | None = None,
                 fail_statements: set[str] | None = None):
        self.query_rows = query_rows if query_rows is not None else []
        self.fail_query = fail_query
        self.fail_statements = fail_statements or set()
        self.executed: list[str] = []
        self.committed = False
        self.rolled_back = False

    def __enter__(self) -> "_FakeOdbcConnection":
        return self

    def __exit__(self, *exc_info) -> None:
        return None

    def query(self, sql: str):
        if self.fail_query is not None:
            raise self.fail_query
        return self.query_rows

    def execute(self, sql: str) -> int:
        if sql in self.fail_statements:
            raise access_odbc.AccessError(f"実行できませんでした: {sql}")
        self.executed.append(sql)
        return 1

    @contextmanager
    def transaction(self):
        try:
            yield self
        except Exception:
            self.rolled_back = True
            raise
        else:
            self.committed = True


class ImportTableOdbcTests(unittest.TestCase):
    def test_success_returns_rows(self) -> None:
        fake = _FakeOdbcConnection(query_rows=[{"品名": "A", "単位質量": "1.2"}])
        with patch.object(odbc_backend.access_odbc, "connect", return_value=fake) as mock_connect:
            result = odbc_backend.import_table_odbc(Path("m.accdb"), "資材重量")

        self.assertTrue(result.success)
        self.assertEqual(result.rows, [{"品名": "A", "単位質量": "1.2"}])
        # 読み取り専用で開いていること
        _, kwargs = mock_connect.call_args
        self.assertTrue(kwargs.get("read_only"))

    def test_sql_filter_is_appended_as_where_clause(self) -> None:
        fake = _FakeOdbcConnection(query_rows=[])
        captured_sql = {}

        def fake_query(sql):
            captured_sql["sql"] = sql
            return []
        fake.query = fake_query

        with patch.object(odbc_backend.access_odbc, "connect", return_value=fake):
            odbc_backend.import_table_odbc(Path("m.accdb"), "T", sql_filter="[品名]='X'")

        self.assertIn("WHERE [品名]='X'", captured_sql["sql"])

    def test_access_error_is_classified_and_returned(self) -> None:
        fake = _FakeOdbcConnection(fail_query=access_odbc.AccessError("他のユーザーがロックしています"))
        with patch.object(odbc_backend.access_odbc, "connect", return_value=fake):
            result = odbc_backend.import_table_odbc(Path("m.accdb"), "T")

        self.assertFalse(result.success)
        self.assertEqual(result.error.kind, ErrorKind.LOCK_CONFLICT)

    def test_unexpected_exception_does_not_propagate(self) -> None:
        with patch.object(odbc_backend.access_odbc, "connect", side_effect=RuntimeError("想定外")):
            result = odbc_backend.import_table_odbc(Path("m.accdb"), "T")

        self.assertFalse(result.success)
        self.assertEqual(result.error.kind, ErrorKind.UNKNOWN)


class PushStatementsOdbcTests(unittest.TestCase):
    def test_success_executes_all_statements_in_one_transaction(self) -> None:
        fake = _FakeOdbcConnection()
        with patch.object(odbc_backend.access_odbc, "connect", return_value=fake):
            result = odbc_backend.push_statements_odbc(Path("m.accdb"), ["DELETE FROM T", "INSERT INTO T VALUES (1)"])

        self.assertTrue(result.success)
        self.assertEqual(fake.executed, ["DELETE FROM T", "INSERT INTO T VALUES (1)"])
        self.assertTrue(fake.committed)
        self.assertFalse(fake.rolled_back)

    def test_failure_rolls_back_and_reports_error(self) -> None:
        fake = _FakeOdbcConnection(fail_statements={"INSERT INTO T VALUES (1)"})
        with patch.object(odbc_backend.access_odbc, "connect", return_value=fake):
            result = odbc_backend.push_statements_odbc(Path("m.accdb"), ["DELETE FROM T", "INSERT INTO T VALUES (1)"])

        self.assertFalse(result.success)
        self.assertTrue(fake.rolled_back)
        self.assertFalse(fake.committed)
        self.assertIsNotNone(result.error)


class BackendDispatchTests(unittest.TestCase):
    """``SETTINGS.access_backend`` の値に応じて呼び分けられることを確認する。

    【モジュールはその場で引き直す】
    このファイルの先頭で `import` したものを掴んだままにすると、
    **Web版のテストが `sys.modules` を入れ替えたあとで空振りします。**
    `tests/_web.py` は1件ごとに `nippou.*` / `app.*` を消して読み込み
    直すので、先に走ったWeb版のテストのあとでは、ここが持っている
    モジュールと `import_table` が実際に呼ぶモジュールが**別物**に
    なります(差し替えたつもりの関数が呼ばれず、0回で落ちる)。

    実行の順番で結果が変わるテストにしないため、ここでは毎回
    `sys.modules` から引き直します。
    """

    def setUp(self) -> None:
        self._original_backend = self.settings().access_backend

    def tearDown(self) -> None:
        object.__setattr__(self.settings(), "access_backend",
                           self._original_backend)

    # -- いま読み込まれているモジュールを引く ------------------------
    @staticmethod
    def settings():
        from nippou.config import SETTINGS as current

        return current

    @staticmethod
    def modules():
        from nippou.access_bridge import importer as imp
        from nippou.access_bridge import odbc_backend as odbc
        from nippou.access_bridge import pusher as push

        return imp, odbc, push

    def test_import_table_dispatches_to_odbc_backend_when_configured(self) -> None:
        importer, odbc_backend, _ = self.modules()
        object.__setattr__(self.settings(), "access_backend", "odbc")
        with patch.object(odbc_backend, "import_table_odbc") as mock_import:
            mock_import.return_value = importer.ImportResult(success=True, rows=[{"a": "1"}])
            result = importer.import_table(Path("m.accdb"), "T", sql_filter="X")

        mock_import.assert_called_once_with(Path("m.accdb"), "T", "X")
        self.assertTrue(result.success)

    def test_import_table_uses_vbscript_path_by_default(self) -> None:
        importer, odbc_backend, _ = self.modules()
        object.__setattr__(self.settings(), "access_backend", "vbscript")

        class AlwaysFailsRunner:
            def run(self, script_text: str) -> ScriptResult:
                return ScriptResult(success=False, err_desc="接続失敗")

        with patch.object(odbc_backend, "import_table_odbc") as mock_import:
            importer.import_table(Path("m.accdb"), "T", runner=AlwaysFailsRunner())

        mock_import.assert_not_called()

    def test_push_one_dispatches_to_odbc_backend_when_configured(self) -> None:
        _, odbc_backend, pusher = self.modules()
        from nippou.db.connection import connect
        from nippou.db.models import DetailRecord, HeaderRecord
        from nippou.db.repository import NippouRepository

        object.__setattr__(self.settings(), "access_backend", "odbc")
        with tempfile.TemporaryDirectory() as tmpdir:
            conn = connect(Path(tmpdir) / "test.sqlite3")
            repo = NippouRepository(conn)
            header = HeaderRecord(report_date="2026年8月3日", line="L-1", shift="1直", page=1, worker="山田")
            details = [DetailRecord(report_date=header.report_date, line="L-1", shift="1直", page=1, row_no=1)]
            repo.save(header, details)

            with patch.object(odbc_backend, "push_statements_odbc") as mock_push:
                mock_push.return_value = ScriptResult(success=True)
                summary = pusher.push_pending(repo, Path("/dummy.accdb"))

            mock_push.assert_called_once()
            self.assertEqual(len(summary.succeeded), 1)
            conn.close()


if __name__ == "__main__":
    unittest.main()
