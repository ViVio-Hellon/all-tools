"""共有側を「読めなかった」と「本当に無い・空」を取り違えない。

別のツールで、共有ファイルを読めなかった(ネットワークが一瞬切れた・ロック中・権限・開けない)のを
「実績の表が無い」と取り違えて手元の実績を送り直し、共有側に重複を大量に作った事案があった。
看板でも同じ形が起きないかを、読めない状況を patch で作って確かめる。

* 写しが 0 バイト(空)に見えたのを「表が 1 つも無い DB」として読まない(:meth:`SharedDb.local_copy`)
  ── 取り込みが全ラインを「表が無い」にし、アクセス権限が空の結果を「前回読めた内容」として覚えていた
* 看板履歴のファイルを確かめられないときに「無い」として作りに行かない(:func:`history.create_if_missing`)
* 書き戻しで、共有DBを一時的に開けない・ロック中・理由を読めない を、諦め(要確認)の回数に数えない
* 看板履歴へ送った印は、共有に見えなくても外さない・送り直しても重複しない(追記なので最重要)
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kanban import access_control, config
from kanban.db import history, sync
from kanban.db.shared import SharedDb, SharedDbError, StatementResult
from kanban.db.store import MAX_SYNC_ATTEMPTS, Store
from kanban.domain import models

from test_web_app import HAS_FLASK, TEST_ADMIN_PASSWORD, RouteTestBase


def _make_shared(path: Path) -> None:
    """看板_LVC(看板 2 枚)と Form状態管理 がある看板マスタ。"""
    c = sqlite3.connect(str(path))
    c.execute("CREATE TABLE [看板_LVC] ([管理番号] TEXT, [資材] TEXT, [サイズ] TEXT, [欲] TEXT, [不] TEXT,"
              " [更新日] TEXT, [発送] TEXT, [倉庫確認日時] TEXT, [常設品] TEXT, [保留] TEXT, [注文中日時] TEXT)")
    c.executemany("INSERT INTO [看板_LVC] VALUES (?, '外装紙', ?, '', '〇', '', '', '', '〇', '', '')",
                  [("1", "A"), ("2", "B")])
    c.execute("CREATE TABLE [Form状態管理] ([管理番号] INTEGER, [ライン名] TEXT, [状態] TEXT)")
    c.commit()
    c.close()


def _empty_copy(_self, _source, target) -> None:
    """共有ファイルが一瞬空に見えた(入れ替え・コピーの途中、届きかけで切れた)ときの写し。"""
    Path(target).write_bytes(b"")


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="kanban_unreadable_"))
        self.store = Store(str(self.dir / "local.sqlite3"), host_name="PC-1")
        self.store.ensure_schema()
        self.shared = self.dir / "看板マスタ.sqlite3"
        _make_shared(self.shared)
        self.gateway = SharedDb(str(self.shared), cache_dir=str(self.dir / "cache"))

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 写しが空に見えた
# ---------------------------------------------------------------------------
class EmptyCopyIsUnreadableTest(Fixture):
    def test_an_empty_copy_is_not_read_as_a_db_without_tables(self):
        """0 バイトの写しを「表が 1 つも無い」と読まず、読めなかったと言う。"""
        with mock.patch.object(SharedDb, "_copy_files", _empty_copy):
            with self.assertRaises(SharedDbError) as caught:
                self.gateway.table_names()
        self.assertIn("0 バイト", str(caught.exception))

    def test_import_does_not_call_tables_missing_when_the_copy_was_empty(self):
        """空に見えた回の取り込みは「表が無い」にも「取り込めた」にもしない。手元の看板は消さない。"""
        first = sync.import_all(self.store, self.gateway, lines=["LVC"])
        self.assertEqual([r.ok for r in first.lines], [True])
        imported_at = self.store.get_meta("last_import_at", "")
        self.assertTrue(imported_at)
        self.store.set_meta("last_import_at", "2000/01/01 00:00:00")   # 進んだかを見分ける

        self.gateway.forget_copy()
        with mock.patch.object(SharedDb, "_copy_files", _empty_copy):
            result = sync.import_all(self.store, self.gateway, lines=["LVC"])

        line = result.lines[0]
        self.assertFalse(line.ok)
        self.assertNotEqual(line.message, sync.TABLE_MISSING, "読めなかったのを「表が無い」にした")
        self.assertEqual(self.store.get_meta("missing_line_tables", ""), "",
                         "読めなかっただけで盤に「表が無い」の帯を出す(ボタンを止める)")
        self.assertEqual(self.store.get_meta("last_import_at", ""), "2000/01/01 00:00:00",
                         "読めなかったのに「最後に取り込めた時刻」を進めた")
        self.assertIn("看板_LVC", self.store.get_meta(sync.META_IMPORT_PROBLEM, ""),
                      "読めなかった理由を画面に出す材料を残していない")
        self.assertEqual(sorted(i.mgmt_no for i in self.store.items("LVC")), ["1", "2"])

    def test_access_rules_read_as_empty_are_not_remembered(self):
        """空に見えた梱包資材マスタを「権限の表が無い」と読んで、前回読めた内容を空で上書きしない。"""
        master = self.dir / "梱包資材マスタ.sqlite3"
        c = sqlite3.connect(str(master))
        c.execute(access_control.DDL)
        c.execute("INSERT INTO [アクセス権限] ([ログインID], [PC名], [権限], [有効]) VALUES ('', 'PC-W', ?, 1)",
                  (access_control.PERM_MATERIAL,))
        c.commit()
        c.close()
        db = access_control.AccessDb(str(master), cache_dir=str(self.dir / "cache"))
        me = access_control.Identity("someone", "PC-W")

        fresh = access_control.grant_for(config.Config(), self.store, identity=me, db=db)
        self.assertEqual(fresh.source, "fresh")
        self.assertTrue(fresh.allows(config.MODE_WAREHOUSE))

        db.forget_copy()
        with mock.patch.object(SharedDb, "_copy_files", _empty_copy):
            blink = access_control.grant_for(config.Config(), self.store, identity=me, db=db)
        self.assertEqual(blink.source, "cache", "空に見えた表を「いま読んだ」として使った")
        self.assertTrue(blink.allows(config.MODE_WAREHOUSE), "一瞬空に見えただけで倉庫モードを取り上げた")

        db.forget_copy()
        again = access_control.grant_for(config.Config(), self.store, identity=me, db=db)
        self.assertTrue(again.allows(config.MODE_WAREHOUSE))
        self.assertEqual(len(access_control._load_cache(self.store, db.path)[0]), 1,
                         "前回読めた内容が空で上書きされた")


# ---------------------------------------------------------------------------
# 看板履歴のファイル
# ---------------------------------------------------------------------------
class HistoryFileTest(Fixture):
    def test_a_history_file_that_cannot_be_checked_is_not_created(self):
        """ファイルを確かめられない(権限・共有の一時的な不調)ときは「無い」として作らない。"""
        target = self.dir / config.HISTORY_DB_NAME
        db = history.HistoryDb(str(target), cache_dir=str(self.dir / "cache"))
        history.create_if_missing(db)                           # 本当に無いので作る
        self.assertTrue(target.is_file())

        real_stat = os.stat

        def stat(path, *args, **kwargs):
            if os.fspath(path) == str(target):
                raise PermissionError(13, "アクセスが拒否されました", str(target))
            return real_stat(path, *args, **kwargs)

        with mock.patch("os.stat", stat), mock.patch.object(history.applog, "info") as said:
            with self.assertRaises(SharedDbError):
                history.create_if_missing(db)
        made = [c for c in said.call_args_list if "作りました" in str(c.args[0])]
        self.assertEqual(made, [], "あるファイルを確かめられないだけで「作りました」と記録した")

    def test_a_zero_byte_history_file_is_set_up_and_events_go_there(self):
        """0 バイトの看板履歴(開きかけて残ったもの)は、読めないままにせず表まで用意して送る。"""
        target = self.dir / config.HISTORY_DB_NAME
        target.write_bytes(b"")
        db = history.HistoryDb(str(target), cache_dir=str(self.dir / "cache"))
        ready = sync.ensure_history_table(db)
        self.assertTrue(ready.ok, ready.message)
        sync.import_all(self.store, self.gateway, lines=["LVC"])
        self.store.apply_transition("LVC", "1", models.order_button_changes, operation="order")
        self.assertEqual(sync.export_events(self.store, db), 1)
        self.assertEqual(history.row_count(db), 1)


# ---------------------------------------------------------------------------
# 看板履歴へ送った印(追記なので、送り直すと重複する)
# ---------------------------------------------------------------------------
class HistorySentMarkTest(Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.history = history.HistoryDb(str(self.dir / config.HISTORY_DB_NAME),
                                          cache_dir=str(self.dir / "cache"))
        sync.import_all(self.store, self.gateway, lines=["LVC"])
        self.store.apply_transition("LVC", "1", models.order_button_changes, operation="order")
        self.exporter = sync.Exporter(self.store, self.gateway, interval_sec=0,
                                      history_gateway=self.history)

    def rows(self) -> list[str]:
        c = sqlite3.connect(str(self.history.path))
        try:
            return [r[0] for r in c.execute("SELECT [ID] FROM [看板履歴]")]
        finally:
            c.close()

    def test_sent_events_are_not_sent_again_when_history_looks_empty_or_unreadable(self):
        self.exporter.run_once()
        sent = self.rows()
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.store.unsent_event_count(), 0)

        # 次の出来事(発送)が積まれたところで、看板履歴が読めない・空に見える・表が無いと言われる。
        # どれも送った印を外す理由にならない。新しい出来事だけが、読めたときに 1 回入る
        self.store.apply_transition("LVC", "1", models.ship_button_changes, operation="ship")
        self.assertEqual(self.store.unsent_event_count(), 1)
        self.history.forget_copy()
        with mock.patch.object(SharedDb, "_copy_files", _empty_copy):
            self.exporter.run_once()
        with mock.patch.object(history.HistoryDb, "column_names", side_effect=SharedDbError("読めません")):
            self.exporter.run_once()
        self.assertEqual(self.rows(), sent, "読めなかったのに送った・送り直した")
        self.assertEqual(self.store.unsent_event_count(), 1, "読めなかったのに送れたことにした")
        with mock.patch.object(history.HistoryDb, "column_names", return_value=[]):
            self.exporter.run_once()      # 表が無いと見えた(作る文は IF NOT EXISTS。送るのは未送信だけ)
        self.history.forget_copy()
        self.exporter.run_once()
        after = self.rows()
        self.assertEqual(len(after), 2, "送った出来事をもう一度入れた / 新しい出来事が届かない")
        self.assertEqual(len(set(after)), 2)
        self.assertEqual(self.store.unsent_event_count(), 0)

    def test_an_event_sent_twice_lands_once(self):
        """送ったあと印を付ける前に落ちた(=同じ出来事を 2 回送る)ときも、看板履歴には 1 行。"""
        with mock.patch.object(self.store, "mark_events_sent"):
            self.exporter.run_once()          # 届いたが印を付けられなかった
        self.assertEqual(self.store.unsent_event_count(), 1)
        self.exporter.run_once()
        self.assertEqual(self.store.unsent_event_count(), 0)
        self.assertEqual(len(self.rows()), 1, "同じ出来事が看板履歴に 2 行入った")


# ---------------------------------------------------------------------------
# 書き戻し: 一時的に送れないのを「送り先が無い」と同じに扱わない
# ---------------------------------------------------------------------------
class TransientExportFailureTest(Fixture):
    def setUp(self) -> None:
        super().setUp()
        sync.import_all(self.store, self.gateway, lines=["LVC"])
        self.store.apply_transition("LVC", "1", models.order_button_changes, operation="order")

    def assert_still_sendable(self, why: str) -> None:
        self.assertEqual(self.store.sync_failures(), [], f"{why}で「要確認」(諦め)にした")
        self.assertEqual([r["mgmt_no"] for r in self.store.pending_items()], ["1"],
                         f"{why}で送るのをやめた")
        self.assertTrue(self.store.dirty_sync_error(), "理由を帯に出す材料を残していない")

    def sent_ok(self) -> None:
        self.gateway.forget_copy()
        result = sync.export_pending(self.store, self.gateway)
        self.assertEqual(result.succeeded, 1, result.errors)
        self.assertEqual(self.store.pending_count(), 0)

    def test_shared_db_that_cannot_be_opened_does_not_use_up_the_attempts(self):
        broken = SharedDbError("共有DBを開けませんでした (x): unable to open database file")
        with mock.patch.object(SharedDb, "execute", side_effect=broken):
            for _ in range(MAX_SYNC_ATTEMPTS + 1):
                result = sync.export_pending(self.store, self.gateway)
                self.assertEqual(result.failed, 1)
        self.assert_still_sendable("共有DBを一時的に開けないだけ")
        # 「送れない操作を捨てる」(送り先が無いもの)にも並べない
        self.assertEqual(self.store.discard_failed(), [])
        self.sent_ok()

    def test_a_locked_shared_db_does_not_use_up_the_attempts(self):
        locked = [StatementResult(ok=False, error_message="database is locked")]
        with mock.patch.object(SharedDb, "execute", return_value=locked):
            for _ in range(MAX_SYNC_ATTEMPTS + 1):
                sync.export_pending(self.store, self.gateway)
        self.assert_still_sendable("ロック中なだけ")
        self.sent_ok()

    def test_a_reason_that_could_not_be_read_is_not_taken_as_a_missing_row(self):
        """0 行更新の理由を確かめに読めなかったとき、「行がありません」と決めつけて諦めない。"""
        nothing = [StatementResult(ok=True, affected=0)]
        with mock.patch.object(SharedDb, "execute", return_value=nothing), \
                mock.patch.object(SharedDb, "select", side_effect=SharedDbError("共有DBを読めませんでした")):
            for _ in range(MAX_SYNC_ATTEMPTS + 1):
                sync.export_pending(self.store, self.gateway)
        self.assert_still_sendable("理由を読めなかっただけ")
        self.assertNotIn("行がありません", self.store.dirty_sync_error())
        self.sent_ok()

    def test_rows_given_up_for_a_lock_by_the_old_version_are_sent_again_on_start(self):
        """前の版がロック中を数えて諦めた行は、起動したときに送り直しへ戻す。ほかの理由のものは触らない。"""
        self.store.apply_transition("LVC", "2", models.order_button_changes, operation="order")
        for _ in range(MAX_SYNC_ATTEMPTS):
            self.store.mark_items_failed([("LVC", "1")], "database is locked")
            self.store.mark_items_failed([("LVC", "2")], "対象行なし: 共有DBの 看板_LVC に管理番号 2 の行がありません")
        self.assertEqual(len(self.store.sync_failures()), 2)
        exporter = sync.Exporter(self.store, self.gateway, interval_sec=0)
        exporter.start()
        try:
            self.assertEqual([f["mgmt_no"] for f in self.store.sync_failures()], ["2"])
            self.assertEqual([r["mgmt_no"] for r in self.store.pending_items()], ["1"])
        finally:
            exporter.stop(final_export=False, timeout=5)

    def test_a_row_really_missing_is_still_given_up(self):
        """本当に送り先の行が無いときは、今までどおり数えて「要確認」にする(変えない)。"""
        c = sqlite3.connect(str(self.shared))
        c.execute("DELETE FROM [看板_LVC] WHERE [管理番号] = '1'")
        c.commit()
        c.close()
        self.gateway.forget_copy()
        for _ in range(MAX_SYNC_ATTEMPTS):
            sync.export_pending(self.store, self.gateway)
        failures = self.store.sync_failures()
        self.assertEqual([f["mgmt_no"] for f in failures], ["1"])
        self.assertIn("行がありません", failures[0]["sync_error"])


# ---------------------------------------------------------------------------
# 送れない操作を捨てたあとの取り込み直し(画面の文言)
# ---------------------------------------------------------------------------
@unittest.skipUnless(HAS_FLASK, "Flask が入っていないためスキップ")
class DiscardThenUnreadableTest(RouteTestBase):
    MODE = config.MODE_WAREHOUSE
    LINE = ""

    def setUp(self) -> None:
        super().setUp()
        import json

        self.shared = self.dir / "看板マスタ.sqlite3"
        _make_shared(self.shared)
        (self.dir / "config.json").write_text(json.dumps({
            "shared_db_path": str(self.shared),
            "admin_password_hash": config.hash_password(TEST_ADMIN_PASSWORD)}, ensure_ascii=False),
            encoding="utf-8")
        self.gateway = SharedDb(str(self.shared), cache_dir=str(self.dir / "cache"))
        sync.import_all(self.store, self.gateway, lines=["LVC"])
        self.app.config["MANUAL_REFRESH"] = lambda: sync.import_all(self.store, self.gateway, lines=["LVC"])
        # 発注したが、送り先の行が消されて送れない(要確認)
        self.store.apply_transition("LVC", "2", models.order_button_changes, operation="order")
        c = sqlite3.connect(str(self.shared))
        c.execute("DELETE FROM [看板_LVC] WHERE [管理番号] = '2'")
        c.commit()
        c.close()
        self.gateway.forget_copy()
        for _ in range(MAX_SYNC_ATTEMPTS):
            sync.export_pending(self.store, self.gateway)

    def test_discard_does_not_claim_a_reimport_that_could_not_read(self):
        self.gateway.forget_copy()
        with mock.patch.object(SharedDb, "_copy_files", _empty_copy):
            res = self.post("/api/sync/discard-failed", {"password": TEST_ADMIN_PASSWORD})
        self.assertEqual(res.status_code, 200, res.json)
        message = res.json["message"]
        self.assertNotIn("取り込み直しました", message, "読めなかったのに取り込み直したと言った")
        self.assertIn("読めなかった", message)
        # 読めなかったあいだ、手元の看板は消さない(次に読めたときに共有DBの状態になる)
        self.assertIsNotNone(self.store.item("LVC", "2"))

        self.gateway.forget_copy()
        sync.import_all(self.store, self.gateway, lines=["LVC"])
        self.assertIsNone(self.store.item("LVC", "2"), "読めたあと、共有DBに無い看板が残った")


if __name__ == "__main__":
    unittest.main()
