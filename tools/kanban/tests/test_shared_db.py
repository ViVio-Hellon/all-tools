"""共有 SQLite(正式なデータの置き場所)のテスト。

**Access をやめてここに来た。** 以前は ACE OLEDB や ODBC の有無で動いたり
動かなかったりし、書き戻しは Windows でしか試せませんでした。いまは共有側も
sqlite3 なので、**書き戻しまで含めてどの端末でも丸ごと検証できます** ──
このファイルが存在できること自体が移行の成果です。
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from kanban.accdb.types import CATEGORY_DATE, CATEGORY_NUMBER, CATEGORY_TEXT
from kanban.db.shared import SharedDb, SharedDbError, build_update


class SharedDbTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="kanban_shared_"))
        self.path = self.dir / "看板マスタ.sqlite3"
        conn = sqlite3.connect(str(self.path))
        conn.execute(
            "CREATE TABLE [看板_LVC] ("
            "[管理番号] NUMERIC, [資材] TEXT, [サイズ] TEXT, "
            "[欲] TEXT, [発送] TEXT, [更新日] DATETIME)"
        )
        conn.executemany(
            "INSERT INTO [看板_LVC] VALUES (?, ?, ?, ?, ?, ?)",
            [
                (1, "外装紙", "2200", "", "", ""),
                (2, "外装紙", "1900", "〇", "", "2026/07/14 09:05:00"),
            ],
        )
        conn.commit()
        conn.close()
        # 写しの置き場はテスト用に分ける(ローカル領域を汚さない)
        self.cache = self.dir / "cache"
        self.db = SharedDb(str(self.path), cache_dir=str(self.cache))

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def rows(self) -> list[dict]:
        return self.db.read_table("看板_LVC").rows


class ReadTest(SharedDbTestBase):
    def test_lists_tables(self):
        self.assertEqual(self.db.table_names(), ["看板_LVC"])

    def test_reads_rows(self):
        snapshot = self.db.read_table("看板_LVC")
        self.assertEqual(len(snapshot.rows), 2)
        self.assertEqual(snapshot.rows[0]["資材"], "外装紙")

    def test_key_column_is_first(self):
        self.assertEqual(self.db.read_table("看板_LVC").key_column(), "管理番号")

    def test_categories_come_from_declared_types(self):
        """宣言型から型カテゴリを復元する。

        変換ツールが元の Access の型に合わせて宣言しているので、ここで
        読み戻せないと日時が文字列扱いになり、書き戻しの型がずれる。
        """
        cats = self.db.read_table("看板_LVC").categories
        self.assertEqual(cats["管理番号"], CATEGORY_NUMBER)
        self.assertEqual(cats["更新日"], CATEGORY_DATE)
        self.assertEqual(cats["資材"], CATEGORY_TEXT)

    def test_missing_file_says_what_to_check(self):
        db = SharedDb(str(self.dir / "ない.sqlite3"))
        with self.assertRaises(SharedDbError) as caught:
            db.table_names()
        self.assertIn("共有フォルダ", str(caught.exception))

    def test_unknown_table_is_reported(self):
        with self.assertRaises(SharedDbError):
            self.db.read_table("看板_架空")

    def test_reading_cannot_write(self):
        """読むために開いた接続では書けない。

        写しへ書いても誰にも届かない。書けてしまうと「直したのに反映され
        ない」という一番たちの悪い壊れ方になる。
        """
        conn = self.db._connect(read_only=True)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("UPDATE [看板_LVC] SET [欲] = '×'")
        finally:
            conn.close()
        self.assertEqual(self.rows()[0]["欲"], "")


class LocalCopyTest(SharedDbTestBase):
    """**読むときは共有ファイルを直接開かない。**

    他端末が開いている最中の sqlite3 を読みに行くと、共有フォルダ側に
    一時ファイルが残ることがある(現場では ``pending_<日時>.sqlite3`` が
    増えていく形で現れた)。写してから読めば、共有に対してはバイト列を
    コピーするだけになる。
    """

    def shared_dir_files(self) -> set[str]:
        return {p.name for p in self.dir.iterdir() if p.is_file()}

    def test_read_makes_a_local_copy(self):
        self.db.table_names()
        copies = list(self.cache.glob("shared_*"))
        self.assertTrue(copies, "写しが作られていない")

    def test_read_leaves_no_new_file_beside_the_shared_db(self):
        """共有フォルダに余計なファイルを残さない ── これが今回の目的。"""
        before = self.shared_dir_files()
        self.db.table_names()
        self.db.read_table("看板_LVC")
        self.db.read_table("看板_LVC")
        self.assertEqual(self.shared_dir_files(), before)

    def test_copy_is_reused_while_unchanged(self):
        """取り込みはテーブルごとに開き直す。毎回写すと共有への往復で待たされる。"""
        first = self.db.local_copy()
        stamp = first.stat().st_mtime_ns
        self.db.read_table("看板_LVC")
        self.db.read_table("看板_LVC")
        self.assertEqual(self.db.local_copy(), first)
        self.assertEqual(first.stat().st_mtime_ns, stamp, "写し直されている")

    def test_copy_count_is_visible(self):
        """**共有フォルダへの往復が見えること。**

        読むたびに丸ごと写す作りなので、費用は「大きさ × 回数」。
        見えないと、心拍や書き戻しの間隔を調整のしようがない。
        """
        self.assertEqual(self.db.copies_made, 0)
        self.db.read_table("看板_LVC")
        self.assertEqual(self.db.copies_made, 1)

        # 変わっていなければ、何度読んでも増えない
        self.db.read_table("看板_LVC")
        self.db.read_table("看板_LVC")
        self.assertEqual(self.db.copies_made, 1, "変わっていないのに写している")

        # 誰かが書いたら、次に読むときに写し直す
        conn = sqlite3.connect(str(self.path))
        conn.execute("UPDATE [看板_LVC] SET [欲] = '〇' WHERE [管理番号] = 1")
        conn.commit()
        conn.close()
        self.db.read_table("看板_LVC")
        self.assertEqual(self.db.copies_made, 2)

    def test_copy_refreshes_when_shared_db_changes(self):
        """他端末が書き戻した内容は、次に読むときに見えなければならない。"""
        self.db.read_table("看板_LVC")
        conn = sqlite3.connect(str(self.path))
        conn.execute("UPDATE [看板_LVC] SET [欲] = '〇' WHERE [管理番号] = 1")
        conn.commit()
        conn.close()
        self.assertEqual(self.rows()[0]["欲"], "〇")

    def test_copy_includes_sidecars(self):
        """``-wal`` を置いていくと、直前に書かれた分が抜けた中身を読む。

        写す処理そのものを見る ── 写したあと開いて確かめる過程で、SQLite が
        不要になった ``-wal`` を片付けることがあるため(それ自体は正しい
        振る舞いなので、写し終わった時点で確かめる)。
        """
        side = Path(str(self.path) + "-wal")
        side.write_bytes(b"dummy")
        try:
            target = self.cache / "copy.sqlite3"
            self.cache.mkdir(parents=True, exist_ok=True)
            self.db._copy_files(self.path, target)
            self.assertTrue(
                Path(str(target) + "-wal").exists(), "付属ファイルが写されていない"
            )
        finally:
            side.unlink(missing_ok=True)

    def test_stale_sidecar_is_removed_from_the_copy(self):
        """前回の付属ファイルが残っていると、古い変更が混ざって見える。"""
        self.cache.mkdir(parents=True, exist_ok=True)
        target = self.cache / "copy.sqlite3"
        Path(str(target) + "-wal").write_bytes(b"old")
        # 共有側に ``-wal`` が無い状態で写す
        self.db._copy_files(self.path, target)
        self.assertFalse(Path(str(target) + "-wal").exists())

    def test_writes_go_to_the_shared_file_not_the_copy(self):
        """**書き戻しは写しへ向けない。** 写しへ書いても誰にも届かない。"""
        self.db.read_table("看板_LVC")  # 先に写しを作らせる
        self.db.execute([build_update("看板_LVC", {"欲": "〇"}, "管理番号", 1)])
        conn = sqlite3.connect(str(self.path))
        try:
            value = conn.execute(
                "SELECT [欲] FROM [看板_LVC] WHERE [管理番号] = 1"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(value, "〇", "共有ファイルに書かれていない")

    def test_broken_copy_is_reported(self):
        """写しが sqlite3 として読めなければ、黙って読まずに理由を返す。"""
        db = SharedDb(str(self.dir / "こわれ.sqlite3"), cache_dir=str(self.cache))
        (self.dir / "こわれ.sqlite3").write_text(
            "これは sqlite3 ではない", encoding="utf-8"
        )
        with self.assertRaises(SharedDbError) as caught:
            db.table_names()
        self.assertIn("書き込み中", str(caught.exception))

    def test_different_paths_do_not_share_a_copy(self):
        other = self.dir / "別.sqlite3"
        shutil.copyfile(self.path, other)
        db2 = SharedDb(str(other), cache_dir=str(self.cache))
        self.assertNotEqual(self.db.local_copy(), db2.local_copy())


class ConcurrentCopyTest(SharedDbTestBase):
    """**同じ共有 DB を同時に読んでも、書きかけの写しを読まない。**

    設定画面はアクセス権限・マスタ・状態を一度に取りに来る(要求は 8 本まで同時に
    走る)。写しの名前が 1 つだったころは、読んでいる最中の写しへ別の要求が写し直し、
    読む側が「アクセス権限の表に行が無い」と読んで倉庫モードを断った。
    """

    def test_many_readers_with_forced_recopies_always_see_every_row(self):
        import threading

        results: list[int] = []
        errors: list[str] = []

        def work(n: int) -> None:
            for i in range(25):
                db = SharedDb(str(self.path), cache_dir=str(self.cache))
                if (n + i) % 3 == 0:
                    db.forget_copy()   # 「いま取り込む」と同じ: 必ず写し直す
                try:
                    results.append(len(db.read_table("看板_LVC").rows))
                except Exception as exc:  # noqa: BLE001
                    errors.append(str(exc))

        threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        self.assertEqual(set(results), {2})
        self.assertEqual(len(results), 200)

    def test_waiting_reader_uses_the_copy_another_one_just_made(self):
        first = SharedDb(str(self.path), cache_dir=str(self.cache)).local_copy()
        second = SharedDb(str(self.path), cache_dir=str(self.cache)).local_copy()
        self.assertEqual(first, second, "中身が同じなら、ほかの接続が作った写しを使う")
        forced = SharedDb(str(self.path), cache_dir=str(self.cache))
        forced.forget_copy()
        self.assertNotEqual(forced.local_copy(), first, "写し直すときは新しい名前で作る(読んでいる写しを上書きしない)")
        self.assertTrue(first.exists(), "古い写しはすぐには消さない(読んでいる最中かもしれない)")


class StaleAttributesTest(SharedDbTestBase):
    """**書いた直後に取り込んでも、書く前の写しを使わないこと。**

    写し直すかどうかは「大きさと更新時刻」で決めている。Windows の共有
    フォルダは、この 2 つを数秒ぶん覚えて返すので、書いた直後は「変わって
    いない」と読みうる ── マスタで看板を足したのに看板画面に出ない、の一因。
    ここでは大きさと更新時刻を書く前と同じに戻して、その状況を作る。
    """

    def write_keeping_attributes(self) -> None:
        import os

        before = self.path.stat()
        conn = sqlite3.connect(str(self.path))
        conn.execute("UPDATE [看板_LVC] SET [資材] = '直した' WHERE [管理番号] = 1")
        conn.commit()
        conn.close()
        self.assertEqual(self.path.stat().st_size, before.st_size)
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))

    def materials(self) -> list[str]:
        return [r["資材"] for r in self.db.read_table("看板_LVC").rows]

    def test_the_stale_copy_is_what_you_get_without_forgetting(self):
        self.assertIn("外装紙", self.materials())
        self.write_keeping_attributes()
        self.assertNotIn("直した", self.materials(), "前提が崩れた(写し直している)")

    def test_forget_copy_makes_the_next_read_fresh(self):
        self.materials()
        self.write_keeping_attributes()
        self.db.forget_copy()
        self.assertIn("直した", self.materials())


class ForgetWhileCopyingTest(StaleAttributesTest):
    """**「写し直せ」は、写している最中に言われても・別の接続が言っても消えない。**

    以前は接続ごとの印(_recopy)を写し終えたところで下ろしていたので、取り込みが写している
    最中にマスタで看板を足して「写し直せ」と言うと、その言葉が消え、共有フォルダが大きさ・
    時刻を覚えて返すあいだ古い写しを使い続けた。マスタの画面は要求ごとに別の接続を作るので、
    取り込みの接続には最初から届いていなかった。
    """

    def test_写している最中に言われたら_次は写し直す(self):
        other = SharedDb(str(self.path), cache_dir=str(self.cache))    # マスタの画面の接続
        real = self.db._copy_files
        done = []

        def copy_then_edit(source, target):
            real(source, target)                  # 書く前の中身を写し終えた
            if not done:
                done.append(1)
                self.write_keeping_attributes()   # その直後にマスタで直して
                other.forget_copy()               # 「写し直せ」と言う

        self.db._copy_files = copy_then_edit
        self.assertNotIn("直した", self.materials())     # 写していた最中の 1 回は古くてよい
        self.assertIn("直した", self.materials(), "写している最中の「写し直せ」が消えた")

    def test_別の接続が言っても写し直す(self):
        self.materials()
        self.write_keeping_attributes()
        SharedDb(str(self.path), cache_dir=str(self.cache)).forget_copy()
        self.assertIn("直した", self.materials(), "別の接続の「写し直せ」が届かない")


class WriteTest(SharedDbTestBase):
    def test_update_applies(self):
        stmt = build_update("看板_LVC", {"欲": "〇", "発送": ""}, "管理番号", 1)
        results = self.db.execute([stmt])
        self.assertTrue(results[0].ok)
        self.assertEqual(results[0].affected, 1)
        self.assertEqual(self.rows()[0]["欲"], "〇")

    def test_values_are_not_embedded_in_sql(self):
        """**値は文へ埋め込まない。** Access 方言のリテラル生成が不要になった。

        引用符を含む値でも壊れないことで、エスケープ漏れという種類の不具合が
        構造的に起きないことを示す。
        """
        sql, params = build_update(
            "看板_LVC", {"資材": "外装紙'A\"B"}, "管理番号", 1
        )
        self.assertNotIn("外装紙", sql)
        self.assertIn("?", sql)
        self.db.execute([(sql, params)])
        self.assertEqual(self.rows()[0]["資材"], "外装紙'A\"B")

    def test_missing_row_reports_zero_affected(self):
        """行が無ければ 0 件。**エラーにはしない** ── 呼び出し側が
        「Access 側に該当行なし」として要確認に回す。"""
        results = self.db.execute(
            [build_update("看板_LVC", {"欲": "〇"}, "管理番号", 999)]
        )
        self.assertTrue(results[0].ok)
        self.assertEqual(results[0].affected, 0)

    def test_each_statement_is_independent(self):
        """1 文が失敗しても、他の文は取り消されない。

        まとめて巻き戻すと、実際には届いていない更新を「成功」と誤認して
        ``dirty`` を落とし、静かにデータを取りこぼす。
        """
        good = build_update("看板_LVC", {"欲": "〇"}, "管理番号", 1)
        bad = ("UPDATE [看板_LVC] SET [ない列] = ?", ["x"])
        results = self.db.execute([good, bad, good])
        self.assertTrue(results[0].ok)
        self.assertFalse(results[1].ok)
        self.assertTrue(results[2].ok)
        # 1 文目は取り消されていない
        self.assertEqual(self.rows()[0]["欲"], "〇")

    def test_empty_statements(self):
        self.assertEqual(self.db.execute([]), [])

    def test_build_update_requires_columns(self):
        with self.assertRaises(ValueError):
            build_update("看板_LVC", {}, "管理番号", 1)

    def test_identifier_with_bracket_is_escaped(self):
        sql, _ = build_update("看板_LVC", {"a]b": "x"}, "管理番号", 1)
        self.assertIn("[a]]b]", sql)


class SharedFolderSafetyTest(SharedDbTestBase):
    """共有フォルダの上で気をつけていること。"""

    def test_does_not_switch_to_wal(self):
        """**WAL にしない。**

        WAL は共有メモリ(``-shm``)を使うが、SMB にはそれが無い。共有
        フォルダに置いた WAL のファイルは読むだけでも開けないことがある
        (参照リポジトリが現場で踏んだ)。
        """
        self.db.execute([build_update("看板_LVC", {"欲": "〇"}, "管理番号", 1)])
        conn = sqlite3.connect(str(self.path))
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        self.assertNotEqual(mode.lower(), "wal")
        self.assertFalse((self.dir / "看板マスタ.sqlite3-wal").exists())

    def test_can_write_reflects_reachability(self):
        """Access の頃は ACE の有無で決まっていた。いまは届くかどうかだけ。"""
        self.assertTrue(self.db.can_write)
        self.assertFalse(SharedDb(str(self.dir / "ない.sqlite3")).can_write)

    def test_mode_has_no_branch(self):
        """接続経路の分岐(ado / odbc / file)が無くなった。"""
        self.assertEqual(self.db.mode, "sqlite")


class WritePathTest(unittest.TestCase):
    """**書くときは共有フォルダのパスをそのまま開く。**

    以前は ``file:`` の URI に組み直していた。``\\\\nlmfangyshrd\\各課共有\\…`` は
    ``file://nlmfangyshrd/…`` になり、SQLite はホスト名の部分を**ネットワークに
    触る前に**断る(``invalid uri authority``)。読むほうは手元へ写してから
    開くので動いており、**書くほうだけが共有フォルダで全滅**していた
    ── マスタに行を足すと 500、書き戻しは失敗し続けて「未反映」が減らない。

    以前の試験は「URI の形がそれらしいか」を見ていて、SQLite が本当に開けるか
    は確かめていなかったので、これを通してしまった。
    """

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="kanban_write_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_share_path_reaches_sqlite_unchanged(self):
        """共有フォルダのパスが、組み直されずに SQLite へ渡ること。

        ここ(Linux)では共有フォルダを開けないので、**何を渡したか**を見る。
        """
        unc = r"\\nlmfangyshrd\各課共有\0130_日軽稲沢\梱包課\AIM\【■】_参照用ファイル\看板マスタ.sqlite3"
        db = SharedDb(unc)
        seen = {}

        class Stop(Exception):
            pass

        def fake_connect(database, *args, **kwargs):
            seen["database"] = database
            seen["uri"] = kwargs.get("uri", False)
            raise Stop

        with mock.patch.object(db, "_require_exists"), \
                mock.patch("kanban.db.shared.sqlite3.connect", fake_connect):
            with self.assertRaises(Stop):
                db._connect(read_only=False)

        self.assertEqual(seen["database"], unc, "パスを組み直している")
        self.assertFalse(seen["uri"], "URI として開いている(ホスト名で断られる)")

    def test_the_old_uri_form_is_what_sqlite_refuses(self):
        """直した理由そのものを残す: ホスト名付きの URI は SQLite が断る。"""
        with self.assertRaises(sqlite3.OperationalError) as caught:
            sqlite3.connect("file://nlmfangyshrd/share/k.sqlite3", uri=True)
        self.assertIn("authority", str(caught.exception))

    def test_writes_work_with_awkward_characters_in_the_path(self):
        """実際の置き場所に近い、記号・空白・全角の混じったパスで書けること。"""
        folder = self.dir / "Python + DB + HTML" / "【■】_参照用ファイル" / "#1 100%"
        folder.mkdir(parents=True)
        path = folder / "看板マスタ.sqlite3"
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE [看板_AIM] ([管理番号] NUMERIC, [資材] TEXT)")
        conn.commit()
        conn.close()

        db = SharedDb(str(path), cache_dir=str(self.dir / "cache"))
        results = db.execute([("INSERT INTO [看板_AIM] VALUES (?, ?)", [5, "中性紙"])])
        self.assertTrue(results[0].ok, results[0].error_message)
        rows = sqlite3.connect(str(path)).execute("SELECT * FROM [看板_AIM]").fetchall()
        self.assertEqual(rows, [(5, "中性紙")])


if __name__ == "__main__":
    unittest.main()
