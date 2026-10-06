"""VC計算マスタ: 初期値・早見表・版の更新・書く取引・読めないとき・マスをまとめて作る

vc-calculator `tests/test_master_db.py` / `test_master_admin.py`(QuickGridTest)の移植。
**期待値は同じです**(早見表 133 セル・紙の早見表と違う 13 セル・版1/版2 からの更新)。

読み方だけが日報管理ツールに合わせて変わっています ── 手元に写してから読み
(`source_db`)、読めなければ「最後に読めた中身 → その控え → VBA の初期値」。
"""
from __future__ import annotations

import math
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou import source_db  # noqa: E402
from nippou.vc import db, grid, masters, quick_table, seed  # noqa: E402
from nippou.vc.calc import vc_length  # noqa: E402


class MasterCase(unittest.TestCase):
    """新しいマスタ(初期値入り)を1つ持つ試験。**一時フォルダの外に書かない。**"""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="vc_master_"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = self.dir / "VC計算マスタ.sqlite3"
        copies = self.dir / "copies"
        copies.mkdir()
        for target, value in ((masters, "master_path"), (masters, "cache_path"),
                              (source_db, "copy_dir")):
            fake = {"master_path": lambda: self.path,
                    "cache_path": lambda: self.dir / "cache" / "snap.json",
                    "copy_dir": lambda: copies}[value]
            patcher = mock.patch.object(target, value, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        db._backed_up.clear()
        self.assertEqual(db.ensure_database(self.path), "created")
        masters.reset()
        source_db.forget()
        self.addCleanup(masters.reset)
        self.addCleanup(source_db.forget)

    def write(self, sql: str) -> None:
        """ほかの端末(または管理者)が直した、のつもりで書く。"""
        with db.writing(self.path) as conn:
            conn.execute(sql)
            db.bump_revision(conn)
        source_db.forget(self.path)
        masters.invalidate()


class SeedTest(MasterCase):
    def test_products_are_the_vba_list(self) -> None:
        snap = masters.current()
        self.assertEqual(snap.source, "db")
        self.assertEqual([p.name for p in snap.products], [
            "2008系/2001SR/310GH5", "224R/AL200R", "TF200/TF200B",
            "V325系", "B500系/T500系", "ｽﾐﾛﾝVE系"])
        nitto = snap.product("2008系/2001SR/310GH5")
        self.assertTrue(nitto.chooses_inside)
        self.assertEqual([(c.label, c.inside) for c in nitto.choices],
                         [("小", 87.0), ("大", 95.0)])
        tf = snap.product("TF200/TF200B")
        self.assertFalse(tf.chooses_inside)
        self.assertEqual((tf.vcatu, tf.inside), (0.06, 87.0))
        self.assertEqual([(s.key, s.label, s.length_mm) for s in snap.sheets],
                         [("M1", "1×2", 2010.0), ("M4", "2×4", 2510.0), ("M5", "5×10", 3065.0)])
        self.assertTrue(snap.reverse_enabled())          # Q3: 使う
        self.assertEqual(snap.setting("早見表の丸め"), "切り捨て")

    def test_quick_table_has_every_cell_of_the_image(self) -> None:
        view = quick_table.build(masters.current())
        self.assertEqual(view["title"], "VCフィルム長さ早見表")
        self.assertEqual(view["revision_label"], "R.1.10/1")
        self.assertEqual(view["rounding"], "切り捨て")
        self.assertEqual([b["name"] for b in view["blocks"]], [
            "2008系/2001SR/310GH5", "V325系", "224R/AL200R", "B500系/T500系",
            "TF200/TF200B", "R575B", "VE系"])
        cells = sum(1 for b in view["blocks"] for r in b["rows"] for c in r["cells"] if c)
        self.assertEqual(cells, 133)
        first = view["blocks"][0]
        self.assertEqual(first["headers"][-1], "47")
        self.assertEqual([r["label"] for r in first["rows"]], ["内径87mm", "内径95mm"])
        self.assertEqual(first["rows"][1]["cells"][-1], "")     # 内径95 の 47 は空欄
        self.assertEqual([b["source"] for b in view["blocks"]],
                         ["式"] * 5 + ["固定値", "式"])

    def test_quick_table_is_the_workbook_formula(self) -> None:
        """早見表 = 正しい式を整数に切り捨て。紙の早見表と違うのは 13 セル(docs/vc/VBA解析.md §12)。"""
        view = quick_table.build(masters.current())
        shown = {}
        for b in view["blocks"]:
            for r in b["rows"]:
                for head, cell in zip(b["headers"], r["cells"]):
                    if cell:
                        shown[(b["name"], float(r["inside"]), float(head))] = int(cell)
        vcatu = {"2008系/2001SR/310GH5": 0.10, "V325系": 0.13, "224R/AL200R": 0.08,
                 "B500系/T500系": 0.06, "TF200/TF200B": 0.06, "VE系": 0.10}
        changed = []
        for name, rows in seed.QUICK_VALUES.items():
            for inside, pairs in rows.items():
                for thickness, paper in pairs:
                    got = shown[(name, inside, float(thickness))]
                    if name not in vcatu:
                        self.assertEqual(got, paper)           # R575B は紙のまま
                        continue
                    self.assertEqual(got, math.floor(vc_length(thickness, vcatu[name], inside)))
                    if got != paper:
                        changed.append((name, inside, thickness, paper, got))
        self.assertEqual(changed, [
            ("2008系/2001SR/310GH5", 87.0, 35, 133, 134),
            ("2008系/2001SR/310GH5", 87.0, 38, 148, 149),
            ("V325系", 83.0, 38, 110, 111),
            ("V325系", 83.0, 44, 134, 135),
            ("224R/AL200R", 87.0, 10, 37, 38),
            ("224R/AL200R", 87.0, 20, 83, 84),
            ("B500系/T500系", 94.0, 14, 78, 79),
            ("B500系/T500系", 94.0, 16, 91, 92),
            ("B500系/T500系", 94.0, 27, 170, 171),
            ("TF200/TF200B", 87.0, 14, 73, 74),
            ("TF200/TF200B", 87.0, 20, 111, 112),
            ("TF200/TF200B", 87.0, 27, 160, 161),
            ("VE系", 98.0, 33, 136, 135),
        ])

    def test_quick_table_follows_film_thickness(self) -> None:
        """VC品種 の VC厚 を直すと早見表も変わる(VC厚を2か所に持たない)。"""
        self.write("UPDATE VC品種 SET VC厚 = 0.2 WHERE 品種名 = 'V325系'")
        block = next(b for b in quick_table.build(masters.current())["blocks"]
                     if b["name"] == "V325系")
        self.assertEqual(block["rows"][0]["cells"][0],
                         str(math.floor(vc_length(5, 0.2, 83))))

    def test_rounding_setting(self) -> None:
        self.write("UPDATE アプリ設定 SET 値 = '四捨五入' WHERE キー = '早見表の丸め'")
        block = next(b for b in quick_table.build(masters.current())["blocks"]
                     if b["name"] == "2008系/2001SR/310GH5")
        self.assertEqual(block["rows"][0]["cells"][1], "24")   # 23.81… → 24


class CreateTest(MasterCase):
    def test_second_ensure_is_a_no_op(self) -> None:
        self.assertEqual(db.ensure_database(self.path), "ok")

    def test_not_wal(self) -> None:
        with db.reading(self.path) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        self.assertFalse(self.path.with_name(self.path.name + "-wal").exists())

    def test_two_requests_at_once_on_a_new_master(self) -> None:
        """画面を開くと計算と設定の要求が同時に来る。**初めてのマスタでも両方が読める。**

        前は同じ一時ファイルへ初期値を2度入れ、片方が「UNIQUE constraint
        failed: VC品種.品種名」で落ちて、しばらく控えで動いていた(v3.96.0)。
        """
        self.path.unlink()
        masters.reset()
        slow_seed = db.insert_seed

        def seed_slowly(conn):
            slow_seed(conn)
            time.sleep(0.2)                  # 作っている途中にもう1つの要求が来る

        got: list = []
        with mock.patch.object(db, "insert_seed", seed_slowly):
            threads = [threading.Thread(target=lambda: got.append(masters.current()))
                       for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        self.assertEqual([s.source for s in got], ["db", "db"], [s.problem for s in got])
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()
                                if p.name.endswith(".creating")), [])

    def test_folder_is_not_created(self) -> None:
        """参照用マスタのフォルダが無ければ作らない(黙って別の場所にマスタができる)。"""
        with self.assertRaises(db.DbUnavailable):
            db.ensure_database(self.dir / "無いフォルダ" / "VC計算マスタ.sqlite3")
        self.assertFalse((self.dir / "無いフォルダ").exists())

    def test_version_1_master_is_upgraded(self) -> None:
        """vc-calculator VER1.0.0 のマスタ(早見表に紙の数値・逆算 0)を今の版へ。"""
        conn = sqlite3.connect(str(self.path), isolation_level=None)
        conn.execute("BEGIN")
        conn.execute("ALTER TABLE 早見表ブロック DROP COLUMN 計算品種")
        conn.execute("ALTER TABLE 早見表値 RENAME TO t")
        conn.execute('CREATE TABLE "早見表値" ("品種名" TEXT NOT NULL REFERENCES '
                     '"早見表ブロック"("品種名") ON UPDATE CASCADE ON DELETE RESTRICT, '
                     '"内径" REAL NOT NULL, "肉厚" REAL NOT NULL, "長さ" INTEGER NOT NULL, '
                     '"更新日時" TEXT, UNIQUE ("品種名", "内径", "肉厚"))')
        conn.execute("DROP TABLE t")
        for name, rows in seed.QUICK_VALUES.items():
            for inside, pairs in rows.items():
                for t, v in pairs:
                    conn.execute("INSERT INTO 早見表値 VALUES (?, ?, ?, ?, NULL)",
                                 [name, inside, t, v])
        conn.execute("UPDATE アプリ設定 SET 値 = '0' WHERE キー = '肉厚の逆算'")
        conn.execute("DELETE FROM アプリ設定 WHERE キー = '早見表の丸め'")
        conn.execute("UPDATE _メタ SET 値 = '1' WHERE キー = 'schema_version'")
        conn.execute("COMMIT")
        conn.close()

        self.assertEqual(db.ensure_database(self.path), "upgraded")
        source_db.forget(self.path)
        masters.reset()
        snap = masters.current()
        self.assertTrue(snap.reverse_enabled())
        self.assertEqual(snap.setting("早見表の丸め"), "切り捨て")
        ve = next(b for b in snap.quick if b.name == "VE系")
        self.assertEqual((ve.product, ve.vcatu), ("ｽﾐﾛﾝVE系", 0.1))
        self.assertTrue(all(v is None for r in ve.rows for v in r.cells.values()))
        r575 = next(b for b in snap.quick if b.name == "R575B")
        self.assertEqual(r575.rows[0].cells[5.0], 26)
        with db.reading(self.path) as conn:
            self.assertEqual(db.meta_get(conn, "schema_version"), db.SCHEMA_VERSION)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM 早見表値").fetchone()[0], 133)
        self.assertEqual(db.ensure_database(self.path), "ok")

    def test_version_2_master_gets_sheet_names(self) -> None:
        """VER1.5.0 のマスタ(表示名 = 記号)は 1×2 / 2×4 / 5×10 に。直した表示名は残す。"""
        with db.writing(self.path) as conn:
            conn.execute("UPDATE 枚数定尺 SET 表示名 = 記号")
            conn.execute("UPDATE 枚数定尺 SET 表示名 = '特寸' WHERE 記号 = 'M5'")
            conn.execute("UPDATE _メタ SET 値 = '2' WHERE キー = 'schema_version'")
        self.assertEqual(db.ensure_database(self.path), "upgraded")
        source_db.forget(self.path)
        labels = {s.key: s.label for s in masters.current().sheets}
        self.assertEqual(labels, {"M1": "1×2", "M4": "2×4", "M5": "特寸"})
        with db.reading(self.path) as conn:
            self.assertEqual(db.meta_get(conn, "schema_version"), "3")
            logged = conn.execute(
                "SELECT COUNT(*) FROM 変更履歴 WHERE 表 = '枚数定尺'").fetchone()[0]
        self.assertEqual(logged, 2)

    def test_lost_setting_is_added_back(self) -> None:
        with db.writing(self.path) as conn:
            conn.execute("DELETE FROM アプリ設定 WHERE キー = '肉厚の逆算'")
        self.assertEqual(db.ensure_database(self.path), "upgraded")
        source_db.forget(self.path)
        self.assertEqual(masters.current().setting("肉厚の逆算"), "1")

    def test_reading_connection_cannot_write(self) -> None:
        with db.reading(self.path) as conn:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM VC品種")

    def test_daily_backup_before_first_write(self) -> None:
        with db.writing(self.path) as conn:
            db.bump_revision(conn)
        files = list(db.backup_dir(self.path).glob("*.sqlite3"))
        self.assertEqual(len(files), 1)


class RevisionTest(MasterCase):
    def test_other_terminal_change_is_seen_on_next_read(self) -> None:
        """ほかの端末(vc-calculator など)が直した ── 手元の写しは大きさ・時刻で気づく。"""
        first = masters.current()
        conn = sqlite3.connect(str(self.path))
        conn.execute("UPDATE VC品種 SET VC厚 = 0.2 WHERE 品種名 = 'V325系'")
        conn.execute("UPDATE _メタ SET 値 = CAST(CAST(値 AS INTEGER) + 1 AS TEXT)"
                     " WHERE キー = 'revision'")
        conn.commit()
        conn.close()
        source_db.forget(self.path)       # 同じ秒・同じ大きさでも読み直すように
        second = masters.current()
        self.assertEqual(second.revision, first.revision + 1)
        self.assertEqual(second.product("V325系").vcatu, 0.2)


class FallbackTest(MasterCase):
    def test_unreachable_share_uses_last_good_copy(self) -> None:
        good = masters.current()
        self.path.rename(self.path.with_suffix(".moved"))
        masters.reset()                   # 覚えていたものも忘れる → JSON の控え
        with mock.patch.object(db, "ensure_database",
                               side_effect=db.DbUnavailable("共有に届きません")):
            snap = masters.current()
        self.assertEqual(snap.source, "cache")
        self.assertIn("共有に届きません", snap.problem)
        self.assertEqual(snap.revision, good.revision)
        self.assertEqual(len(snap.products), 6)

    def test_nothing_at_all_falls_back_to_vba_values(self) -> None:
        """控えも無ければ VBA の直書きの値で計算を続ける(VBA を下回らない)。"""
        self.path.unlink()
        masters.reset()
        with mock.patch.object(db, "ensure_database",
                               side_effect=db.DbUnavailable("共有に届きません")), \
                mock.patch.object(masters, "_load_cache", return_value=None):
            snap = masters.current()
        self.assertEqual(snap.source, "seed")
        self.assertTrue(snap.usable)
        self.assertEqual(len(snap.products), 6)
        self.assertEqual(len(snap.sheets), 3)

    def test_failure_is_not_retried_every_request(self) -> None:
        """届かない共有を要求のたびに見に行かない(30秒は控えで返す)。"""
        masters.reset()
        calls = []

        def refuse(path):
            calls.append(path)
            raise db.DbUnavailable("共有に届きません")

        with mock.patch.object(db, "ensure_database", side_effect=refuse), \
                mock.patch.object(masters, "_load_cache", return_value=None):
            masters.current()
            masters.current()
        self.assertEqual(len(calls), 1)


class LockTest(MasterCase):
    def test_writer_waits_then_refuses_with_a_message(self) -> None:
        blocker = sqlite3.connect(str(self.path), isolation_level=None)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            with mock.patch.object(db, "BUSY_TIMEOUT_SEC", 0.1), \
                    mock.patch.object(db, "LOCK_RETRY", 1), \
                    mock.patch.object(db, "LOCK_RETRY_WAIT_SEC", 0.01):
                with self.assertRaises(db.DbLocked) as ctx:
                    with db.writing(self.path) as conn:
                        db.bump_revision(conn)
            self.assertIn("ほかの端末", str(ctx.exception))
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()

    def test_concurrent_writers_lose_nothing(self) -> None:
        errors = []

        def bump():
            try:
                for _ in range(10):
                    conn = db._begin_immediate(self.path)
                    try:
                        db.bump_revision(conn)
                        db._commit(conn)
                    finally:
                        conn.close()
            except Exception as exc:          # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=bump) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        with db.reading(self.path) as conn:
            self.assertEqual(db.revision(conn), 1 + 40)

    def test_note_edit_leaves_history_and_bumps_revision(self) -> None:
        """マスタ管理が書いたあと: 変更履歴に1行、更新番号 +1(vc-calculator の端末にも効く)。"""
        db.note_edit(self.path, "VC品種", "更新", 3, {"VC厚": 0.06}, {"VC厚": 0.07})
        with db.reading(self.path) as conn:
            self.assertEqual(db.revision(conn), 2)
            row = conn.execute("SELECT 表, 操作, 行, 変更前, 変更後 FROM 変更履歴").fetchone()
        self.assertEqual(tuple(row), ("VC品種", "更新", 3, '{"VC厚": 0.06}', '{"VC厚": 0.07}'))


class QuickGridTest(MasterCase):
    """早見表のマスをまとめて作る(vc-calculator `test_master_admin.QuickGridTest`)。"""

    def test_new_block_gets_cells_and_lengths_from_the_formula(self) -> None:
        with db.writing(self.path) as conn:
            conn.execute("INSERT INTO 早見表ブロック (表示順, 品種名, 業者名, 枠色, 有効, 計算品種)"
                         " VALUES (8, 'TF200 新', 'ミツイ', 'green', 1, 'TF200/TF200B')")
        result = grid.make_grid(self.path, "TF200 新", "87, 95", "5 8 10")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.added, 6)
        self.assertIn("6 マス足しました", result.message)
        view = quick_table.build(masters.current())
        block = next(b for b in view["blocks"] if b["name"] == "TF200 新")
        self.assertEqual(block["headers"], ["5", "8", "10"])
        self.assertEqual(block["rows"][0]["cells"],
                         [str(math.floor(vc_length(t, 0.06, 87))) for t in (5, 8, 10)])
        with db.reading(self.path) as conn:
            logged = conn.execute(
                "SELECT COUNT(*) FROM 変更履歴 WHERE 操作 = 'まとめて足す'").fetchone()[0]
            self.assertEqual(db.revision(conn), 2)          # 初期 1 → まとめて足して 2
        self.assertEqual(logged, 1)

    def test_existing_cells_are_kept(self) -> None:
        before = masters.current()
        result = grid.make_grid(self.path, "R575B", "88", "5, 6")
        self.assertTrue(result.ok)
        self.assertEqual(result.added, 1)                 # 5 はもうある
        snap = masters.current()
        r575 = next(b for b in snap.quick if b.name == "R575B")
        self.assertEqual(r575.rows[0].cells[5.0], 26)    # 紙の数値はそのまま
        self.assertIsNone(r575.rows[0].cells[6.0])       # 足したマスは空欄
        self.assertGreater(snap.revision, before.revision)
        again = grid.make_grid(self.path, "R575B", "88", "5, 6")
        self.assertTrue(again.ok)
        self.assertEqual(again.added, 0)
        self.assertIn("すでにあります", again.message)

    def test_refusals(self) -> None:
        for block, insides, thicknesses, words in (
                ("無い枠", "87", "5", "ありません"),
                ("R575B", "", "5", "1つ以上"),
                ("R575B", "87", "abc", "数でない"),
                ("R575B", "87", "0", "0 より大きく"),
                ("R575B", ",".join(str(v) for v in range(80, 92)), "5", "10 個まで")):
            with self.subTest(block=block, insides=insides, thicknesses=thicknesses):
                result = grid.make_grid(self.path, block, insides, thicknesses)
                self.assertFalse(result.ok)
                self.assertIn(words, result.message)

    # ---- 計算品種の無い枠(固定値)も、VC厚 を決めれば長さを式で入れる(v3.96.0) ----
    #
    #     計算値を当ててくれればいいのでは? マスタの早見表値を編集しないと
    #     だめですか?
    def add_fixed_block(self, name: str = "畳論マン TEST") -> None:
        with db.writing(self.path) as conn:
            conn.execute("INSERT INTO 早見表ブロック (表示順, 品種名, 業者名, 枠色, 有効)"
                         " VALUES (9, ?, '', 'navy', 1)", [name])

    def lengths(self, name: str) -> dict[tuple[float, float], object]:
        with db.reading(self.path) as conn:
            return {(r[0], r[1]): r[2] for r in conn.execute(
                "SELECT 内径, 肉厚, 長さ FROM 早見表値 WHERE 品種名 = ?", [name])}

    def test_fixed_block_gets_lengths_from_a_product(self) -> None:
        self.add_fixed_block()
        result = grid.make_grid(self.path, "畳論マン TEST", "88", "5, 20, 30",
                                length_mode=grid.LENGTH_PRODUCT,
                                product="TF200/TF200B")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.added, 3)
        got = self.lengths("畳論マン TEST")
        for t in (5, 20, 30):
            self.assertEqual(got[(88.0, float(t))], math.floor(vc_length(t, 0.06, 88)))
        self.assertIn("VC品種「TF200/TF200B」の VC厚 0.06mm", result.message)
        # 早見表にもそのまま出る(固定値の枠なので、入れた数を出す)
        view = quick_table.build(masters.current())
        block = next(b for b in view["blocks"] if b["name"] == "畳論マン TEST")
        self.assertEqual(block["rows"][0]["cells"],
                         [str(math.floor(vc_length(t, 0.06, 88))) for t in (5, 20, 30)])

    def test_fixed_block_with_a_typed_vcatu(self) -> None:
        self.add_fixed_block()
        result = grid.make_grid(self.path, "畳論マン TEST", "88", "45",
                                length_mode=grid.LENGTH_VALUE, vcatu_text="０.１")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(self.lengths("畳論マン TEST")[(88.0, 45.0)],
                         math.floor(vc_length(45, 0.1, 88)))

    def test_empty_cells_are_filled_but_typed_lengths_are_kept(self) -> None:
        """前に足して長さが空のマス(画面の「畳論マン TEST」)に、あとから入れる。"""
        self.add_fixed_block()
        grid.make_grid(self.path, "畳論マン TEST", "88", "5, 20")        # 長さは空
        with db.writing(self.path) as conn:
            conn.execute("UPDATE 早見表値 SET 長さ = 999 WHERE 品種名 = ? AND 肉厚 = 20",
                         ["畳論マン TEST"])
        result = grid.make_grid(self.path, "畳論マン TEST", "88", "5, 20",
                                length_mode=grid.LENGTH_VALUE, vcatu_text="0.1")
        self.assertTrue(result.ok, result.message)
        self.assertEqual((result.added, result.filled, result.replaced), (0, 1, 0))
        self.assertIn("長さが空だった 1 マスに長さを入れました", result.message)
        got = self.lengths("畳論マン TEST")
        self.assertEqual(got[(88.0, 5.0)], math.floor(vc_length(5, 0.1, 88)))
        self.assertEqual(got[(88.0, 20.0)], 999)                          # 入っていた値はそのまま
        # 置き換えると決めたときだけ書き換える
        again = grid.make_grid(self.path, "畳論マン TEST", "88", "20",
                               length_mode=grid.LENGTH_VALUE, vcatu_text="0.1",
                               overwrite=True)
        self.assertEqual(again.replaced, 1)
        self.assertEqual(self.lengths("畳論マン TEST")[(88.0, 20.0)],
                         math.floor(vc_length(20, 0.1, 88)))

    def test_rounding_follows_the_app_setting(self) -> None:
        self.add_fixed_block()
        with db.writing(self.path) as conn:
            conn.execute("UPDATE アプリ設定 SET 値 = '四捨五入' WHERE キー = '早見表の丸め'")
        grid.make_grid(self.path, "畳論マン TEST", "88", "30",
                       length_mode=grid.LENGTH_VALUE, vcatu_text="0.1")
        self.assertEqual(self.lengths("畳論マン TEST")[(88.0, 30.0)],
                         round(vc_length(30, 0.1, 88)))

    def test_formula_block_ignores_the_vcatu(self) -> None:
        """計算品種のある枠は長さを入れない(毎回 VC厚 から出す。2か所に持たない)。"""
        result = grid.make_grid(self.path, "R575B", "88", "7",
                                length_mode=grid.LENGTH_VALUE, vcatu_text="0.1")
        self.assertTrue(result.ok)
        with db.writing(self.path) as conn:
            conn.execute("UPDATE 早見表ブロック SET 計算品種 = 'TF200/TF200B'"
                         " WHERE 品種名 = 'R575B'")
        result = grid.make_grid(self.path, "R575B", "88", "9",
                                length_mode=grid.LENGTH_VALUE, vcatu_text="0.1")
        self.assertTrue(result.ok)
        self.assertIsNone(self.lengths("R575B")[(88.0, 9.0)])

    def test_vcatu_in_microns_is_the_same_length(self) -> None:
        """VC厚の単位はmm単位ではない ミクロンなのでは ── **μm でも入れられる**。

        マスタと式は mm のまま(0.10)。100(μm)と打っても 0.10mm と同じ長さになる。"""
        self.add_fixed_block()
        by_mm = grid.make_grid(self.path, "畳論マン TEST", "88", "5",
                               length_mode=grid.LENGTH_VALUE, vcatu_text="0.10")
        self.assertTrue(by_mm.ok, by_mm.message)
        mm_lengths = self.lengths("畳論マン TEST")
        self.assertIn("0.1mm(100μm)", by_mm.message)
        by_um = grid.make_grid(self.path, "畳論マン TEST", "88", "5",
                               length_mode=grid.LENGTH_VALUE, vcatu_text="100",
                               overwrite=True)
        self.assertTrue(by_um.ok, by_um.message)
        self.assertEqual(self.lengths("畳論マン TEST"), mm_lengths)
        for text, mm in (("100", 0.1), ("130μm", 0.13), ("80um", 0.08),
                         ("0.06mm", 0.06), ("０.１３", 0.13)):
            with self.subTest(text=text):
                self.assertAlmostEqual(grid.parse_vcatu(text)[0], mm)
                self.assertEqual(grid.parse_vcatu(text)[1], "")

    def test_bad_vcatu_is_refused_and_nothing_is_written(self) -> None:
        self.add_fixed_block()
        for mode, product, text, words in (
                (grid.LENGTH_VALUE, "", "", "VC厚(mm か μm)を入れてください"),
                (grid.LENGTH_VALUE, "", "abc", "数ではありません"),
                # 0.10mm の打ち間違いか 10μm か分からない ── 黙って10倍違う長さを書かない
                (grid.LENGTH_VALUE, "", "10", "μm なら薄すぎ"),
                (grid.LENGTH_VALUE, "", "1.2", "mm なら厚すぎ"),
                (grid.LENGTH_VALUE, "", "1500", "μm としても厚すぎます"),
                (grid.LENGTH_VALUE, "", "0", "0 より大きく"),
                (grid.LENGTH_PRODUCT, "無い品種", "", "ありません")):
            with self.subTest(text=text, product=product):
                result = grid.make_grid(self.path, "畳論マン TEST", "88", "5",
                                        length_mode=mode, product=product, vcatu_text=text)
                self.assertFalse(result.ok)
                self.assertIn(words, result.message)
        self.assertEqual(self.lengths("畳論マン TEST"), {})

    def test_blocks_list_tells_empty_cells_and_products(self) -> None:
        self.add_fixed_block()
        grid.make_grid(self.path, "畳論マン TEST", "88", "5, 20")
        blocks = {b["name"]: b for b in grid.quick_blocks(self.path)}
        self.assertEqual(blocks["畳論マン TEST"]["empty"], 2)
        self.assertEqual(blocks["畳論マン TEST"]["product"], "")
        names = [p["name"] for p in grid.products(self.path)]
        self.assertIn("TF200/TF200B", names)

    def test_full_width_and_japanese_commas(self) -> None:
        values, problem = grid.parse_numbers("５、８　１０", "肉厚", 40)
        self.assertEqual(problem, "")
        self.assertEqual(values, [5.0, 8.0, 10.0])



class BlockManageTest(MasterCase):
    """早見表の品種を1回で足す・消す・切り替える(v3.97.0)。

        1行消すがないと追加したVCが永遠に消せない(早見表を先に消せ)
        早見表値から消そうと思うと相当数消さないといけない
        早見表に追加でVCを足すのもわかりにくい
        固定値(式なし)に知らない間になっていたがそれもよくわからない
    """

    def count(self, sql: str, *args) -> int:
        with db.reading(self.path) as conn:
            return int(conn.execute(sql, args).fetchone()[0])

    def cells(self, block: str) -> dict[tuple[float, float], object]:
        with db.reading(self.path) as conn:
            return {(r[0], r[1]): r[2] for r in conn.execute(
                "SELECT 内径, 肉厚, 長さ FROM 早見表値 WHERE 品種名 = ?", [block])}

    def shown(self, block: str) -> list[list[str]]:
        view = quick_table.build(masters.current())
        found = next(b for b in view["blocks"] if b["name"] == block)
        return [r["cells"] for r in found["rows"]]

    def test_add_a_new_vc_in_one_go(self) -> None:
        """新しい VC品種・枠・マスを1回で。長さは式(VC厚 から毎回)。"""
        before = masters.current().revision
        result = grid.add_block(self.path, new_product="畳論マン", new_vcatu="０.１",
                                new_vendor="畳論", insides_text="88",
                                thicknesses_text="5, 20, 30")
        self.assertTrue(result.ok, result.message)
        self.assertIn("早見表に「畳論マン」を足しました(マス 3)", result.message)
        self.assertIn("VC品種「畳論マン」も足しました", result.message)
        snap = masters.current()
        self.assertGreater(snap.revision, before)
        product = snap.product("畳論マン")
        self.assertEqual((product.vcatu, product.inside, product.vendor), (0.1, 88.0, "畳論"))
        block = next(b for b in snap.quick if b.name == "畳論マン")
        self.assertEqual(block.product, "畳論マン")              # 式(固定値にならない)
        self.assertEqual(set(self.cells("畳論マン").values()), {None})  # 長さは持たない
        self.assertEqual(self.shown("畳論マン"),
                         [[str(math.floor(vc_length(t, 0.1, 88))) for t in (5, 20, 30)]])
        # 色は、まだ使っていない色(初期の7枠で navy〜teal は使っている → 先頭に戻る)
        listed = {b["name"]: b for b in grid.quick_blocks(self.path)}["畳論マン"]
        self.assertIn("式: VC品種「畳論マン」の VC厚 0.1mm", listed["kind"])
        self.assertEqual(listed["inside_cells"], {"88": 3})

    def test_add_fixed_from_an_existing_product(self) -> None:
        result = grid.add_block(self.path, product="TF200/TF200B", block_name="TF 紙",
                                formula=False, insides_text="87", thicknesses_text="5, 8",
                                tone="teal")
        self.assertTrue(result.ok, result.message)
        self.assertIn("固定値なので", result.message)
        self.assertEqual(self.cells("TF 紙"), {
            (87.0, 5.0): math.floor(vc_length(5, 0.06, 87)),
            (87.0, 8.0): math.floor(vc_length(8, 0.06, 87))})
        listed = {b["name"]: b for b in grid.quick_blocks(self.path)}["TF 紙"]
        self.assertEqual((listed["product"], listed["tone"]), ("", "teal"))
        self.assertIn("固定値", listed["kind"])
        self.assertIn("VC品種と結びついていない", listed["kind"])   # なぜ固定値か

    def test_add_refusals_write_nothing(self) -> None:
        products = self.count("SELECT COUNT(*) FROM VC品種")
        blocks = self.count("SELECT COUNT(*) FROM 早見表ブロック")
        for kwargs, words in (
                ({"product": "V325系"}, "枠はもうあります"),        # 名前が同じ枠がある
                ({"new_product": "V325系", "new_vcatu": "0.1"}, "はもうあります"),
                ({"new_product": "新しい"}, "VC厚(mm か μm)を入れてください"),
                ({"new_product": "新しい", "new_vcatu": "10"}, "μm なら薄すぎ"),
                ({}, "VC品種を選ぶか"),
                ({"product": "無い品種"}, "ありません"),
                ({"product": "TF200/TF200B", "block_name": "別", "tone": "pink"}, "枠の色"),
                ({"product": "TF200/TF200B", "block_name": "別", "insides_text": ""}, "1つ以上")):
            with self.subTest(kwargs=kwargs):
                args = {"insides_text": "88", "thicknesses_text": "5", **kwargs}
                result = grid.add_block(self.path, **args)
                self.assertFalse(result.ok)
                self.assertIn(words, result.message)
        self.assertEqual(self.count("SELECT COUNT(*) FROM VC品種"), products)
        self.assertEqual(self.count("SELECT COUNT(*) FROM 早見表ブロック"), blocks)

    def test_delete_a_block_with_its_cells_at_once(self) -> None:
        """前はマスタ管理で 早見表値 を1行ずつ消してからでないと枠を消せなかった。"""
        grid.add_block(self.path, new_product="畳論マン", new_vcatu="0.1",
                       insides_text="88, 95", thicknesses_text="5, 20, 30, 45, 80")
        self.assertEqual(len(self.cells("畳論マン")), 10)
        result = grid.delete_block(self.path, "畳論マン")
        self.assertTrue(result.ok, result.message)
        self.assertIn("「畳論マン」の枠とマス 10 を消しました", result.message)
        self.assertIn("VC品種「畳論マン」は残しています", result.message)
        self.assertEqual(self.cells("畳論マン"), {})
        self.assertNotIn("畳論マン", [b.name for b in masters.current().quick])
        self.assertIsNotNone(masters.current().product("畳論マン"))
        with db.reading(self.path) as conn:
            before = conn.execute("SELECT 変更前 FROM 変更履歴 WHERE 操作 = '枠を消す'").fetchone()[0]
        self.assertIn('["95", "80", null]', before)              # 消したマスは履歴に残る
        again = grid.delete_block(self.path, "畳論マン")
        self.assertFalse(again.ok)
        self.assertIn("もう消えています", again.message)

    def test_delete_a_row_or_a_column(self) -> None:
        """「1行消す」── 内径の行・肉厚の列ごと。"""
        grid.add_block(self.path, new_product="畳論マン", new_vcatu="0.1",
                       insides_text="88, 95", thicknesses_text="5, 20, 30")
        result = grid.delete_cells(self.path, "畳論マン", insides_text="95")
        self.assertTrue(result.ok, result.message)
        self.assertIn("内径 95 の行(マス 3)を消しました", result.message)
        self.assertEqual(sorted(self.cells("畳論マン")), [(88.0, 5.0), (88.0, 20.0), (88.0, 30.0)])
        result = grid.delete_cells(self.path, "畳論マン", thicknesses_text="30")
        self.assertIn("肉厚 30 の列(マス 1)を消しました", result.message)
        result = grid.delete_cells(self.path, "畳論マン", insides_text="88", thicknesses_text="5")
        self.assertIn("内径 88 × 肉厚 5", result.message)
        self.assertEqual(sorted(self.cells("畳論マン")), [(88.0, 20.0)])
        for kwargs, words in (({}, "消す内径か肉厚を入れてください"),
                              ({"insides_text": "70"}, "内径 70 の行はありません"),
                              ({"thicknesses_text": "x"}, "数でない")):
            with self.subTest(kwargs=kwargs):
                refused = grid.delete_cells(self.path, "畳論マン", **kwargs)
                self.assertFalse(refused.ok)
                self.assertIn(words, refused.message)
        last = grid.delete_cells(self.path, "畳論マン", insides_text="88")
        self.assertIn("この枠のマスは無くなりました", last.message)

    def test_switch_formula_to_fixed_keeps_what_is_shown(self) -> None:
        """式 → 固定値は、いま出ている数のまま(切り替えた瞬間に表の数が変わらない)。"""
        shown = self.shown("TF200/TF200B")
        result = grid.set_source(self.path, "TF200/TF200B", "")
        self.assertTrue(result.ok, result.message)
        self.assertIn("固定値にしました", result.message)
        self.assertEqual(self.shown("TF200/TF200B"), shown)
        block = next(b for b in masters.current().quick if b.name == "TF200/TF200B")
        self.assertEqual(block.product, "")
        # VC厚 を直しても、固定値の枠は変わらない
        self.write("UPDATE VC品種 SET VC厚 = 0.2 WHERE 品種名 = 'TF200/TF200B'")
        self.assertEqual(self.shown("TF200/TF200B"), shown)
        # 式に戻すと VC厚 0.2 で出る
        result = grid.set_source(self.path, "TF200/TF200B", "TF200/TF200B")
        self.assertTrue(result.ok, result.message)
        self.assertIn("式で出すようにしました", result.message)
        self.assertNotEqual(self.shown("TF200/TF200B"), shown)
        for product, words in (("TF200/TF200B", "もう VC品種「TF200/TF200B」の式"),
                               ("無い品種", "ありません")):
            with self.subTest(product=product):
                refused = grid.set_source(self.path, "TF200/TF200B", product)
                self.assertFalse(refused.ok)
                self.assertIn(words, refused.message)
        self.assertIn("もう固定値", grid.set_source(self.path, "R575B", "").message)

    def test_fixed_block_can_become_a_formula(self) -> None:
        """「固定値(式なし)に知らない間になっていた」── 式に切り替えられる。"""
        result = grid.set_source(self.path, "R575B", "TF200/TF200B")
        self.assertTrue(result.ok, result.message)
        self.assertIn("使わなくなります", result.message)          # 紙の数は使わない
        self.assertEqual(self.cells("R575B")[(88.0, 5.0)], 26)     # 消してはいない
        self.assertEqual(self.shown("R575B")[0][0], str(math.floor(vc_length(5, 0.06, 88))))

    def test_delete_a_product_with_everything_that_uses_it(self) -> None:
        """「追加したVCが永遠に消せない」── 枠・マス・内径の選択肢ごと1回で。"""
        listed = {p["name"]: p for p in grid.products(self.path)}["224R/AL200R"]
        self.assertEqual((listed["blocks"], listed["choices"]), (["224R/AL200R"], 2))
        cells = listed["cells"]
        self.assertGreater(cells, 0)
        others = len(self.cells("V325系"))
        result = grid.delete_product(self.path, "224R/AL200R")
        self.assertTrue(result.ok, result.message)
        self.assertIn(f"早見表の枠「224R/AL200R」(マス {cells})", result.message)
        self.assertIn("内径の選択肢 2", result.message)
        snap = masters.current()
        self.assertIsNone(snap.product("224R/AL200R"))
        self.assertNotIn("224R/AL200R", [b.name for b in snap.quick])
        self.assertEqual(self.count("SELECT COUNT(*) FROM VC内径選択肢 WHERE 品種名 = '224R/AL200R'"), 0)
        self.assertEqual(len(self.cells("V325系")), others)        # ほかは触らない
        with db.reading(self.path) as conn:
            logged = conn.execute("SELECT 変更前 FROM 変更履歴 WHERE 操作 = '品種ごと消す'").fetchone()[0]
        self.assertIn('"VC厚": 0.08', logged)

    def test_hidden_products_are_listed_so_they_can_be_deleted(self) -> None:
        self.write("UPDATE VC品種 SET 有効 = 0 WHERE 品種名 = 'V325系'")
        listed = {p["name"]: p for p in grid.products(self.path)}
        self.assertFalse(listed["V325系"]["active"])
        self.assertTrue(grid.delete_product(self.path, "V325系").ok)


if __name__ == "__main__":
    unittest.main()
