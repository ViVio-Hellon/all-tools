"""取り込み元が sqlite3 のとき (`access_bridge/importer.py`)

参照用マスタは Access から sqlite3 へ、**ファイル単位で**移っていく。
呼び出し側(GW計算・人員・全停)が「いまどちらか」を気にせずに済むよう、
振り分けは `import_table` の1か所にある。

【ここで見ること】
sqlite3 経路が Access 経路と**同じ形**を返すこと。呼び出し側は
`row.get("製造板厚")` を `float()` に掛け、`.strip()` する ── Access 経路が
CSV由来で全部文字列だから。揃っていないと、上流が移った日に
`AttributeError: 'float' object has no attribute 'strip'` が画面の
あちこちで出る。
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou import source_db
from nippou.access_bridge import importer


class SqliteImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db = self.tmp / "SIKALOT.sqlite3"
        conn = sqlite3.connect(str(self.db))
        conn.execute('CREATE TABLE "仕掛" ("ﾛｯﾄ番号" TEXT, "製造板厚" REAL,'
                     ' "用途ｺｰﾄﾞ" REAL, "取引先名" TEXT, "備考")')
        conn.executemany('INSERT INTO "仕掛" VALUES (?, ?, ?, ?, ?)', [
            ("L001", 3.5, 12.0, "得意先A", None),
            ("L002", 4.0, 7.0, "得意先B", "覚え書き"),
        ])
        conn.commit()
        conn.close()

        self._patch = patch.object(source_db, "_COPY_DIR", self.tmp / "cache")
        self._patch.start()
        (self.tmp / "cache").mkdir()
        source_db.forget()

    def tearDown(self) -> None:
        self._patch.stop()
        source_db.forget()
        self._tmp.cleanup()

    def test_sqlite3の道はsqlite3として読む(self) -> None:
        result = importer.import_table(self.db, "仕掛")
        self.assertTrue(result.success)
        self.assertEqual(len(result.rows), 2)

    def test_値は全部文字列で返す(self) -> None:
        """Access 経路(CSV由来)と同じ形。**呼び出し側を変えない。**"""
        row = importer.import_table(self.db, "仕掛").rows[0]
        self.assertTrue(all(isinstance(v, str) for v in row.values()),
                        f"文字列でない値があります: {row}")

    def test_整数として入っている小数は整数の字にする(self) -> None:
        """`用途ｺｰﾄﾞ` は数字だが**文字として突き合わせる**列。

        VBAが書いた整数が sqlite3 では REAL で入っていることがあり、
        そのまま文字にすると `12.0` になって照合が外れる。
        """
        row = importer.import_table(self.db, "仕掛").rows[0]
        self.assertEqual(row["用途ｺｰﾄﾞ"], "12")
        # 本当の小数は小数のまま
        self.assertEqual(row["製造板厚"], "3.5")

    def test_NULLは空文字にする(self) -> None:
        # 呼び出し側は `.strip()` する。None だと落ちる
        row = importer.import_table(self.db, "仕掛").rows[0]
        self.assertEqual(row["備考"], "")

    def test_Accessの絞り込みがそのまま通る(self) -> None:
        """`[ﾛｯﾄ番号]='L002'` ── 角括弧を二重引用符に直せば通る。"""
        result = importer.import_table(self.db, "仕掛",
                                       sql_filter="[ﾛｯﾄ番号]='L002'")
        self.assertTrue(result.success)
        self.assertEqual([r["ﾛｯﾄ番号"] for r in result.rows], ["L002"])

    def test_値の中の角括弧は置き換えない(self) -> None:
        """`'...'` の中の `[` は列名ではない。"""
        self.assertEqual(importer._to_sqlite_filter("[a]='x[1]y'"),
                         '"a"=\'x[1]y\'')

    def test_読めなくても例外にしない(self) -> None:
        """取り込めないことと、アプリが落ちることは別。"""
        result = importer.import_table(self.tmp / "無い.sqlite3", "仕掛")
        self.assertFalse(result.success)
        self.assertIsNotNone(result.error)

    def test_無い表でも例外にしない(self) -> None:
        result = importer.import_table(self.db, "そんな表は無い")
        self.assertFalse(result.success)

    def test_共有を開かず写しから読む(self) -> None:
        importer.import_table(self.db, "仕掛")
        self.assertTrue(list((self.tmp / "cache").glob("*.sqlite3")),
                        "写しがありません(共有を直接開いています)")

    def test_LotInfoが組み立てられる(self) -> None:
        """**上流が移っても GW計算はそのまま動く**ことの確認。"""
        from nippou.access_bridge import gw_master

        found = gw_master.search_lot("L001", master_path=self.db)
        self.assertIsNotNone(found)
        self.assertEqual(found.lot_no, "L001")
        self.assertEqual(found.thickness_mm, 3.5)
        self.assertEqual(found.customer, "得意先A")


class DispatchTests(unittest.TestCase):
    """振り分けは道の拡張子で決まる。"""

    def test_accdbはAccess経路へ行く(self) -> None:
        with patch.object(importer, "_import_sqlite") as fake:
            importer.import_table(Path("/どこか/日報管理.accdb"), "時間用")
        fake.assert_not_called()

    def test_dbもsqlite3として扱う(self) -> None:
        with patch.object(importer, "_import_sqlite") as fake:
            importer.import_table(Path("/どこか/x.db"), "仕掛")
        fake.assert_called_once()


class PickSourceTests(unittest.TestCase):
    """同じフォルダに両方あるあいだは sqlite3 を採る(`config._pick_source`)。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_sqlite3があればそちら(self) -> None:
        from nippou.config import _pick_source

        (self.tmp / "梱包資材マスタ.sqlite3").write_bytes(b"x")
        (self.tmp / "梱包資材マスタ.accdb").write_bytes(b"x")
        self.assertEqual(_pick_source(self.tmp, "梱包資材マスタ.accdb").suffix,
                         ".sqlite3")

    def test_無ければ渡された名前のまま(self) -> None:
        from nippou.config import _pick_source

        # **見つからなくても道を返す。** 「ありません」は読む側が言うほうが、
        # どのファイルを探して無かったのかまで伝わる
        self.assertEqual(_pick_source(self.tmp, "梱包資材マスタ.accdb").name,
                         "梱包資材マスタ.accdb")

    def test_届かないフォルダでも落ちない(self) -> None:
        from nippou.config import _pick_source

        self.assertEqual(_pick_source(self.tmp / "無い", "x.accdb").name, "x.accdb")


class OlderNameTests(unittest.TestCase):
    """参照DBの旧名(``…NOW``)しか無いフォルダでも読める。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_旧名しか無ければ旧名を読む(self) -> None:
        from nippou.config import _pick_source

        # 上流の書き出しが新しい名前になる前に配っても止まらない
        (self.tmp / "SIKALOTNOW.sqlite3").write_bytes(b"x")
        self.assertEqual(_pick_source(self.tmp, "SIKALOT.sqlite3").name,
                         "SIKALOTNOW.sqlite3")

    def test_両方あれば新しい名前(self) -> None:
        from nippou.config import _pick_source

        (self.tmp / "SIKAHIKI.sqlite3").write_bytes(b"x")
        (self.tmp / "SIKAHIKINOW.sqlite3").write_bytes(b"x")
        self.assertEqual(_pick_source(self.tmp, "SIKAHIKI.sqlite3").name,
                         "SIKAHIKI.sqlite3")

    def test_どちらも無ければ新しい名前で言う(self) -> None:
        from nippou.config import _pick_source

        # 「ありません」の文言に出るのは設定されている名前のほう
        self.assertEqual(_pick_source(self.tmp, "SIKAODR.sqlite3").name,
                         "SIKAODR.sqlite3")

    def test_旧名の橋にあるもの(self) -> None:
        from nippou.config import OLDER_NAMES

        # VC計算マスタ … vc-calculator の `vc_master` を同じフォルダで読む(共有できる)
        self.assertEqual(set(OLDER_NAMES),
                         {"SIKALOT", "SIKAODR", "SIKAHIKI", "日報データ", "VC計算マスタ"})

    def test_日報は旧名のファイルへ書き続ける(self) -> None:
        """**新名で作り直すと、これまでのぶんが入っていない空ができる。**"""
        from nippou.config import _pick_source

        (self.tmp / "日報管理.sqlite3").write_bytes(b"x")
        self.assertEqual(_pick_source(self.tmp, "日報データ.sqlite3").name,
                         "日報管理.sqlite3")


if __name__ == "__main__":
    unittest.main()
