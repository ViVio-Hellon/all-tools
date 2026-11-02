"""看板コメントは**いまの接続先(共有DB)にあるもの**だけを出す・数える・送る

現場の指摘(1.5.81)::

    参照パス変えても参照してなくない？ コメントが看板マスタ上には残っていないのに残り続ける。
    何度も何度も変えたものが残ってるとか勝手に増えるとか本当に嫌。別のPCを見てきたら同じ
    参照パスでコメントは見えなかった。コメントはそのPCローカルで残してる？？
    コメントしていないのに終了しようとすると「まだ送っていないコメントがあります」とでる

原因: 取り込みはコメントを**足すだけ**(共有DBで消えたものを手元から消さない)で、接続先を
変えても手元の作業用DBのコメント・看板・未送信の印をそのまま持ち越していた。前の接続先の
コメントが新しい接続先の盤に出続け(💬の数)、前の接続先宛ての未送信は新しい接続先へ送られた。
未送信の数には、開いただけで積む既読の印・押しただけで積む確認の印まで「コメント」として数えていた。
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kanban.db import sync
from kanban.db.shared import SharedDb, SharedDbError
from kanban.db.store import Store
from kanban.domain import models

LINE = "AIM"
#: 接続先を変えたので前の接続先へ送らなかった、の知らせ(kanban.db.store.NOTICE_SWITCHED)
NOTICE_SWITCHED = "switched"
TABLE = "看板_AIM"


def make_shared(path: Path, items: list[tuple], comments: list[tuple] = ()) -> None:
    """看板マスタ(共有DB)を作る。管理番号は現場のファイルと同じく数値と文字が混ざる。"""
    c = sqlite3.connect(str(path))
    c.execute(f'CREATE TABLE "{TABLE}" ("管理番号", "資材", "サイズ", "欲", "不", "更新日", "発送",'
              ' "倉庫確認日時", "常設品", "保留", "注文中日時")')
    c.executemany(f'INSERT INTO "{TABLE}" VALUES (?, ?, ?, ?, ?, "", "", "", "〇", "", "")', items)
    c.execute('CREATE TABLE "Form状態管理" ("管理番号", "ライン名", "状態")')
    c.execute("CREATE TABLE [看板コメント] ([ID] TEXT NOT NULL DEFAULT '', [日時] TEXT NOT NULL DEFAULT '',"
              " [ライン] TEXT NOT NULL DEFAULT '', [管理番号] TEXT NOT NULL DEFAULT '',"
              " [種類] TEXT NOT NULL DEFAULT '', [書いた側] TEXT NOT NULL DEFAULT '',"
              " [端末] TEXT NOT NULL DEFAULT '', [本文] TEXT NOT NULL DEFAULT '')")
    c.executemany("INSERT INTO [看板コメント] VALUES (?, ?, ?, ?, ?, ?, ?, ?)", comments)
    c.commit()
    c.close()


def shared_comments(path: Path) -> list[tuple]:
    c = sqlite3.connect(str(path))
    try:
        return c.execute("SELECT [ID], [管理番号], [種類], [本文] FROM [看板コメント] ORDER BY rowid").fetchall()
    finally:
        c.close()


def comment(uid: str, no: str, body: str, side: str = "倉庫", at: str = "2099/01/01 10:00:00") -> tuple:
    return (uid, at, LINE, no, "コメント", side, "PC-OTHER", body)


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="kanban_comment_source_"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        # 共有A: 赤の付いた看板 1〜4 と、そのやり取り(5・3・1・1 件)
        self.a = self.dir / "A" / "看板マスタ.sqlite3"
        self.a.parent.mkdir()
        ordered = [(1, "中性紙", "1600 × 150M : 4本", "〇", ""), (2, "中性紙", "1600 × 150M : 2本", "〇", ""),
                   (3, "中性紙", "1300 × 100M : 4本", "〇", ""), (4, "中性紙", "1300 × 100M : 2本", "〇", ""),
                   ("5", "HB", "1600 × 2500", "", "〇")]
        talk = ([comment(f"a1-{i}", "1", f"一{i}") for i in range(5)]
                + [comment(f"a2-{i}", "2", f"二{i}") for i in range(3)]
                + [comment("a3", "3", "三"), comment("a4", "4", "四")])
        make_shared(self.a, ordered, talk)
        # 共有B: 同じ看板の番号だが中身は別(赤なし)・コメントは 0 行(現場から届いた b.sqlite3 と同じ形)
        self.b = self.dir / "B" / "看板マスタ.sqlite3"
        self.b.parent.mkdir()
        make_shared(self.b, [(1, "中性紙B", "1600 × 150M : 4本", "", "〇"), (2, "中性紙B", "x", "", "〇"),
                             (3, "中性紙B", "y", "", "〇"), (4, "中性紙B", "z", "", "〇"),
                             ("5", "HB", "1600 × 2500", "", "〇")])

    def store(self, name: str = "pc1") -> Store:
        st = Store(str(self.dir / f"{name}.sqlite3"), host_name=name.upper())
        st.ensure_schema()
        self.addCleanup(st.close)
        return st

    def gw(self, path: Path, name: str = "pc1") -> SharedDb:
        return SharedDb(str(path), cache_dir=str(self.dir / f"cache_{name}"))

    def take(self, st: Store, path: Path, name: str = "pc1"):
        return sync.import_all(st, self.gw(path, name), lines=[LINE])

    def send(self, st: Store, path: Path, name: str = "pc1"):
        return sync.Exporter(st, self.gw(path, name), interval_sec=0).run_once()

    def shown(self, st: Store) -> dict[str, int]:
        """盤の 💬 の数(看板ごと)。"""
        return {no: n for no, (n, _u) in st.comment_counts(LINE, "現場").items()}


class SwitchSourceTest(Fixture):
    """(a) 参照パス(接続先)を A → B に変えたら、画面も取り込みも B だけを見る。"""

    def test_comments_of_the_old_shared_db_are_not_shown_after_switching(self):
        st = self.store()
        self.take(st, self.a)
        self.assertEqual(self.shown(st), {"1": 5, "2": 3, "3": 1, "4": 1})
        self.take(st, self.b)
        self.assertEqual(self.shown(st), {}, "前の接続先(共有A)のコメントが共有B の盤に残っている")
        self.assertEqual(st.connection.execute("SELECT COUNT(*) FROM kanban_comment").fetchone()[0], 0)

    def test_the_board_shows_the_new_shared_db(self):
        st = self.store()
        self.take(st, self.a)
        self.take(st, self.b)
        items = {i.mgmt_no: i for i in st.items(LINE)}
        self.assertEqual(items["1"].material, "中性紙B")
        self.assertFalse(items["1"].is_ordered, "共有A の赤が共有B の盤に残っている")

    def test_nothing_from_the_old_shared_db_is_sent_to_the_new_one(self):
        """共有A で書いて送れていないコメント・押して送れていない操作は、共有B へは送らない。"""
        st = self.store()
        self.take(st, self.a)
        st.mark_comments_read(LINE, "1", share_side="現場")      # 開いた(既読の印)
        st.add_comment(LINE, "1", "現場", "共有A 宛て")
        st.apply_transition(LINE, "5", models.order_button_changes, operation="order")
        # 共有A へ送る前に接続先を B に変えた。書き戻しが先に回っても B へは送らない
        self.send(st, self.b)
        self.take(st, self.b)
        self.send(st, self.b)
        self.assertEqual(shared_comments(self.b), [], "共有A 宛てのコメント・印が共有B に届いた")
        c = sqlite3.connect(str(self.b))
        want = c.execute(f'SELECT "欲" FROM "{TABLE}" WHERE "管理番号" = ?', ("5",)).fetchone()[0]
        c.close()
        self.assertEqual(want, "", "共有A で押した発注が共有B に書かれた")
        self.assertEqual(st.unsent().comments, 0)
        self.assertEqual(st.pending_count(), 0)
        # 黙って捨てない: 画面の帯に出す(前の接続先へ届かなかったコメント・操作)
        notes = [n for n in st.dropped_notices() if n["reason"] == NOTICE_SWITCHED]
        self.assertEqual(sorted(n["mgmt_no"] for n in notes), ["1", "5"])
        self.assertIn("共有A 宛て", " ".join(n["what"] for n in notes))

    def test_switching_back_and_forth_does_not_pile_up(self):
        st = self.store()
        for _ in range(3):
            self.take(st, self.a)
            self.take(st, self.b)
        self.take(st, self.a)
        self.assertEqual(self.shown(st), {"1": 5, "2": 3, "3": 1, "4": 1}, "行き来するたびに増えた")
        self.assertEqual(shared_comments(self.b), [])

    def test_an_unreadable_new_shared_db_does_not_show_the_old_one(self):
        """変えた先が読めなくても、前の接続先の盤・コメントを新しい接続先のものとして出さない。"""
        st = self.store()
        self.take(st, self.a)
        missing = self.dir / "C" / "看板マスタ.sqlite3"
        result = self.take(st, missing)
        self.assertFalse(result.ok_lines)
        self.assertEqual(self.shown(st), {})
        self.assertEqual(st.items(LINE), [])
        self.assertEqual(st.get_meta("last_import_at", ""), "", "読めていないのに「取り込めた」ことになっている")

    def test_the_same_path_written_differently_is_the_same_shared_db(self):
        st = self.store()
        self.take(st, self.a)
        st.add_comment(LINE, "1", "現場", "まだ送っていない")
        self.assertTrue(Store.same_source(r"\\SERVER\Share\看板\看板マスタ.sqlite3",
                                          "//server/share/看板/看板マスタ.sqlite3/"))
        self.take(st, self.a)
        self.assertEqual(st.unsent().comments, 1, "同じ接続先なのに手元で書いたコメントを片付けた")


class UnsentCountTest(Fixture):
    """(b) コメントを書いていないのに「送っていないコメントがあります」と言わない。"""

    def test_imported_comments_are_not_unsent(self):
        st = self.store()
        self.take(st, self.a)
        self.assertEqual(st.unsent().comments, 0)
        self.assertEqual(st.unsent().busy_reason(), "")

    def test_opening_or_pressing_alone_is_not_an_unsent_comment(self):
        """開いただけ(既読の印)・押しただけ(確認の印)では「送っていないコメント」と言わない。"""
        st = self.store()
        self.take(st, self.a)
        st.mark_comments_read(LINE, "1", share_side="現場")
        st.apply_transition(LINE, "2", models.order_button_changes, operation="order")   # 赤を取り消した
        self.assertGreater(st.unsent_comment_count(), 0, "印は送る(同じ側のほかの端末に既読を伝える)")
        self.assertEqual(st.unsent().comments, 0)
        self.assertNotIn("コメント", st.unsent().busy_reason())

    def test_a_comment_really_written_here_is_unsent_until_sent(self):
        st = self.store()
        self.take(st, self.a)
        st.add_comment(LINE, "1", "現場", "急ぎ")
        self.assertIn("送っていないコメントが 1 件", st.unsent().busy_reason())
        self.send(st, self.a)
        self.assertEqual(st.unsent().comments, 0)


class DeletedOnSharedTest(Fixture):
    """(c) 共有DBで消えたコメントは、読めた回の取り込みで手元からも消える。読めない回は消さない。"""

    def wipe_shared_comments(self) -> None:
        c = sqlite3.connect(str(self.a))
        c.execute("DELETE FROM [看板コメント]")
        c.commit()
        c.close()

    def test_comments_removed_from_the_shared_db_disappear_here(self):
        st = self.store()
        self.take(st, self.a)
        self.wipe_shared_comments()
        self.take(st, self.a)
        self.assertEqual(self.shown(st), {}, "看板マスタ上に無いコメントが手元に残り続ける")

    def test_a_comment_written_here_and_not_yet_sent_is_kept(self):
        st = self.store()
        self.take(st, self.a)
        st.add_comment(LINE, "1", "現場", "まだ送っていない")
        self.wipe_shared_comments()
        self.take(st, self.a)
        self.assertEqual(self.shown(st), {"1": 1})
        self.send(st, self.a)
        self.assertEqual([r[3] for r in shared_comments(self.a) if r[2] == "コメント"], ["まだ送っていない"])

    def test_an_unreadable_round_removes_nothing(self):
        st = self.store()
        self.take(st, self.a)
        with mock.patch.object(SharedDb, "select", side_effect=SharedDbError("共有DBを読めませんでした")):
            self.take(st, self.a)
        self.assertEqual(self.shown(st), {"1": 5, "2": 3, "3": 1, "4": 1}, "読めない回に消した")
        with mock.patch.object(SharedDb, "column_names", side_effect=SharedDbError("届かない")):
            self.take(st, self.a)
        self.assertEqual(self.shown(st), {"1": 5, "2": 3, "3": 1, "4": 1}, "読めない回に消した")
        self.a.rename(self.a.with_suffix(".away"))            # 一瞬見えない
        self.take(st, self.a)
        self.a.with_suffix(".away").rename(self.a)
        self.assertEqual(self.shown(st), {"1": 5, "2": 3, "3": 1, "4": 1}, "見えない回に消した")

    def test_a_comment_sent_after_the_read_started_is_not_removed(self):
        """読み始めたあとに(別のプロセスが)送ったコメントは、その回に読んだ写しに無くても消さない。"""
        st = self.store()
        self.take(st, self.a)
        st.add_comment(LINE, "1", "現場", "読んだあとに送れた")
        real_select = SharedDb.select

        def select_then_send(gw, sql, params=()):
            rows = real_select(gw, sql, params)
            # 読んだ直後に送れて、送った印が付いた(写しには入っていない)
            ids = [r["id"] for r in st.unsent_comments()]
            st.mark_comments_sent(ids)
            return rows

        with mock.patch.object(SharedDb, "select", select_then_send):
            self.take(st, self.a)
        self.assertEqual(self.shown(st)["1"], 6)


class TwoTerminalsTest(Fixture):
    """(d) 同じ参照パスの 2 台: 片方が書いて送ったコメントがもう片方に出る。片方だけに残るものが無い。"""

    def test_both_terminals_show_the_same_comments(self):
        pc1, pc2 = self.store("pc1"), self.store("pc2")
        self.take(pc1, self.b, "pc1")
        self.take(pc2, self.b, "pc2")
        pc1.apply_transition(LINE, "1", models.order_button_changes, operation="order")
        pc1.add_comment(LINE, "1", "現場", "急ぎ")
        self.send(pc1, self.b, "pc1")
        self.take(pc1, self.b, "pc1")
        self.take(pc2, self.b, "pc2")
        self.assertEqual(pc2.comment_counts(LINE, "倉庫"), {"1": (1, 1)})
        self.assertEqual(self.shown(pc1), self.shown(pc2))

    def test_a_terminal_that_once_looked_elsewhere_agrees_with_the_other(self):
        """共有A を見ていた端末を共有B に変えると、共有B だけを見てきた端末と同じものが出る。"""
        pc1, pc2 = self.store("pc1"), self.store("pc2")
        self.take(pc1, self.a, "pc1")
        self.take(pc1, self.b, "pc1")
        self.take(pc2, self.b, "pc2")
        self.assertEqual(self.shown(pc1), self.shown(pc2))
        self.assertEqual(self.shown(pc1), {})


class BoardCountTest(Fixture):
    """盤の 💬 は共有DBにあるコメントを数え、この端末から送っていないものは別に数える。"""

    def test_unsent_comments_are_counted_apart(self):
        from kanban.domain.service import KanbanService
        from kanban.presenters import board

        st = self.store()
        self.take(st, self.a)
        st.add_comment(LINE, "3", "現場", "まだ送っていない")
        view = board.build(KanbanService(st), LINE, "site")
        rows = {r.mgmt_no: r for g in view.groups for r in g.rows}
        self.assertEqual((rows["3"].comments, rows["3"].comments_unsent), (1, 1))
        self.assertEqual((rows["1"].comments, rows["1"].comments_unsent), (5, 0))
        thread = st.comment_thread(LINE, "3")["open"]
        self.assertEqual([c.get("unsent") for c in thread], [False, True])


if __name__ == "__main__":
    unittest.main()
