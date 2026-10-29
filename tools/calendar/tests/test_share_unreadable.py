"""共有を「読めなかった」ことを「無い・空・まだ送っていない」と取り違えない

【起きたこと(別のツール)】
共有ファイルが一瞬読めなかった(ネットワークが切れた・ロック中・権限・開けない)のを
「実績の表が無い」と読み、手元の実績を送り直していた。追記で送るので、共有側に
同じ行が大量にできた。

このツールで同じ形をしていた所を、1つずつ塞いだことを確かめる。原則は

* **読めなかった と 本当に無い/空 を分ける**
* 読めなかったときは 送らない・作らない・送った印(送信ID)を捨てない・手元を消さない
  (次の回にやり直す)。理由は送れない理由として画面とログに出る

読めない・書けない状態は、``SourceConnection`` の ``query`` / ``execute`` に
例外を差し込んで作る(共有フォルダを本当に切るのは試験ではできない)。
"""

from __future__ import annotations

import datetime as _dt
import os
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Optional
from unittest import mock

from . import _web
from calendar_app import (
    access_control, config, db, importer, master_admin, settings as user_settings,
    terminals,
)
from calendar_app.dbkit import outbox_sync, source_db
from calendar_app.repository import Repository
from calendar_app.sync import business_rules
from calendar_app.sync import deletes as sync_deletes
from calendar_app.sync import specs as sync_specs
from calendar_app.sync import total_pending_count
from calendar_app.sync.autosync import SqliteTransport, TransportError

D = _dt.date(2026, 8, 3)
DS = "2026/08/03"
OP = outbox_sync.DEFAULT_OP_ID_COLUMN


@contextmanager
def share_fails(*, query: Optional[Callable[..., bool]] = None,
                execute: Optional[Callable[..., bool]] = None,
                message: str = "database is locked"):
    """共有への問い合わせ(``query``)・書き込み(``execute``)のうち、条件に合う SQL だけ落とす。

    落とし方は ``SourceConnection`` が実際に投げるのと同じ ``SourceError``(やり直しを
    使い切ったあとの形)。``insert`` / ``update`` / ``insert_unless`` 以外の書き込みは
    ``execute`` を通るので、そこで止まる。条件には SQL を渡す(``params=`` を受ける
    条件には値も渡す)。
    """
    real_query = source_db.SourceConnection.query
    real_execute = source_db.SourceConnection.execute

    def hit(rule, sql, params) -> bool:
        if rule is None:
            return False
        try:
            return bool(rule(sql, params=tuple(params)))
        except TypeError:
            return bool(rule(sql))

    def fake_query(self, sql, params=(), **kwargs):
        if hit(query, sql, params):
            raise source_db.SourceError(message)
        return real_query(self, sql, params, **kwargs)

    def fake_execute(self, sql, params=(), **kwargs):
        if hit(execute, sql, params):
            raise source_db.SourceError(message)
        return real_execute(self, sql, params, **kwargs)

    with mock.patch.object(source_db.SourceConnection, "query", fake_query), \
            mock.patch.object(source_db.SourceConnection, "execute", fake_execute):
        yield


def _shape_of(table: str) -> Callable[..., bool]:
    """その表の形を見る問い合わせ(列・索引)。**ほかの表は読める。**"""
    def rule(sql: str, params: tuple = ()) -> bool:
        if "table_info" in sql:
            return table in sql
        return "type='index'" in sql and any(table in str(p) for p in params)
    return rule


class _Base(unittest.TestCase):
    """手元のDB(ファイル)と、本物の取り込み元を1組。"""

    def setUp(self) -> None:
        _web.reset_sync()
        self.addCleanup(_web.reset_sync)
        outbox_sync.reset_op_id_cache()
        self.addCleanup(outbox_sync.reset_op_id_cache)
        business_rules.reset_added_column_cache()
        self.addCleanup(business_rules.reset_added_column_cache)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.data_db = _web.make_source(
            self.folder, config.SOURCE_FILE_DATA, _web.SOURCE_SCHEMA)
        self.conn = db.connect(self.folder / "local.sqlite3")
        self.addCleanup(self.conn.close)
        self.repo = Repository(self.conn)

    def send(self) -> None:
        SqliteTransport().send(self.conn, str(self.data_db))

    def try_send(self) -> str:
        """送ってみる。送れなかった理由(送れたら空)。"""
        try:
            self.send()
        except TransportError as exc:
            return str(exc)
        return ""

    def shared(self, text: str) -> list[dict]:
        return [r for r in _web.read_source(self.data_db, config.TABLE_DATA)
                if r["登録内容"] == text]

    def arrived_but_not_marked(self, text: str) -> int:
        """**共有へ届いたが、手元に「済」を書く前に落ちた**行を1つ作る。

        送信の予約(送信中・送信ID)は残り、10分たつと同じ送信IDで送り直される
        (``outbox_sync.claim_rows`` の期限切れ拾い直し)。
        """
        rid = self.repo.save_record(D, config.KUBUN_OTHER, text, "-",
                                    group="B", line="L-1")
        rows, op_ids = outbox_sync.claim_rows(self.conn, sync_specs.DATA_SPEC)
        row = next(r for r in rows if int(r["ID"]) == rid)
        values = {k: row[k] for k in row.keys() if k != "ID"}
        values[OP] = op_ids[rid]
        with source_db.connect(self.data_db, read_only=False) as source:
            source.insert(config.TABLE_DATA, values)
        # 予約から10分を超えた(アプリが落ちて、次に起動した)
        self.conn.execute(
            f'UPDATE "{outbox_sync.SYNC_LOG_TABLE}" '
            "SET 同期日時 = datetime('now','localtime','-11 minutes')")
        self.conn.commit()
        return rid


# ---------------------------------------------------------------------------
# 1. 列を読めない ≠ 表が無い
# ---------------------------------------------------------------------------
class ColumnsTests(_Base):

    def test_列を読めないときは空と答えない(self) -> None:
        with source_db.connect(self.data_db, read_only=True) as source, \
                share_fails(query=lambda sql: "table_info" in sql):
            with self.assertRaises(source_db.SourceError):
                source.columns(config.TABLE_DATA)

    def test_本当に無い表は空のまま(self) -> None:
        """こちらは今までどおり(初めての共有フォルダ)。"""
        with source_db.connect(self.data_db, read_only=True) as source:
            self.assertEqual(source.columns("まだ無い表"), [])


# ---------------------------------------------------------------------------
# 2. 送信ID(二重に入れない仕組み)を、読めなかったせいで外さない
# ---------------------------------------------------------------------------
class OpIdGuardTests(_Base):

    def setUp(self) -> None:
        super().setUp()
        # ふだんの1回。送信ID列と一意索引がここで共有にできる
        self.repo.save_record(D, config.KUBUN_OTHER, "最初の連絡", "-",
                              group="B", line="L-1")
        self.send()

    def test_形を読めない間は送信IDなしで送り直さない(self) -> None:
        """読めなかった → 「送信ID列が無い」と見て、送信IDを付けずに送り直していた。

        送信ID が付いていれば共有の一意索引が二度目を弾くが、付けずに送ると
        **届いていた連絡がもう1行できた**。
        """
        self.arrived_but_not_marked("届いていた連絡")
        with share_fails(query=_shape_of(config.TABLE_DATA),
                         execute=lambda sql: sql.startswith(("ALTER", "CREATE UNIQUE"))):
            why = self.try_send()
        self.assertEqual(len(self.shared("届いていた連絡")), 1, "読めない間に送り直して重複した")
        self.assertIn("読めません", why)
        self.assertEqual(total_pending_count(self.conn), 1, "送信待ちから消えた")

        # 読めるようになったら、同じ送信IDで送る → 共有が弾く → 送れた扱い
        self.send()
        self.assertEqual(len(self.shared("届いていた連絡")), 1)
        self.assertEqual(total_pending_count(self.conn), 0)

    def test_送り直しに失敗しても送信IDを捨てない(self) -> None:
        """送り直しが一時的に失敗したら、予約ごと消して**新しい送信ID**で送っていた。

        前の送信が届いていた行は、新しい送信IDだと共有の一意索引をすり抜けて2行になる。
        """
        self.arrived_but_not_marked("届いていた連絡")
        with share_fails(execute=lambda sql: (sql.startswith("INSERT")
                                              and config.TABLE_DATA in sql)):
            self.assertTrue(self.try_send())
        self.send()
        self.assertEqual(len(self.shared("届いていた連絡")), 1, "新しい送信IDで送り直して重複した")
        self.assertEqual(total_pending_count(self.conn), 0)

    def test_一度も送っていない行は失敗しても次に送れる(self) -> None:
        """今までどおり: 届いていないと分かっている行は、次の回に1行だけ届く。"""
        self.repo.save_record(D, config.KUBUN_OTHER, "まだの連絡", "-",
                              group="B", line="L-1")
        with share_fails(execute=lambda sql: (sql.startswith("INSERT")
                                              and config.TABLE_DATA in sql)):
            self.assertTrue(self.try_send())
        self.assertEqual(self.shared("まだの連絡"), [])
        self.send()
        self.assertEqual(len(self.shared("まだの連絡")), 1)
        self.assertEqual(total_pending_count(self.conn), 0)


class OpIdCacheTests(_Base):
    """一時的な失敗を「作れない相手」として覚えない。"""

    def test_一時的な失敗のあとで送信ID列を作れる(self) -> None:
        with source_db.connect(self.data_db, read_only=False) as source:
            with share_fails(execute=lambda sql: sql.startswith("ALTER"),
                             message="disk I/O error"):
                try:
                    outbox_sync.ensure_op_id_column(source, sync_specs.DATA_SPEC)
                except source_db.SourceError:
                    pass
            # 前は「作れない」と覚え、再起動するまで送信IDなしで送り続けた
            self.assertTrue(outbox_sync.ensure_op_id_column(source, sync_specs.DATA_SPEC))
            self.assertIn(OP, source.columns(config.TABLE_DATA))

    def test_一時的な失敗のあとで後から足した列を足せる(self) -> None:
        """前は瞬断を「足せない」と覚え、再起動するまで記入班・登録日時を省いて送った。"""
        with source_db.connect(self.data_db, read_only=False) as source:
            with share_fails(execute=lambda sql: sql.startswith("ALTER"),
                             message="disk I/O error"):
                business_rules.missing_added_columns(source, config.TABLE_DATA)
            self.assertEqual(
                business_rules.missing_added_columns(source, config.TABLE_DATA),
                frozenset(), "一時的に足せなかった列を、ずっと省いて送る")
            present = source.columns(config.TABLE_DATA)
            for name in config.DATA_ADDED_COLUMNS:
                self.assertIn(name, present)


# ---------------------------------------------------------------------------
# 3. 削除: 送信ID列を読めないときに、同じ文面の別の行を消さない
# ---------------------------------------------------------------------------
class DeleteTests(_Base):

    def test_送信ID列を読めないときは自然キーで消さない(self) -> None:
        source = source_db.connect(self.data_db, read_only=False)
        self.addCleanup(source.close)
        outbox_sync.ensure_op_id_column(source, sync_specs.DATA_SPEC)
        source.insert(config.TABLE_DATA, {
            "日付": DS, "区分": config.KUBUN_OTHER, "登録内容": "残業なし",
            "識別コード": "-", "班": "B", "ライン": "L-1", OP: "ほかの端末の連絡"})
        # 送っている最中に消した連絡(届いていなかった)。送信IDでだけ当てるはず
        db.queue_pending_delete(self.conn, config.TABLE_DATA, access_id=None, natural_key={
            "日付": DS, "区分": config.KUBUN_OTHER, "登録内容": "残業なし",
            "識別コード": "-", "班": "B", "ライン": "L-1",
            sync_deletes.OP_ID_KEY: "届いていない送信ID"})
        self.conn.commit()

        with share_fails(query=lambda sql: "table_info" in sql):
            sent, errors = sync_deletes.forward_pending_deletes(
                self.conn, source, config.TABLE_DATA)
        self.assertEqual(len(self.shared("残業なし")), 1, "ほかの端末の同じ文面の連絡が消えた")
        self.assertEqual(sent, 0)
        self.assertTrue(errors)
        self.assertEqual(len(db.pending_deletes(self.conn)), 1, "削除の予約が消えた")

        # 読めるようになれば送信IDで当てる(何にも当たらない = 正しい)
        sent, errors = sync_deletes.forward_pending_deletes(
            self.conn, source, config.TABLE_DATA)
        self.assertEqual((sent, errors), (1, []))
        self.assertEqual(len(self.shared("残業なし")), 1)


# ---------------------------------------------------------------------------
# 4. 端末一覧: 列を読めないときに表を作り直さない
# ---------------------------------------------------------------------------
class TerminalsTests(_Base):

    def tables(self) -> list[str]:
        conn = sqlite3.connect(self.data_db)
        try:
            return [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
        finally:
            conn.close()

    def test_列を読めないときは作り直さない(self) -> None:
        with source_db.connect(self.data_db, read_only=False) as source:
            self.assertTrue(terminals.report(source))
        before = _web.read_source(self.data_db, terminals.TABLE)

        with source_db.connect(self.data_db, read_only=False) as source, \
                share_fails(query=lambda sql: ("table_info" in sql
                                               and terminals.TABLE in sql)):
            self.assertFalse(terminals.report(source, force=True))
        self.assertNotIn(f"{terminals.TABLE}_作り直し", self.tables(),
                         "読めなかっただけで、端末一覧を作り直しにかかった")
        self.assertEqual(_web.read_source(self.data_db, terminals.TABLE), before)


# ---------------------------------------------------------------------------
# 5. 取り込み: 送信待ちを数えられないときに、手元を入れ替えない
# ---------------------------------------------------------------------------
class ImportTests(_Base):

    def test_送信待ちを数えられないときは入れ替えない(self) -> None:
        """数えられない → 「0件」と読んで総入れ替えし、まだ送っていない連絡が消えていた。"""
        self.repo.save_record(D, config.KUBUN_OTHER, "まだ送っていない連絡", "-",
                              group="B", line="L-1")
        with mock.patch.object(outbox_sync, "pending_rows",
                               side_effect=sqlite3.OperationalError("disk I/O error")):
            with self.assertRaises(sqlite3.Error):
                importer.import_source(self.conn, self.data_db)
        rows = self.conn.execute(
            f'SELECT "登録内容" FROM "{config.TABLE_DATA}"').fetchall()
        self.assertEqual([r[0] for r in rows], ["まだ送っていない連絡"])
        self.assertEqual(total_pending_count(self.conn), 1)


# ---------------------------------------------------------------------------
# 6. 書き込みで開くとき、無いファイルを作らない
# ---------------------------------------------------------------------------
class OpenForWriteTests(_Base):

    def test_見えた直後に消えたファイルを作らない(self) -> None:
        """「ある」と見てから開くまでに消えた(差し替え・共有の瞬断)。

        前は sqlite3 が**空の新しい共有DBを作り**、そこへ端末一覧などを書き始めた。
        """
        gone = self.folder / "消えた連絡帳.sqlite3"
        with mock.patch("pathlib.Path.exists", return_value=True):
            with self.assertRaises(source_db.SourceError):
                source_db.connect(gone, read_only=False)
        self.assertFalse(os.path.exists(gone), "無い共有DBを新しく作った")

    def test_ふだんの共有DBは書ける(self) -> None:
        with source_db.connect(self.data_db, read_only=False) as source:
            self.assertFalse(source.read_only)


# ---------------------------------------------------------------------------
# 7. マスタ確認: 読めないときに「まだ1件もありません」と言わない・足さない
# ---------------------------------------------------------------------------
class MasterAdminTests(unittest.TestCase):

    def setUp(self) -> None:
        _web.reset_sync()
        self.addCleanup(_web.reset_sync)
        self.conn = _web.bind_db(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name)
        self.master_db = _web.make_source(
            folder, config.SOURCE_FILE_MASTER,
            _web.MASTER_SCHEMA + access_control.SOURCE_SCHEMA + ";")
        conn = sqlite3.connect(self.master_db)
        conn.execute(f'INSERT INTO "{access_control.TABLE}" ("ログインID","権限") '
                     "VALUES ('yamada', ?)", (config.ALL_LINE_NAMES[0],))
        conn.commit()
        conn.close()
        for key in (user_settings.KEY_MASTER_DB_DIR, user_settings.KEY_DATA_DB_DIR):
            user_settings.set_value(key, str(folder))
            self.addCleanup(user_settings.set_value, key, "")
        # 管理者パスワードの関門はここでは確かめない(別の試験の受け持ち)
        guard = mock.patch("calendar_app.admin_password.guard", return_value=None)
        guard.start()
        self.addCleanup(guard.stop)

    def rows(self) -> list[dict]:
        return _web.read_source(self.master_db, access_control.TABLE)

    def test_列を読めないときは空と言わない(self) -> None:
        with share_fails(query=lambda sql: "table_info" in sql):
            view = master_admin.page(access_control.TABLE)
        self.assertFalse(view.editable, "読めないのに直せる画面を出した")
        self.assertNotIn("まだ1件もありません", view.why)
        self.assertIn("読めません", view.why)

    def test_行を読めないときは空の一覧で直させない(self) -> None:
        with share_fails(query=lambda sql: sql.startswith("SELECT *")
                         and access_control.TABLE in sql):
            view = master_admin.page(access_control.TABLE)
        self.assertFalse(view.editable, "読めないのに 0 件の一覧を直せる画面で出した")
        self.assertIn("読めません", view.why)

    def test_列を読めないときは行を足さない(self) -> None:
        """「まだ無い」と見て定義どおりの列で足していた ── 見えなかった行がもう1行できる。"""
        with share_fails(query=lambda sql: "table_info" in sql):
            result = master_admin.add_row(self.conn, access_control.TABLE, {
                "ログインID": "yamada", "権限": config.ALL_LINE_NAMES[0], "有効": "1"})
        self.assertFalse(result.ok)
        self.assertIn("読めません", result.message)
        self.assertEqual(len(self.rows()), 1, "読めない間に行を足した")


if __name__ == "__main__":
    unittest.main()
