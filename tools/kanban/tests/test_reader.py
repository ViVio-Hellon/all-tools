"""内蔵 Access リーダーのテスト。

実ファイルを使う検証は、環境変数 ``KANBAN_TEST_ACCDB`` に .accdb のパスを
指定したときだけ実行する(リポジトリには DB を含めないため)。

    KANBAN_TEST_ACCDB=/path/to/看板マスタ.accdb python -m unittest discover tests
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from kanban import config
from kanban.accdb import reader, types

ACCDB_PATH = os.environ.get("KANBAN_TEST_ACCDB", "")


class DecodeTextTest(unittest.TestCase):
    """Access の Unicode 圧縮テキストのデコード。"""

    def test_plain_utf16(self):
        raw = "外装紙".encode("utf-16-le")
        self.assertEqual(reader._decode_text(raw), "外装紙")

    def test_compressed_ascii(self):
        raw = b"\xff\xfe" + b"2200 x 50M"
        self.assertEqual(reader._decode_text(raw), "2200 x 50M")

    def test_compressed_then_uncompressed_segment(self):
        """0x00 で圧縮 / 非圧縮が切り替わる。

        ``"2200 × 50M : 3本"`` のように ASCII と日本語が混ざる値は、
        前半が 1 バイト、``0x00`` を挟んで後半が UTF-16LE で格納される。
        """
        raw = b"\xff\xfe" + b"2200 : 3" + b"\x00" + "本".encode("utf-16-le")
        self.assertEqual(reader._decode_text(raw), "2200 : 3本")

    def test_toggle_back_to_compressed(self):
        raw = (
            b"\xff\xfe"
            + b"A"
            + b"\x00"
            + "本".encode("utf-16-le")
            + b"\x00"
            + b"B"
        )
        self.assertEqual(reader._decode_text(raw), "A本B")

    def test_empty(self):
        self.assertEqual(reader._decode_text(b""), "")


class BitmapTest(unittest.TestCase):
    def test_bits_to_page_numbers(self):
        # 0b00000101 -> ページ 0, 2
        self.assertEqual(reader._bitmap_pages(b"\x05", 0), [0, 2])
        self.assertEqual(reader._bitmap_pages(b"\x00\x01", 100), [108])

    def test_empty_bitmap(self):
        self.assertEqual(reader._bitmap_pages(b"\x00\x00", 0), [])


class TypeCategoryTest(unittest.TestCase):
    def test_categories_match_vba_classification(self):
        self.assertEqual(types.type_category(types.LONG), types.CATEGORY_NUMBER)
        self.assertEqual(types.type_category(types.DOUBLE), types.CATEGORY_NUMBER)
        self.assertEqual(types.type_category(types.DATETIME), types.CATEGORY_DATE)
        self.assertEqual(types.type_category(types.TEXT), types.CATEGORY_TEXT)
        self.assertEqual(types.type_category(types.MEMO), types.CATEGORY_TEXT)

    def test_serial_conversion_roundtrip(self):
        import datetime as dt

        value = dt.datetime(2026, 1, 28, 13, 48)
        serial = types.datetime_to_serial(value)
        self.assertEqual(types.serial_to_datetime(serial), value)


#: ライン管理カレンダーの試験に入っている**実物の** .accdb(匿名化済み)。
#: 更新で別ページへ移った行(0x4000 / 0x8000)と削除済みの行(0xC000)を含む
SAMPLE_ACCDB = Path(__file__).resolve().parents[2] / "calendar" / "tests" / "data" / "sample.accdb"


@unittest.skipUnless(SAMPLE_ACCDB.exists(), f"実物のサンプルがありません: {SAMPLE_ACCDB}")
class SampleFileTest(unittest.TestCase):
    """**どの表も、テーブル定義の行数どおりに読める。**

    以前は移動元(0x4000)の 4 バイトの行き先をたどり、移動先(0x8000)を飛ばしていた。
    行き先が読めないと更新された行を黙って落とした(23 行の表を 18 行、2 行の表を 1 行)。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = reader.AccdbReader(str(SAMPLE_ACCDB))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.close()

    def test_every_table_reads_as_many_rows_as_its_definition(self):
        mismatches = []
        for name in self.db.table_names(include_system=True):
            tdef = self.db._table_def_by_name(name)
            rows = list(self.db._iter_rows(tdef))
            if len(rows) != tdef.num_rows:
                mismatches.append((name, tdef.num_rows, len(rows)))
        self.assertEqual(mismatches, [])

    def test_moved_rows_are_read_once(self):
        self.assertEqual(len(self.db.read_table("休み管理").rows), 2)
        self.assertEqual(len(self.db.read_table("MSysAccessStorage").rows), 23)


@unittest.skipUnless(ACCDB_PATH, "KANBAN_TEST_ACCDB が未設定のためスキップ")
class RealFileTest(unittest.TestCase):
    """実際の .accdb を読む検証。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = reader.AccdbReader(ACCDB_PATH)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.close()

    def test_catalog_lists_kanban_tables(self):
        names = self.db.table_names()
        self.assertTrue(
            any(n.startswith(config.KANBAN_TABLE_PREFIX) for n in names), names
        )

    def test_row_counts_match_table_definition(self):
        """読み取り行数がテーブル定義の行数と一致すること。

        移動済み行(オーバーフロー行)や使用状況マップの扱いを誤ると、
        ここで件数がずれる。
        """
        mismatches = []
        for name in self.db.table_names():
            tdef = self.db._table_def_by_name(name)
            rows = list(self.db._iter_rows(tdef))
            if len(rows) != tdef.num_rows:
                mismatches.append((name, tdef.num_rows, len(rows)))
        # 削除後に定義側の件数が更新されないテーブルが稀にあるため、
        # 看板テーブルについては完全一致を必須とする
        kanban_mismatches = [
            m for m in mismatches if m[0].startswith(config.KANBAN_TABLE_PREFIX)
        ]
        self.assertEqual(kanban_mismatches, [], f"看板テーブルの件数不一致: {mismatches}")

    def test_kanban_table_has_required_columns(self):
        for name in self.db.table_names():
            if not name.startswith(config.KANBAN_TABLE_PREFIX):
                continue
            columns = {c.name for c in self.db.columns(name)}
            missing = set(config.REQUIRED_COLUMNS) - columns
            self.assertEqual(missing, set(), f"{name} に不足列: {missing}")

    def test_values_are_decoded(self):
        table = None
        for name in self.db.table_names():
            if name.startswith(config.KANBAN_TABLE_PREFIX):
                table = self.db.read_table(name)
                if table.rows:
                    break
        self.assertIsNotNone(table)
        row = table.rows[0]
        self.assertIsInstance(row[config.COL_MATERIAL], str)
        self.assertTrue(row[config.COL_MATERIAL])


if __name__ == "__main__":
    unittest.main()
