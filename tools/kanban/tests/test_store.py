"""SQLite ストアのテスト(取り込み・状態遷移・同時アクセス)。"""

from __future__ import annotations

import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from kanban.db.store import DEFAULT_COLUMN_MAP, ConflictError, Store
from kanban.domain import models
from kanban.domain.service import KanbanService

CATEGORIES = {name: "TEXT" for name in DEFAULT_COLUMN_MAP.values()}


def sample_rows(count: int = 3) -> list[dict[str, str]]:
    return [
        {
            "mgmt_no": str(i),
            "material": "外装紙" if i <= 2 else "アングル",
            "size": f"サイズ{i}",
            "want": "",
            "unwant": "〇",
            "ordered_at": "",
            "shipped": "",
            "confirmed_at": "",
            "permanent": "〇",
        }
        for i in range(1, count + 1)
    ]


class StoreTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="kanban_test_"))
        self.store = Store(str(self.dir / "kanban.sqlite3"), host_name="PC-TEST")
        self.store.ensure_schema()
        self._import(sample_rows())

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _import(self, rows, line="LVC", keep_local_changes=True):
        return self.store.import_line(
            line=line,
            table_name=f"看板_{line}",
            key_column="管理番号",
            key_category="TEXT",
            column_map=dict(DEFAULT_COLUMN_MAP),
            categories=CATEGORIES,
            rows=rows,
            source_path="dummy.accdb",
            keep_local_changes=keep_local_changes,
        )


class ImportTest(StoreTestBase):
    def test_import_inserts_rows(self):
        items = self.store.items("LVC")
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0].mgmt_no, "1")
        self.assertEqual(items[0].row_order, 0)

    def test_reimport_updates_clean_rows(self):
        rows = sample_rows()
        rows[0]["want"] = "〇"
        rows[0]["unwant"] = ""
        inserted, updated, protected = self._import(rows)
        self.assertEqual((inserted, protected), (0, 0))
        # 値が実際に変わった行(1件)だけがカウントされる。値が変わらない
        # 行まで rev を進めると、画面が持つ expected_rev が無関係な理由で
        # 古くなり、ボタン操作が誤って競合エラーになってしまうため
        self.assertEqual(updated, 1)
        self.assertTrue(self.store.item("LVC", "1").is_ordered)

    def test_reimport_with_no_changes_does_not_bump_rev(self):
        """値が何も変わらない再取り込みは rev を進めない(偽の競合防止)。"""
        before = {item.mgmt_no: item.rev for item in self.store.items("LVC")}
        inserted, updated, protected = self._import(sample_rows())
        self.assertEqual((inserted, updated, protected), (0, 0, 0))
        after = {item.mgmt_no: item.rev for item in self.store.items("LVC")}
        self.assertEqual(before, after)

    def test_reimport_protects_unsynced_changes(self):
        # 端末で発注 -> まだ Access へ書き戻していない状態
        self.store.apply_transition(
            "LVC", "1", models.order_button_changes, operation="order"
        )
        self.assertTrue(self.store.item("LVC", "1").is_ordered)

        # 古い内容(未発注)で再取り込みしても操作は消えない。
        # 他の行は値が変わっていないので updated には含まれない
        inserted, updated, protected = self._import(sample_rows())
        self.assertEqual(protected, 1)
        self.assertEqual(updated, 0)
        self.assertTrue(self.store.item("LVC", "1").is_ordered)

    def test_reimport_refreshes_non_dirty_columns_of_protected_row(self):
        """未反映の列だけ保護し、それ以外の状態列は Access の最新値へ更新される。

        現場が「欲→不」(発送列には触れない操作)をした直後でまだ Access へ
        送っていない間に、倉庫が別のタイミングで Access へ発送済みを
        書き戻していた、という状況を再現する。倉庫の発送は消えてはいけない
        し、現場の未反映の操作(不への変更)も消えてはいけない。
        """
        # 前提: row 1 が既に発注済みの状態から始める
        rows = sample_rows()
        rows[0]["want"] = "〇"
        rows[0]["unwant"] = ""
        self._import(rows)
        self.assertTrue(self.store.item("LVC", "1").is_ordered)

        # 現場: 発注解除(欲→不)。この操作は発送列に一切触れない
        self.store.apply_transition(
            "LVC", "1", models.order_button_changes, operation="order"
        )
        local_before = self.store.item("LVC", "1")
        self.assertFalse(local_before.is_ordered)
        self.assertFalse(local_before.is_shipped)
        dirty_cols = self.store.connection.execute(
            "SELECT dirty_columns FROM kanban_item WHERE line='LVC' AND mgmt_no='1'"
        ).fetchone()["dirty_columns"]
        self.assertNotIn("shipped", dirty_cols.split(","))

        # Access 側では既に倉庫が発送済みにしていた、という取り込み内容
        # (欲・不は現場側で操作中の内容と食い違うが、これは Access 反映前の
        # 古いスナップショットなので、現場側の未反映操作を優先すべき列)
        rows2 = sample_rows()
        rows2[0]["want"] = "〇"
        rows2[0]["unwant"] = ""
        rows2[0]["shipped"] = "〇"
        rows2[0]["confirmed_at"] = "2026/07/14 10:00:00"
        self._import(rows2)

        item = self.store.item("LVC", "1")
        # 現場の未反映の操作(不への変更)は消えない
        self.assertFalse(item.is_ordered)
        # 倉庫が Access へ書き戻した発送は、この端末にも反映される
        self.assertTrue(item.is_shipped)
        self.assertEqual(item.confirmed_at, "2026/07/14 10:00:00")
        # 欲/不/更新日はまだ Access へ送っていないので未反映のまま
        self.assertEqual(self.store.pending_count(), 1)

    def test_reimport_removes_deleted_rows(self):
        self._import(sample_rows(2))
        self.assertEqual(len(self.store.items("LVC")), 2)

    def test_a_deleted_kanban_with_an_unsent_operation_is_removed_too(self):
        """**共有DBから消された看板は、未反映の操作があっても手元から消す。**

        送り先の行がもう無いので永久に送れない。以前は残していたため、マスタで
        消した看板が看板画面に出続け、「要確認」からも消えなかった。
        """
        self._import(sample_rows())
        self.store.apply_transition(
            "LVC", "1", models.order_button_changes, operation="order"
        )
        self.assertEqual(self.store.pending_count(), 1)
        self._import([r for r in sample_rows() if r["mgmt_no"] != "1"])
        self.assertIsNone(self.store.item("LVC", "1"))
        self.assertEqual(self.store.pending_count(), 0)

    def test_re_adding_the_same_number_starts_clean(self):
        """消して同じ番号で足し直したとき、古い発注が新しい看板に乗らない。"""
        self._import(sample_rows())
        self.store.apply_transition(
            "LVC", "1", models.order_button_changes, operation="order"
        )
        self._import([r for r in sample_rows() if r["mgmt_no"] != "1"])   # 消した
        self._import(sample_rows())                                       # 足し直した
        item = self.store.item("LVC", "1")
        self.assertFalse(item.is_ordered, "消した看板の発注が乗っている")
        self.assertEqual(self.store.pending_count(), 0)

    def test_line_source_roundtrip(self):
        source = self.store.line_source("LVC")
        self.assertEqual(source["table_name"], "看板_LVC")
        self.assertEqual(source["key_column"], "管理番号")
        self.assertEqual(source["column_map"]["want"], "欲")

    def test_import_carries_hold_and_hold_at(self):
        rows = sample_rows()
        rows[0]["hold"] = "〇"
        rows[0]["hold_at"] = "2026/07/14 09:00:00"
        self._import(rows)
        item = self.store.item("LVC", "1")
        self.assertTrue(item.is_held)
        self.assertEqual(item.hold_at, "2026/07/14 09:00:00")

    def test_reimport_protects_unsynced_hold_column(self):
        """注文中も他の状態列と同じく dirty_columns による列単位保護の対象。"""
        # 注文中は発注済みの行にしか付かない(現場の発注を倉庫が注文する)
        self.store.apply_transition(
            "LVC", "1", models.order_button_changes, operation="order"
        )
        self.store.apply_transition(
            "LVC", "1", models.hold_button_changes, operation="hold"
        )
        self.assertTrue(self.store.item("LVC", "1").is_held)

        # 古い内容(注文中でない)で再取り込みしても注文中は消えない
        inserted, updated, protected = self._import(sample_rows())
        self.assertEqual(protected, 1)
        item = self.store.item("LVC", "1")
        self.assertTrue(item.is_held)


class TransitionTest(StoreTestBase):
    def test_order_marks_dirty_and_bumps_rev(self):
        before = self.store.item("LVC", "1")
        after = self.store.apply_transition(
            "LVC", "1", models.order_button_changes, operation="order"
        )
        # 版は全行通しの番号(ほかの行・ほかのラインの版と重ならない)
        self.assertGreater(after.rev, before.rev)
        others = {i.rev for i in self.store.items("LVC") if i.mgmt_no != "1"}
        self.assertNotIn(after.rev, others)
        self.assertTrue(after.is_ordered)
        self.assertEqual(self.store.pending_count(), 1)
        self.assertEqual(after.updated_by, "PC-TEST")

    def test_stale_revision_is_rejected(self):
        stale = self.store.item("LVC", "1").rev
        self.store.apply_transition(
            "LVC", "1", models.order_button_changes, operation="order"
        )
        with self.assertRaises(ConflictError):
            self.store.apply_transition(
                "LVC",
                "1",
                models.order_button_changes,
                operation="order",
                expected_rev=stale,
            )

    def test_missing_row_raises_conflict(self):
        with self.assertRaises(ConflictError):
            self.store.apply_transition(
                "LVC", "999", models.order_button_changes, operation="order"
            )

    def test_batch_applies_only_matching_rows(self):
        for no in ("1", "2"):
            self.store.apply_transition(
                "LVC", no, models.order_button_changes, operation="order"
            )
            self.store.apply_transition(
                "LVC", no, models.ship_button_changes, operation="ship"
            )
        updated = self.store.apply_batch(
            "LVC",
            select=lambda item: item.is_delivered_candidate,
            changes_for=lambda item: models.batch_reset_changes(),
            operation="batch_reset",
        )
        self.assertEqual(len(updated), 2)
        self.assertFalse(self.store.item("LVC", "1").is_ordered)
        self.assertFalse(self.store.item("LVC", "1").is_shipped)
        self.assertFalse(self.store.item("LVC", "3").is_ordered)

    def test_operation_log_records_changes(self):
        self.store.apply_transition(
            "LVC", "1", models.order_button_changes, operation="order"
        )
        entries = self.store.recent_operations()
        self.assertTrue(any(e["operation"] == "order" for e in entries))

    def test_opening_the_store_clears_out_old_operations(self):
        """**操作履歴は放っておくと増え続ける。**

        押すたびに 1 行増えるのに、片付ける ``prune_operation_log`` を呼ぶ
        場所がどこにも無く、端末を使い続けるかぎり溜まる一方でした。
        開くときに 1 回だけ通る ``ensure_schema`` で落とします。
        """
        self.store.connection.execute(
            "INSERT INTO operation_log(line, mgmt_no, operation, detail, at, host)"
            " VALUES('LVC','1','order','古い','2020/01/01 00:00:00','PC')"
        )
        self.store.apply_transition(
            "LVC", "1", models.order_button_changes, operation="order"
        )
        self.assertEqual(len(self.store.recent_operations()), 2)

        self.store.ensure_schema()
        remaining = self.store.recent_operations()
        self.assertEqual(len(remaining), 1, "古い履歴が片付いていない")
        self.assertNotEqual(remaining[0]["detail"], "古い")

    def test_hold_marks_dirty_columns_including_shipped(self):
        """注文中にすると、保留・注文中日時に加えて強制解除した発送列も
        dirty_columns に載り、書き戻し対象になる。"""
        self.store.apply_transition(
            "LVC", "1", models.order_button_changes, operation="order"
        )
        shipped = self.store.apply_transition(
            "LVC", "1", models.ship_button_changes, operation="ship"
        )
        self.store.mark_items_synced([("LVC", "1", shipped.rev)])
        self.assertEqual(self.store.pending_count(), 0)

        self.store.apply_transition(
            "LVC",
            "1",
            models.hold_button_changes,
            operation="hold",
        )
        item = self.store.item("LVC", "1")
        self.assertTrue(item.is_held)
        self.assertTrue(item.hold_at)
        self.assertFalse(item.is_shipped)
        self.assertEqual(self.store.pending_count(), 1)
        dirty_cols = self.store.connection.execute(
            "SELECT dirty_columns FROM kanban_item WHERE line='LVC' AND mgmt_no='1'"
        ).fetchone()["dirty_columns"]
        self.assertEqual(set(dirty_cols.split(",")), {"hold", "hold_at", "shipped"})

    def test_unhold_does_not_mark_shipped_dirty(self):
        self.store.apply_transition(
            "LVC", "1", models.order_button_changes, operation="order"
        )
        held = self.store.apply_transition(
            "LVC",
            "1",
            models.hold_button_changes,
            operation="hold",
        )
        self.store.mark_items_synced([("LVC", "1", held.rev)])

        self.store.apply_transition(
            "LVC", "1", models.hold_button_changes, operation="hold"
        )
        item = self.store.item("LVC", "1")
        self.assertFalse(item.is_held)
        self.assertEqual(item.hold_at, "")
        dirty_cols = self.store.connection.execute(
            "SELECT dirty_columns FROM kanban_item WHERE line='LVC' AND mgmt_no='1'"
        ).fetchone()["dirty_columns"]
        self.assertEqual(set(dirty_cols.split(",")), {"hold", "hold_at"})


class ChangeTokenTest(StoreTestBase):
    def test_token_changes_after_update(self):
        before = self.store.change_token("LVC")
        self.store.apply_transition(
            "LVC", "1", models.order_button_changes, operation="order"
        )
        self.assertNotEqual(before, self.store.change_token("LVC"))


class DeviceModeTest(StoreTestBase):
    def test_unset_mode_is_empty_string(self):
        self.assertEqual(self.store.get_device_mode(), "")

    def test_set_then_get_roundtrips(self):
        self.store.set_device_mode("warehouse")
        self.assertEqual(self.store.get_device_mode(), "warehouse")

    def test_set_overwrites_previous_value(self):
        self.store.set_device_mode("site")
        self.store.set_device_mode("view")
        self.assertEqual(self.store.get_device_mode(), "view")

    def test_persists_across_connections(self):
        self.store.set_device_mode("warehouse")
        from kanban.db.store import Store

        other = Store(self.store.path, host_name="PC-OTHER")
        try:
            self.assertEqual(other.get_device_mode(), "warehouse")
        finally:
            other.close()


class SyncBookkeepingTest(StoreTestBase):
    def test_pending_and_clear(self):
        item = self.store.apply_transition(
            "LVC", "1", models.order_button_changes, operation="order"
        )
        pending = self.store.pending_items()
        self.assertEqual(len(pending), 1)
        cleared = self.store.mark_items_synced([("LVC", "1", item.rev)])
        self.assertEqual(cleared, 1)
        self.assertEqual(self.store.pending_count(), 0)

    def test_clear_skips_rows_changed_during_export(self):
        item = self.store.apply_transition(
            "LVC", "1", models.order_button_changes, operation="order"
        )
        # 書き戻し中に現場がもう一度操作した
        self.store.apply_transition(
            "LVC", "1", models.order_button_changes, operation="order"
        )
        cleared = self.store.mark_items_synced([("LVC", "1", item.rev)])
        self.assertEqual(cleared, 0)
        self.assertEqual(self.store.pending_count(), 1)

    def test_failed_rows_stop_retrying(self):
        self.store.apply_transition(
            "LVC", "1", models.order_button_changes, operation="order"
        )
        for _ in range(5):
            self.store.mark_items_failed([("LVC", "1")], "テスト失敗")
        self.assertEqual(self.store.pending_items(), [])
        self.assertEqual(len(self.store.sync_failures()), 1)

    def test_rows_that_failed_because_of_our_bug_are_sent_again(self):
        """**アプリ側の不具合で諦めた行は、直したあと送り直す。**

        共有フォルダへの書き込みを URI の形で開いていたころ、書き戻しは
        ``invalid uri authority`` で失敗し続け、5 回で諦めていた(要確認)。
        諦めた行は二度と送られないので、送り直さないと押した操作が共有DBへ
        永久に届かない。**それ以外の理由で諦めた行には触らない。**
        """
        from kanban.db.store import MAX_SYNC_ATTEMPTS, URI_AUTHORITY_FAILURE

        for no in ("1", "2"):
            self.store.apply_transition(
                "LVC", no, models.order_button_changes, operation="order"
            )
        for _ in range(MAX_SYNC_ATTEMPTS):
            self.store.mark_items_failed(
                [("LVC", "1")],
                f"共有DBを開けませんでした (\\\\nlmfangyshrd\\x): {URI_AUTHORITY_FAILURE}: nlmfangyshrd",
            )
            self.store.mark_items_failed([("LVC", "2")], "対象行なし")
        self.assertEqual(self.store.retryable_pending_count(), 0)

        self.store.ensure_schema()  # 起動時の手入れ

        pending = {r["mgmt_no"] for r in self.store.pending_items()}
        self.assertEqual(pending, {"1"}, "直した不具合の行だけが送り直しに戻るべき")
        self.assertEqual([f["mgmt_no"] for f in self.store.sync_failures()], ["2"])

    def test_a_row_we_gave_up_on_does_not_keep_the_app_alive(self):
        """**諦めた行を「まだ送れる」に数えないこと。**

        数えてしまうと ``start_app._busy_reason`` が永久に
        「未反映がある」と答え、**二度と終われなくなります** ── タブを
        閉じてもアプリが残り、次の起動が「すでに起動しています」で止まる。
        共有DB側からその行が消された(要確認)ときに実際に起きます。

        画面の「未反映」(:meth:`pending_count`)からは外しません ──
        届いていないことに変わりはないので、0 件と言うのは嘘になります。
        """
        self.store.apply_transition(
            "LVC", "1", models.order_button_changes, operation="order"
        )
        self.assertEqual(self.store.retryable_pending_count(), 1)

        for _ in range(5):
            self.store.mark_items_failed([("LVC", "1")], "対象行なし")

        self.assertEqual(self.store.retryable_pending_count(), 0, "諦めた行を待ち続けている")
        self.assertEqual(self.store.pending_count(), 1, "届いていないのに 0 件と言っている")
        self.assertEqual(len(self.store.sync_failures()), 1, "要確認にも出ていない")

    def test_busy_reason_lets_go_of_rows_that_can_never_be_sent(self):
        """見張りが見る値そのもので確かめる(組み合わせ違いを防ぐ)。"""
        import start_app

        self.store.apply_transition(
            "LVC", "1", models.order_button_changes, operation="order"
        )
        self.assertTrue(start_app._busy_reason(self.store))
        for _ in range(5):
            self.store.mark_items_failed([("LVC", "1")], "対象行なし")
        # 押したときに積んだ出来事(看板履歴)は送れたことにする(待つのは別の理由。下の試験)
        self.store.mark_events_sent([e["id"] for e in self.store.unsent_events()])
        self.assertEqual(start_app._busy_reason(self.store), "", "止まれないままになっている")

    def test_busy_reason_waits_for_unsent_comments_and_recent_events(self):
        """送っていないコメント・記録があるあいだは止めない(相手に届く前に終わらない)。"""
        import start_app

        self.assertEqual(start_app._busy_reason(self.store), "")
        self.store.add_comment("LVC", "1", "現場", "急ぎです")
        self.assertIn("コメント", start_app._busy_reason(self.store))
        self.store.mark_comments_sent([c["id"] for c in self.store.unsent_comments()])
        self.assertEqual(start_app._busy_reason(self.store), "")

        self.store.apply_transition("LVC", "1", models.order_button_changes, operation="order")
        self.store.mark_items_synced([("LVC", "1", self.store.item("LVC", "1").rev)])
        # 押すと確認の区切り(これもコメントの 1 行)が積まれる。送れたことにする
        self.store.mark_comments_sent([c["id"] for c in self.store.unsent_comments()])
        self.assertIn("記録", start_app._busy_reason(self.store))
        # 何日も送れていない記録は待たない(看板履歴の置き場所が見えないまま。二度と終われなくなる)
        self.store.connection.execute("UPDATE kanban_event SET at = '2000/01/01 00:00:00'")
        self.assertEqual(start_app._busy_reason(self.store), "")


class UnsentOneCountTest(StoreTestBase):
    """「まだ届いていない」の数え方は Store.unsent の 1 か所(止める判断・送り直しの見張り・帯)。

    以前は止める判断は諦めた行を外し、送り直しの見張りは数えていたので、諦めた行があるあいだ
    見張りが 5 秒ごとに書き戻しを回し続けた。
    """

    def _exporter(self, shared_exists=True, history_exists=True):
        from unittest import mock

        from kanban.db.sync import Exporter

        gateway = mock.Mock(exists=mock.Mock(return_value=shared_exists))
        history = mock.Mock(exists=mock.Mock(return_value=history_exists))
        exporter = Exporter(self.store, gateway, interval_sec=60, history_gateway=history)
        exporter._task = mock.Mock()
        exporter.RETRY_SEC = 0.01
        return exporter

    def _run_retry_once(self, exporter):
        import threading

        calls = []
        stop = exporter._retry_stop
        real_wait = stop.wait

        def wait_once(timeout=None):
            if calls:
                return True
            calls.append(1)
            return real_wait(0)

        stop.wait = wait_once
        exporter._last_run = 0.0
        exporter._retry_loop()
        return exporter._task.request_now.call_count

    def test_諦めた行だけなら送り直さない(self):
        self.store.apply_transition("LVC", "1", models.order_button_changes, operation="order")
        self.store.mark_events_sent([e["id"] for e in self.store.unsent_events()])
        self.store.mark_comments_sent([c["id"] for c in self.store.unsent_comments()])
        for _ in range(5):
            self.store.mark_items_failed([("LVC", "1")], "対象行なし")
        left = self.store.unsent()
        self.assertEqual((left.items, left.retryable_items), (1, 0))
        exporter = self._exporter()
        self.assertEqual(exporter.unsent(), 0, "諦めた行を送り直しに数えている")
        self.assertEqual(self._run_retry_once(exporter), 0, "諦めた行のために送り直した")

    def test_出来事は看板履歴が見えたら送り直す(self):
        self.store.apply_transition("LVC", "1", models.order_button_changes, operation="order")
        self.store.mark_items_synced([("LVC", "1", self.store.item("LVC", "1").rev)])
        self.store.mark_comments_sent([c["id"] for c in self.store.unsent_comments()])
        self.assertTrue(self.store.unsent().to_history())
        # 共有DBは見えないが看板履歴は見える → 送る(以前は共有DBしか見ていなかった)
        self.assertEqual(self._run_retry_once(self._exporter(shared_exists=False)), 1)
        self.assertEqual(self._run_retry_once(self._exporter(history_exists=False)), 0)

    def test_数え方の内訳(self):
        self.store.apply_transition("LVC", "1", models.order_button_changes, operation="order")
        self.store.add_comment("LVC", "1", "現場", "急ぎです")
        left = self.store.unsent()
        self.assertEqual(left.items, self.store.pending_count())
        self.assertEqual(left.retryable_items, self.store.retryable_pending_count())
        self.assertEqual(left.comments, self.store.unsent_comment_count())
        self.assertEqual(left.events, self.store.unsent_event_count())
        self.assertTrue(left.worth_waiting())


class LockTest(StoreTestBase):
    def test_lock_is_exclusive_between_hosts(self):
        other = Store(self.store.path, host_name="PC-OTHER")
        try:
            self.assertTrue(self.store.acquire_lock("accdb_export"))
            self.assertFalse(other.acquire_lock("accdb_export"))
            self.store.release_lock("accdb_export")
            self.assertTrue(other.acquire_lock("accdb_export"))
        finally:
            other.close()

    def test_stale_lock_is_taken_over(self):
        """異常終了した端末が残したロックは心拍が途絶えた時点で奪える。"""
        other = Store(self.store.path, host_name="PC-OTHER")
        try:
            self.assertTrue(other.acquire_lock("accdb_export"))
            self.assertFalse(self.store.acquire_lock("accdb_export"))
            # 心拍を過去に書き換えて「落ちた端末」を再現する
            with other.write_transaction() as conn:
                conn.execute(
                    "UPDATE app_lock SET heartbeat_at = ? WHERE name = ?",
                    ("2000/01/01 00:00:00", "accdb_export"),
                )
            self.assertTrue(self.store.acquire_lock("accdb_export"))
        finally:
            other.close()


class ConcurrencyTest(StoreTestBase):
    """複数ライン(複数端末)からの同時操作。"""

    def test_parallel_updates_do_not_lose_writes(self):
        rows = [
            {
                "mgmt_no": str(i),
                "material": "外装紙",
                "size": f"サイズ{i}",
                "want": "",
                "unwant": "〇",
                "ordered_at": "",
                "shipped": "",
                "confirmed_at": "",
                "permanent": "〇",
            }
            for i in range(1, 21)
        ]
        self._import(rows)

        errors: list[BaseException] = []
        barrier = threading.Barrier(4)

        def worker(offset: int) -> None:
            store = Store(self.store.path, host_name=f"PC-{offset}")
            try:
                barrier.wait(timeout=10)
                for i in range(1 + offset, 21, 4):
                    store.apply_transition(
                        "LVC", str(i), models.order_button_changes, operation="order"
                    )
            except BaseException as exc:  # noqa: BLE001 - テストで報告する
                errors.append(exc)
            finally:
                store.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        self.assertEqual(errors, [], f"同時更新でエラー: {errors}")
        ordered = [item for item in self.store.items("LVC") if item.is_ordered]
        self.assertEqual(len(ordered), 20)
        self.assertEqual(self.store.pending_count(), 20)

    def test_same_row_from_two_hosts_is_serialized(self):
        """同じ行を 2 台が同時に押しても状態が壊れないこと。"""
        seq_before = int(self.store.get_meta("rev_seq", "0"))
        errors: list[BaseException] = []
        conflicts = []
        barrier = threading.Barrier(2)

        def worker(name: str) -> None:
            store = Store(self.store.path, host_name=name)
            try:
                item = store.item("LVC", "1")
                barrier.wait(timeout=10)
                store.apply_transition(
                    "LVC",
                    "1",
                    models.order_button_changes,
                    operation="order",
                    expected_rev=item.rev,
                )
            except ConflictError:
                conflicts.append(name)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                store.close()

        threads = [
            threading.Thread(target=worker, args=(f"PC-{i}",)) for i in range(2)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        self.assertEqual(errors, [])
        # 片方は必ず競合として弾かれ、状態は 1 回ぶんだけ進む
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(int(self.store.get_meta("rev_seq", "0")), seq_before + 1)
        self.assertEqual(self.store.item("LVC", "1").rev, seq_before + 1)


class ServiceGroupingTest(StoreTestBase):
    def test_groups_sorted_like_vba(self):
        rows = []
        # 外装紙 3 サイズ / アングル 2 サイズ / テープ 1 サイズ
        for i, (material, size) in enumerate(
            [
                ("外装紙", "A"),
                ("外装紙", "B"),
                ("外装紙", "C"),
                ("アングル", "A"),
                ("アングル", "B"),
                ("テープ", "A"),
            ],
            start=1,
        ):
            rows.append(
                {
                    "mgmt_no": str(i),
                    "material": material,
                    "size": size,
                    "want": "",
                    "unwant": "〇",
                    "ordered_at": "",
                    "shipped": "",
                    "confirmed_at": "",
                    "permanent": "〇",
                }
            )
        self._import(rows)
        service = KanbanService(self.store)
        groups = service.groups("LVC")
        self.assertEqual(
            [g.material for g in groups], ["外装紙", "アングル", "テープ"]
        )
        self.assertEqual([g.size_count for g in groups], [3, 2, 1])

    def test_duplicate_sizes_are_ignored(self):
        rows = [
            {
                "mgmt_no": "1",
                "material": "外装紙",
                "size": "A",
                "want": "",
                "unwant": "〇",
                "ordered_at": "",
                "shipped": "",
                "confirmed_at": "",
                "permanent": "",
            },
            {
                "mgmt_no": "2",
                "material": "外装紙",
                "size": "A",
                "want": "",
                "unwant": "〇",
                "ordered_at": "",
                "shipped": "",
                "confirmed_at": "",
                "permanent": "",
            },
        ]
        self._import(rows)
        groups = KanbanService(self.store).groups("LVC")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].size_count, 1)


if __name__ == "__main__":
    unittest.main()
