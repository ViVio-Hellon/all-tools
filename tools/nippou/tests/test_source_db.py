"""共有sqlite3を手元に写してから読む (`nippou/source_db.py`)

【なぜこれが要るのか】
現場で、参照しただけで `pending_20260811_164850.sqlite3` のようなファイルが
共有フォルダに残りました。読むだけのつもりでも、**開けばそのファイルに
手を出している**からです。

だから「共有のファイルを開かない」ことそのものが仕様になります。
ここではそれを機械で見ます ── 元のファイルが**読み取り専用でも**読めること、
写しが破れていたら読ませないこと、置き場所を変えたら写し直すこと。
"""
from __future__ import annotations

import os
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou import source_db


def _make_db(path: Path, rows=(("A", 1), ("B", 2))) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute('CREATE TABLE "資材重量" ("梱包資材名" TEXT, "単位質量" REAL)')
    conn.executemany('INSERT INTO "資材重量" VALUES (?, ?)', rows)
    conn.commit()
    conn.close()


class SourceTestCase(unittest.TestCase):
    """1件ごとに、共有役のフォルダと写しの置き場を作り直す。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.shared = self.tmp / "共有"
        self.shared.mkdir()
        self.db = self.shared / "梱包資材マスタ.sqlite3"
        _make_db(self.db)

        # 写しの置き場所をこの1件ぶんに寄せる
        self._patch = patch.object(source_db, "_COPY_DIR", self.tmp / "cache")
        self._patch.start()
        (self.tmp / "cache").mkdir()
        source_db.forget()

    def tearDown(self) -> None:
        self._patch.stop()
        source_db.forget()
        self._tmp.cleanup()


class CopyFirstTests(SourceTestCase):
    def test_写しを作ってから読む(self) -> None:
        rows = source_db.read_table(self.db, "資材重量")
        self.assertEqual([r["梱包資材名"] for r in rows], ["A", "B"])
        # **手元に写しができていること。** これが無いなら共有を開いている
        copies = list((self.tmp / "cache").glob("*.sqlite3"))
        self.assertEqual(len(copies), 1, f"写しがありません: {copies}")

    def test_写しの名前から元が分かる(self) -> None:
        copy = source_db.local_copy(self.db)
        # 調べるときに開くのは写しのほう。`3f2a1b.sqlite3` では困る
        self.assertIn("梱包資材マスタ", copy.name)

    def test_元が変わらなければ写し直さない(self) -> None:
        first = source_db.local_copy(self.db)
        stamp = first.stat().st_mtime_ns
        second = source_db.local_copy(self.db)
        self.assertEqual(first, second)
        self.assertEqual(second.stat().st_mtime_ns, stamp)

    def test_元が変われば写し直す(self) -> None:
        source_db.local_copy(self.db)
        # 大きさが変わるように行を足す(更新時刻だけだと粒度で拾えない)
        conn = sqlite3.connect(str(self.db))
        conn.executemany('INSERT INTO "資材重量" VALUES (?, ?)',
                         [(f"X{i}", i) for i in range(200)])
        conn.commit()
        conn.close()
        rows = source_db.read_table(self.db, "資材重量")
        self.assertEqual(len(rows), 202)

    def test_共有が読み取り専用でも読める(self) -> None:
        """**ここが要点。** 開くのではなく写すだけなので、書けなくてよい。

        共有フォルダの参照用マスタは読み取り専用で置かれることがある。
        `sqlite3.connect()` は読み取り専用のファイルでも `-journal` を
        作ろうとして失敗しうるが、写しは手元にあるので関係ない。
        """
        os.chmod(self.db, stat.S_IRUSR)
        try:
            rows = source_db.read_table(self.db, "資材重量")
        finally:
            os.chmod(self.db, stat.S_IRUSR | stat.S_IWUSR)
        self.assertEqual(len(rows), 2)


class NeverOpenSharedTests(SourceTestCase):
    """**読むたびに共有のファイルを開かない。**

    【`SIKALOT.pending_20260914_091528.sqlite3` が残る件】
    上流は書きかけを `SIKALOT.pending_<日時>.sqlite3` に作ってから
    `SIKALOT.sqlite3` へ置き換えます(rename)。**Windowsでは、誰かが
    開いているファイルは置き換えられません。** 置き換えが失敗すると、
    書きかけのほうが取り残されます。

    ここは以前、写しの控えを見る**前に** `looks_like_sqlite()` を
    呼んでいました。あれは中身を確かめるために共有のファイルを実際に
    開きます ── つまり写しが新しくても、読むたびに開いていました
    (GWでロットを1本引くだけで3〜4回)。上流の置き換えとぶつかる窓が、
    その回数だけありました。

    控えが効いているかは**大きさと更新時刻だけ**で分かります(`stat` は
    開きません)。開くのは、本当に写し直すときだけで足ります。
    """

    def opens(self, work) -> list[str]:
        """そのあいだに `open()` された共有のファイルを数える。"""
        seen: list[str] = []
        real = open

        def watched(file, *args, **kwargs):      # noqa: ANN001
            if str(self.shared) in str(file):
                seen.append(str(file))
            return real(file, *args, **kwargs)

        with patch("builtins.open", watched):
            work()
        return seen

    def test_2回目の読みは共有を開かない(self) -> None:
        """**ここが本体。** 1回目は写すので開きます。2回目からは0回。"""
        source_db.read_table(self.db, "資材重量")     # 写しを作る
        opened = self.opens(lambda: source_db.read_table(self.db, "資材重量"))
        self.assertEqual(opened, [], f"共有を開いています: {opened}")

    def test_何度読んでも開かない(self) -> None:
        """画面は開くたびに何度もマスタを引きます ── 窓を増やさない。"""
        source_db.local_copy(self.db)

        def many():
            for _ in range(10):
                source_db.read_table(self.db, "資材重量")

        self.assertEqual(self.opens(many), [])

    def test_元が変わったときだけ開く(self) -> None:
        """置き換えが済んだら、当然そちらを読みに行きます。"""
        source_db.local_copy(self.db)
        conn = sqlite3.connect(str(self.db))
        conn.executemany('INSERT INTO "資材重量" VALUES (?, ?)',
                         [(f"X{i}", i) for i in range(200)])
        conn.commit()
        conn.close()
        self.assertTrue(self.opens(lambda: source_db.local_copy(self.db)))


class LeftoverTests(SourceTestCase):
    """上流の書きかけ(`.pending_...`)を、マスタとして並べない。

    中身は本物ですが、**いつのものか分かりません。** 一覧に並ぶと、
    どれが本物か分からなくなります。こちらからは消しません ──
    上流のファイルなので。
    """

    def test_書きかけは一覧に出さない(self) -> None:
        leftover = self.shared / "SIKALOT.pending_20260914_091528.sqlite3"
        _make_db(leftover)
        names = [p.name for p in source_db.list_source_files(self.shared)]
        self.assertIn("梱包資材マスタ.sqlite3", names)
        self.assertNotIn(leftover.name, names)

    def test_見分けかた(self) -> None:
        self.assertTrue(source_db.is_leftover(
            Path("SIKAODR.pending_20260914_091519.sqlite3")))
        self.assertFalse(source_db.is_leftover(Path("SIKAODR.sqlite3")))
        # 「pending」が名前に入っているだけのマスタを巻き添えにしない
        self.assertFalse(source_db.is_leftover(Path("pending集計.sqlite3")))


class SidecarTests(SourceTestCase):
    """付き添い(`-wal` / `-shm` / `-journal`)の扱い。

    **本体だけ写すと、直前に書かれた分が抜けた中身を読む。** 一緒に写す。

    ここで `_do_copy` を直に見るのは、`local_copy` を通すと写した直後に
    写しを開いて確かめる(`_is_intact`)ため。SQLite は最後の接続を閉じる
    ときに `-wal` を畳んで消すので、**写せていても消えたあとになる**。
    """

    def test_付き添いも一緒に写す(self) -> None:
        Path(str(self.db) + "-wal").write_bytes(b"x" * 16)
        target = self.tmp / "cache" / "写し.sqlite3"
        source_db._do_copy(self.db, target)
        self.assertTrue(Path(str(target) + "-wal").exists())

    def test_古い付き添いは消してから写す(self) -> None:
        # 前回は `-wal` があり、今回は無い ── 残していると、本体は新しいのに
        # 付き添いだけ古い、という一番たちの悪い組み合わせになる
        target = self.tmp / "cache" / "写し.sqlite3"
        Path(str(target) + "-wal").write_bytes("古い".encode("utf-8"))
        source_db._do_copy(self.db, target)
        self.assertFalse(Path(str(target) + "-wal").exists())


class BrokenCopyTests(SourceTestCase):
    """**破れた写しは読ませない。** 黙って欠けた中身を取り込まない。"""

    def test_破れていれば写し直す(self) -> None:
        calls = []
        real = source_db._is_intact

        def flaky(path):
            calls.append(path)
            return False if len(calls) == 1 else real(path)

        with patch.object(source_db, "_is_intact", flaky):
            rows = source_db.read_table(self.db, "資材重量")
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(calls), 2, "写し直していません")

    def test_何度写しても破れていれば断る(self) -> None:
        with patch.object(source_db, "_is_intact", lambda _p: False), \
             patch.object(source_db, "COPY_RETRY_WAIT_SEC", 0):
            with self.assertRaises(source_db.SourceError) as caught:
                source_db.read_table(self.db, "資材重量")
        # **何をすればよいかまで言う**
        self.assertIn("書き込みが終わってから", str(caught.exception))

    def test_破れた写しを控えに残さない(self) -> None:
        with patch.object(source_db, "_is_intact", lambda _p: False), \
             patch.object(source_db, "COPY_RETRY_WAIT_SEC", 0):
            with self.assertRaises(source_db.SourceError):
                source_db.local_copy(self.db)
        # 控えに入っていると、次に読むとき破れたものをそのまま返す
        self.assertNotIn(str(self.db.resolve()), source_db._COPIES)


class ConcurrentReadTests(SourceTestCase):
    """同じマスタを**同時に**引く(VC長さ計算を開くと、計算と設定の2本)。

    前は写しを**その場で上書き**していたので、ほかの要求が読んでいる最中の
    写しが一度空になり、「no such table: _メタ」で落ちていた(v3.96.0)。
    """

    def grow(self) -> None:
        """元が書き換わった(大きさも変わる)。"""
        conn = sqlite3.connect(str(self.db))
        conn.executemany('INSERT INTO "資材重量" VALUES (?, ?)',
                         [(f"X{i}", i) for i in range(200)])
        conn.commit()
        conn.close()

    @unittest.skipIf(os.name == "nt", "Windows は読んでいる写しを置き換えず、読み終わるまで待つ")
    def test_読みかけの写しは最後まで前の中身(self) -> None:
        copy = source_db.local_copy(self.db)
        with source_db.open_source(self.db) as held:
            self.grow()
            self.assertEqual(source_db.local_copy(self.db), copy)   # 同じ名前へ写し直す
            # 上書きなら、ここで新しい(あるいは空の)中身が見える
            self.assertEqual(held.execute('SELECT COUNT(*) FROM "資材重量"').fetchone()[0], 2)
        rows = source_db.read_table(self.db, "資材重量")
        self.assertEqual(len(rows), 202)
        self.assertEqual([p.name for p in (self.tmp / "cache").iterdir()
                          if p.name.endswith(".part")], [])

    def test_同時に写し直しても写すのは1回(self) -> None:
        import threading
        import time

        real = source_db._do_copy
        calls = []

        def slow_copy(src, dst):
            calls.append(dst)
            time.sleep(0.2)                  # 写している途中にもう1本来る
            real(src, dst)

        got, errors = [], []

        def read() -> None:
            try:
                got.append(len(source_db.read_table(self.db, "資材重量")))
            except Exception as exc:         # noqa: BLE001 - 落ちたことを数える
                errors.append(exc)

        with patch.object(source_db, "_do_copy", slow_copy):
            threads = [threading.Thread(target=read) for _ in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(got, [2, 2, 2])
        self.assertEqual(len(calls), 1, calls)

    def test_置き換えられなければ少し待つ(self) -> None:
        """Windows: ほかの要求がまだ前の写しを開いていると置き換えが断られる。"""
        source_db.local_copy(self.db)
        self.grow()
        real = os.replace
        refused = []

        def busy_then_ok(src, dst):
            if str(src).endswith(".part") and len(refused) < 2:
                refused.append(dst)
                raise PermissionError("使用中")
            return real(src, dst)

        with patch.object(source_db.os, "replace", busy_then_ok), \
             patch.object(source_db, "SWAP_RETRY_WAIT_SEC", 0):
            rows = source_db.read_table(self.db, "資材重量")
        self.assertEqual(len(rows), 202)
        self.assertEqual(len(refused), 2)

    def test_ずっと使用中なら断り_写しを残さない(self) -> None:
        source_db.local_copy(self.db)
        self.grow()

        def always_busy(src, dst):
            raise PermissionError("使用中")

        with patch.object(source_db.os, "replace", always_busy), \
             patch.object(source_db, "SWAP_RETRY_WAIT_SEC", 0):
            with self.assertRaises(source_db.SourceError) as caught:
                source_db.local_copy(self.db)
        self.assertIn("もう一度お試しください", str(caught.exception))
        self.assertEqual([p.name for p in (self.tmp / "cache").iterdir()
                          if p.name.endswith(".part")], [])


class RefusalTests(SourceTestCase):
    def test_無いファイルは理由を言う(self) -> None:
        with self.assertRaises(source_db.SourceError) as caught:
            source_db.read_table(self.shared / "無い.sqlite3", "資材重量")
        self.assertIn("見つかりません", str(caught.exception))

    def test_中身が別物なら写さない(self) -> None:
        """名前を変えただけの Access ファイル等。**共有を無駄に往復させない。**"""
        fake = self.shared / "にせもの.sqlite3"
        fake.write_bytes(b"Standard Jet DB\x00" + b"\x00" * 64)
        with self.assertRaises(source_db.SourceError) as caught:
            source_db.local_copy(fake)
        self.assertIn("sqlite3 のファイルではありません", str(caught.exception))
        self.assertEqual(list((self.tmp / "cache").glob("にせもの*")), [])

    def test_書けない形で開く(self) -> None:
        """写しを書き換えても誰にも届かない。**書けること自体が間違いのもと。**"""
        with source_db.open_source(self.db) as conn:
            with self.assertRaises(sqlite3.Error):
                conn.execute('DELETE FROM "資材重量"')

    def test_一覧は例外にしない(self) -> None:
        # 画面を組み立てる途中で落ちない。読めなければ空
        self.assertEqual(source_db.list_tables(self.shared / "無い.sqlite3"), [])
        self.assertEqual(source_db.table_counts(self.shared / "無い.sqlite3"), {})


class ReadTests(SourceTestCase):
    def test_テーブル一覧(self) -> None:
        self.assertEqual(source_db.list_tables(self.db), ["資材重量"])

    def test_列名(self) -> None:
        self.assertEqual(source_db.columns(self.db, "資材重量"),
                         ["梱包資材名", "単位質量"])

    def test_行数(self) -> None:
        self.assertEqual(source_db.table_counts(self.db), {"資材重量": 2})

    def test_絞り込んで引ける(self) -> None:
        rows = source_db.read_query(
            self.db, 'SELECT * FROM "資材重量" WHERE "梱包資材名" = ?', ("B",))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["単位質量"], 2)


class ProbeTests(SourceTestCase):
    def test_読めるファイルの診断(self) -> None:
        out = source_db.probe(self.db)
        self.assertTrue(out.ok)
        self.assertTrue(out.is_sqlite)
        self.assertEqual(out.tables, ["資材重量"])
        # **写しの場所も出す。** 「どれを読んでいるのか」を画面から辿れる
        self.assertTrue(out.copied_to)

    def test_無いファイルの診断は例外にしない(self) -> None:
        out = source_db.probe(self.shared / "無い.sqlite3")
        self.assertFalse(out.ok)
        self.assertFalse(out.exists)
        self.assertEqual(out.error, "ファイルがありません")

    def test_中身が別物のときの診断(self) -> None:
        fake = self.shared / "にせもの.sqlite3"
        fake.write_bytes(b"not a database at all")
        out = source_db.probe(fake)
        self.assertFalse(out.ok)
        # **どこまで届いたか**が分かる: ファイルはあるが印が無い
        self.assertTrue(out.exists)
        self.assertFalse(out.is_sqlite)


class FindTests(SourceTestCase):
    def test_拡張子違いも拾う(self) -> None:
        other = self.shared / "SIKALOT.db"
        _make_db(other)
        self.assertEqual(source_db.find(self.shared, "SIKALOT.sqlite3"), other)

    def test_Accessの名前でもsqlite3を見つける(self) -> None:
        """上流が `.accdb` から移った日に、設定を触らせない。"""
        self.assertEqual(source_db.find(self.shared, "梱包資材マスタ.accdb"),
                         self.db)

    def test_一覧に出す(self) -> None:
        self.assertEqual([p.name for p in source_db.list_source_files(self.shared)],
                         ["梱包資材マスタ.sqlite3"])

    def test_届かないフォルダでも落ちない(self) -> None:
        self.assertEqual(source_db.list_source_files(self.tmp / "無い"), [])
        self.assertIsNone(source_db.find(self.tmp / "無い", "x.sqlite3"))


class NoUriTests(SourceTestCase):
    """**URI では開きません。** 開くのは手元の写しだけ。

    ここには `to_uri()`(共有フォルダ UNC を `file:////…` に直す関数)の
    試験がありました。その関数は**一度も呼ばれていません** ── 書いた回に
    同時に「読むときは写しを開く」と決めたので、URI で共有を開く道が
    その場で無くなっています。

    使われない関数に試験を付けておくと、**そこを通っているつもり**に
    なります。関数ごと消して、代わりに「通らないこと」を見ます。
    """

    def test_URIを組み立てる関数は持たない(self) -> None:
        self.assertFalse(hasattr(source_db, "to_uri"))

    def test_開くのは写しだけ(self) -> None:
        """共有の道が `sqlite3.connect` に渡らないこと。

        渡っていたら、それは写さずに開いています ── `pending_...` が
        残る道が戻ってきた、ということです。
        """
        opened: list[str] = []
        real = sqlite3.connect

        def watched(target, *args, **kwargs):     # noqa: ANN001
            opened.append(str(target))
            return real(target, *args, **kwargs)

        with patch.object(source_db.sqlite3, "connect", watched):
            source_db.read_table(self.db, "資材重量")
        self.assertTrue(opened, "1度も開いていません")
        for target in opened:
            with self.subTest(target=target):
                self.assertNotIn(str(self.shared), target)
                self.assertIn(str(self.tmp / "cache"), target)


class IdentifierTests(unittest.TestCase):
    def test_二重引用符を畳む(self) -> None:
        # 日本語のテーブル名がそのまま出てくるので囲みは必須
        self.assertEqual(source_db.quote_identifier('a"b'), '"a""b"')


if __name__ == "__main__":
    unittest.main()
