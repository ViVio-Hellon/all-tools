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

from kanban import access_control
from kanban import table_refresh as tr
from kanban.db.shared import SharedDb

KANBAN_COLS = ("管理番号", "資材", "サイズ", "欲", "不", "更新日", "発送", "倉庫確認日時", "常設品", "保留", "注文中日時")


#: Access のファイルの頭。**読み方は拡張子ではなく中身で決める**(``table_refresh.sniff``)ので、
#: 偽の Access もこの頭で始める
ACCESS_HEAD = b"\x00\x01\x00\x00Standard ACE DB\x00" + b"\x00" * 64


def fake_access(tables):
    """偽の Access 読み取り(内蔵リーダー ``kanban.accdb.reader.AccdbReader`` の差し替え)。

    ``tables`` は ``{表: (列, 行)}`` か ``{表: (列, 行, 定義の行数)}``。定義の行数を省くと読めた行数。
    """
    by_name = {}
    for name, spec in tables.items():
        columns, rows = spec[0], spec[1]
        table = mock.Mock()
        table.column_names.return_value = list(columns)
        table.rows = [dict(r) for r in rows]
        by_name[name] = (table, spec[2] if len(spec) > 2 else len(rows))
    reader = mock.MagicMock()
    db = reader.__enter__.return_value
    db.table_names.return_value = list(by_name)
    db.read_table.side_effect = lambda name: by_name[name][0]
    db.defined_rows.side_effect = lambda name: by_name[name][1]
    return mock.patch("kanban.accdb.reader.AccdbReader", return_value=reader)


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
        accdb.write_bytes(ACCESS_HEAD)
        with fake_access({"看板_LVC": (["管理番号", "資材", "サイズ", "更新日"], [
                {"管理番号": 3, "資材": "アングル", "サイズ": "C3",
                 "更新日": dt.datetime(2026, 9, 1, 8, 5, 0)}])}):
            plan = tr.plan(str(accdb), self.shared)
            self.assertTrue(plan.candidates[0].can_refresh, plan.message)
            source = tr.read_source(accdb)
        self.assertEqual(source["看板_LVC"].rows[0]["更新日"], "2026/09/01 08:05:00")

    def test_access_internal_tables_are_not_counted(self):
        """添付ファイルの表(f_…_Data)は Access が自分のために持つ表。「Access にだけある表」に数えない。"""
        accdb = self.dir / "看板マスタ.accdb"
        accdb.write_bytes(ACCESS_HEAD)
        row = {"管理番号": 3, "資材": "アングル"}
        with fake_access({"f_54475901977C489E9CC61748E5CDB271_Data": (["管理番号", "資材"], [row]),
                          "看板_LVC": (["管理番号", "資材"], [row])}):
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


class NoDuplicatesTest(Fixture):
    """**入れ替えを何度しても行が増えない。**

    別のツール(python-web-tools の「表を持ってくる」)で共有DBに同じ内容の行が溜まった。
    看板では、共有DBの 管理番号 の列が TEXT で Access が数値(Double)だと、1 回目に
    '1.0' と書かれ、2 回目にそれを 1 と突き合わせられず、同じ看板を足して元の行も
    残す(発注中なので)── **表が倍になっていた**。
    """

    def text_keyed_shared(self, keys, states):
        c = sqlite3.connect(str(self.shared_path))
        c.execute("DROP TABLE [看板_LVC]")
        c.execute("CREATE TABLE [看板_LVC] (" + ", ".join(f"[{k}] TEXT" for k in KANBAN_COLS) + ")")
        for k, want in zip(keys, states):
            c.execute(f"INSERT INTO [看板_LVC] VALUES ({', '.join('?' for _ in KANBAN_COLS)})",
                      (k, "外装紙", "A", want, "" if want else "〇", "2026/10/01 08:00:00" if want else "",
                       "", "", "〇", "", ""))
        c.commit()
        c.close()

    def double_source(self, keys):
        """Access の 管理番号 が Double(内蔵リーダーは float で返す)。"""
        table = tr.SourceTable("看板_LVC", list(KANBAN_COLS), [
            dict(zip(KANBAN_COLS, (float(k), "外装紙X", "A", "", "〇", "", "", "", "〇", "", ""))) for k in keys])
        patcher = mock.patch.object(tr, "read_source", return_value={"看板_LVC": table})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_何度入れ替えても行が増えない(self):
        for _ in range(5):
            self.assertTrue(self.run_refresh(["看板_LVC", "資材一覧"]).ok)
            tr._cache.clear()
        self.assertEqual(len(self.rows("看板_LVC")), 4)
        self.assertEqual(len(self.rows("資材一覧")), 2)

    def test_TEXTの管理番号とAccessの数値でも倍にならない_書き方もそのまま(self):
        self.text_keyed_shared(["1", "2", "3"], ["〇", "〇", ""])
        self.double_source([1, 2, 3])
        for _ in range(3):
            self.assertTrue(self.run_refresh(["看板_LVC"]).ok)
        rows = self.rows("看板_LVC")
        self.assertEqual([r["管理番号"] for r in rows], ["1", "2", "3"], "'1.0' と書いた・倍になった")
        self.assertEqual([r["欲"] for r in rows], ["〇", "〇", ""], "状態を消した")
        self.assertEqual(rows[0]["資材"], "外装紙X")

    def test_倍になってしまった表は次の入れ替えで1行に戻る_発注中の行を残す(self):
        # 以前の版で倍になった姿: '1.0'(Access から足した・出していない)と '1.0'(元の・発注中)
        self.text_keyed_shared(["1.0", "2.0", "1.0", "2.0"], ["", "", "〇", ""])
        self.double_source([1, 2])
        plan = tr.plan(str(self.src), self.shared)
        lvc = next(c for c in plan.candidates if c.name == "看板_LVC")
        self.assertIn("同じ管理番号が重なっている 2 行を 1 行にまとめます", lvc.preview)
        result = self.run_refresh(["看板_LVC"])
        self.assertTrue(result.ok, result.message)
        rows = self.rows("看板_LVC")
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["管理番号"]: r["欲"] for r in rows}, {"1.0": "〇", "2.0": ""},
                         "重なりの片方(発注中)を消した")
        self.assertTrue(any("重なっていた 2 行を 1 行にまとめました" in n for n in result.notes), result.notes)

    def test_同じ看板を別の書き方で2行持つAccessは断る(self):
        self.double_source([1])
        table = tr.read_source(self.src)["看板_LVC"]
        table.rows.append(dict(table.rows[0], 管理番号="１"))
        self.assertIn("重なっています", tr.refresh_why("看板_LVC", table, list(KANBAN_COLS), 0))


class AccessToSqliteTest(Fixture):
    """**偽の Access 読み取りから共有DB(sqlite3)へ、通しで入れ替える。**

    python-web-tools(``table_bring.refresh``)と項目ごとに突き合わせたもの: 列・件数・型が
    合う / 同じファイルに同居する表(アクセス権限・タブ表示権限・看板履歴・看板コメント)と
    索引が残る / 途中で失敗したら元のまま / 2 回やっても同じ / ほかの PC が開いているとき。
    """

    ACCESS_COLS = KANBAN_COLS + ("備考",)

    def setUp(self) -> None:
        super().setUp()
        c = sqlite3.connect(str(self.shared_path))
        # 同じファイルに同居することがある表(梱包資材マスタと看板マスタを 1 つにした PC)
        c.execute(access_control.DDL)
        c.executemany('INSERT INTO "アクセス権限" ("ログインID", "PC名", "権限") VALUES (?, ?, ?)',
                      [("yamada", "PC-01", "mode:material"), ("suzuki", "", "mode:field LVC")])
        c.execute("CREATE TABLE [タブ表示権限] ([ログインID] TEXT, [PC名] TEXT, [表示タブ] TEXT)")
        c.execute("INSERT INTO [タブ表示権限] VALUES ('yamada', 'PC-01', 'kanban,nippou')")
        c.execute("CREATE INDEX [idx_タブ表示権限] ON [タブ表示権限] ([ログインID])")
        c.commit()
        c.close()
        self.accdb = self.dir / "看板マスタ.accdb"
        self.accdb.write_bytes(ACCESS_HEAD)
        # Access(内蔵リーダーが返す形): 管理番号は Double、日時は datetime、Single は短い 10 進
        self.access_rows = [
            self.kanban_row(1.0, "外装紙X", "A", 不="〇"),
            self.kanban_row(3.0, "アングル", "C2", 不="〇"),
            self.kanban_row(5.0, "テープ", "幅50", 欲="〇", 更新日=dt.datetime(2026, 10, 1, 8, 0, 0)),
        ]
        self.tables = {
            "看板_LVC": (list(self.ACCESS_COLS), self.access_rows),
            "資材一覧": (["名前", "単位"], [{"名前": "外装紙", "単位": "本"}, {"名前": "テープ", "単位": "巻"}]),
            # Access にも同じ名前の表がある(古い中身)。入れ替えると、ここで足した行が消える
            "アクセス権限": (["管理番号", "ログインID", "PC名", "権限"], [
                {"管理番号": 1, "ログインID": "old", "PC名": "", "権限": "mode:field"}]),
            "タブ表示権限": (["ログインID", "PC名", "表示タブ"], []),
            "看板履歴": (["ID", "出来事"], []),
        }

    def kanban_row(self, key, material, size, **state):
        row = dict.fromkeys(self.ACCESS_COLS, "")
        row.update({"管理番号": key, "資材": material, "サイズ": size, "常設品": "〇"}, **state)
        return row

    def refresh(self, tables, shared=None):
        with fake_access(self.tables):
            return tr.refresh(str(self.accdb), shared or self.shared, tables, self.backup)

    def plan(self):
        with fake_access(self.tables):
            return tr.plan(str(self.accdb), self.shared)

    def snapshot(self):
        """共有DBのすべて(表・索引・トリガーの定義と、全部の表の中身)。"""
        c = sqlite3.connect(str(self.shared_path))
        try:
            master = c.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
            data = {n: c.execute(f"SELECT * FROM [{n}] ORDER BY rowid").fetchall()
                    for t, n, _s in master if t == "table"}
        finally:
            c.close()
        return master, data

    # -- 列・件数・型 ------------------------------------------------------
    def test_列と件数が合い_値は移行の変換と同じ書き方(self):
        c = sqlite3.connect(str(self.shared_path))
        before_cols = list(c.execute("PRAGMA table_info([看板_LVC])"))
        c.close()
        result = self.refresh(["看板_LVC", "資材一覧"])
        self.assertTrue(result.ok, result.message)
        # 件数: Access の 3 枚 + Access に無いが発注中の 2 = 4。資材一覧は Access の 2 行
        self.assertEqual(result.refreshed, [("看板_LVC", 4, 4), ("資材一覧", 1, 2)])
        self.assertIn("ファイルを開き直して確かめました: 看板_LVC 4 行、資材一覧 2 行(中身も入れ替えたとおり)",
                      result.verified)
        c = sqlite3.connect(str(self.shared_path))
        after_cols = list(c.execute("PRAGMA table_info([看板_LVC])"))
        c.close()
        self.assertEqual(after_cols, before_cols, "表の定義(列・型)を変えた")
        rows = {r["管理番号"]: r for r in self.rows("看板_LVC")}
        self.assertEqual(sorted(rows), [1, 2, 3, 5], "管理番号を 1.0 の書き方で入れた・看板を落とした")
        self.assertEqual((rows[1]["資材"], rows[1]["欲"], rows[1]["発送"]), ("外装紙X", "〇", "〇"),
                         "資材は Access、状態はいまのまま、になっていない")
        self.assertEqual(rows[5]["更新日"], "2026/10/01 08:00:00", "日時の書き方が移行の変換と違う")
        self.assertEqual(rows[5]["不"], "")
        self.assertEqual(self.rows("資材一覧"), [{"名前": "外装紙", "単位": "本"}, {"名前": "テープ", "単位": "巻"}])

    def test_読めた行数がAccessの定義と違う表は入れ替えない(self):
        """内蔵リーダーが行を読み落とすと、その看板を「Access で消した」と取り違えて消してしまう。"""
        self.tables["看板_LVC"] = (list(self.ACCESS_COLS), self.access_rows, 4)
        lvc = next(c for c in self.plan().candidates if c.name == "看板_LVC")
        self.assertFalse(lvc.can_refresh)
        self.assertIn("定義では 4行 ですが、読めたのは 3行", lvc.refresh_why)
        before = self.snapshot()
        result = self.refresh(["看板_LVC"])
        self.assertFalse(result.ok)
        self.assertIn("何も変えていません", result.message)
        self.assertEqual(self.snapshot(), before)

    def test_読み方は名前ではなく中身で決める(self):
        """名前が .accdb でも中身が sqlite3 ならそのまま読む。空のファイルは理由を言って断る。"""
        renamed = self.dir / "変換済み.accdb"
        shutil.copyfile(self.src, renamed)
        plan = tr.plan(str(renamed), self.shared)
        self.assertTrue(plan.ok, plan.message)
        self.assertIn("看板_LVC", [c.name for c in plan.candidates])
        self.assertFalse(tr.plan_dict(plan)["access"], "中身は sqlite3 なのに Access と出した")
        empty = self.dir / "書きかけ.accdb"
        empty.write_bytes(b"")
        self.assertIn("は空です", tr.plan(str(empty), self.shared).message)
        blank = self.dir / "書きかけ.sqlite3"           # sqlite3 としては開けて「表 0 個」になる
        blank.write_bytes(b"")
        self.assertIn("は空です", tr.plan(str(blank), self.shared).message)
        book = self.dir / "名前だけ.accdb"
        book.write_bytes(b"PK\x03\x04" + b"\x00" * 60)
        self.assertIn("zip の形", tr.plan(str(book), self.shared).message)

    # -- 同居する表 --------------------------------------------------------
    def test_同居する表は残り_Accessの中身では入れ替えない(self):
        by = {c.name: c for c in self.plan().candidates}
        self.assertIn("マスタ管理で直す表", by["アクセス権限"].refresh_why)
        self.assertIn("大設定で直す表", by["タブ表示権限"].refresh_why)
        self.assertIn("このツールが書き込む表", by["看板履歴"].refresh_why)
        before = self.snapshot()
        for owned in ("アクセス権限", "タブ表示権限"):
            result = self.refresh(["看板_LVC", owned])
            self.assertFalse(result.ok, owned)
            self.assertIn("何も変えていません", result.message)
        self.assertEqual(self.snapshot(), before, "断ったのに書き換えた")
        self.assertFalse(self.backup.exists(), "断ったのに控えを取った")

        self.assertTrue(self.refresh(["看板_LVC", "資材一覧"]).ok)
        master, data = self.snapshot()
        for table in ("アクセス権限", "タブ表示権限", "看板履歴", "看板コメント", "Form状態管理"):
            self.assertEqual(data[table], before[1][table], f"{table} の中身が変わった")
        indexes = {n for t, n, _s in master if t == "index"}
        self.assertTrue({"idx_看板履歴_ID", "idx_タブ表示権限"} <= indexes, "同居する表の索引が消えた")
        self.assertEqual([r for r in master if r[0] == "table"], [r for r in before[0] if r[0] == "table"],
                         "表の定義が変わった")

    # -- 途中で失敗したとき ------------------------------------------------
    def test_途中で失敗したら元のまま_ほかの表は入れ替わる(self):
        c = sqlite3.connect(str(self.shared_path))
        # 3 枚目(テープ)を入れるところで落ちる。そこまでに 消す・2 枚入れる は済んでいる
        c.execute("CREATE TRIGGER [落とす] BEFORE INSERT ON [看板_LVC] WHEN NEW.[管理番号] = 5"
                  " BEGIN SELECT RAISE(ABORT, '途中で失敗しました'); END")
        c.commit()
        c.close()
        _master, before = self.snapshot()
        result = self.refresh(["看板_LVC", "資材一覧"])
        self.assertFalse(result.ok)
        self.assertIn("看板_LVC(途中で失敗しました)", result.message)
        self.assertIn("元のまま", result.message)
        _master, after = self.snapshot()
        self.assertEqual(after["看板_LVC"], before["看板_LVC"], "途中まで消えた・入った")
        self.assertEqual(len(after["資材一覧"]), 2, "ほかの表まで止まった")
        self.assertTrue(Path(result.backup).is_file(), "戻すための控えが無い")

    def test_書いた中身が合わなければ確定せずに戻す(self):
        """python-web-tools の ``_verify_in``。確定する前に読み直し、入れるつもりの中身と照らし合わせる。"""
        c = sqlite3.connect(str(self.shared_path))
        c.execute("CREATE TRIGGER [書き換える] AFTER INSERT ON [看板_LVC] WHEN NEW.[管理番号] = 3"
                  " BEGIN UPDATE [看板_LVC] SET [サイズ] = '別の値' WHERE rowid = NEW.rowid; END")
        c.commit()
        c.close()
        before = self.snapshot()
        result = self.refresh(["看板_LVC"])
        self.assertFalse(result.ok)
        self.assertIn("書く前に戻しました", result.message)
        self.assertIn("書いた表に無い行 1行", result.message)
        self.assertEqual(self.snapshot(), before)

    def test_確定したあと開き直して合わなければ失敗として言う(self):
        """共有フォルダのロックが効かず、ほかの PC が同時に書いた、を見逃さない。"""
        real = tr._refresh_one

        def and_then_someone_writes(shared, src):
            out = real(shared, src)
            c = sqlite3.connect(str(self.shared_path))
            c.execute("UPDATE [資材一覧] SET [単位] = '箱' WHERE [名前] = 'テープ'")
            c.commit()
            c.close()
            return out

        with mock.patch.object(tr, "_refresh_one", and_then_someone_writes):
            result = self.refresh(["資材一覧"])
        self.assertFalse(result.ok)
        self.assertIn("書いたあと開き直すと入れ替えた中身と合いません", result.message)
        self.assertIn("ほかの PC が同時に書いた可能性", result.message)
        self.assertEqual(result.refreshed, [("資材一覧", 1, 2)])

    # -- 2 回やっても同じ --------------------------------------------------
    def test_2回やっても同じ(self):
        self.assertTrue(self.refresh(["看板_LVC", "資材一覧"]).ok)
        once = self.snapshot()
        tr._cache.clear()
        again = self.refresh(["看板_LVC", "資材一覧"])
        self.assertTrue(again.ok, again.message)
        self.assertEqual(again.refreshed, [("看板_LVC", 4, 4), ("資材一覧", 2, 2)])
        self.assertEqual(self.snapshot(), once, "2 回目で中身が変わった・行が増えた")
        lvc = next(c for c in self.plan().candidates if c.name == "看板_LVC")
        self.assertEqual(lvc.preview, "看板: 変わる 0・足す 0・消す 0・残す(Access に無いが発注中など) 1 枚")

    # -- ほかの PC が開いているとき -----------------------------------------
    def short_wait(self):
        return SharedDb(str(self.shared_path), busy_timeout_ms=300, cache_dir=str(self.dir / "cache_short"))

    def test_ほかのPCが書いている最中なら待ってから断り_元のまま(self):
        other = sqlite3.connect(str(self.shared_path), isolation_level=None)
        self.addCleanup(other.close)
        other.execute("BEGIN IMMEDIATE")               # ほかの PC が書き戻している最中
        before = self.snapshot()
        result = self.refresh(["看板_LVC"], self.short_wait())
        self.assertFalse(result.ok)
        self.assertIn("ほかの PC が共有DBに書いている最中で、0.3 秒待っても書けませんでした", result.message)
        self.assertIn("共有DBは変えていません", result.message)
        other.execute("ROLLBACK")
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.refresh(["看板_LVC"], self.short_wait()).ok, "書き終わったあとも入れ替えられない")

    def test_ほかのPCが読んでいる最中なら確定できずに元のまま(self):
        other = sqlite3.connect(str(self.shared_path))
        self.addCleanup(other.close)
        reading = other.execute("SELECT * FROM [看板_LVC]")
        reading.fetchone()                              # 読み終わっていない(共有の鍵を持ったまま)
        before = self.snapshot()
        result = self.refresh(["看板_LVC"], self.short_wait())
        self.assertFalse(result.ok)
        self.assertIn("ほかの PC が共有DBに書いている最中", result.message)
        reading.close()
        self.assertEqual(self.snapshot(), before)

    def test_ほかのPCが開いているだけなら入れ替えられ_写しにも届く(self):
        idle = sqlite3.connect(str(self.shared_path))  # 開いているだけ(読み書きしていない)
        self.addCleanup(idle.close)
        other_pc = SharedDb(str(self.shared_path), cache_dir=str(self.dir / "cache_other_pc"))
        self.assertEqual(self.size_of(other_pc, 3), "C")
        self.assertEqual(self.size_of(self.shared, 3), "C")
        result = self.refresh(["看板_LVC"])
        self.assertTrue(result.ok, result.message)
        # この端末の写しは「写し直せ」(世代)ですぐ、ほかの PC の写しはファイルが変わったので次に読むとき
        self.assertEqual(self.size_of(self.shared, 3), "C2", "入れ替えたのに古い写しを読んだ")
        self.assertEqual(self.size_of(other_pc, 3), "C2", "ほかの PC の写しが古いまま")
        self.assertEqual(idle.execute("SELECT [サイズ] FROM [看板_LVC] WHERE [管理番号] = 3").fetchone(),
                         ("C2",))

    @staticmethod
    def size_of(shared, key):
        return next(r["サイズ"] for r in shared.read_table("看板_LVC").rows if int(r["管理番号"]) == key)

if __name__ == "__main__":
    unittest.main()
