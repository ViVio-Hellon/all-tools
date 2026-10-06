"""Access リーダ (accdb/reader.py) の確認。

実際の .accdb (tests/data/sample.accdb) を読み、
テーブル構造と全レコードが期待どおり取得できるかを検証する。
"""

import datetime as dt
import unittest
from pathlib import Path

from calendar_app.accdb.reader import AccdbError, AccdbReader

SAMPLE = Path(__file__).parent / "data" / "sample.accdb"


@unittest.skipUnless(SAMPLE.exists(), f"サンプル DB がありません: {SAMPLE}")
class AccdbReaderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.reader = AccdbReader(str(SAMPLE))

    def test_header(self) -> None:
        self.assertEqual(self.reader.jet_version, 2)  # ACE (Access 2007+)
        self.assertGreater(self.reader.page_count, 0)

    def test_user_tables_found(self) -> None:
        names = self.reader.table_names()
        self.assertIn("休み管理", names)
        self.assertIn("削除履歴", names)
        # 既定ではシステムテーブルを除外する
        self.assertFalse([n for n in names if n.startswith("MSys")])

    def test_system_tables_available_on_request(self) -> None:
        names = self.reader.table_names(include_system=True)
        self.assertIn("MSysObjects", names)

    def test_data_table_schema(self) -> None:
        table = self.reader.table("休み管理")
        self.assertEqual(
            table.column_names,
            ["ID", "日付", "区分", "登録内容", "識別コード",
             "直", "残業者", "早出者", "班", "ライン"],
        )
        types = {c.name: c.type_name for c in table.columns}
        self.assertEqual(types["ID"], "LONG")
        self.assertEqual(types["日付"], "DATETIME")
        self.assertEqual(types["登録内容"], "TEXT")

    def test_row_count_matches_table_definition(self) -> None:
        """テーブル定義の行数と、実際に読めた行数が一致すること。

        削除済み行を誤って拾うと ここがずれる。
        """
        for name in self.reader.table_names(include_system=True):
            table = self.reader.table(name)
            with self.subTest(table=name):
                self.assertEqual(len(list(self.reader.rows(name))), table.row_count)

    def test_data_values(self) -> None:
        """日本語テキスト・日付・数値が正しく復元されること。"""
        rows = {r["ID"]: r for r in self.reader.rows("休み管理")}
        self.assertEqual(set(rows), {1, 21})

        row = rows[21]
        self.assertEqual(row["日付"], dt.datetime(2026, 7, 25))
        self.assertEqual(row["区分"], "休み")
        self.assertEqual(row["登録内容"], "田中三郎")
        self.assertEqual(row["識別コード"], "50")
        self.assertEqual(row["直"], "2")
        self.assertEqual(row["残業者"], "鈴木一郎")
        self.assertEqual(row["早出者"], "未登録")
        self.assertEqual(row["班"], "D")
        self.assertEqual(row["ライン"], "コイル")

        row = rows[1]
        self.assertEqual(row["日付"], dt.datetime(2026, 7, 20))
        self.assertEqual(row["登録内容"], "佐藤四")
        self.assertEqual(row["残業者"], "未登録")
        self.assertEqual(row["早出者"], "鈴木一郎")

    def test_history_values(self) -> None:
        """削除履歴は日時(時刻あり)を保持している。"""
        rows = {r["ID"]: r for r in self.reader.rows("削除履歴")}
        row = rows[21]
        self.assertEqual(row["削除日時"], dt.datetime(2026, 7, 28, 8, 51, 19))
        self.assertEqual(row["対象日付"], dt.datetime(2026, 7, 28))
        self.assertEqual(row["区分"], "休み")
        self.assertEqual(row["登録内容"], "高橋次郎")
        self.assertEqual(row["削除実行者"], "testuser@EXAMPLE-PC-0100000")

    def test_scalar_in_variable_area(self) -> None:
        """可変長領域に置かれた数値列も型どおりに読めること。

        MSysNavPaneObjectIDs.Id は LONG だが固定長フラグが立っていない。
        """
        for row in self.reader.rows("MSysNavPaneObjectIDs"):
            self.assertIsInstance(row["Id"], int)
            self.assertIsInstance(row["Type"], int)

    def test_memo_column(self) -> None:
        """メモ型 (MSysObjects.Connect/Database) が文字列か None で返ること。"""
        for row in self.reader.rows("MSysObjects"):
            for column in ("Connect", "Database"):
                self.assertIsInstance(row[column], (str, type(None)))

    def test_missing_table_raises(self) -> None:
        with self.assertRaises(AccdbError):
            self.reader.table("存在しないテーブル")

    def test_context_manager(self) -> None:
        with AccdbReader(str(SAMPLE)) as db:
            self.assertTrue(db.table_names())


class AccdbReaderErrorTest(unittest.TestCase):
    def test_not_an_access_file(self) -> None:
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".accdb", delete=False) as fh:
            fh.write(b"not an access file" * 500)
            path = fh.name
        with self.assertRaises(AccdbError):
            AccdbReader(path)

    def test_too_small(self) -> None:
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".accdb", delete=False) as fh:
            fh.write(b"\x00\x01\x00\x00")
            path = fh.name
        with self.assertRaises(AccdbError):
            AccdbReader(path)


if __name__ == "__main__":
    unittest.main()


class DecodeTextTest(unittest.TestCase):
    """テキストのデコード (特に Unicode 圧縮形式) の確認。

    圧縮形式では 1 バイト 1 文字で格納され、0x00 が
    圧縮/非圧縮モードの切替エスケープとして働く。
    ここを 1 バイト固定で読むと ASCII と日本語が混在する値が化ける。
    """

    @staticmethod
    def _decode(raw: bytes) -> str:
        from calendar_app.accdb.reader import _decode_text

        return _decode_text(raw)

    def test_plain_utf16(self) -> None:
        self.assertEqual(self._decode("横井".encode("utf-16-le")), "横井")

    def test_compressed_ascii_only(self) -> None:
        self.assertEqual(self._decode(b"\xff\xfePET"), "PET")

    def test_compressed_switches_to_unicode(self) -> None:
        """"PETバンド" : ASCII 部分は圧縮、0x00 で非圧縮へ切り替わる。"""
        raw = b"\xff\xfePET\x00" + "バンド".encode("utf-16-le")
        self.assertEqual(self._decode(raw), "PETバンド")

    def test_compression_toggles_back(self) -> None:
        """"MFX2は450" : 圧縮 -> 非圧縮 -> 圧縮 と 2 回切り替わる。"""
        raw = b"\xff\xfeMFX2\x00" + "は".encode("utf-16-le") + b"\x00450"
        self.assertEqual(self._decode(raw), "MFX2は450")

    def test_starts_unicode_then_compressed(self) -> None:
        """"横桟60X90使用" : 先頭で即 非圧縮へ切り替わるケース。"""
        raw = (
            b"\xff\xfe\x00"
            + "横桟".encode("utf-16-le")
            + b"\x0060X90\x00"
            + "使用".encode("utf-16-le")
        )
        self.assertEqual(self._decode(raw), "横桟60X90使用")

    def test_trailing_nulls_are_trimmed(self) -> None:
        self.assertEqual(self._decode("A".encode("utf-16-le") + b"\x00\x00"), "A")

    def test_odd_length_is_tolerated(self) -> None:
        self.assertEqual(self._decode("AB".encode("utf-16-le") + b"\x01"), "AB")


class RowColumnCountTest(unittest.TestCase):
    """行が持つ列数がテーブル定義と違っていても読めること。

    Access では列を削除しても既存行の列数は変わらないため、
    行の列数が定義より多くなることがある。逆に列を追加した場合は少なくなる。
    """

    def _table(self, column_count: int):
        from calendar_app.accdb.reader import Column, Table

        return Table(
            name="T",
            tdef_page=1,
            row_count=1,
            columns=[
                Column(
                    name=f"c{i}",
                    index=i,
                    type_code=0x04,  # LONG
                    size=4,
                    fixed=True,
                    fixed_offset=i * 4,
                    var_index=0,
                )
                for i in range(column_count)
            ],
        )

    @staticmethod
    def _row(num_cols: int, values: list[int]) -> bytes:
        """固定長 LONG 列だけの行を組み立てる (可変長列なし)。"""
        import struct

        body = struct.pack("<H", num_cols) + b"".join(
            struct.pack("<i", v) for v in values
        )
        bitmask_size = (num_cols + 7) // 8
        # 可変長列数 0 + 全列 非NULL のビットマスク
        return body + struct.pack("<H", 0) + b"\xff" * bitmask_size

    def _parse(self, table, raw: bytes):
        # 固定長列の解釈にファイル本体は不要なので、
        # __init__ を通さないインスタンスで行の解釈だけを検証する
        reader = AccdbReader.__new__(AccdbReader)
        return reader._parse_row(raw, table)

    def test_row_has_more_columns_than_definition(self) -> None:
        """列を削除した後の行 (定義 2 列 / 行 3 列) でも値が読めること。"""
        result = self._parse(self._table(2), self._row(3, [10, 20, 30]))
        self.assertEqual(result, {"c0": 10, "c1": 20})

    def test_row_has_fewer_columns_than_definition(self) -> None:
        """列を追加した後の古い行は、追加分が None になること。"""
        result = self._parse(self._table(3), self._row(2, [10, 20]))
        self.assertEqual(result, {"c0": 10, "c1": 20, "c2": None})

    def test_absurd_column_count_is_rejected(self) -> None:
        """壊れた行 (Access の上限 255 列を超える) は捨てられること。"""
        import struct

        raw = struct.pack("<H", 30000) + b"\x00" * 40
        self.assertIsNone(self._parse(self._table(2), raw))


@unittest.skipUnless(SAMPLE.exists(), f"サンプル DB がありません: {SAMPLE}")
class UsageMapTest(unittest.TestCase):
    """データページの特定に使用ページマップを使っていることの確認。

    全ページ走査だけに頼ると、テーブルから切り離された古いページまで拾い、
    件数が実際より多くなる。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.reader = AccdbReader(str(SAMPLE))

    def test_usage_map_is_parsed(self) -> None:
        for name in ("休み管理", "削除履歴"):
            table = self.reader.table(name)
            with self.subTest(table=name):
                pages = self.reader._usage_map_pages(table.tdef_page)
                self.assertIsNotNone(pages, "使用ページマップを解釈できていない")
                self.assertTrue(pages)

    def test_data_pages_are_within_usage_map(self) -> None:
        for name in self.reader.table_names(include_system=True):
            table = self.reader.table(name)
            mapped = self.reader._usage_map_pages(table.tdef_page)
            if not mapped:
                continue
            with self.subTest(table=name):
                self.assertLessEqual(
                    set(self.reader._data_pages(table.tdef_page)), mapped
                )

    def test_falls_back_when_map_unavailable(self) -> None:
        """マップが読めない場合は全ページ走査へ退避すること。"""
        table = self.reader.table("休み管理")
        original = self.reader._usage_map_pages
        try:
            self.reader._usage_map_pages = lambda _pg: None  # type: ignore[method-assign]
            self.assertTrue(self.reader._data_pages(table.tdef_page))
        finally:
            self.reader._usage_map_pages = original  # type: ignore[method-assign]


class DateTimeRoundingTest(unittest.TestCase):
    """日時のシリアル値は浮動小数点誤差を含むため秒単位に丸めること。"""

    @staticmethod
    def _decode(serial: float):
        import struct

        from calendar_app.accdb.reader import _decode_datetime

        return _decode_datetime(struct.pack("<d", serial))

    def test_exact_date(self) -> None:
        # 1899-12-30 が 0 日目
        self.assertEqual(self._decode(0.0), dt.datetime(1899, 12, 30))
        self.assertEqual(self._decode(1.0), dt.datetime(1899, 12, 31))

    def test_rounds_up_to_nearest_second(self) -> None:
        base = (dt.datetime(2026, 7, 28, 10, 35, 5) - dt.datetime(1899, 12, 30))
        serial = base.days + base.seconds / 86400.0
        # 実ファイルでは 10:35:04.962 のようにわずかに小さい値で格納されている
        value = self._decode(serial - 0.038 / 86400.0)
        self.assertEqual(value, dt.datetime(2026, 7, 28, 10, 35, 5))
        self.assertEqual(value.microsecond, 0)

    def test_out_of_range_returns_none(self) -> None:
        self.assertIsNone(self._decode(1e18))
