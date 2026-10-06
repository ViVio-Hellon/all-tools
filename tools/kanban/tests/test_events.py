"""看板の出来事(集計のための記録)。

看板の表はいまの状態しか持たないので、「いつ出して・いつ届いて・次にいつ出したか」は
状態が変わった瞬間に記録する。記録は手元に溜め、書き戻しと一緒に共有DBの
[看板履歴] へ送る。
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from kanban.db import sync
from kanban.db.shared import SharedDb
from kanban.db.store import DEFAULT_COLUMN_MAP, Store
from kanban.domain import events, models
from kanban.domain.models import KanbanItem


def item(**kw) -> KanbanItem:
    base = dict(line="LVC", mgmt_no="1", material="中性紙", size="A", want="", unwant="〇")
    base.update(kw)
    return KanbanItem(**base)


class ClassifyTest(unittest.TestCase):
    """出来事は**状態の変わり方**で決める(どのボタンか、ではなく)。"""

    def test_ordering(self):
        self.assertEqual(events.classify(item(), models.order_button_changes(item())),
                         [events.ORDERED])

    def test_cancelling_before_it_ships(self):
        ordered = item(want="〇", unwant="")
        self.assertEqual(events.classify(ordered, models.order_button_changes(ordered)),
                         [events.CANCELLED])

    def test_shipping_and_unshipping(self):
        ordered = item(want="〇", unwant="")
        self.assertEqual(events.classify(ordered, models.ship_button_changes(ordered)),
                         [events.SHIPPED])
        shipped = item(want="〇", unwant="", shipped="〇")
        self.assertEqual(events.classify(shipped, models.ship_button_changes(shipped)),
                         [events.UNSHIPPED])

    def test_delivered_by_the_button_or_the_batch_reset(self):
        """届いたので赤を消した ── 1 枚ずつでも一括リセットでも同じ出来事。"""
        shipped = item(want="〇", unwant="", shipped="〇")
        self.assertEqual(
            events.classify(shipped, models.order_button_changes(shipped, treat_as_delivered=True)),
            [events.DELIVERED])
        self.assertEqual(events.classify(shipped, models.batch_reset_changes()),
                         [events.DELIVERED])

    def test_nothing_happens(self):
        shipped = item(want="〇", unwant="", shipped="〇")
        self.assertEqual(events.classify(shipped, models.order_button_changes(shipped)), [])


class SharedFixture(unittest.TestCase):
    """看板が 1 枚ある手元と、空の共有DB。"""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="kanban_events_"))
        self.store = Store(str(self.dir / "local.sqlite3"), host_name="PC-1")
        self.store.ensure_schema()
        rows = [
            {"mgmt_no": "1", "material": "中性紙", "size": "A", "want": "", "unwant": "〇",
             "ordered_at": "", "shipped": "", "confirmed_at": "", "permanent": "〇",
             "hold": "", "hold_at": ""},
        ]
        self.store.import_line(
            line="LVC", table_name="看板_LVC", key_column="管理番号", key_category="TEXT",
            column_map=dict(DEFAULT_COLUMN_MAP),
            categories={n: "TEXT" for n in DEFAULT_COLUMN_MAP.values()},
            rows=rows, source_path="x",
        )
        # 共有DB(看板マスタ)。看板の表が無いファイルには、このツールは表を作らない
        self.shared = self.dir / "看板マスタ.sqlite3"
        c = sqlite3.connect(str(self.shared))
        c.execute("CREATE TABLE [看板_LVC] ([管理番号] TEXT)")
        c.commit()
        c.close()
        self.gateway = SharedDb(str(self.shared), cache_dir=str(self.dir / "cache"))

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def cycle(self) -> None:
        self.store.apply_transition("LVC", "1", models.order_button_changes, operation="order")
        self.store.apply_transition("LVC", "1", models.ship_button_changes, operation="ship")
        self.store.apply_transition(
            "LVC", "1", lambda i: models.order_button_changes(i, treat_as_delivered=True),
            operation="order")


class RecordAndSendTest(SharedFixture):
    def test_each_change_is_recorded_with_the_press(self):
        self.cycle()
        kinds = [e["kind"] for e in self.store.unsent_events()]
        self.assertEqual(kinds, [events.ORDERED, events.SHIPPED, events.DELIVERED])
        delivered = self.store.unsent_events()[-1]
        self.assertTrue(delivered["ordered_at"], "届いたときに、その回の出した日時を持っていない")
        self.assertEqual((delivered["material"], delivered["size"]), ("中性紙", "A"))

    def test_batch_operations_are_recorded_too(self):
        self.store.apply_transition("LVC", "1", models.order_button_changes, operation="order")
        self.store.apply_batch("LVC", lambda i: i.is_ordered, lambda i: models.batch_ship_changes(i),
                               operation="batch_ship")
        self.store.apply_batch("LVC", lambda i: i.is_ordered and i.is_shipped,
                               lambda i: models.batch_reset_changes(), operation="batch_reset")
        kinds = [e["kind"] for e in self.store.unsent_events()]
        self.assertEqual(kinds, [events.ORDERED, events.SHIPPED, events.DELIVERED])

    def test_they_reach_the_shared_history_once(self):
        """送ったあと「送った」と付ける前に落ちても、二重に入らない。"""
        self.cycle()
        pending = self.store.unsent_events()
        self.assertEqual(sync.export_events(self.store, self.gateway), 3)
        self.assertEqual(self.store.unsent_event_count(), 0)
        # 付ける前に落ちた、を作る(同じ出来事をもう一度送る)
        conn = self.store.connection
        conn.execute("UPDATE kanban_event SET sent = 0")
        conn.commit()
        sync.export_events(self.store, self.gateway)
        c = sqlite3.connect(str(self.shared))
        rows = c.execute("SELECT [ID], [出来事], [ライン], [管理番号] FROM [看板履歴]").fetchall()
        c.close()
        self.assertEqual(len(rows), 3, "二重に入った")
        self.assertEqual({r[0] for r in rows}, {e["uid"] for e in pending})

    def test_the_exporter_sends_them_with_the_state(self):
        self.cycle()
        exporter = sync.Exporter(self.store, self.gateway, interval_sec=0)
        exporter.run_once()
        self.assertEqual(self.store.unsent_event_count(), 0)

    def test_a_failing_write_back_is_recorded_not_raised(self):
        """書き戻しの 1 文が失敗しても、例外で周期ごと止めずに失敗として残す。

        失敗の文を組み立てるところで、Access の頃の ``error_number`` を参照して
        いたため例外になり、失敗も記録されず、その周期の残りも送られなかった。
        (この共有DBには 看板_LVC が無いので、書き戻しの文は失敗する)
        """
        self.store.apply_transition("LVC", "1", models.order_button_changes, operation="order")
        result = sync.export_pending(self.store, self.gateway)
        self.assertEqual(result.failed, 1)
        self.assertTrue(self.store.sync_failures() or self.store.pending_details())


class HistoryTableTest(SharedFixture):
    """共有DBの 看板履歴: 無ければ作る・足りない列を足す。"""

    def columns(self):
        c = sqlite3.connect(str(self.shared))
        try:
            return [r[1] for r in c.execute("PRAGMA table_info([看板履歴])")]
        finally:
            c.close()

    def test_it_is_created_when_missing(self):
        result = sync.ensure_history_table(self.gateway)
        self.assertTrue(result.ok and result.created)
        self.assertEqual(self.columns(), [name for name, _ in sync.HISTORY_COLUMNS])

    def test_missing_columns_are_added_and_nothing_is_lost(self):
        c = sqlite3.connect(str(self.shared))
        c.execute("CREATE TABLE [看板履歴] ([ID] TEXT, [日時] TEXT, [メモ] TEXT)")
        c.execute("INSERT INTO [看板履歴] VALUES ('old', '2026/09/01 09:00:00', '手で入れた')")
        c.commit()
        c.close()
        result = sync.ensure_history_table(self.gateway)
        self.assertTrue(result.ok)
        self.assertFalse(result.created)
        self.assertEqual(result.added_columns,
                         ["ライン", "管理番号", "出来事", "資材", "サイズ", "出した日時", "端末"])
        self.assertIn("メモ", self.columns(), "余分な列を消した")
        c = sqlite3.connect(str(self.shared))
        row = c.execute("SELECT [ID], [メモ], [ライン] FROM [看板履歴]").fetchone()
        c.close()
        self.assertEqual(row, ("old", "手で入れた", ""), "前からある行が消えた・足した列が空でない")

    def test_not_created_in_a_file_that_is_not_the_kanban_master(self):
        """接続先が別のツールの sqlite3(梱包資材マスタなど)なら、そこへ表を作らない。"""
        other = self.dir / "梱包資材マスタ.sqlite3"
        c = sqlite3.connect(str(other))
        c.execute("CREATE TABLE PalletMaster (幅 INTEGER)")
        c.commit()
        c.close()
        gateway = SharedDb(str(other), cache_dir=str(self.dir / "cache_other"))
        for ensure in (sync.ensure_history_table, sync.ensure_comment_table):
            result = ensure(gateway)
            self.assertFalse(result.ok)
            self.assertIn("看板の表が無い", result.message)
        self.store.apply_transition("LVC", "1", models.order_button_changes, operation="order")
        sync.Exporter(self.store, gateway, interval_sec=0).run_once()
        c = sqlite3.connect(str(other))
        names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        c.close()
        self.assertEqual(names, ["PalletMaster"], "別のツールのファイルに表を作った")
        self.assertEqual(self.store.unsent_event_count(), 1, "送れていないのに送ったことにした")

    def test_a_complete_table_is_left_alone(self):
        sync.ensure_history_table(self.gateway)
        again = sync.ensure_history_table(self.gateway)
        self.assertTrue(again.ok)
        self.assertEqual((again.created, again.added_columns), (False, []))

    def test_a_table_dropped_while_running_is_rebuilt_in_the_same_cycle(self):
        exporter = sync.Exporter(self.store, self.gateway, interval_sec=0)
        self.store.apply_transition("LVC", "1", models.order_button_changes, operation="order")
        exporter.run_once()
        c = sqlite3.connect(str(self.shared))
        c.execute("DROP TABLE [看板履歴]")
        c.execute("CREATE TABLE [看板履歴] ([ID] TEXT)")     # 列の欠けた表に置き換えられた
        c.commit()
        c.close()
        self.store.apply_transition("LVC", "1", models.order_button_changes, operation="order")
        exporter.run_once()
        self.assertEqual(self.store.unsent_event_count(), 0, "次の周期まで送れない")

    def test_the_exporter_repairs_a_short_table_before_sending(self):
        """列が欠けた表でも、送る前に足してから送る(記録が端末に溜まり続けない)。"""
        c = sqlite3.connect(str(self.shared))
        c.execute("CREATE TABLE [看板履歴] ([ID] TEXT, [日時] TEXT)")
        c.commit()
        c.close()
        self.cycle()
        sync.Exporter(self.store, self.gateway, interval_sec=0).run_once()
        self.assertEqual(self.store.unsent_event_count(), 0)
        c = sqlite3.connect(str(self.shared))
        kinds = [r[0] for r in c.execute("SELECT [出来事] FROM [看板履歴] ORDER BY [日時]")]
        c.close()
        self.assertEqual(sorted(kinds), sorted([events.ORDERED, events.SHIPPED, events.DELIVERED]))


if __name__ == "__main__":
    unittest.main()
