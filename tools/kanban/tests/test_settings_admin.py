"""設定画面の「参照...」とマスタ管理のテスト。

どちらも Web 化と Access 廃止で必要になったもの。

* **参照** … ブラウザのファイル選択ダイアログはクライアント側のパスしか
  返さないので、サーバ側のフォルダを一覧する(tkinter 版は同じプロセスに
  画面があったので ``askopenfilename`` がそのまま使えていた)
* **マスタ管理** … Access をやめたことで「Access を開いて目視する・手で
  直す」ができなくなった。その受け皿

**パスが無ければ何もできない**ことを、どちらの側でも確かめる。
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from kanban import config
from kanban.db.shared import SharedDb
from kanban.presenters import fs_browse, master


class FsBrowseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="kanban_fs_"))
        (self.dir / "sub").mkdir()
        (self.dir / "看板マスタ.sqlite3").write_bytes(b"")
        (self.dir / "古い.accdb").write_bytes(b"")
        (self.dir / "無関係.txt").write_text("x", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_lists_dirs_and_shared_dbs(self):
        view = fs_browse.browse(str(self.dir))
        self.assertTrue(view.exists)
        self.assertTrue(view.readable)
        self.assertEqual([d.name for d in view.dirs], ["sub"])
        names = {f.name for f in view.files}
        self.assertIn("看板マスタ.sqlite3", names)

    def test_hides_unrelated_files(self):
        """**名前しか出さない**うえに、探しているものだけを出す。

        ここが「サーバの中を読む窓口」にならないようにするため。
        """
        view = fs_browse.browse(str(self.dir))
        self.assertNotIn("無関係.txt", {f.name for f in view.files})

    def test_shows_legacy_accdb_but_marks_it(self):
        """移行の途中は「.accdb はあるが .sqlite3 がまだ」になる。

        そこで何も見えないと、フォルダを間違えたのか変換がまだなのかが
        分からない。
        """
        view = fs_browse.browse(str(self.dir))
        legacy = [f for f in view.files if f.legacy]
        self.assertEqual([f.name for f in legacy], ["古い.accdb"])

    def test_message_tells_what_to_do_when_only_accdb(self):
        only = Path(tempfile.mkdtemp(prefix="kanban_fs2_"))
        try:
            (only / "看板マスタ.accdb").write_bytes(b"")
            view = fs_browse.browse(str(only))
            self.assertIn("accdb_to_sqlite", view.message)
        finally:
            shutil.rmtree(only, ignore_errors=True)

    def test_pointing_at_a_file_opens_its_folder(self):
        """「参照」で目当てのファイルを見つけたとき、親へ戻らせない。"""
        view = fs_browse.browse(str(self.dir / "看板マスタ.sqlite3"))
        self.assertEqual(view.path, str(self.dir))
        self.assertEqual(view.picked, "看板マスタ.sqlite3")

    def test_missing_folder_is_reported(self):
        view = fs_browse.browse(str(self.dir / "ない"))
        self.assertFalse(view.exists)
        self.assertIn("見えません", view.message)

    def test_empty_path_returns_roots_only(self):
        view = fs_browse.browse("")
        self.assertFalse(view.exists)
        self.assertTrue(view.roots, "出発点が無いと、どこから探せばよいか分からない")

    def test_entry_count_is_capped(self):
        big = Path(tempfile.mkdtemp(prefix="kanban_fs3_"))
        try:
            for i in range(fs_browse.MAX_ENTRIES + 20):
                (big / f"d{i:04d}").mkdir()
            view = fs_browse.browse(str(big))
            self.assertTrue(view.truncated)
            self.assertLessEqual(len(view.dirs) + len(view.files), fs_browse.MAX_ENTRIES)
        finally:
            shutil.rmtree(big, ignore_errors=True)


def add_date_column(path: Path, table: str) -> None:
    """看板の状態ではない日時の列を足す(日時の形の確かめに使う)。"""
    conn = sqlite3.connect(str(path))
    conn.execute(f"ALTER TABLE [{table}] ADD COLUMN [棚卸日] DATETIME")
    conn.commit()
    conn.close()


class MasterTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="kanban_master_"))
        self.path = self.dir / "看板マスタ.sqlite3"
        conn = sqlite3.connect(str(self.path))
        # **本物と同じで、主キーも NOT NULL も UNIQUE も付けない。**
        # Access から変換したファイルがそうなっている(`accdb_to_sqlite.py`
        # は元の形をそのまま写す)。守りはアプリ側にある、という前提を
        # テストでも崩さない
        conn.execute(
            f"CREATE TABLE [{config.KANBAN_TABLE_PREFIX}LVC] "
            "([管理番号] NUMERIC, [資材] TEXT, [サイズ] TEXT, [不] TEXT,"
            " [常設品] TEXT, [更新日] DATETIME)"
        )
        conn.executemany(
            f"INSERT INTO [{config.KANBAN_TABLE_PREFIX}LVC]"
            " ([管理番号], [資材], [サイズ]) VALUES (?, ?, ?)",
            [(1, "外装紙", "2200"), (2, "アングル", "2510")],
        )
        conn.execute(
            f"CREATE TABLE [{config.TABLE_STATE}] ([ライン名] TEXT, [状態] TEXT)"
        )
        conn.commit()
        conn.close()
        self.cache = self.dir / "cache"
        self.db = SharedDb(str(self.path), cache_dir=str(self.cache))
        self.table = f"{config.KANBAN_TABLE_PREFIX}LVC"

    def row_key(self, mgmt_no, table: str | None = None):
        """その管理番号の行を指す ``__行``(= rowid)。

        画面は表を開いたときにこれを受け取り、書き換え・削除で送り返す。
        """
        view = master.open_table(self.db, table or self.table)
        for row in view.rows:
            if str(row.get(config.COL_KEY, "")).strip() == str(mgmt_no):
                return row[view.row_key]
        raise AssertionError(f"管理番号 {mgmt_no} の行がない")


class MasterAvailabilityTest(MasterTestBase):
    """**パスが無ければ何もできない。**

    空表を出すと「マスタが消えた」に見える。理由と、次に何をすればよいかを返す。
    """

    def test_unset_path_is_blocked(self):
        view = master.frame(SharedDb(""))
        self.assertFalse(view.available)
        self.assertEqual(view.blocked, master.BLOCK_NO_PATH)
        self.assertIn("設定されていません", view.why)

    def test_missing_file_is_blocked(self):
        view = master.frame(SharedDb(str(self.dir / "ない.sqlite3")))
        self.assertFalse(view.available)
        self.assertEqual(view.blocked, master.BLOCK_MISSING)
        self.assertIn("共有フォルダ", view.why)

    def test_none_is_blocked(self):
        self.assertEqual(master.frame(None).blocked, master.BLOCK_NO_PATH)

    def test_reachable_db_is_available(self):
        view = master.frame(self.db)
        self.assertTrue(view.available)
        self.assertEqual(view.blocked, "")

    def test_edit_is_blocked_without_a_path(self):
        """開くだけでなく、直す側でも同じ関門を通す。"""
        result = master.update_cell(SharedDb(""), "看板_LVC", 1, "資材", "x")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, master.BLOCK_NO_PATH)


class MasterRowKeyTest(MasterTestBase):
    """**行は取り込み元の言葉で指す**(参照リポの思想2)。

    業務のキー(``管理番号``)で指すと、そのキーが重複した表では 1 回の
    操作が 2 行に当たる ──「直したつもりが別の行だった」になる。
    ``rowid`` で指せば、重複していても 1 行だけを指せる。
    """

    def duplicate(self) -> None:
        """管理番号が重なった行を作る(スキーマは UNIQUE を持たない)。"""
        conn = sqlite3.connect(str(self.path))
        conn.execute(
            f"INSERT INTO [{self.table}] ([管理番号], [資材]) VALUES (1, 'そっくりさん')"
        )
        conn.commit()
        conn.close()

    def materials(self) -> list[str]:
        conn = sqlite3.connect(str(self.path))
        try:
            return [
                r[0]
                for r in conn.execute(
                    f"SELECT [資材] FROM [{self.table}] ORDER BY rowid"
                )
            ]
        finally:
            conn.close()

    def test_rows_carry_a_row_key(self):
        view = master.open_table(self.db, self.table)
        self.assertEqual(view.row_key, "__行")
        self.assertTrue(all(view.row_key in r for r in view.rows))

    def test_the_row_key_is_not_shown_as_a_column(self):
        """隠し列。画面の表に出すものではない。"""
        view = master.open_table(self.db, self.table)
        self.assertNotIn(view.row_key, view.columns)
        self.assertNotIn(view.row_key, [c.name for c in view.column_info])

    def test_editing_a_duplicated_key_touches_only_one_row(self):
        """**ここが本題。** 管理番号が重なっていても 1 行だけ直す。"""
        self.duplicate()
        self.assertEqual(self.materials(), ["外装紙", "アングル", "そっくりさん"])

        view = master.open_table(self.db, self.table)
        target = [r for r in view.rows if str(r[config.COL_KEY]) == "1"][-1]
        result = master.update_cell(
            self.db, self.table, target[view.row_key], "資材", "直した"
        )
        self.assertTrue(result.ok, result.message)
        self.assertEqual(self.materials(), ["外装紙", "アングル", "直した"])

    def test_deleting_a_duplicated_key_removes_only_one_row(self):
        self.duplicate()
        view = master.open_table(self.db, self.table)
        target = [r for r in view.rows if str(r[config.COL_KEY]) == "1"][0]
        self.assertTrue(
            master.delete_row(self.db, self.table, target[view.row_key]).ok
        )
        self.assertEqual(self.materials(), ["アングル", "そっくりさん"])

    def test_a_missing_row_key_is_refused(self):
        """どの行か分からないまま書かない。当てずっぽうで別の行を触るより断る。"""
        result = master.update_cell(self.db, self.table, "", "資材", "x")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "no_row_key")


class MasterShiftedRowTest(MasterTestBase):
    """**画面を開いたあとに rowid が振り直されても、別の看板を直さない・消さない。**

    表に INTEGER PRIMARY KEY が無いので、DB Browser の「最適化」(VACUUM)や
    消して入れ直すと rowid が変わる。以前は 1 番を直したつもりで 3 番が変わった。
    """

    def shift(self) -> None:
        conn = sqlite3.connect(str(self.path))
        rows = conn.execute(f"SELECT * FROM [{self.table}] ORDER BY [管理番号] DESC").fetchall()
        conn.execute(f"DELETE FROM [{self.table}]")
        conn.executemany(
            f"INSERT INTO [{self.table}] VALUES ({','.join('?' * len(rows[0]))})", rows)
        conn.commit()
        conn.close()

    def material(self, no):
        conn = sqlite3.connect(str(self.path))
        try:
            return conn.execute(
                f"SELECT [資材] FROM [{self.table}] WHERE [管理番号] = ?", (no,)).fetchone()[0]
        finally:
            conn.close()

    def test_edit_is_refused_when_the_row_now_holds_another_kanban(self):
        seen = self.row_key(1)
        self.shift()
        result = master.update_cell(self.db, self.table, seen, "資材", "直した", expect_key=1)
        self.assertEqual(result.reason, "not_found")
        self.assertEqual((self.material(1), self.material(2)), ("外装紙", "アングル"))

    def test_delete_is_refused_too(self):
        seen = self.row_key(1)
        self.shift()
        result = master.delete_row(self.db, self.table, seen, expect_key=1)
        self.assertEqual(result.reason, "not_found")
        self.assertEqual((self.material(1), self.material(2)), ("外装紙", "アングル"))

    def test_the_same_row_still_works(self):
        """数と文字の違い(DB の 1 と画面の "1")では断らない。"""
        result = master.update_cell(self.db, self.table, self.row_key(1), "資材", "直した",
                                    expect_key="1")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(self.material(1), "直した")


class MasterViewOnlyTest(MasterTestBase):
    """**直せない表は隠さず、理由を出す**(参照リポの思想3)。

    隠すと、探している人には画面が壊れて見える。
    """

    def test_the_state_table_is_shown_but_not_editable(self):
        view = master.open_table(self.db, config.TABLE_STATE)
        self.assertTrue(view.available, "隠してはいけない")
        self.assertTrue(view.rows == [] or view.rows)  # 中身は読める
        self.assertIn("自動で書き込む", view.view_only_why)

    def test_kanban_tables_stay_editable(self):
        self.assertEqual(master.open_table(self.db, self.table).view_only_why, "")
        self.assertTrue(master.editable(self.table))

    def test_writing_to_a_view_only_table_is_refused(self):
        for call in (
            lambda: master.update_cell(self.db, config.TABLE_STATE, 1, "状態", "開"),
            lambda: master.delete_row(self.db, config.TABLE_STATE, 1),
            lambda: master.insert_row(self.db, config.TABLE_STATE, {"ライン名": "X"}),
        ):
            result = call()
            self.assertFalse(result.ok)
            self.assertEqual(result.reason, "view_only")

    def test_unknown_tables_get_a_default_reason(self):
        """知らない表は触らせない。何のための表か分からないまま書き換えない。"""
        conn = sqlite3.connect(str(self.path))
        conn.execute("CREATE TABLE [よその表] (a)")
        conn.commit()
        conn.close()
        self.assertIn("この道具が直す表ではありません", master.view_only_why("よその表"))


class MasterBrowseTest(MasterTestBase):
    def test_lists_tables_without_reading_them(self):
        view = master.frame(self.db)
        names = {t.name for t in view.tables}
        self.assertEqual(names, {self.table, config.TABLE_STATE})
        # 一覧の時点では中身を読まない(共有への往復を増やさない)
        self.assertEqual(view.rows, [])

    def test_classifies_tables(self):
        kinds = {t.name: t.kind for t in master.frame(self.db).tables}
        self.assertEqual(kinds[self.table], "kanban")
        self.assertEqual(kinds[config.TABLE_STATE], "state")

    def test_open_table_returns_rows_and_key(self):
        view = master.open_table(self.db, self.table)
        self.assertEqual(view.table, self.table)
        self.assertEqual(view.key_column, config.COL_KEY)
        self.assertEqual(view.total, 2)
        self.assertEqual(view.rows[0]["資材"], "外装紙")

    def test_unknown_table_is_refused(self):
        """**一覧に無いものは開かせない。** 画面で絞るだけでは足りない。"""
        view = master.open_table(self.db, "sqlite_master")
        self.assertFalse(view.available)
        self.assertIn("ありません", view.why)

    def test_paging(self):
        view = master.open_table(self.db, self.table, page=99)
        # 範囲外のページは最後のページへ畳む
        self.assertEqual(view.page, view.pages - 1)


class MasterEditTest(MasterTestBase):
    def value(self, key: int, column: str):
        conn = sqlite3.connect(str(self.path))
        try:
            return conn.execute(
                f"SELECT [{column}] FROM [{self.table}] WHERE [管理番号] = ?", (key,)
            ).fetchone()[0]
        finally:
            conn.close()

    def test_updates_one_cell(self):
        result = master.update_cell(self.db, self.table, self.row_key(1), "資材", "外装紙B")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(self.value(1, "資材"), "外装紙B")
        # 他の行は触らない
        self.assertEqual(self.value(2, "資材"), "アングル")

    def test_key_column_cannot_be_changed(self):
        """キーを書き換えると、どの行だったのかが辿れなくなる。"""
        result = master.update_cell(self.db, self.table, self.row_key(1), config.COL_KEY, "99")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "key_readonly")

    def test_unknown_column_is_refused(self):
        result = master.update_cell(self.db, self.table, self.row_key(1), "架空", "x")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "no_column")

    def test_unknown_table_is_refused(self):
        result = master.update_cell(self.db, "sqlite_master", 1, "name", "x")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "no_table")

    def test_missing_row_is_reported(self):
        """他端末が先に消していた場合。画面を取り直せば正しい状態が見える。"""
        result = master.update_cell(self.db, self.table, 999_999, "資材", "x")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "not_found")

    def test_bad_datetime_is_refused(self):
        """日時列に日時でない字を入れさせない。

        **黙って直さない**のも大事 ── 打ち間違いを別の日時にすり替えると、
        いつの操作だったのかが分からなくなる。(``更新日`` は看板の状態で
        読むだけなので、状態でない日時の列で確かめる)
        """
        add_date_column(self.path, self.table)
        result = master.update_cell(self.db, self.table, self.row_key(1), "棚卸日", "きのう")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "bad_value")
        self.assertTrue(
            master.update_cell(self.db, self.table, self.row_key(1), "棚卸日", "2026/8/13 9:05").ok
        )


class MasterInsertTest(MasterTestBase):
    """**看板を 1 枚増やす。** これが無いと結局 Access を開くことになる。"""

    def rows(self) -> list[tuple]:
        conn = sqlite3.connect(str(self.path))
        try:
            return conn.execute(
                f"SELECT [管理番号], [資材] FROM [{self.table}] ORDER BY [管理番号]"
            ).fetchall()
        finally:
            conn.close()

    def test_adds_a_row(self):
        result = master.insert_row(
            self.db, self.table,
            {"管理番号": "3", "資材": "ハードボード", "サイズ": "660 × 1050"},
        )
        self.assertTrue(result.ok, result.message)
        self.assertEqual(self.rows(), [(1, "外装紙"), (2, "アングル"), (3, "ハードボード")])

    def test_key_is_stored_as_a_number(self):
        """キーは列の型に合わせて入れる。

        画面から来るのは文字なので、そのまま入れると同じ ``3`` でも
        文字の ``"3"`` と数の ``3`` が混ざる。
        """
        master.insert_row(self.db, self.table, {"管理番号": "3", "資材": "x", "サイズ": "サイズ3"})
        self.assertEqual(self.rows()[-1][0], 3)

    def test_duplicate_key_is_refused(self):
        """**スキーマが止めてくれない分をここで止める。**

        ``看板_*`` に UNIQUE は無い。重複すると、書き戻し
        (``WHERE 管理番号 = ?``)が 1 回で 2 行を書き換える。
        """
        result = master.insert_row(self.db, self.table, {"管理番号": "1", "資材": "x", "サイズ": "サイズ1"})
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "duplicate_key")
        self.assertEqual(len(self.rows()), 2)

    def test_duplicate_key_is_caught_across_types(self):
        """DB に数で入っている ``1`` と、画面から来る ``"1"`` は同じ行。

        型のまま比べると重複を見逃す。
        """
        result = master.insert_row(self.db, self.table, {"管理番号": 1, "資材": "x", "サイズ": "サイズ1"})
        self.assertEqual(result.reason, "duplicate_key")

    def test_sql_itself_refuses_a_duplicate(self):
        """**読んでから書く**あいだに他端末が入れた場合。

        事前の確認だけでは擦り抜ける(UNIQUE が無いので DB も止めない)。
        1 文の中でも確かめていることを、確認を飛ばして確かめる。
        """
        snapshot = self.db.read_table(self.table)
        snapshot.rows.clear()          # 事前の確認が「無い」と答える状況を作る
        with unittest.mock.patch.object(self.db, "read_table", return_value=snapshot):
            result = master.insert_row(self.db, self.table, {"管理番号": "1", "資材": "x", "サイズ": "サイズ1"})
        self.assertEqual(result.reason, "duplicate_key")
        self.assertEqual(len(self.rows()), 2, "重複が入っている")

    def test_key_cannot_be_blank(self):
        """キーの無い行は、書き戻しから永久に外れる(誰も直せない行)。"""
        result = master.insert_row(self.db, self.table, {"管理番号": "", "資材": "x", "サイズ": "サイズ空"})
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "required")

    def test_material_cannot_be_blank_on_a_kanban_table(self):
        """資材が空の看板は、画面ではただの空ボタン。足した本人も見分けが付かない。"""
        result = master.insert_row(self.db, self.table, {"管理番号": "3", "資材": "", "サイズ": "サイズ3"})
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "required")

    def test_other_tables_cannot_be_written(self):
        """**直せるのは看板の表だけ。**

        ``Form状態管理`` は各端末が自動で書き込む表なので、手で足しても
        次の書き込みで食い違う。
        """
        result = master.insert_row(
            self.db, config.TABLE_STATE, {"ライン名": "LVC", "状態": "開"}
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "view_only")
        self.assertIn("自動で書き込む", result.message)

    def test_unknown_column_is_refused(self):
        result = master.insert_row(
            self.db, self.table, {"管理番号": "3", "資材": "x", "架空": "y"}
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "no_column")

    def test_bad_datetime_is_refused(self):
        add_date_column(self.path, self.table)
        result = master.insert_row(
            self.db, self.table, {"管理番号": "3", "資材": "x", "サイズ": "サイズ3", "棚卸日": "きのう"}
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "bad_value")
        self.assertEqual(len(self.rows()), 2, "断ったのに入っている")

    def test_blank_stays_blank_not_null(self):
        """空欄は空文字のまま。**ここだけ NULL を混ぜない。**

        Access から変換したファイルは空文字で揃っている。混ぜると、
        SQLite を直接覗いた人に「この行だけ何か違う」と読ませてしまう。
        """
        master.insert_row(self.db, self.table, {"管理番号": "3", "資材": "x", "サイズ": "サイズ3"})
        conn = sqlite3.connect(str(self.path))
        try:
            value = conn.execute(
                f"SELECT [常設品] FROM [{self.table}] WHERE [管理番号] = 3"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(value, "")

    def test_insert_is_blocked_without_a_path(self):
        result = master.insert_row(SharedDb(""), self.table, {"管理番号": "3"})
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, master.BLOCK_NO_PATH)


class MasterAddFormTest(MasterTestBase):
    """画面が入力欄を組むための材料。"""

    def test_suggests_the_next_key(self):
        view = master.open_table(self.db, self.table)
        self.assertEqual(view.next_key, "3")

    def test_suggests_one_for_an_empty_table(self):
        view = master.open_table(self.db, config.TABLE_STATE)
        self.assertEqual(view.next_key, "1")

    def test_does_not_guess_when_keys_are_not_numbers(self):
        """枝番などの運用を、当てずっぽうで塞がない。"""
        conn = sqlite3.connect(str(self.path))
        conn.execute(
            f"INSERT INTO [{self.table}] ([管理番号], [資材]) VALUES ('3-1', 'x')"
        )
        conn.commit()
        conn.close()
        self.assertEqual(master.open_table(self.db, self.table).next_key, "")

    def test_kanban_defaults_are_shown_not_applied_silently(self):
        """既定値は**画面に入れて見せる**。サーバが黙って足さない。"""
        view = master.open_table(self.db, self.table)
        self.assertEqual(view.defaults, {"常設品": config.MARK_ON})

        # 送られてこなければ、そのまま空で入る
        master.insert_row(self.db, self.table, {"管理番号": "3", "資材": "x", "サイズ": "サイズ3"})
        conn = sqlite3.connect(str(self.path))
        try:
            value = conn.execute(
                f"SELECT [常設品] FROM [{self.table}] WHERE [管理番号] = 3"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(value, "")

    def test_the_initial_state_is_shown_as_fixed(self):
        """看板の状態は選ばせない。固定で入る値を、読むだけの欄として見せる。"""
        view = master.open_table(self.db, self.table)
        # この表にある状態の列だけ(不・更新日)
        self.assertEqual(view.fixed, {"不": config.MARK_ON, "更新日": ""})
        self.assertEqual(master.open_table(self.db, config.TABLE_STATE).fixed, {})

    def test_no_defaults_for_other_tables(self):
        self.assertEqual(master.open_table(self.db, config.TABLE_STATE).defaults, {})

    def test_column_info_marks_key_and_required(self):
        view = master.open_table(self.db, self.table)
        info = {c.name: c for c in view.column_info}
        self.assertTrue(info[config.COL_KEY].is_key)
        self.assertTrue(info[config.COL_KEY].required)
        self.assertTrue(info["資材"].required)
        # サイズも要る。空の看板は盤に出ない(足せたと言われるのに見えない)
        self.assertTrue(info["サイズ"].required)
        self.assertFalse(info["不"].required)
        self.assertEqual(info["更新日"].category, "DATE")
        # 看板の状態は読むだけ。資材・サイズ・常設品は直せる
        self.assertEqual({n for n, c in info.items() if c.state}, {"不", "更新日"})


class MasterKanbanRulesTest(MasterTestBase):
    """**看板として盤に出る形でなければ、足させない・直させない。**

    再点検で見つかったもの。どれも「足しました/更新しました」と言われるのに、
    盤には出ない・印が効かない、という一番気付きにくい壊れ方をしていた。
    """

    def add(self, **values):
        return master.insert_row(self.db, self.table, values)

    def edit(self, mgmt_no, column, value):
        return master.update_cell(self.db, self.table, self.row_key(mgmt_no), column, value)

    # -- 足す --------------------------------------------------------------
    def test_size_is_required(self):
        """サイズが空の看板は盤に出ない(資材・サイズの両方がある行だけ出す)。"""
        result = self.add(管理番号="3", 資材="中性紙", サイズ="")
        self.assertEqual(result.reason, "required")
        self.assertIn("盤に出ません", result.message)

    def test_same_material_and_size_is_refused(self):
        """盤は資材ごとにサイズの重複を 1 枚にまとめる。2 枚目は足せても出ない。"""
        result = self.add(管理番号="3", 資材="外装紙", サイズ="2200")
        self.assertEqual(result.reason, "duplicate_kanban")
        self.assertIn("管理番号 1", result.message, "どれと重なったかを言っていない")

    def test_same_size_under_another_material_is_fine(self):
        self.assertTrue(self.add(管理番号="3", 資材="中性紙", サイズ="2200").ok)

    def test_nan_and_inf_are_not_numbers(self):
        """``float()`` は "nan" を数として通す。nan は NULL になり、キーの無い行ができた。"""
        for value in ("nan", "inf", "-inf"):
            result = self.add(管理番号=value, 資材="中性紙", サイズ=value)
            self.assertEqual(result.reason, "bad_value", value)

    def test_marks_must_be_the_real_characters(self):
        """アプリは印を 1 文字で見分ける。似た字が入ると、印が無いものとして扱われる。"""
        for value in ("x", "X", "○", "0"):
            result = self.add(管理番号="3", 資材="中性紙", サイズ="1", 常設品=value)
            self.assertEqual(result.reason, "bad_mark", f"常設品={value}")
        self.assertTrue(self.add(管理番号="3", 資材="中性紙", サイズ="1", 常設品="×").ok)

    # -- 直す --------------------------------------------------------------
    def test_editing_follows_the_same_rules_as_adding(self):
        """以前は足すときだけ確かめていて、直せば資材を空にできた(看板が盤から消える)。"""
        self.assertEqual(self.edit(2, "資材", "").reason, "required")
        self.assertEqual(self.edit(2, "サイズ", "").reason, "required")
        self.assertEqual(self.edit(2, "常設品", "x").reason, "bad_mark")

    def test_editing_into_a_duplicate_is_refused(self):
        self.add(管理番号="3", 資材="外装紙", サイズ="1900")
        self.assertEqual(self.edit(3, "サイズ", "2200").reason, "duplicate_kanban")

    def test_a_row_is_not_a_duplicate_of_itself(self):
        """自分自身と比べて「重複」と言わないこと(同じ値で直し直すことはある)。"""
        self.assertTrue(self.edit(1, "サイズ", "2200").ok)

    def test_editing_one_column_ignores_odd_values_elsewhere(self):
        """直す列だけを確かめる。昔から入っている変わった値のせいで、関係の
        ない修正まで断らない。"""
        conn = sqlite3.connect(str(self.path))
        conn.execute(f"UPDATE [{self.table}] SET [不] = '○' WHERE [管理番号] = 1")
        conn.commit()
        conn.close()
        self.assertTrue(self.edit(1, "サイズ", "2300").ok)

    # -- 看板の状態は読むだけ ----------------------------------------------
    def test_state_columns_cannot_be_edited(self):
        """発注・発送はボタンが組にして書く(欲と不と更新日を同時に)。
        1 マスだけ直すと、ボタンでは作れない看板ができる。"""
        for column, value in (("不", ""), ("不", "〇"), ("更新日", "2026/08/31 09:00:00")):
            result = self.edit(1, column, value)
            self.assertEqual(result.reason, "state_readonly", column)
            self.assertIn("看板画面のボタン", result.message)

    def test_a_new_kanban_starts_as_not_ordered(self):
        """足すときは状態を選ばせず、「まだ発注していない」(不 = 〇)で入れる。"""
        self.assertTrue(self.add(管理番号="3", 資材="中性紙", サイズ="1").ok)
        conn = sqlite3.connect(str(self.path))
        try:
            row = conn.execute(
                f"SELECT [不], [更新日] FROM [{self.table}] WHERE [管理番号] = 3"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row, (config.MARK_ON, ""))

    def test_sending_another_state_on_insert_is_refused(self):
        """黙って捨てると、入れたつもりの値が消えたように見える。"""
        for column, value in (("不", "×"), ("更新日", "2026/08/31 09:00:00")):
            result = self.add(管理番号="3", 資材="中性紙", サイズ="1", **{column: value})
            self.assertEqual(result.reason, "state_readonly", column)
        # 初期の状態と同じ値なら通す
        self.assertTrue(self.add(管理番号="3", 資材="中性紙", サイズ="1", 不="〇").ok)

    def test_blank_state_values_mean_not_specified(self):
        """**古い画面は状態の欄を空で送ってくる。** それを断っていたので、
        画面が古いままの端末では何も足せなかった(「不」が空で断られる)。"""
        result = self.add(管理番号="3", 資材="中性紙", サイズ="1",
                          不="", 更新日="")
        self.assertTrue(result.ok, result.message)
        conn = sqlite3.connect(str(self.path))
        try:
            row = conn.execute(
                f"SELECT [不] FROM [{self.table}] WHERE [管理番号] = 3").fetchone()
        finally:
            conn.close()
        self.assertEqual(row, (config.MARK_ON,), "空で送られても初期の状態で入る")

    # -- 次の番号 ----------------------------------------------------------
    def test_a_blank_key_row_does_not_stop_the_next_key(self):
        """変換した表には管理番号が NULL の行が混ざり得る。以前は 1 行あるだけで
        「数字でないキー」と取り違えて、次の番号を当てるのをやめていた。"""
        conn = sqlite3.connect(str(self.path))
        conn.execute(f"INSERT INTO [{self.table}] ([資材]) VALUES ('ゴミ行')")
        conn.commit()
        conn.close()
        self.assertEqual(master.open_table(self.db, self.table).next_key, "3")


class MasterDeleteTest(MasterTestBase):
    def keys(self) -> list[int]:
        conn = sqlite3.connect(str(self.path))
        try:
            return [
                r[0]
                for r in conn.execute(f"SELECT [管理番号] FROM [{self.table}] ORDER BY 1")
            ]
        finally:
            conn.close()

    def test_deletes_one_row(self):
        result = master.delete_row(self.db, self.table, self.row_key(1))
        self.assertTrue(result.ok, result.message)
        self.assertEqual(self.keys(), [2])

    def test_missing_row_is_reported(self):
        result = master.delete_row(self.db, self.table, 999_999)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "not_found")

    def test_delete_is_blocked_without_a_path(self):
        result = master.delete_row(SharedDb(""), self.table, 1)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, master.BLOCK_NO_PATH)

    def test_a_mistyped_row_can_be_taken_back(self):
        """**足せるなら消せないと困る。**

        キー列は直せないので、番号を打ち間違えて足した行は消すしかない。
        """
        master.insert_row(self.db, self.table, {"管理番号": "33", "資材": "打ち間違い", "サイズ": "サイズ33"})
        self.assertIn(33, self.keys())
        self.assertTrue(master.delete_row(self.db, self.table, self.row_key(33)).ok)
        self.assertNotIn(33, self.keys())


if __name__ == "__main__":
    unittest.main()
