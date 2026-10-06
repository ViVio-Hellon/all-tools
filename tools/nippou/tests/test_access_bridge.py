import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.access_bridge import script_gen
from nippou.access_bridge.errors import ErrorKind, classify_error, is_lock_error
from nippou.access_bridge.lock import ConcurrencyStatus, has_laccdb_lock, is_book_opened, laccdb_path
from nippou.access_bridge.runner import ScriptResult, ScriptRunner, run_with_retry


class ErrorClassificationTests(unittest.TestCase):
    def test_known_lock_error_numbers(self) -> None:
        for num in (-2147467259, -2147217887, 3260, 3261, 3045, 3050, 3218):
            self.assertTrue(is_lock_error(num, ""), num)

    def test_lock_keyword_in_description(self) -> None:
        self.assertTrue(is_lock_error(999, "record is locked"))
        self.assertTrue(is_lock_error(999, "テーブルがロックされています"))

    def test_non_lock_error(self) -> None:
        self.assertFalse(is_lock_error(-2147217900, "syntax error in query"))

    def test_classify_driver_mismatch(self) -> None:
        err = classify_error(-2147221164, "Class not registered")
        self.assertEqual(err.kind, ErrorKind.DRIVER_MISMATCH)

    def test_classify_network_unreachable(self) -> None:
        err = classify_error(52, "network path was not found")
        self.assertEqual(err.kind, ErrorKind.NETWORK_UNREACHABLE)

    def test_classify_sql_syntax(self) -> None:
        err = classify_error(-2147217900, "Syntax error in FROM clause")
        self.assertEqual(err.kind, ErrorKind.SQL_SYNTAX)

    def test_classify_unknown_falls_back(self) -> None:
        err = classify_error(1, "something else entirely")
        self.assertEqual(err.kind, ErrorKind.UNKNOWN)


class LockDetectionTests(unittest.TestCase):
    def test_laccdb_sibling_path(self) -> None:
        self.assertEqual(laccdb_path(Path("/x/db.accdb")), Path("/x/db.laccdb"))

    def test_has_laccdb_lock_reflects_file_presence(self, tmp_path: Path | None = None) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            accdb = Path(d) / "db.accdb"
            accdb.touch()
            self.assertFalse(has_laccdb_lock(accdb))
            laccdb_path(accdb).touch()
            self.assertTrue(has_laccdb_lock(accdb))

    def test_is_book_opened_false_for_missing_file(self) -> None:
        self.assertFalse(is_book_opened(Path("/nonexistent/path/db.accdb")))

    def test_is_book_opened_false_when_writable(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            accdb = Path(d) / "db.accdb"
            accdb.write_bytes(b"stub")
            self.assertFalse(is_book_opened(accdb))

    def test_concurrency_status_message(self) -> None:
        self.assertIn("使用中", ConcurrencyStatus(laccdb_present=False, exclusively_locked=True).message())
        self.assertIn("開いています", ConcurrencyStatus(laccdb_present=True, exclusively_locked=False).message())
        self.assertEqual(ConcurrencyStatus(laccdb_present=False, exclusively_locked=False).message(), "")


class ScriptGenTests(unittest.TestCase):
    def test_sql_literal_text_escapes_quotes(self) -> None:
        self.assertEqual(script_gen.sql_literal("O'Brien", "TEXT"), "'O''Brien'")

    def test_sql_literal_number_passthrough(self) -> None:
        self.assertEqual(script_gen.sql_literal("42", "NUMBER"), "42")

    def test_sql_literal_blank_number_is_null(self) -> None:
        self.assertEqual(script_gen.sql_literal("", "NUMBER"), "NULL")

    def test_sql_literal_date_wraps_in_hashes(self) -> None:
        self.assertEqual(script_gen.sql_literal("2026-08-03", "DATE"), "#2026-08-03#")

    def test_import_script_contains_select_and_output_path(self) -> None:
        text = script_gen.build_import_script("C:\\db.accdb", "T_日報ヘッダー_L-1", "C:\\out.csv")
        self.assertIn("SELECT * FROM [T_日報ヘッダー_L-1]", text)
        self.assertIn("C:\\out.csv", text)
        self.assertIn("ERRCODE=", text)

    def test_push_script_embeds_all_statements(self) -> None:
        statements = ["DELETE FROM [T] WHERE 1=1", "INSERT INTO [T] VALUES (1)"]
        text = script_gen.build_push_script("C:\\db.accdb", statements)
        for stmt in statements:
            self.assertIn(stmt, text)
        self.assertIn("BeginTrans", text)
        self.assertIn("CommitTrans", text)
        self.assertIn("RollbackTrans", text)

    def test_generated_scripts_have_balanced_quotes(self) -> None:
        # A quick sanity check that VBScript string literals aren't
        # accidentally left unterminated by embedded quote characters.
        text = script_gen.build_push_script("C:\\db.accdb", ["INSERT INTO [T] ([A]) VALUES ('it''s')"])
        self.assertEqual(text.count('"') % 2, 0)


class FakeCompletedProcess:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class RunnerTests(unittest.TestCase):
    def test_parses_success_output(self) -> None:
        runner = ScriptRunner(executor=lambda path, timeout: FakeCompletedProcess("OK:5\n"))
        result = runner.run("Option Explicit")
        self.assertTrue(result.success)
        self.assertEqual(result.rows, 5)

    def test_parses_error_output(self) -> None:
        runner = ScriptRunner(
            executor=lambda path, timeout: FakeCompletedProcess("ERRCODE=-2147467259|ERRDESC=lock timeout\n", returncode=1)
        )
        result = runner.run("Option Explicit")
        self.assertFalse(result.success)
        self.assertEqual(result.err_number, -2147467259)
        self.assertEqual(result.err_desc, "lock timeout")

    def test_missing_cscript_reported_cleanly(self) -> None:
        def raise_not_found(path: str, timeout: int):
            raise FileNotFoundError()

        runner = ScriptRunner(executor=raise_not_found)
        result = runner.run("Option Explicit")
        self.assertFalse(result.success)
        self.assertIn("cscript", result.err_desc)

    def test_timeout_reported_cleanly(self) -> None:
        def raise_timeout(path: str, timeout: int):
            raise subprocess.TimeoutExpired(cmd="cscript", timeout=timeout)

        runner = ScriptRunner(executor=raise_timeout)
        result = runner.run("Option Explicit")
        self.assertFalse(result.success)
        self.assertIn("タイムアウト", result.err_desc)

    def test_retries_only_on_lock_conflict(self) -> None:
        attempts: list[int] = []

        def flaky_runner_run(script_text: str) -> ScriptResult:
            attempts.append(1)
            if len(attempts) < 3:
                return ScriptResult(success=False, err_number=-2147467259, err_desc="lock")
            return ScriptResult(success=True)

        class StubRunner:
            run = staticmethod(flaky_runner_run)

        sleeps: list[float] = []
        result = run_with_retry(
            lambda: "script", runner=StubRunner(), max_retry=5, base_wait_sec=1.0, sleep=sleeps.append
        )
        self.assertTrue(result.success)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_gives_up_after_max_retry(self) -> None:
        class AlwaysLockedRunner:
            def run(self, script_text: str) -> ScriptResult:
                return ScriptResult(success=False, err_number=-2147467259, err_desc="lock")

        sleeps: list[float] = []
        result = run_with_retry(
            lambda: "script", runner=AlwaysLockedRunner(), max_retry=2, base_wait_sec=0.1, sleep=sleeps.append
        )
        self.assertFalse(result.success)
        self.assertEqual(result.attempts, 3)  # initial try + 2 retries

    def test_unexpected_oserror_from_executor_is_not_raised(self) -> None:
        # 「他ラインを落とさない」ための最終防衛ライン: 権限エラーや
        # 共有ネットワークドライブの一時切断など、FileNotFoundError /
        # TimeoutExpired 以外のOSErrorでもクラッシュせず失敗を返すこと。
        def raise_permission_error(path: str, timeout: int):
            raise PermissionError("アクセスが拒否されました")

        runner = ScriptRunner(executor=raise_permission_error)
        result = runner.run("Option Explicit")  # 例外を投げてはいけない
        self.assertFalse(result.success)
        self.assertIn("予期しないエラー", result.err_desc)

    def test_run_with_retry_survives_script_builder_exception(self) -> None:
        # SQL文組み立て(script_gen呼び出し)自体が想定外の例外を投げても、
        # run_with_retry の外へは伝播させない。
        def broken_builder() -> str:
            raise ValueError("組み立て失敗（想定外のデータ型など）")

        runner = ScriptRunner(executor=lambda path, timeout: FakeCompletedProcess("OK\n"))
        result = run_with_retry(broken_builder, runner=runner)
        self.assertFalse(result.success)
        self.assertIn("スクリプト生成に失敗しました", result.err_desc)

    def test_run_with_retry_survives_runner_exception(self) -> None:
        # runner.run 自体が（テストダブルや将来の別実装で）例外を投げる
        # ケースでも run_with_retry は例外を外に漏らさない。
        class ExplodingRunner:
            def run(self, script_text: str) -> ScriptResult:
                raise RuntimeError("想定外の異常")

        result = run_with_retry(lambda: "script", runner=ExplodingRunner())
        self.assertFalse(result.success)
        self.assertIn("スクリプト実行に失敗しました", result.err_desc)

    def test_does_not_retry_non_lock_errors(self) -> None:
        class SyntaxErrorRunner:
            def run(self, script_text: str) -> ScriptResult:
                return ScriptResult(success=False, err_number=1, err_desc="syntax error")

        sleeps: list[float] = []
        result = run_with_retry(lambda: "script", runner=SyntaxErrorRunner(), max_retry=5, sleep=sleeps.append)
        self.assertFalse(result.success)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(sleeps, [])


if __name__ == "__main__":
    unittest.main()
