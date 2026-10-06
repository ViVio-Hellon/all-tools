"""看板ごとのコメント(倉庫 ⇔ 現場のやり取り)

* 看板ごとに書く。**届いた(赤を消した)ら画面から片付く**(共有DBには残る)
* 相手側が書いたものは未読。開いたら既読(端末ごと)
* 手元に積んで書き戻しと一緒に共有DBの [看板コメント] へ送り、他の端末は取り込みで受け取る
"""

from __future__ import annotations

import csv
import io
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from kanban import config
from kanban.db import sync
from kanban.db.shared import SharedDb
from kanban.db.store import DEFAULT_COLUMN_MAP, Store
from kanban.domain import models
from kanban.presenters import stats


def rows():
    return [
        {"mgmt_no": no, "material": "外装紙", "size": size, "want": "", "unwant": "〇",
         "ordered_at": "", "shipped": "", "confirmed_at": "", "permanent": "〇",
         "hold": "", "hold_at": ""}
        for no, size in (("1", "A"), ("2", "B"))
    ]


class Fixture(unittest.TestCase):
    """同じ共有DBを見る 現場の端末 と 倉庫の端末。"""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="kanban_comments_"))
        self.shared_path = self.dir / "看板マスタ.sqlite3"
        c = sqlite3.connect(str(self.shared_path))
        c.execute("CREATE TABLE [看板_LVC] ([管理番号] TEXT, [資材] TEXT, [サイズ] TEXT, [欲] TEXT,"
                  " [不] TEXT, [更新日] TEXT, [発送] TEXT, [倉庫確認日時] TEXT, [常設品] TEXT,"
                  " [保留] TEXT, [注文中日時] TEXT)")
        c.executemany("INSERT INTO [看板_LVC] VALUES (?, '外装紙', ?, '', '〇', '', '', '', '〇', '', '')",
                      [("1", "A"), ("2", "B")])
        c.commit()
        c.close()
        self.site = self.store("site", "PC-SITE")
        self.wh = self.store("wh", "PC-WH")

    def store(self, name: str, host: str) -> Store:
        st = Store(str(self.dir / f"{name}.sqlite3"), host_name=host)
        st.ensure_schema()
        st.import_line(
            line="LVC", table_name="看板_LVC", key_column="管理番号", key_category="TEXT",
            column_map=dict(DEFAULT_COLUMN_MAP),
            categories={n: "TEXT" for n in DEFAULT_COLUMN_MAP.values()},
            rows=rows(), source_path="x",
        )
        self.addCleanup(st.close)
        return st

    def gateway(self, name: str) -> SharedDb:
        return SharedDb(str(self.shared_path), cache_dir=str(self.dir / f"cache_{name}"))

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def send(self, store: Store, name: str) -> None:
        sync.Exporter(store, self.gateway(name), interval_sec=0).run_once()

    def receive(self, store: Store, name: str) -> None:
        sync.import_all(store, self.gateway(name), lines=["LVC"])


def arrived(at: str, side: str, body: str, uid: str = "u1") -> dict:
    """他の端末が書いて、取り込みで届いたコメント。"""
    return {"uid": uid, "at": at, "line": "LVC", "mgmt_no": "1", "kind": "コメント",
            "side": side, "host": "PC-OTHER", "body": body}


class StoreTest(Fixture):
    def test_unread_is_what_the_other_side_wrote(self):
        self.site.add_comment("LVC", "1", "現場", "急ぎでお願いします")
        self.assertEqual(self.site.comment_counts("LVC", "現場"), {"1": (1, 0)}, "自分で書いたものが未読")
        self.site.merge_comments([arrived("2099/01/01 10:00:00", "倉庫", "了解です")])
        self.assertEqual(self.site.comment_counts("LVC", "現場"), {"1": (2, 1)})

    def test_opening_marks_it_read(self):
        self.site.merge_comments([arrived("2099/01/01 10:00:00", "倉庫", "来週入荷です")])
        self.assertEqual(self.site.comment_counts("LVC", "現場")["1"], (1, 1))
        self.site.mark_comments_read("LVC", "1")
        self.assertEqual(self.site.comment_counts("LVC", "現場")["1"], (1, 0))

    def test_stage_buttons_mark_but_keep_the_round_open_until_delivery(self):
        """赤 → 一言 → 倉庫の黄・緑では畳まない(確認の区切りが入るだけ)。届いたら前の回へ畳む。"""
        self.site.apply_transition("LVC", "1", models.order_button_changes, operation="order")
        self.site.add_comment("LVC", "1", "現場", "急ぎ")
        self.site.apply_transition("LVC", "1", models.ship_button_changes, operation="ship")
        thread = self.site.comment_thread("LVC", "1")
        self.assertEqual([(c["kind"], c["body"]) for c in thread["open"]],
                         [("コメント", "急ぎ"), ("確認", "倉庫が発送(緑)にした")])
        self.assertEqual(thread["closed"], [])
        self.site.apply_transition(
            "LVC", "1", lambda i: models.order_button_changes(i, treat_as_delivered=True), operation="order")
        thread = self.site.comment_thread("LVC", "1")
        self.assertEqual(thread["open"], [])
        self.assertEqual([c["body"] for c in thread["closed"]],
                         ["急ぎ", "倉庫が発送(緑)にした", "現場が受け取った(赤を消した)"])
        self.assertEqual(self.site.comment_counts("LVC", "倉庫"), {})

    def test_pressing_marks_the_other_sides_comment_read(self):
        """押す前に中身を見せているので、押した側が読んだことにする(書いた側に ✓ 既読)。"""
        self.site.apply_transition("LVC", "1", models.order_button_changes, operation="order")
        self.site.add_comment("LVC", "1", "現場", "100 個でお願いします")
        self.site.apply_transition("LVC", "1", models.hold_button_changes, operation="hold")
        opened = self.site.comment_thread("LVC", "1")["open"]
        self.assertEqual(opened[0]["body"], "100 個でお願いします")
        self.assertEqual(opened[0]["read"]["side"], "倉庫")
        self.assertEqual(opened[1]["body"], "倉庫が注文中(黄)にした")

    def test_a_whole_round_reads_in_order(self):
        """赤 → 現場の一言 → 倉庫の返事 → 黄 → 倉庫の一言 → 緑: どれも開いたまま、区切りつきで並ぶ。"""
        self.site.apply_transition("LVC", "1", models.order_button_changes, operation="order")
        self.site.add_comment("LVC", "1", "現場", "100 個で")
        self.site.add_comment("LVC", "1", "倉庫", "了解")
        self.site.apply_transition("LVC", "1", models.hold_button_changes, operation="hold")
        self.site.add_comment("LVC", "1", "倉庫", "在庫切れ、来週入荷")
        self.site.apply_transition("LVC", "1", models.ship_button_changes, operation="ship")
        self.assertEqual([c["body"] for c in self.site.comment_thread("LVC", "1")["open"]],
                         ["100 個で", "了解", "倉庫が注文中(黄)にした", "在庫切れ、来週入荷", "倉庫が発送(緑)にした"])
        self.assertEqual(self.site.comment_counts("LVC", "現場")["1"][0], 3, "盤の 💬 は届くまで数えたまま")

    def test_a_mark_is_added_only_after_new_comments(self):
        """区切りの後ろに新しいコメントが無ければ、押しても区切りを増やさない。"""
        self.site.apply_transition("LVC", "1", models.order_button_changes, operation="order")
        self.site.add_comment("LVC", "1", "現場", "急ぎ")
        self.site.apply_transition("LVC", "1", models.hold_button_changes, operation="hold")
        self.site.apply_transition("LVC", "1", models.hold_button_changes, operation="hold")   # 外した
        marks = [c for c in self.site.comment_thread("LVC", "1")["open"] if c["kind"] == "確認"]
        self.assertEqual(len(marks), 1)

    def test_a_cancel_clears_it_too(self):
        self.site.apply_transition("LVC", "1", models.order_button_changes, operation="order")
        self.site.add_comment("LVC", "1", "現場", "間違えました")
        self.site.apply_transition("LVC", "1", models.order_button_changes, operation="order")
        self.assertEqual(self.site.comment_thread("LVC", "1")["open"], [])
        self.assertEqual(self.site.comment_thread("LVC", "1")["closed"][-1]["body"], "現場が赤を取り消した")

    def test_nothing_to_close_adds_no_mark(self):
        """コメントの無い看板を押しても、片付けの印は積まない(押すたびに行を増やさない)。"""
        self.site.apply_transition("LVC", "1", models.order_button_changes, operation="order")
        self.site.apply_transition("LVC", "1", models.ship_button_changes, operation="ship")
        n = self.site.connection.execute("SELECT COUNT(*) FROM kanban_comment").fetchone()[0]
        self.assertEqual(n, 0)

    def test_a_late_comment_is_unread_even_if_it_was_written_earlier(self):
        """共有フォルダに届かない間に書かれ、後から届いたコメント(書いた日時は古い)も未読。"""
        self.site.merge_comments([arrived("2026/09/27 10:10:00", "倉庫", "一通目", uid="a")])
        self.site.mark_comments_read("LVC", "1")
        self.site.merge_comments([arrived("2026/09/27 10:00:00", "倉庫", "遅れて届いた", uid="b")])
        self.assertEqual(self.site.comment_counts("LVC", "現場")["1"], (2, 1))

    def test_merging_the_same_comment_twice_adds_it_once(self):
        row = {"uid": "u1", "at": "2026/09/27 10:00:00", "line": "LVC", "mgmt_no": "1",
               "kind": "コメント", "side": "倉庫", "host": "PC-WH", "body": "x"}
        self.assertEqual(self.site.merge_comments([row]), 1)
        self.assertEqual(self.site.merge_comments([row]), 0)


class TwoTerminalsTest(Fixture):
    """書いた端末 → 共有DB → 相手の端末。"""

    def test_a_comment_reaches_the_other_terminal(self):
        self.wh.add_comment("LVC", "1", "倉庫", "来週入荷です")
        self.send(self.wh, "wh")
        c = sqlite3.connect(str(self.shared_path))
        cols = [r[1] for r in c.execute("PRAGMA table_info([看板コメント])")]
        c.close()
        self.assertEqual(cols, [n for n, _ in sync.COMMENT_COLUMNS], "共有DBに表が用意されていない")
        self.receive(self.site, "site")
        self.assertEqual(self.site.comment_counts("LVC", "現場"), {"1": (1, 1)})
        self.assertEqual(self.site.comment_thread("LVC", "1")["open"][0]["body"], "来週入荷です")
        self.assertEqual(self.wh.unsent_comment_count(), 0)

    def test_the_delivery_clears_it_on_the_other_terminal_too(self):
        self.site.apply_transition("LVC", "1", models.order_button_changes, operation="order")
        self.send(self.site, "site")
        self.receive(self.wh, "wh")
        self.wh.add_comment("LVC", "1", "倉庫", "来週入荷です")
        self.send(self.wh, "wh")
        self.receive(self.site, "site")
        self.site.apply_transition("LVC", "1", models.order_button_changes, operation="order")  # 赤を消した
        self.send(self.site, "site")
        self.receive(self.wh, "wh")
        self.assertEqual(self.wh.comment_thread("LVC", "1")["open"], [], "倉庫の画面に残ったまま")

    def test_sending_twice_does_not_duplicate(self):
        self.site.add_comment("LVC", "1", "現場", "急ぎ")
        self.send(self.site, "site")
        self.site.connection.execute("UPDATE kanban_comment SET sent = 0")
        self.site.connection.commit()
        self.send(self.site, "site")
        c = sqlite3.connect(str(self.shared_path))
        n = c.execute("SELECT COUNT(*) FROM [看板コメント] WHERE [種類] = 'コメント'").fetchone()[0]
        c.close()
        self.assertEqual(n, 1)

    def test_a_table_dropped_while_running_is_rebuilt_in_the_same_cycle(self):
        """動いている間に 看板コメント を消されても、次に送るとき(同じ周期で)作り直して送る。"""
        exporter = sync.Exporter(self.site, self.gateway("site"), interval_sec=0)
        self.site.add_comment("LVC", "1", "現場", "一通目")
        exporter.run_once()
        c = sqlite3.connect(str(self.shared_path))
        c.execute("DROP TABLE [看板コメント]")
        c.commit()
        c.close()
        self.site.add_comment("LVC", "1", "現場", "二通目")
        exporter.run_once()
        self.assertEqual(self.site.unsent_comment_count(), 0, "次の周期まで送れない")

    def test_a_short_table_gets_its_columns(self):
        c = sqlite3.connect(str(self.shared_path))
        c.execute("CREATE TABLE [看板コメント] ([ID] TEXT, [日時] TEXT)")
        c.commit()
        c.close()
        result = sync.ensure_comment_table(self.gateway("x"))
        self.assertTrue(result.ok)
        self.assertEqual(result.added_columns, ["ライン", "管理番号", "種類", "書いた側", "端末", "本文"])

    def test_the_detail_csv_carries_the_conversation(self):
        """看板集計の明細 CSV に、その回のやり取りが載る。"""
        self.site.apply_transition("LVC", "1", models.order_button_changes, operation="order")
        self.site.add_comment("LVC", "1", "現場", "急ぎ")
        self.site.apply_transition("LVC", "1", models.ship_button_changes, operation="ship")
        self.site.apply_transition(
            "LVC", "1", lambda i: models.order_button_changes(i, treat_as_delivered=True), operation="order")
        self.send(self.site, "site")
        data = stats.load(self.gateway("stats"))
        months = [stats.month_key(stats.datetime.now())]
        table = list(csv.reader(io.StringIO(stats.build_csv("cycles", data, months))))
        col = table[0].index("コメント")
        self.assertIn("現場: 急ぎ", table[1][col])


if __name__ == "__main__":
    unittest.main()


class SharedReadTest(Fixture):
    """**既読は側ごとに共有する。** 倉庫の 1 台が読めば倉庫の全端末で既読、現場はそのラインの端末で共有。"""

    def setUp(self) -> None:
        super().setUp()
        self.wh2 = self.store("wh2", "PC-WH2")
        self.site2 = self.store("site2", "PC-SITE2")

    def sync_all(self) -> None:
        for st, name in ((self.site, "site"), (self.wh, "wh"), (self.wh2, "wh2"), (self.site2, "site2")):
            self.send(st, name)
        for st, name in ((self.site, "site"), (self.wh, "wh"), (self.wh2, "wh2"), (self.site2, "site2")):
            self.receive(st, name)

    def unread(self, store: Store, side: str) -> int:
        return store.comment_counts("LVC", side).get("1", (0, 0))[1]

    def test_one_warehouse_terminal_reading_clears_it_for_all_warehouse_terminals(self):
        self.site.add_comment("LVC", "1", "現場", "急ぎでお願いします")
        self.sync_all()
        self.assertEqual((self.unread(self.wh, "倉庫"), self.unread(self.wh2, "倉庫")), (1, 1))
        self.assertTrue(self.wh.mark_comments_read("LVC", "1", share_side="倉庫"))
        self.sync_all()
        self.assertEqual(self.unread(self.wh2, "倉庫"), 0, "倉庫のほかの端末で未読のまま")
        # 書いた側には「倉庫が読んだ」と見える
        thread = self.site.comment_thread("LVC", "1")
        self.assertEqual(thread["open"][0]["read"]["side"], "倉庫")
        self.assertEqual(thread["open"][0]["read"]["host"], "PC-WH")
        # 現場のほかの端末でも(書いたのは同じ側なので未読にはならない)
        self.assertEqual(self.unread(self.site2, "現場"), 0)

    def test_site_terminals_of_the_line_share_it_too(self):
        self.wh.add_comment("LVC", "1", "倉庫", "来週入荷です")
        self.sync_all()
        self.assertEqual((self.unread(self.site, "現場"), self.unread(self.site2, "現場")), (1, 1))
        self.assertEqual(self.site.comment_thread("LVC", "1")["open"][0]["read"], None)
        self.site2.mark_comments_read("LVC", "1", share_side="現場")
        self.sync_all()
        self.assertEqual(self.unread(self.site, "現場"), 0)
        self.assertEqual(self.wh.comment_thread("LVC", "1")["open"][0]["read"]["host"], "PC-SITE2")

    def test_one_sides_read_does_not_count_for_the_other_side(self):
        """現場が開いても、倉庫の未読は消えない(読んだのは現場)。"""
        self.site.add_comment("LVC", "1", "現場", "急ぎ")
        self.wh.add_comment("LVC", "1", "倉庫", "了解")
        self.sync_all()
        self.site2.mark_comments_read("LVC", "1", share_side="現場")
        self.sync_all()
        self.assertEqual(self.unread(self.wh2, "倉庫"), 1)

    def test_writing_a_reply_means_you_read_it(self):
        self.site.add_comment("LVC", "1", "現場", "急ぎ")
        self.sync_all()
        self.wh.add_comment("LVC", "1", "倉庫", "了解")
        self.sync_all()
        self.assertEqual(self.unread(self.wh2, "倉庫"), 0)

    def test_view_terminal_does_not_read_for_the_warehouse(self):
        """倉庫参照は見るだけ。開いても倉庫の端末の未読は消さない。"""
        self.site.add_comment("LVC", "1", "現場", "急ぎ")
        self.sync_all()
        self.assertFalse(self.wh.mark_comments_read("LVC", "1"))   # 共有しない(この端末の中だけ)
        self.assertEqual(self.unread(self.wh, "倉庫"), 0)
        self.sync_all()
        self.assertEqual(self.unread(self.wh2, "倉庫"), 1)

    def test_opening_again_adds_no_more_marks(self):
        self.site.add_comment("LVC", "1", "現場", "急ぎ")
        self.sync_all()
        self.assertTrue(self.wh.mark_comments_read("LVC", "1", share_side="倉庫"))
        self.assertFalse(self.wh.mark_comments_read("LVC", "1", share_side="倉庫"))
        self.sync_all()
        self.assertFalse(self.wh2.mark_comments_read("LVC", "1", share_side="倉庫"), "読んだものにもう一度印")
        # 新しいコメントが来たら、また未読・また印
        self.site.add_comment("LVC", "1", "現場", "やっぱり明日で")
        self.sync_all()
        self.assertEqual(self.unread(self.wh2, "倉庫"), 1)
        self.assertTrue(self.wh2.mark_comments_read("LVC", "1", share_side="倉庫"))
        self.sync_all()
        self.assertEqual(self.unread(self.wh, "倉庫"), 0)

    def test_marks_are_not_counted_as_comments(self):
        self.site.add_comment("LVC", "1", "現場", "急ぎ")
        self.sync_all()
        self.wh.mark_comments_read("LVC", "1", share_side="倉庫")
        self.sync_all()
        self.assertEqual(self.wh2.comment_counts("LVC", "倉庫")["1"], (1, 0))
        self.assertEqual([c["body"] for c in self.site2.comment_thread("LVC", "1")["open"]], ["急ぎ"])
        data = stats.load(self.gateway("stats"))
        self.assertEqual([c["body"] for c in data.comments], ["急ぎ"])
