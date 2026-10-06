"""看板履歴のファイル(kanban/db/history.py)

看板履歴は看板マスタとは**別のファイル**(``看板履歴.sqlite3``)に置く。
各端末はそこへ出来事を送り、看板集計はそこを読む。看板マスタに残っている以前の
記録は、まだ無い行だけ写す。
"""

from __future__ import annotations

import sqlite3

from kanban import config
from kanban.db import history, sync
from kanban.db.history import HistoryDb
from kanban.domain import events, models
from kanban.presenters import stats

from test_events import SharedFixture


class PathTest(SharedFixture):
    def test_default_is_next_to_the_shared_db(self):
        cfg = config.Config(shared_db_path=str(self.shared))
        self.assertEqual(cfg.resolved_history_db_path(), str(self.dir / "看板履歴.sqlite3"))
        self.assertEqual(config.Config(history_db_path="x.sqlite3").resolved_history_db_path(), "x.sqlite3")

    def test_what_may_be_the_history_file(self):
        self.assertEqual(history.path_problem(str(self.dir / "看板履歴.sqlite3")), "", "まだ無いファイルは作れる")
        self.assertIn("フォルダが見つかりません", history.path_problem(str(self.dir / "無い" / "a.sqlite3")))
        self.assertIn("sqlite3 のファイル名", history.path_problem(str(self.dir / "a.txt")))
        self.assertIn("看板マスタ", history.path_problem(str(self.shared)))
        other = self.dir / "梱包資材マスタ.sqlite3"
        c = sqlite3.connect(str(other))
        c.execute("CREATE TABLE BoardMaster (a)")
        c.commit()
        c.close()
        self.assertIn("別のツール", history.path_problem(str(other)))
        self.assertIn("場所が空", history.path_problem(""))


class FileTest(SharedFixture):
    def setUp(self) -> None:
        super().setUp()
        self.path = self.dir / "看板履歴.sqlite3"
        self.history = HistoryDb(str(self.path), cache_dir=str(self.dir / "cache"))

    def rows(self, path=None):
        c = sqlite3.connect(str(path or self.path))
        try:
            return c.execute("SELECT [ID], [出来事] FROM [看板履歴] ORDER BY rowid").fetchall()
        finally:
            c.close()

    def test_the_file_is_made_when_missing(self):
        result = sync.ensure_history_table(self.history)
        self.assertTrue(result.ok, result.message)
        self.assertTrue(result.created)
        self.assertIn("作りました", result.message)
        c = sqlite3.connect(str(self.path))
        cols = [r[1] for r in c.execute("PRAGMA table_info([看板履歴])")]
        c.close()
        self.assertEqual(cols, [n for n, _ in sync.HISTORY_COLUMNS])
        again = sync.ensure_history_table(HistoryDb(str(self.path), cache_dir=str(self.dir / "cache")))
        self.assertTrue(again.ok)
        self.assertFalse(again.created)

    def test_events_go_to_the_history_file_not_the_kanban_master(self):
        self.cycle()
        exporter = sync.Exporter(self.store, self.gateway, interval_sec=0, history_gateway=self.history)
        exporter.run_once()
        self.assertEqual(self.store.unsent_event_count(), 0)
        self.assertEqual([k for _, k in self.rows()],
                         [events.ORDERED, events.SHIPPED, events.DELIVERED])
        c = sqlite3.connect(str(self.shared))
        names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        c.close()
        self.assertNotIn("看板履歴", names, "看板マスタにも書いている")

    def test_old_records_in_the_kanban_master_are_copied_once(self):
        """以前の置き場所(看板マスタの看板履歴)の行を、**まだ無いものだけ**写す。元は消さない。"""
        self.cycle()
        sync.export_events(self.store, self.gateway)          # 以前の版と同じく看板マスタへ
        sync.ensure_history_table(self.history)
        self.assertEqual(history.copy_from_shared(self.gateway, self.history), 3)
        self.assertEqual(len(self.rows()), 3)
        self.gateway.forget_copy()
        self.assertEqual(history.copy_from_shared(self.gateway, self.history), 0, "2 回入った")
        # 入れ替えの途中で古い版の端末が看板マスタへ書いた分も、次に写る
        self.store.apply_transition("LVC", "1", models.order_button_changes, operation="order")
        sync.export_events(self.store, self.gateway)
        self.gateway.forget_copy()
        self.assertEqual(history.copy_from_shared(self.gateway, self.history), 1)
        self.assertEqual(len(self.rows()), 4)
        self.assertEqual(len(self.rows(self.shared)), 4, "看板マスタの表を消した")

    def test_nothing_to_copy_when_the_kanban_master_has_no_history(self):
        sync.ensure_history_table(self.history)
        self.assertEqual(history.copy_from_shared(self.gateway, self.history), 0)

    def test_stats_read_the_history_file(self):
        self.cycle()
        sync.Exporter(self.store, self.gateway, interval_sec=0, history_gateway=self.history).run_once()
        data = stats.load(self.gateway, self.history)
        self.assertTrue(data.available)
        self.assertEqual(data.history_path, str(self.path))
        self.assertEqual(len(data.events), 3)
        # 看板履歴.sqlite3 がまだ無ければ、看板マスタの看板履歴(以前の置き場所)を読む
        missing = HistoryDb(str(self.dir / "まだ無い.sqlite3"), cache_dir=str(self.dir / "cache"))
        self.assertEqual(stats.load(self.gateway, missing).events, [])

    def test_history_csv_is_the_records_themselves(self):
        self.cycle()
        sync.Exporter(self.store, self.gateway, interval_sec=0, history_gateway=self.history).run_once()
        data = stats.load(self.gateway, self.history)
        month = data.events[0].at.strftime("%Y-%m")
        text = stats.build_csv("history", data, [month])
        lines = text.split("\r\n")
        self.assertEqual(lines[0].split(","), list(stats.HISTORY_CSV_COLUMNS))
        self.assertEqual([ln.split(",")[4] for ln in lines[1:4]],
                         [events.ORDERED, events.SHIPPED, events.DELIVERED])
        self.assertIn(config.display_name("LVC"), lines[1])
        # 期間の外・ほかのラインは出さない
        self.assertEqual(stats.build_csv("history", data, ["2000-01"]).strip().count("\r\n"), 0)
        self.assertEqual(stats.build_csv("history", data, [month], "LS").strip().count("\r\n"), 0)
        self.assertEqual(stats.csv_file_name("history", [month, month], "LVC"),
                         f"看板履歴_{month}_{month}_{config.display_name('LVC')}")
