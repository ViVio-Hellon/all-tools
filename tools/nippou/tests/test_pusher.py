import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.access_bridge import pusher
from nippou.access_bridge.runner import ScriptResult
from nippou.db.connection import connect
from nippou.db.models import DetailRecord, HeaderRecord
from nippou.db.repository import NippouRepository


class SafeLineNameTests(unittest.TestCase):
    def test_replaces_unsafe_characters(self) -> None:
        self.assertEqual(pusher.safe_line_name("L[1]"), "L(1)")
        self.assertEqual(pusher.safe_line_name("A.B!C`D"), "A_B_C_D")

    def test_blank_becomes_unknown(self) -> None:
        self.assertEqual(pusher.safe_line_name("   "), "Unknown")

    def test_table_name_templates(self) -> None:
        self.assertEqual(pusher.header_table_name("L-1"), "T_日報ヘッダー_L-1")
        self.assertEqual(pusher.detail_table_name("L-1"), "T_日報明細_L-1")


class StatementBuilderTests(unittest.TestCase):
    def test_header_statements_delete_then_insert(self) -> None:
        header = HeaderRecord(report_date="2026年8月3日", line="L-1", shift="1直", page=1, worker="山田")
        statements = pusher.build_header_statements(header)
        self.assertEqual(len(statements), 2)
        self.assertTrue(statements[0].startswith("DELETE FROM [T_日報ヘッダー_L-1]"))
        self.assertTrue(statements[1].startswith("INSERT INTO [T_日報ヘッダー_L-1]"))
        self.assertIn("'山田'", statements[1])

    def test_detail_statements_one_delete_plus_one_insert_per_row(self) -> None:
        header = HeaderRecord(report_date="2026年8月3日", line="L-1", shift="1直", page=1)
        details = [
            DetailRecord(report_date=header.report_date, line=header.line, shift=header.shift, page=1, row_no=i, lot=f"LOT{i}")
            for i in range(1, 4)
        ]
        statements = pusher.build_detail_statements(header, details)
        self.assertEqual(len(statements), 1 + 3)
        self.assertTrue(statements[0].startswith("DELETE FROM [T_日報明細_L-1]"))
        self.assertIn("'LOT1'", statements[1])
        self.assertIn("'LOT3'", statements[3])

    def test_quotes_in_data_are_escaped(self) -> None:
        header = HeaderRecord(report_date="2026年8月3日", line="L-1", shift="1直", page=1, worker="O'Brien")
        statements = pusher.build_header_statements(header)
        self.assertIn("'O''Brien'", statements[1])


class PushPendingResilienceTests(unittest.TestCase):
    """「他ラインを落とさない」ための耐障害性: 反映対象キーの1件が
    想定外の例外で失敗しても、残りのキーの反映処理・呼び出し元プロセスは
    継続することを検証する（複数ラインが同じAccessファイルへ同時に
    Pushしうる運用を想定）。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmpdir.name) / "test.sqlite3")
        self.repo = NippouRepository(self.conn)
        # 2直分の未反映(dirty)ヘッダーを用意する。
        for shift in ("1直", "2直"):
            header = HeaderRecord(report_date="2026年8月3日", line="L-1", shift=shift, page=1)
            details = [
                DetailRecord(report_date=header.report_date, line="L-1", shift=shift, page=1, row_no=i)
                for i in range(1, 13)
            ]
            self.repo.save(header, details)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmpdir.cleanup()

    def test_one_key_raising_unexpected_exception_does_not_block_others(self) -> None:
        call_count = {"n": 0}

        class FlakyRunner:
            """1回目の呼び出しでだけ想定外の例外を投げるダミーrunner。"""

            def run(self, script_text: str) -> ScriptResult:
                call_count["n"] += 1
                if call_count["n"] == 1:
                    raise RuntimeError("想定外の異常（例: ファイルシステムの一時的な不調）")
                return ScriptResult(success=True)

        summary = pusher.push_pending(self.repo, Path("/dummy/does-not-exist.accdb"), runner=FlakyRunner())

        # push_pending 自体は例外を投げず、成功1件・失敗1件のサマリを返す。
        self.assertEqual(len(summary.succeeded), 1)
        self.assertEqual(len(summary.failed), 1)
        # 失敗したキーだけ dirty のまま残り、成功したキーは同期済みになる。
        remaining = {h.key() for h in self.repo.pending_sync_headers()}
        self.assertEqual(len(remaining), 1)

    def test_repo_load_exception_for_one_key_does_not_block_others(self) -> None:
        # repo.load 自体が例外を投げるケース(SQLite側の想定外の不調)も、
        # push_pending のループレベルで捕捉されることを確認する。
        original_load = self.repo.load
        call_count = {"n": 0}

        def flaky_load(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("ローカルDB読み込みで想定外のエラー")
            return original_load(*args, **kwargs)

        self.repo.load = flaky_load  # type: ignore[method-assign]

        class AlwaysSucceedsRunner:
            def run(self, script_text: str) -> ScriptResult:
                return ScriptResult(success=True)

        summary = pusher.push_pending(self.repo, Path("/dummy/does-not-exist.accdb"), runner=AlwaysSucceedsRunner())
        self.assertEqual(len(summary.succeeded), 1)
        self.assertEqual(len(summary.failed), 1)


if __name__ == "__main__":
    unittest.main()
