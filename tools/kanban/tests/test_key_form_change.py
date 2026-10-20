"""共有DBの管理番号の書き方が変わっても、同じ看板として取り込む (VER3.5.7)

【見つけた不具合】取り込みは管理番号を**文字のまま**比べ、書き戻しと表の入れ替えは揃えてから
比べていた(`keys.normalize`)。共有DBの書き方が変わると(1 → 1.0、'1' → '１' など。Access・
ほかの道具・表の入れ替えが書く)、取り込みは同じ看板を別物とみなして手元の行を消し、新しい行を
足していた ── **その看板の未送信の操作も一緒に消える**。

いまは届いた番号を手元の番号に結ぶ(`keys.pair`)。手元は手元の番号のまま持ち、共有DBの書き方は
変えない。1 対 1 で結べない(書き方の違う看板が2つ届いた)ときは推し量らず、未送信の行は残す。
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from kanban import config, keys
from kanban.db import sync
from kanban.domain import models
from tests.test_shared_shapes import LINE, TABLE, Terminal, SharedShapesTest
from tests.test_store import StoreTestBase, sample_rows

ON = config.MARK_ON


class PairTest(unittest.TestCase):
    def test_書き方が違っても1対1なら結ぶ(self) -> None:
        for raw in ("1.0", "１", "01", "+1", "1.00"):
            with self.subTest(raw=raw):
                self.assertEqual(keys.pair([raw], ["1"]), ({raw: "1"}, set()))
        self.assertEqual(keys.pair(["ＡＢ１"], ["AB1"]), ({"ＡＢ１": "AB1"}, set()))

    def test_文字がそのまま同じものが先(self) -> None:
        self.assertEqual(keys.pair(["1", "１"], ["1"]), ({}, set()))

    def test_1対1でなければ結ばない(self) -> None:
        self.assertEqual(keys.pair(["1.0", "１"], ["1"]), ({}, {"1"}))
        self.assertEqual(keys.pair(["1.0"], ["1", "１"]), ({}, {"1"}))

    def test_違う番号は結ばない(self) -> None:
        self.assertEqual(keys.pair(["A-01"], ["A-1"]), ({}, set()))
        self.assertEqual(keys.pair(["2"], ["1"]), ({}, set()))


class ImportFormChangeTest(StoreTestBase):
    def order(self, no: str = "1") -> None:
        self.store.apply_transition("LVC", no, models.order_button_changes, operation="order")

    def rows_with(self, form) -> list[dict]:
        rows = sample_rows()
        rows[0] = dict(rows[0], mgmt_no=form)
        return rows

    def test_未送信の発注は書き方が変わっても消えない(self) -> None:
        for form in ("1.0", "１", " 1 ", 1, 1.0, "01"):
            with self.subTest(form=form):
                self._import(sample_rows())
                self.order()
                inserted, _updated, protected = self._import(self.rows_with(form))
                self.assertEqual(inserted, 0, "新しい看板として足さない")
                self.assertEqual(protected, 1)
                item = self.store.item("LVC", "1")
                self.assertIsNotNone(item, "手元の看板は手元の番号のまま")
                self.assertTrue(item.is_ordered)
                self.assertEqual(self.store.pending_count(), 1)
                self.assertEqual(self.store.dropped_notices(), [])
                self.assertEqual(len(self.store.items("LVC")), 3)
                # 次の小テストのために戻す
                self.store.close()
                shutil.rmtree(self.dir, ignore_errors=True)
                self.setUp()

    def test_送り済みの看板も同じ行のまま(self) -> None:
        self._import(sample_rows())
        before = self.store.item("LVC", "1")
        self._import(self.rows_with("1.0"))
        after = self.store.item("LVC", "1")
        self.assertIsNotNone(after)
        self.assertEqual(after.rev, before.rev, "書き方が変わっただけでは変えない")
        self.assertIsNone(self.store.item("LVC", "1.0"))

    def test_書き方の違う看板が2つ届いたら未送信の行は残す(self) -> None:
        self._import(sample_rows())
        self.order()
        rows = sample_rows()
        rows[0] = dict(rows[0], mgmt_no="1.0")
        rows.append(dict(rows[0], mgmt_no="１"))
        self._import(rows)
        self.assertTrue(self.store.item("LVC", "1").is_ordered, "推し量らず、未送信は消さない")
        self.assertEqual(self.store.pending_count(), 1)
        self.assertEqual(self.store.dropped_notices(), [])

    def test_同じ番号が2行あっても初めての取り込みが止まらない(self) -> None:
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir = Path(tempfile.mkdtemp(prefix="kanban_test_"))
        from kanban.db.store import Store
        self.store = Store(str(self.dir / "kanban.sqlite3"), host_name="PC-TEST")
        self.store.ensure_schema()
        rows = sample_rows()
        rows.append(dict(rows[0], mgmt_no=1))            # 文字にすると同じ '1'
        self._import(rows)                               # 前は IntegrityError で止まった
        self.assertEqual(len(self.store.items("LVC")), 3)


class SiteWarehouseFormChangeTest(unittest.TestCase):
    """現場で発注して送る前に、共有DBの管理番号の書き方が変わった ── 現場・倉庫の2台で通す。"""

    def run_case(self, key_decl: str, key_of, new_form) -> None:
        root = Path(tempfile.mkdtemp(prefix="kanban_form_"))
        self.addCleanup(shutil.rmtree, root, True)
        shared = SharedShapesTest.make_shared(None, root, key_decl, key_of, "TEXT", "", "")
        site, ware = Terminal(root, "現場PC", shared), Terminal(root, "倉庫PC", shared)
        self.addCleanup(site.close)
        self.addCleanup(ware.close)
        sync.ensure_comment_table(site.gateway)
        site.take()
        ware.take()
        no = sorted(i.mgmt_no for i in site.store.items(LINE))[0]
        site.store.apply_transition(LINE, no, models.order_button_changes, operation="order")
        conn = sqlite3.connect(str(shared))
        rowid = next(r for r, k in conn.execute(f"SELECT rowid, [{config.COL_KEY}] FROM [{TABLE}]")
                     if keys.same(k, no))
        conn.execute(f"UPDATE [{TABLE}] SET [{config.COL_KEY}] = ? WHERE rowid = ?", (new_form, rowid))
        conn.commit()
        conn.close()
        site.take()                                       # 書き方が変わってから取り込む
        self.assertTrue(site.item(no).is_ordered, "未送信の発注が残っている")
        self.assertEqual(site.store.dropped_notices(), [])
        site.send()
        self.assertEqual(site.errors, [])
        conn = sqlite3.connect(str(shared))
        row = conn.execute(f"SELECT [{config.COL_KEY}], [{config.COL_WANT}], COUNT(*) OVER () FROM [{TABLE}]"
                           " WHERE rowid = ?", (rowid,)).fetchone()
        total = conn.execute(f"SELECT COUNT(*) FROM [{TABLE}]").fetchone()[0]
        conn.close()
        self.assertEqual(row[1], ON, "共有DBに発注が届いた")
        self.assertEqual(row[0], new_form, "共有DBの書き方は変えない")
        self.assertEqual(total, 4, "行は増えない")
        ware.take()
        self.assertTrue(any(i.is_ordered for i in ware.store.items(LINE) if keys.same(i.mgmt_no, no)),
                        "倉庫にも発注が見える")

    def test_TEXTの列で1が1点0に(self) -> None:
        self.run_case("TEXT", lambda n: str(n), "1.0")

    def test_TEXTの列で1が全角に(self) -> None:
        self.run_case("TEXT", lambda n: str(n), "１")

    def test_型の無い列で数の1が文字の1点0に(self) -> None:
        self.run_case("", lambda n: n, "1.0")

    def test_REALの列で(self) -> None:
        self.run_case("REAL", lambda n: float(n), 1)


if __name__ == "__main__":
    unittest.main()
