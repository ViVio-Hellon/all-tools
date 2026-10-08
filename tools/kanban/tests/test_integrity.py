"""押した・打ったものが消えない・戻らない・別の行に当たらないことの試験。

点検で見つかった次の穴を、それぞれ塞いだことを確かめる(画面の側は views/board.js・
views/settings.js。ここはサーバの側):

* 別のラインの盤を見て押した押下が、版が同じ別のラインの行に当たった(版は全行通し)
* 取り込みが、送り終えたばかりの行を送る前の値で戻した(錠・写し直し・書き戻しの番号)

      打った値が勝手に戻ることがありました

* 一括が、確認で訊いたあとに増えた行まで動かした(訊いた行と版だけを動かす)
* マスタで、この端末から送っていない発注のある看板を消せた・後から直した人が
  先に直した人の値を黙って消した
* 入れ替えが、画面で確かめたのと違うファイルを使えた
* 送っていないコメントが帯にも出ず、終了も待たなかった
* 片付けと同じ秒・時計の遅れた端末のコメントが、前の回へ畳まれて見えなくなった
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest import mock

from kanban import config
from kanban.db import sync
from kanban.db.shared import SharedDb
from kanban.db.store import DEFAULT_COLUMN_MAP, Store
from kanban.domain import models

from test_web_app import RouteTestBase

COLS = ("[管理番号] NUMERIC, [資材] TEXT, [サイズ] TEXT, [欲] TEXT, [不] TEXT, [更新日] TEXT,"
        " [発送] TEXT, [倉庫確認日時] TEXT, [常設品] TEXT, [保留] TEXT, [注文中日時] TEXT")


def make_shared(path: Path, lines=("LVC", "HVC"), count=4) -> None:
    db = sqlite3.connect(str(path))
    for line in lines:
        db.execute(f"CREATE TABLE [看板_{line}] ({COLS})")
        for i in range(1, count + 1):
            db.execute(f"INSERT INTO [看板_{line}] VALUES (?,?,?,'','〇','','','','〇','','')",
                       (i, "外装紙", f"{line}-{i}"))
    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# 版は全行通し
# ---------------------------------------------------------------------------
class GlobalRevTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="kanban_rev_"))
        self.store = Store(str(self.dir / "l.sqlite3"), host_name="PC")
        self.store.ensure_schema()
        rows = [{"mgmt_no": str(i), "material": "m", "size": f"s{i}", "unwant": "〇"} for i in range(1, 6)]
        for line in ("LVC", "HVC"):
            self.store.import_line(line, f"看板_{line}", "管理番号", "TEXT", dict(DEFAULT_COLUMN_MAP),
                                   {}, rows, "x")

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def revs(self):
        return [r[0] for r in self.store.connection.execute("SELECT rev FROM kanban_item")]

    def test_no_two_rows_share_a_rev(self):
        """LVC の 5 番と HVC の 5 番が同じ版にならない(別のラインの盤を見た押下が当たらない)。"""
        self.assertEqual(len(set(self.revs())), len(self.revs()))
        self.store.apply_transition("LVC", "5", models.order_button_changes, operation="order")
        self.store.apply_transition("HVC", "5", models.order_button_changes, operation="order")
        self.assertEqual(len(set(self.revs())), len(self.revs()))

    def test_rev_of_another_lines_row_is_a_conflict(self):
        from kanban.db.store import ConflictError

        lvc = self.store.item("LVC", "5").rev
        with self.assertRaises(ConflictError):
            self.store.apply_transition("HVC", "5", models.ship_button_changes, operation="ship",
                                        expected_rev=lvc)

    def test_old_files_with_shared_revs_are_renumbered_on_open(self):
        """前の版の作業用DB(行ごとに 1, 2, 3…)は、開いたときに通し番号へ振り直す。"""
        self.store.connection.execute("UPDATE kanban_item SET rev = 1")
        self.store.connection.execute("DELETE FROM meta WHERE key = 'rev_seq'")
        self.store.ensure_schema()
        self.assertEqual(len(set(self.revs())), len(self.revs()))
        top = max(self.revs())
        after = self.store.apply_transition("LVC", "1", models.order_button_changes, operation="order")
        self.assertGreater(after.rev, top)


# ---------------------------------------------------------------------------
# 取り込みが送り終えた行を戻さない
# ---------------------------------------------------------------------------
class ImportExportRaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="kanban_race_"))
        self.shared = self.dir / "看板マスタ.sqlite3"
        make_shared(self.shared, lines=("LVC",), count=3)
        self.gw = SharedDb(str(self.shared), cache_dir=str(self.dir / "cache"))
        self.store = Store(str(self.dir / "local.sqlite3"), host_name="LVC-PC")
        self.store.ensure_schema()
        sync.import_all(self.store, self.gw, lines=["LVC"])

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def shared_want(self, no=1):
        c = sqlite3.connect(str(self.shared))
        try:
            return c.execute("SELECT [欲] FROM [看板_LVC] WHERE [管理番号] = ?", (no,)).fetchone()[0]
        finally:
            c.close()

    def test_import_and_export_do_not_overlap(self):
        """取り込みが共有DBを読んでいるあいだ、書き戻しは待つ(錠を共にする)。"""
        self.store.apply_transition("LVC", "1", models.order_button_changes, operation="order")
        snap_taken, go_on = threading.Event(), threading.Event()
        real_read = self.gw.read_table

        def slow_read(name, **kw):
            snap = real_read(name, **kw)
            if name == "看板_LVC":
                snap_taken.set()
                go_on.wait(5)
            return snap

        self.gw.read_table = slow_read
        def run_import():
            try:
                sync.import_all(self.store, self.gw, lines=["LVC"])
            finally:
                self.store.close()   # このスレッドの接続

        def run_export():
            try:
                exported.append(sync.export_pending(
                    self.store, SharedDb(str(self.shared), cache_dir=str(self.dir / "c2"))))
            finally:
                self.store.close()

        exported = []
        importer = threading.Thread(target=run_import)
        importer.start()
        self.assertTrue(snap_taken.wait(5))
        exporter = threading.Thread(target=run_export)
        exporter.start()
        exporter.join(0.7)
        self.assertTrue(exporter.is_alive(), "取り込みの途中で書き戻しが走った")
        go_on.set()
        importer.join(10)
        exporter.join(10)
        self.gw.read_table = real_read
        self.assertEqual(exported[0].succeeded, 1)
        self.assertEqual(self.store.item("LVC", "1").want, "〇", "押した赤が戻った")
        self.assertEqual(self.shared_want(), "〇")

    def test_a_snapshot_read_before_the_export_does_not_revert_the_row(self):
        """別のプロセス(錠の外)で送り終えた行は、送る前に読んだ値で上書きしない。"""
        self.store.apply_transition("LVC", "1", models.order_button_changes, operation="order")
        before = self.store.sync_generation()
        stale = SharedDb(str(self.shared), cache_dir=str(self.dir / "c3")).read_table("看板_LVC")
        result = sync.export_pending(self.store, SharedDb(str(self.shared), cache_dir=str(self.dir / "c4")))
        self.assertEqual(result.succeeded, 1)
        self.assertEqual(self.store.pending_count(), 0)
        rows = [{"mgmt_no": str(int(r["管理番号"])), "material": r["資材"], "size": r["サイズ"],
                 "want": r["欲"], "unwant": r["不"], "permanent": r["常設品"]} for r in stale.rows]
        self.store.import_line("LVC", "看板_LVC", "管理番号", "NUMBER", dict(DEFAULT_COLUMN_MAP), {},
                               rows, "x", synced_before=before)
        self.assertEqual(self.store.item("LVC", "1").want, "〇", "送る前の値で戻した")
        # 次の取り込み(送ったあとに読んだもの)は普通に追いつく
        sync.import_all(self.store, self.gw, lines=["LVC"])
        self.assertEqual(self.store.item("LVC", "1").want, "〇")

    def test_writing_forgets_the_local_copy(self):
        """書いたら次は必ず写し直す(共有フォルダが大きさ・時刻を覚えて返しても古い写しを読まない)。"""
        self.gw.local_copy()
        self.assertFalse(self.gw._recopy)
        self.gw.execute([("UPDATE [看板_LVC] SET [欲] = '〇' WHERE [管理番号] = 1", [])])
        self.assertTrue(self.gw._recopy)


# ---------------------------------------------------------------------------
# コメントの回は、届いた順でも決める
# ---------------------------------------------------------------------------
class CommentRoundTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="kanban_round_"))
        self.store = Store(str(self.dir / "l.sqlite3"), host_name="LVC-PC")
        self.store.ensure_schema()
        self.store.connection.execute(
            "INSERT INTO kanban_item(line, mgmt_no, material, size, want, unwant, row_order, rev,"
            " updated_at, updated_by, dirty) VALUES('LVC','1','m','s','〇','',0,1,'x','x',0)")
        self.store.add_comment("LVC", "1", "現場", "前の回の問いかけ")
        self.store.close_comments("LVC", "1")
        self.close_at = self.store.connection.execute(
            "SELECT at FROM kanban_comment WHERE kind = '片付け'").fetchone()[0]

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def arrive(self, at, body):
        self.store.merge_comments([{"uid": uuid.uuid4().hex, "at": at, "line": "LVC", "mgmt_no": "1",
                                    "kind": "コメント", "side": "倉庫", "host": "SOUKO", "body": body}])

    def test_reply_in_the_same_second_as_the_close_stays_open_and_unread(self):
        self.arrive(self.close_at, "同じ秒の返事")
        thread = self.store.comment_thread("LVC", "1")
        self.assertIn("同じ秒の返事", [c["body"] for c in thread["open"]])
        self.assertEqual(self.store.comment_counts("LVC", "現場")["1"], (1, 1))

    def test_reply_from_a_terminal_whose_clock_is_behind_stays_open(self):
        self.arrive("2000/01/01 00:00:00", "時計が遅れた倉庫の返事")
        thread = self.store.comment_thread("LVC", "1")
        self.assertIn("時計が遅れた倉庫の返事", [c["body"] for c in thread["open"]])
        self.assertNotIn("時計が遅れた倉庫の返事", [c["body"] for c in thread["closed"]])

    def test_the_closed_round_stays_closed(self):
        thread = self.store.comment_thread("LVC", "1")
        self.assertEqual(thread["open"], [])
        self.assertIn("前の回の問いかけ", [c["body"] for c in thread["closed"]])


# ---------------------------------------------------------------------------
# 経路
# ---------------------------------------------------------------------------
class WarehouseRevRouteTest(RouteTestBase):
    MODE = config.MODE_WAREHOUSE
    LINE = ""

    def test_rev_is_required(self):
        """版の無い押下は通さない(どの盤を見て押したか分からない)。盤は返す。"""
        res = self.post("/api/board/ship", {"line": "LVC", "mgmt_no": "2"})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json["error"]["field"], "rev")
        self.assertIn("board", res.json)
        self.assertFalse(self.store.item("LVC", "2").is_shipped)

    def test_a_press_seen_on_another_lines_board_is_refused(self):
        """LS の盤を見て押した(タブを切り替えた直後)押下は、LVC の同じ番号の行に当たらない。"""
        seen_on_ls = self.rev("2", "LS")
        res = self.post("/api/board/ship", {"line": "LVC", "mgmt_no": "2", "rev": seen_on_ls})
        self.assertEqual(res.status_code, 409)
        self.assertFalse(self.store.item("LVC", "2").is_shipped)
        self.assertFalse(self.store.item("LS", "2").is_shipped)

    def test_batch_ship_moves_only_the_rows_that_were_confirmed(self):
        """訊いたときの行と版だけを動かし、変わっていた行は飛ばして知らせる。"""
        shown = self.batch_items("LVC")
        # 訊いたあとに 4 番が動いた(別の端末)・1 番に発注が付いた(訊いていない)
        self.store.apply_transition("LVC", "4", models.hold_button_changes, operation="hold")
        self.store.apply_transition("LVC", "1", models.order_button_changes, operation="order")
        res = self.post("/api/board/batch-ship", {"line": "LVC", "items": [i for i in shown if i["mgmt_no"] != "1"]})
        self.assertEqual(res.status_code, 200, res.json)
        self.assertEqual(res.json["done"], 1)
        self.assertTrue(self.store.item("LVC", "2").is_shipped)
        self.assertFalse(self.store.item("LVC", "4").is_shipped, "訊いたあとに変わった行を動かした")
        self.assertFalse(self.store.item("LVC", "1").is_shipped, "訊いていない行を動かした")
        self.assertIn("4", res.json["skipped"])

    def test_batch_without_items_is_refused(self):
        res = self.post("/api/board/batch-ship", {"line": "LVC"})
        self.assertEqual(res.status_code, 400)
        self.assertFalse(self.store.item("LVC", "2").is_shipped)

    def test_boards_carry_a_state_stamp_that_only_grows(self):
        """画面は古い状態の盤を描かない。そのための番号が盤に付き、押すたびに増える。"""
        first = self.get("/api/board?line=LVC").json["board"]["stamp"]
        after = self.post("/api/board/hold", {"line": "LVC", "mgmt_no": "2", "rev": self.rev("2")}).json
        self.assertGreater(after["board"]["stamp"], first)


class SiteBatchRouteTest(RouteTestBase):
    def test_batch_reset_skips_rows_changed_after_the_question(self):
        for no in ("1", "3"):
            self.store.apply_transition("LVC", no, models.order_button_changes, operation="order")
            self.store.apply_transition("LVC", no, models.ship_button_changes, operation="ship")
        shown = [i for i in self.batch_items() if i["mgmt_no"] in ("1", "3")]   # 画面が訊いた 2 件
        # 訊いたあとに 3 番だけ取り込みで版が進んだ
        self.store.connection.execute("UPDATE kanban_item SET rev = rev + 1000 WHERE line='LVC' AND mgmt_no='3'")
        res = self.post("/api/board/batch-reset", {"line": "LVC", "items": shown})
        self.assertEqual((res.status_code, res.json["done"], res.json["skipped"]), (200, 1, ["3"]))
        self.assertFalse(self.store.item("LVC", "1").is_ordered)
        self.assertTrue(self.store.item("LVC", "3").is_ordered)


class UndeliveredRouteTest(RouteTestBase):
    def test_unsent_comments_count_as_undelivered(self):
        """送っていないコメントも帯に出す(以前は看板の状態だけを数えていた)。"""
        self.store.add_comment("LVC", "2", "現場", "急ぎ")
        self.store.set_meta("undelivered_since", "2026/10/08 09:00:00")
        res = self.get("/api/status").json
        self.assertEqual(res["undelivered"], 1)
        self.assertEqual(res["undelivered_comments"], 1)
        self.assertEqual(res["undelivered_since"], "2026/10/08 09:00:00")
        self.store.mark_comments_sent([c["id"] for c in self.store.unsent_comments()])
        res = self.get("/api/status").json
        self.assertEqual((res["undelivered"], res["undelivered_since"]), (0, ""))

    def test_events_show_only_after_a_failed_send(self):
        self.store.apply_transition("LVC", "1", models.order_button_changes, operation="order")
        self.assertEqual(self.get("/api/status").json["undelivered_events"], 0, "押した直後から帯を出した")
        self.store.set_meta("undelivered_events_since", "2026/10/08 09:00:00")
        self.assertEqual(self.get("/api/status").json["undelivered_events"], 1)


class MasterGuardRouteTest(RouteTestBase):
    """マスタで、届いていない発注のある看板を消さない・先に直した人の値を消さない。"""

    TABLE = "看板_LVC"

    def setUp(self) -> None:
        super().setUp()
        self.shared = self.dir / "看板マスタ.sqlite3"
        conn = sqlite3.connect(str(self.shared))
        conn.execute(f"CREATE TABLE [{self.TABLE}] ({COLS})")
        conn.execute(f"INSERT INTO [{self.TABLE}] ([管理番号], [資材], [サイズ], [欲], [不]) VALUES (2, '外装紙', 'S', '', '〇')")
        conn.execute(f"INSERT INTO [{self.TABLE}] ([管理番号], [資材], [サイズ], [欲], [不]) VALUES (9, 'テープ', 'T', '', '〇')")
        conn.commit()
        conn.close()
        (self.dir / "config.json").write_text(json.dumps({
            "shared_db_path": str(self.shared), "admin_password_hash": config.hash_password("秘密"),
            "line": "LVC"}, ensure_ascii=False), encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"KANBAN_LOCAL_DIR": str(self.dir / "local")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def table(self):
        return self.get(f"/api/master/table?table={self.TABLE}").json

    def row(self, no):
        v = self.table()
        for r in v["rows"]:
            if str(r.get(config.COL_KEY)).strip() in (str(no), f"{no}.0"):
                return v, r
        raise AssertionError(no)

    def test_kanban_with_an_unsent_order_is_not_deleted(self):
        self.store.apply_transition("LVC", "2", models.order_button_changes, operation="order")
        v, r = self.row(2)
        res = self.post("/api/master/delete", {"table": self.TABLE, "row_key": r[v["row_key"]],
                                               "key": r[v["key_column"]], "password": "秘密"})
        self.assertEqual(res.status_code, 409, res.json)
        self.assertEqual(res.json["error"]["code"], "in_use")
        self.assertIn("届いていない", res.json["error"]["message"])
        c = sqlite3.connect(str(self.shared))
        self.assertEqual(c.execute(f"SELECT COUNT(*) FROM [{self.TABLE}] WHERE [管理番号] = 2").fetchone()[0], 1)
        c.close()

    def test_delete_rechecks_the_state_at_the_moment_of_deleting(self):
        """読んでから消すまでに別の端末の発注が届いたら消さない(DELETE の条件で確かめる)。"""
        from kanban.presenters import master

        v, r = self.row(9)
        real = master._write

        def order_arrives_first(shared, statements):
            c = sqlite3.connect(str(self.shared))
            c.execute(f"UPDATE [{self.TABLE}] SET [欲] = '〇', [不] = '' WHERE [管理番号] = 9")
            c.commit()
            c.close()
            return real(shared, statements)

        with mock.patch.object(master, "_write", side_effect=order_arrives_first):
            res = self.post("/api/master/delete", {"table": self.TABLE, "row_key": r[v["row_key"]],
                                                   "key": r[v["key_column"]], "password": "秘密"})
        self.assertEqual(res.status_code, 409, res.json)
        c = sqlite3.connect(str(self.shared))
        self.assertEqual(c.execute(f"SELECT COUNT(*) FROM [{self.TABLE}] WHERE [管理番号] = 9").fetchone()[0], 1)
        c.close()

    def test_update_is_refused_when_the_cell_changed_after_opening(self):
        v, r = self.row(9)
        c = sqlite3.connect(str(self.shared))
        c.execute(f"UPDATE [{self.TABLE}] SET [サイズ] = '別の端末' WHERE [管理番号] = 9")
        c.commit()
        c.close()
        res = self.post("/api/master/update", {"table": self.TABLE, "row_key": r[v["row_key"]],
                                               "key": r[v["key_column"]], "column": "サイズ",
                                               "value": "わたし", "before": r["サイズ"], "password": "秘密"})
        self.assertEqual(res.status_code, 409, res.json)
        c = sqlite3.connect(str(self.shared))
        self.assertEqual(c.execute(f"SELECT [サイズ] FROM [{self.TABLE}] WHERE [管理番号] = 9").fetchone()[0], "別の端末")
        c.close()
        # いまの値を見て直せば通る
        res = self.post("/api/master/update", {"table": self.TABLE, "row_key": r[v["row_key"]],
                                               "key": r[v["key_column"]], "column": "サイズ",
                                               "value": "わたし", "before": "別の端末", "password": "秘密"})
        self.assertEqual(res.status_code, 200, res.json)


class TableRefreshPlanRouteTest(RouteTestBase):
    """入れ替えは、画面で確かめた(読んだ)ファイルと同じときだけ。"""

    def setUp(self) -> None:
        super().setUp()
        from kanban import table_refresh

        self.addCleanup(table_refresh._cache.clear)
        cols = "[管理番号] NUMERIC, [資材] TEXT, [サイズ] TEXT, [欲] TEXT, [不] TEXT, [発送] TEXT"
        self.shared = self.dir / "看板マスタ.sqlite3"
        for path, rows in ((self.shared, [(1, "外装紙", "A")]),
                           (self.dir / "a.sqlite3", [(1, "外装紙A", "A")]),
                           (self.dir / "b.sqlite3", [(1, "外装紙B", "A")])):
            c = sqlite3.connect(str(path))
            c.execute(f"CREATE TABLE [看板_LVC] ({cols})")
            c.executemany("INSERT INTO [看板_LVC] VALUES (?, ?, ?, '', '〇', '')", rows)
            c.commit()
            c.close()
        (self.dir / "config.json").write_text(json.dumps({
            "shared_db_path": str(self.shared), "admin_password_hash": config.hash_password("秘密"),
            "line": "LVC"}, ensure_ascii=False), encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"KANBAN_LOCAL_DIR": str(self.dir / "local")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def plan(self, path):
        p = self.post("/api/table-refresh/plan", {"path": str(path), "password": "秘密"}).json["plan"]
        return {"source": p["source"], "size": p["source_facts"]["size"], "modified": p["source_facts"]["modified"]}

    def material(self):
        c = sqlite3.connect(str(self.shared))
        try:
            return c.execute("SELECT [資材] FROM [看板_LVC]").fetchone()[0]
        finally:
            c.close()

    def test_a_different_file_than_the_checked_one_is_refused(self):
        planned = self.plan(self.dir / "a.sqlite3")
        res = self.post("/api/table-refresh/run", {"path": str(self.dir / "b.sqlite3"), "plan": planned,
                                                   "tables": ["看板_LVC"], "password": "秘密"})
        self.assertEqual(res.status_code, 409, res.json)
        self.assertEqual(res.json["error"]["code"], "plan_mismatch")
        self.assertEqual(self.material(), "外装紙")

    def test_a_file_that_changed_after_checking_is_refused(self):
        planned = self.plan(self.dir / "a.sqlite3")
        planned["size"] = planned["size"] + 1
        res = self.post("/api/table-refresh/run", {"path": str(self.dir / "a.sqlite3"), "plan": planned,
                                                   "tables": ["看板_LVC"], "password": "秘密"})
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.material(), "外装紙")

    def test_the_checked_file_is_used(self):
        planned = self.plan(self.dir / "a.sqlite3")
        res = self.post("/api/table-refresh/run", {"path": str(self.dir / "a.sqlite3"), "plan": planned,
                                                   "tables": ["看板_LVC"], "password": "秘密"})
        self.assertEqual(res.status_code, 200, res.json)
        self.assertEqual(self.material(), "外装紙A")


if __name__ == "__main__":
    unittest.main()
