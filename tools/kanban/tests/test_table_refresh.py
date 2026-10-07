"""表の中身だけを Access の最新に入れ替える (:mod:`kanban.table_refresh`)

Access をまるごと変換して差し替えると、このツールが共有DBに足した表・列・行
(看板履歴・看板コメント・看板の状態)が消える。両方にある表の中身だけを入れ替え、
それらは残ることを確かめる。
"""

from __future__ import annotations

import datetime as dt
import io
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kanban import table_refresh as tr
from kanban.db.shared import SharedDb

KANBAN_COLS = ("管理番号", "資材", "サイズ", "欲", "不", "更新日", "発送", "倉庫確認日時", "常設品", "保留", "注文中日時")


def make_kanban(conn, name, rows, extra=()):
    cols = ", ".join(f"[{c}] {'NUMERIC' if c == '管理番号' else 'TEXT'}" for c in KANBAN_COLS + tuple(extra))
    conn.execute(f"CREATE TABLE [{name}] ({cols})")
    marks = ", ".join("?" for _ in KANBAN_COLS + tuple(extra))
    conn.executemany(f"INSERT INTO [{name}] VALUES ({marks})", rows)


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="kanban_refresh_"))
        self.shared_path = self.dir / "看板マスタ.sqlite3"
        c = sqlite3.connect(str(self.shared_path))
        # 共有DB(いま): 1 は届く前(赤+緑)、2 は発注中、3・4 は出していない
        make_kanban(c, "看板_LVC", [
            (1, "外装紙", "A", "〇", "", "2026/09/20 09:00:00", "〇", "2026/09/21 10:00:00", "〇", "", ""),
            (2, "外装紙", "B", "〇", "", "2026/09/25 09:00:00", "", "", "〇", "", ""),
            (3, "アングル", "C", "", "〇", "", "", "", "〇", "", ""),
            (4, "アングル", "D", "", "〇", "", "", "", "〇", "", ""),
        ])
        # このツールが足した表(Access には無い・または古い)
        c.execute("CREATE TABLE [看板履歴] ([ID] TEXT, [出来事] TEXT)")
        c.executemany("INSERT INTO [看板履歴] VALUES (?, ?)", [("a", "出した"), ("b", "発送")])
        c.execute("CREATE INDEX [idx_看板履歴_ID] ON [看板履歴] ([ID])")
        c.execute("CREATE TABLE [看板コメント] ([ID] TEXT, [本文] TEXT)")
        c.execute("INSERT INTO [看板コメント] VALUES ('c', '来週入荷です')")
        c.execute("CREATE TABLE [Form状態管理] ([フォーム名] TEXT, [開いている] TEXT)")
        c.execute("CREATE TABLE [資材一覧] ([名前] TEXT NOT NULL, [単位] TEXT)")
        c.execute("INSERT INTO [資材一覧] VALUES ('外装紙', '本')")
        c.commit()
        c.close()
        self.shared = SharedDb(str(self.shared_path), cache_dir=str(self.dir / "cache"))

        # Access を変換したもの(新しい): 1 の資材が変わった・2 と 4 が無い・5 が増えた。
        # 状態の列は Access では古い(全部 出していない)
        self.src = self.dir / "看板マスタ_最新.sqlite3"
        c = sqlite3.connect(str(self.src))
        make_kanban(c, "看板_LVC", [
            (1.0, "外装紙X", "A", "", "〇", "", "", "", "〇", "", "", "メモ1"),
            (3.0, "アングル", "C2", "", "〇", "", "", "", "〇", "", "", ""),
            (5.0, "テープ", "幅50", "", "〇", "", "", "", "〇", "", "", ""),
        ], extra=("備考",))
        c.execute("CREATE TABLE [看板履歴] ([ID] TEXT, [出来事] TEXT)")
        c.execute("CREATE TABLE [Form状態管理] ([フォーム名] TEXT, [開いている] TEXT)")
        c.execute("CREATE TABLE [資材一覧] ([名前] TEXT, [単位] TEXT)")
        c.executemany("INSERT INTO [資材一覧] VALUES (?, ?)", [("外装紙", "本"), ("テープ", "巻")])
        c.execute("CREATE TABLE [Accessだけ] ([a] TEXT)")
        c.execute("CREATE TABLE [MSysObjects] ([a] TEXT)")
        c.commit()
        c.close()
        self.backup = self.dir / "backup"

    def tearDown(self) -> None:
        tr._cache.clear()
        shutil.rmtree(self.dir, ignore_errors=True)

    def rows(self, table, order="rowid"):
        c = sqlite3.connect(str(self.shared_path))
        c.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in c.execute(f"SELECT * FROM [{table}] ORDER BY {order}")]
        finally:
            c.close()

    def run_refresh(self, tables):
        return tr.refresh(str(self.src), self.shared, tables, self.backup)


class PlanTest(Fixture):
    def test_only_tables_in_both_and_why_not(self):
        plan = tr.plan(str(self.src), self.shared)
        self.assertTrue(plan.ok, plan.message)
        by = {c.name: c for c in plan.candidates}
        self.assertEqual(list(by), ["看板_LVC", "Form状態管理", "看板履歴", "資材一覧"])
        self.assertTrue(by["看板_LVC"].can_refresh)
        self.assertIn("いまのまま", by["看板_LVC"].keeps)
        self.assertEqual(by["看板_LVC"].preview,
                         "看板: 変わる 2・足す 1・消す 1・残す(Access に無いが発注中など) 1 枚")
        self.assertEqual(by["看板_LVC"].not_copied, ["備考"])
        self.assertEqual((by["看板_LVC"].current_rows, by["看板_LVC"].rows), (4, 3))
        self.assertIn("このツールが書き込む表", by["看板履歴"].refresh_why)
        self.assertIn("このツールが書き込む表", by["Form状態管理"].refresh_why)
        self.assertTrue(by["資材一覧"].can_refresh)
        self.assertEqual(plan.only_in_source, ["Accessだけ"], "Access の内部の表まで出した/扱った")
        # 読んだファイルと書き込み先の姿(画面に「何を読んで、どこへ書くか」を出す)
        self.assertEqual(plan.source_facts.path, str(self.src))
        self.assertEqual((plan.source_facts.tables, plan.source_facts.kanban_tables), (5, ["看板_LVC"]))
        self.assertEqual((plan.dest_facts.tables, plan.dest_facts.kanban_tables), (5, ["看板_LVC"]))
        self.assertTrue(plan.source_facts.modified and plan.source_facts.size)
        self.assertEqual(plan.only_in_dest, ["看板コメント"])
        self.assertEqual(plan.hint, "", "看板の表を入れ替えられるのに、見立てを出した")

    def test_no_table_in_common_says_what_was_read_and_what_to_pick(self):
        """現場: 梱包資材マスタの Access を選ぶと「両方にある表 0 個」とだけ出て、動いているのか分からなかった。"""
        gateway = self.dir / "梱包資材マスタ.sqlite3"
        c = sqlite3.connect(str(gateway))
        for name in ("アクセス権限", "資材マスタ", "用途マスタ"):
            c.execute(f"CREATE TABLE [{name}] ([a] TEXT)")
        c.commit()
        c.close()
        plan = tr.plan(str(gateway), self.shared)
        self.assertTrue(plan.ok, plan.message)
        self.assertEqual(plan.candidates, [])
        self.assertIn("同じ名前の表が 1 つもありません", plan.message)
        self.assertIn("表 3 個", plan.message)
        self.assertIn("看板の表(看板_LVC など)が 1 つもありません: 梱包資材マスタ.sqlite3", plan.hint)
        self.assertIn("梱包資材マスタのようです", plan.hint)
        self.assertIn("看板マスタ.accdb", plan.hint)
        self.assertIn("看板_LVC", plan.only_in_dest)
        body = tr.plan_dict(plan)
        self.assertEqual(body["source_facts"]["name"], "梱包資材マスタ.sqlite3")
        self.assertEqual(body["dest_facts"]["kanban_tables"], ["看板_LVC"])
        self.assertEqual(sorted(body["only_in_source"]), ["アクセス権限", "用途マスタ", "資材マスタ"])

    def test_names_that_differ_only_in_width_or_case_are_pointed_out(self):
        other = self.dir / "看板マスタ_別.sqlite3"
        c = sqlite3.connect(str(other))
        make_kanban(c, "看板_ｌｖｃ ", [(1, "外装紙", "A", "", "〇", "", "", "", "〇", "", "")])
        c.commit()
        c.close()
        plan = tr.plan(str(other), self.shared)
        self.assertEqual(plan.candidates, [])
        self.assertEqual(plan.near, [("看板_ｌｖｃ ", "看板_LVC")])
        self.assertIn("名前が合いません", plan.hint)
        self.assertIn("「看板_ｌｖｃ 」⇔「看板_LVC」", plan.hint)

    def test_refuses_when_the_shared_db_is_not_the_kanban_master(self):
        """接続先が別のツールの sqlite3 を指していたら、入れ替えない(そのファイルを書き換えない)。"""
        other = self.dir / "梱包資材マスタ.sqlite3"
        c = sqlite3.connect(str(other))
        c.execute("CREATE TABLE [資材一覧] ([名前] TEXT)")
        c.execute("INSERT INTO [資材一覧] VALUES ('元のまま')")
        c.commit()
        c.close()
        gateway = SharedDb(str(other), cache_dir=str(self.dir / "cache_other"))
        self.assertIn("看板マスタではありません", tr.plan(str(self.src), gateway).message)
        result = tr.refresh(str(self.src), gateway, ["資材一覧"], self.backup)
        self.assertFalse(result.ok)
        c = sqlite3.connect(str(other))
        self.assertEqual(c.execute("SELECT * FROM [資材一覧]").fetchall(), [("元のまま",)])
        c.close()

    def test_refuses_the_shared_db_itself_and_missing_files(self):
        self.assertIn("共有DBそのもの", tr.plan(str(self.shared_path), self.shared).message)
        self.assertIn("見つかりません", tr.plan(str(self.dir / "ない.accdb"), self.shared).message)
        self.assertIn("選んでください", tr.plan("", self.shared).message)


class RefreshKanbanTest(Fixture):
    def test_master_from_access_state_kept(self):
        """資材・サイズは Access の最新、状態(赤・緑とその日時)はいまのまま。"""
        result = self.run_refresh(["看板_LVC"])
        self.assertTrue(result.ok, result.message)
        rows = {int(r["管理番号"]): r for r in self.rows("看板_LVC")}
        self.assertEqual(rows[1]["資材"], "外装紙X")
        self.assertEqual((rows[1]["欲"], rows[1]["発送"], rows[1]["更新日"]),
                         ("〇", "〇", "2026/09/20 09:00:00"), "状態を Access の古い値で消した")
        self.assertEqual(rows[3]["サイズ"], "C2")
        self.assertIn(5, rows, "Access で足した看板が入っていない")
        self.assertEqual(rows[5]["不"], "〇")
        self.assertIn(2, rows, "Access に無いが発注中の看板を消した")
        self.assertNotIn(4, rows, "Access で消した看板(出していない)が残った")
        self.assertEqual(result.refreshed, [("看板_LVC", 4, 4)])
        self.assertEqual(result.written, str(self.shared_path), "書き込んだファイルを返していない")
        self.assertIn("ファイルを開き直して確かめました: 看板_LVC 4 行", result.verified)
        self.assertTrue(any("発注中・発送済み・注文中の看板 1 枚(2)は残しました" in n for n in result.notes),
                        result.notes)

    def test_tables_the_tool_added_are_untouched(self):
        self.run_refresh(["看板_LVC", "資材一覧"])
        self.assertEqual(len(self.rows("看板履歴")), 2)
        self.assertEqual(self.rows("看板コメント")[0]["本文"], "来週入荷です")
        c = sqlite3.connect(str(self.shared_path))
        idx = c.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_看板履歴_ID'").fetchone()
        cols = [r[1] for r in c.execute("PRAGMA table_info([看板_LVC])")]
        c.close()
        self.assertIsNotNone(idx, "索引が消えた")
        self.assertNotIn("備考", cols, "表の定義を変えた")

    def test_a_plain_table_is_replaced(self):
        self.run_refresh(["資材一覧"])
        self.assertEqual([(r["名前"], r["単位"]) for r in self.rows("資材一覧")],
                         [("外装紙", "本"), ("テープ", "巻")])

    def test_tool_owned_tables_are_refused_and_nothing_changes(self):
        result = self.run_refresh(["看板_LVC", "看板履歴"])
        self.assertFalse(result.ok)
        self.assertIn("何も変えていません", result.message)
        self.assertEqual(self.rows("看板_LVC")[0]["資材"], "外装紙", "断ったのに書き換えた")
        self.assertFalse(self.backup.exists(), "断ったのに控えを取った")

    def test_duplicate_keys_in_access_are_refused(self):
        c = sqlite3.connect(str(self.src))
        c.execute("INSERT INTO [看板_LVC] ([管理番号], [資材]) VALUES (3, '重複')")
        c.commit()
        c.close()
        result = self.run_refresh(["看板_LVC"])
        self.assertFalse(result.ok)
        self.assertIn("管理番号 が重なっています: 3", result.message)

    def test_garbled_rows_are_refused(self):
        c = sqlite3.connect(str(self.src))
        c.execute("UPDATE [看板_LVC] SET [サイズ] = '��' WHERE [管理番号] = 3")
        c.commit()
        c.close()
        self.assertIn("文字化け", self.run_refresh(["看板_LVC"]).message)


class SafetyTest(Fixture):
    def test_a_failing_table_stays_as_it_was(self):
        """1 つの表は 消す+入れる を 1 回で確定する。入れられない行があれば元のまま。"""
        c = sqlite3.connect(str(self.src))
        c.execute("INSERT INTO [資材一覧] VALUES (NULL, '個')")      # 共有DBでは 名前 NOT NULL
        c.commit()
        c.close()
        result = self.run_refresh(["資材一覧", "看板_LVC"])
        self.assertFalse(result.ok)
        self.assertEqual([r["名前"] for r in self.rows("資材一覧")], ["外装紙"], "途中まで消えた")
        self.assertEqual(result.refreshed[0][0], "看板_LVC", "ほかの表まで止まった")
        self.assertIn("元のまま", result.message)

    def test_a_backup_is_taken_first_and_old_ones_are_pruned(self):
        self.backup.mkdir()
        for i in range(tr.BACKUP_KEEP + 3):
            (self.backup / f"看板マスタ_中身を入れ替える前_20260101_0000{i:02d}.sqlite3").write_bytes(b"x")
        result = self.run_refresh(["資材一覧"])
        saved = Path(result.backup)
        self.assertTrue(saved.is_file())
        c = sqlite3.connect(str(saved))
        self.assertEqual(c.execute("SELECT COUNT(*) FROM [資材一覧]").fetchone()[0], 1, "控えが書いたあとのもの")
        c.close()
        self.assertEqual(len(list(self.backup.glob("看板マスタ_中身を入れ替える前_*"))), tr.BACKUP_KEEP)


class AccessSourceTest(Fixture):
    """.accdb は内蔵リーダーで読み、値は移行の変換と同じ書き方にそろえる。"""

    def test_access_values_are_written_like_the_migration(self):
        accdb = self.dir / "看板マスタ.accdb"
        accdb.write_bytes(b"not really access")
        table = mock.Mock()
        table.column_names.return_value = ["管理番号", "資材", "サイズ", "更新日"]
        table.rows = [{"管理番号": 3, "資材": "アングル", "サイズ": "C3",
                       "更新日": dt.datetime(2026, 9, 1, 8, 5, 0)}]
        reader = mock.MagicMock()
        reader.__enter__.return_value.table_names.return_value = ["看板_LVC"]
        reader.__enter__.return_value.read_table.return_value = table
        with mock.patch("kanban.accdb.reader.AccdbReader", return_value=reader):
            plan = tr.plan(str(accdb), self.shared)
            self.assertTrue(plan.candidates[0].can_refresh, plan.message)
            source = tr.read_source(accdb)
        self.assertEqual(source["看板_LVC"].rows[0]["更新日"], "2026/09/01 08:05:00")

    def test_access_internal_tables_are_not_counted(self):
        """添付ファイルの表(f_…_Data)は Access が自分のために持つ表。「Access にだけある表」に数えない。"""
        accdb = self.dir / "看板マスタ.accdb"
        accdb.write_bytes(b"not really access")
        table = mock.Mock()
        table.column_names.return_value = ["管理番号", "資材"]
        table.rows = [{"管理番号": 3, "資材": "アングル"}]
        reader = mock.MagicMock()
        reader.__enter__.return_value.table_names.return_value = [
            "f_54475901977C489E9CC61748E5CDB271_Data", "看板_LVC"]
        reader.__enter__.return_value.read_table.return_value = table
        with mock.patch("kanban.accdb.reader.AccdbReader", return_value=reader):
            plan = tr.plan(str(accdb), self.shared)
        self.assertEqual(plan.only_in_source, [])
        self.assertEqual(plan.source_facts.tables, 1)


class UploadTest(Fixture):
    def test_only_access_or_sqlite_and_only_the_file_name(self):
        saved, why = tr.save_upload("../../evil/看板.accdb", io.BytesIO(b"x"), self.dir / "work")
        self.assertEqual(saved, self.dir / "work" / "看板.accdb", "フォルダを含む名前で外へ書いた")
        saved, why = tr.save_upload("メモ.txt", io.BytesIO(b"x"), self.dir / "work")
        self.assertIsNone(saved)
        self.assertIn(".accdb", why)


if __name__ == "__main__":
    unittest.main()
