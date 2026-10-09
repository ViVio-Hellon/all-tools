"""Access(.accdb / .mdb) を Python 標準ライブラリだけで読み取るリーダー。

ACE(Access 2007 以降 = "Standard ACE DB") と Jet4(Access 2000-2003) の
物理フォーマットを直接解析する。ODBC / ACE OLEDB プロバイダ / 外部パッケージは
一切不要なため、Windows 以外でも起動時取り込み(accdb -> SQLite)が動作する。

対応範囲
    * テーブル定義(列名・型・固定/可変長)
    * データページ上の通常行
    * インラインおよび単一ページ / 複数ページの LVAL(メモ・OLE)
    * 圧縮テキスト(0xFF 0xFE プレフィクス)と UTF-16LE テキスト

非対応(必要になった時点で ADO ブリッジ側にフォールバックする)
    * データベースパスワード付き / 暗号化されたファイル
    * クエリ・リンクテーブルの実体解決
    * 書き込み(このリーダーは読むだけ)
"""

from __future__ import annotations

import datetime as _dt
import struct
from dataclasses import dataclass
from typing import Any, Iterator

from . import types as T

# --- ページ種別 --------------------------------------------------------------
PAGE_DB_DEF = 0x00
PAGE_DATA = 0x01
PAGE_TABLE_DEF = 0x02
PAGE_INDEX_NODE = 0x03
PAGE_INDEX_LEAF = 0x04
PAGE_USAGE_BITMAP = 0x05

_JET3_SIGNATURE = b"Standard Jet DB"
_ACE_SIGNATURE = b"Standard ACE DB"

# 行オフセットの上位ビット。**実物の .accdb で、テーブル定義の行数と突き合わせて決めた**
# (tools/calendar/tests/data/sample.accdb。mdb-export とも一致):
#   0x4000 だけ … 更新で別ページへ移った行の**移動元**。中身は 4 バイトの行き先だけ
#   0x8000 だけ … その**移動先**(本物の行)。そのまま読む
#   0xC000      … 削除済み
# 以前は「0x8000 = 削除・0x4000 = 行き先をたどる」と読んでいた。行き先を書いた 4 バイトが
# 読めないと、更新された行を黙って落とした(サンプルで 23 行の表を 18 行と読んだ)。
# 移動先を直接読めば行き先のバイトに頼らない(ライン管理カレンダーの読み方と同じ)
_ROW_DELETED = 0x8000
_ROW_OVERFLOW = 0x4000
_ROW_OFFSET_MASK = 0x1FFF

_TEXT_COMPRESSED_PREFIX = b"\xff\xfe"


class AccdbError(Exception):
    """Access ファイルの解析に失敗した場合の例外。"""


@dataclass(frozen=True)
class _Format:
    """フォーマット差分(Jet3 / Jet4・ACE)をまとめた定数群。"""

    page_size: int
    is_jet4: bool
    # テーブル定義ページ
    tdef_next_page: int
    tdef_num_rows: int
    tdef_table_type: int
    tdef_num_var_cols: int
    tdef_num_cols: int
    tdef_num_index_slots: int
    tdef_num_indexes: int
    tdef_owned_pages: int
    tdef_index_block: int
    size_index_definition: int
    size_column_header: int
    size_index_column_block: int
    size_index_info_block: int
    # 列定義エントリ内のオフセット
    col_number: int
    col_var_index: int
    col_flags: int
    col_fixed_offset: int
    col_length: int
    # データページ
    data_num_rows: int
    name_length_size: int


_JET4 = _Format(
    page_size=4096,
    is_jet4=True,
    tdef_next_page=4,
    tdef_num_rows=16,
    tdef_table_type=40,
    tdef_num_var_cols=43,
    tdef_num_cols=45,
    tdef_num_index_slots=47,
    tdef_num_indexes=51,
    tdef_owned_pages=55,
    tdef_index_block=63,
    size_index_definition=12,
    size_column_header=25,
    size_index_column_block=52,
    size_index_info_block=28,
    col_number=5,
    col_var_index=7,
    col_flags=15,
    col_fixed_offset=21,
    col_length=23,
    data_num_rows=12,
    name_length_size=2,
)

_JET3 = _Format(
    page_size=2048,
    is_jet4=False,
    tdef_next_page=4,
    tdef_num_rows=12,
    tdef_table_type=20,
    tdef_num_var_cols=23,
    tdef_num_cols=25,
    tdef_num_index_slots=27,
    tdef_num_indexes=31,
    tdef_owned_pages=35,
    tdef_index_block=43,
    size_index_definition=8,
    size_column_header=18,
    size_index_column_block=39,
    size_index_info_block=20,
    col_number=1,
    col_var_index=3,
    col_flags=13,
    col_fixed_offset=14,
    col_length=16,
    data_num_rows=8,
    name_length_size=1,
)

# 列フラグ
_COL_FLAG_FIXED = 0x01
_COL_FLAG_AUTO_NUMBER = 0x04

# MSysObjects.Type
_OBJECT_TYPE_TABLE = 1


def _u8(buf: bytes, off: int) -> int:
    return buf[off]


def _u16(buf: bytes, off: int) -> int:
    return struct.unpack_from("<H", buf, off)[0]


def _i16(buf: bytes, off: int) -> int:
    return struct.unpack_from("<h", buf, off)[0]


def _u32(buf: bytes, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


def _i32(buf: bytes, off: int) -> int:
    return struct.unpack_from("<i", buf, off)[0]


def _decode_text(raw: bytes) -> str:
    """Access のテキスト列をデコードする。

    先頭が 0xFF 0xFE の場合は「Unicode 圧縮」形式で、1 バイト 1 文字の圧縮
    区間と UTF-16LE の非圧縮区間が 0x00 バイトを境に交互に切り替わる。
    ASCII と日本語が混在する ``"2200 × 50M : 3本"`` のような値はこの形式で
    格納されるため、単純に片方だけでデコードすると末尾が壊れる。

    0xFF 0xFE で始まらない場合は全体が UTF-16LE。
    """
    if not raw.startswith(_TEXT_COMPRESSED_PREFIX):
        if len(raw) % 2:
            raw = raw[:-1]
        return raw.decode("utf-16-le", errors="replace")

    out: list[str] = []
    buf = raw[2:]
    pos = 0
    compressed = True
    length = len(buf)
    while pos < length:
        byte = buf[pos]
        if byte == 0x00:
            # 圧縮 / 非圧縮の切り替えマーカー
            compressed = not compressed
            pos += 1
        elif compressed:
            out.append(chr(byte))
            pos += 1
        elif pos + 1 < length:
            out.append(buf[pos : pos + 2].decode("utf-16-le", errors="replace"))
            pos += 2
        else:
            break
    return "".join(out)


class AccdbReader:
    """Access ファイルを読み取るリーダー。

    使用例::

        with AccdbReader(path) as db:
            for name in db.table_names():
                table = db.read_table(name)
    """

    def __init__(self, path: str):
        self.path = str(path)
        with open(self.path, "rb") as fh:
            self._data = fh.read()
        if len(self._data) < 0x20:
            raise AccdbError(f"ファイルが小さすぎます: {self.path}")
        signature = self._data[4:19]
        if signature not in (_JET3_SIGNATURE, _ACE_SIGNATURE):
            raise AccdbError(
                f"Access ファイルとして認識できません(signature={signature!r}): {self.path}"
            )
        version_byte = self._data[0x14]
        self.version_byte = version_byte
        self.fmt = _JET3 if version_byte == 0x00 else _JET4
        if len(self._data) % self.fmt.page_size:
            # 末尾が欠けていても読める範囲は読む(コピー途中のファイル対策)
            pass
        self._page_count = len(self._data) // self.fmt.page_size
        self._tdef_cache: dict[int, _TableDef] = {}
        self._catalog: dict[str, int] | None = None
        self._owned_pages_cache: dict[int, list[int]] = {}
        self._reference_slots: list[tuple[int, int]] = []

    # -- コンテキストマネージャ ------------------------------------------
    def __enter__(self) -> "AccdbReader":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._data = b""
        self._tdef_cache.clear()

    # -- ページ操作 ------------------------------------------------------
    @property
    def page_count(self) -> int:
        return self._page_count

    def _page(self, number: int) -> bytes:
        if number < 0 or number >= self._page_count:
            raise AccdbError(f"ページ番号が範囲外です: {number}")
        start = number * self.fmt.page_size
        return self._data[start : start + self.fmt.page_size]

    # -- カタログ(MSysObjects) -------------------------------------------
    def table_names(self, include_system: bool = False) -> list[str]:
        """テーブル名の一覧を返す(既定ではシステムテーブルを除外)。"""
        catalog = self._load_catalog()
        names = sorted(catalog)
        if include_system:
            return names
        return [n for n in names if not n.startswith(("MSys", "~"))]

    def _load_catalog(self) -> dict[str, int]:
        """テーブル名 -> テーブル定義ページ番号 の辞書を作る。"""
        if self._catalog is not None:
            return self._catalog

        # MSysObjects のテーブル定義は常にページ 2 に置かれる
        tdef = self._table_def(2, "MSysObjects")
        catalog: dict[str, int] = {}
        for row in self._iter_rows(tdef):
            if row.get("Type") != _OBJECT_TYPE_TABLE:
                continue
            name = row.get("Name")
            obj_id = row.get("Id")
            if not isinstance(name, str) or not isinstance(obj_id, int):
                continue
            catalog[name] = obj_id & 0x00FFFFFF
        self._catalog = catalog
        return catalog

    # -- テーブル読み取り ------------------------------------------------
    def read_table(self, name: str) -> T.Table:
        """テーブル 1 つを全件読み込む。"""
        tdef = self._table_def_by_name(name)
        table = T.Table(name=name, columns=list(tdef.columns))
        table.rows = list(self._iter_rows(tdef))
        return table

    def iter_table(self, name: str) -> Iterator[dict[str, Any]]:
        """テーブルを 1 行ずつ返すイテレータ(大きなテーブル向け)。"""
        return self._iter_rows(self._table_def_by_name(name))

    def columns(self, name: str) -> list[T.Column]:
        """テーブルの列定義を返す。"""
        return list(self._table_def_by_name(name).columns)

    def has_table(self, name: str) -> bool:
        return name in self._load_catalog()

    def _table_def_by_name(self, name: str) -> "_TableDef":
        catalog = self._load_catalog()
        if name not in catalog:
            raise AccdbError(f"テーブルが見つかりません: {name}")
        return self._table_def(catalog[name], name)

    # -- テーブル定義の解析 ----------------------------------------------
    def _table_def(self, page_number: int, name: str) -> "_TableDef":
        cached = self._tdef_cache.get(page_number)
        if cached is not None:
            return cached

        fmt = self.fmt
        buf = bytearray(self._page(page_number))
        if buf[0] != PAGE_TABLE_DEF:
            raise AccdbError(
                f"テーブル定義ページではありません(page={page_number}, type={buf[0]:#x})"
            )
        # 継続ページを連結する(先頭 8 バイトのヘッダを除く)
        next_page = _u32(buf, fmt.tdef_next_page)
        visited = {page_number}
        while next_page:
            if next_page in visited or next_page >= self._page_count:
                break
            visited.add(next_page)
            cont = self._page(next_page)
            buf.extend(cont[8:])
            next_page = _u32(cont, fmt.tdef_next_page)

        data = bytes(buf)
        num_rows = _u32(data, fmt.tdef_num_rows)
        num_cols = _u16(data, fmt.tdef_num_cols)
        num_var_cols = _u16(data, fmt.tdef_num_var_cols)
        num_index_slots = _u32(data, fmt.tdef_num_index_slots)
        num_indexes = _u32(data, fmt.tdef_num_indexes)
        owned_pages_ref = _u32(data, fmt.tdef_owned_pages)

        # 索引定義ブロックを読み飛ばして列定義の開始位置を得る
        offset = fmt.tdef_index_block + num_indexes * fmt.size_index_definition

        columns: list[T.Column] = []
        raw_entries: list[dict[str, int]] = []
        for _ in range(num_cols):
            entry = data[offset : offset + fmt.size_column_header]
            if len(entry) < fmt.size_column_header:
                raise AccdbError(f"列定義が途切れています: {name}")
            raw_entries.append(
                {
                    "type": entry[0],
                    "number": _u16(entry, fmt.col_number),
                    "var_index": _u16(entry, fmt.col_var_index),
                    "flags": entry[fmt.col_flags],
                    "fixed_offset": _u16(entry, fmt.col_fixed_offset),
                    "length": _u16(entry, fmt.col_length),
                    # 十進型の小数点の位置(Jet4 の列定義の 12 バイト目。Jet3 に十進型は無い)
                    "scale": entry[12] if fmt.is_jet4 and len(entry) > 12 else 0,
                }
            )
            offset += fmt.size_column_header

        # 列名(列定義と同じ順序で格納されている)
        names: list[str] = []
        for _ in range(num_cols):
            if fmt.name_length_size == 2:
                name_len = _u16(data, offset)
                offset += 2
            else:
                name_len = data[offset]
                offset += 1
            raw_name = data[offset : offset + name_len]
            offset += name_len
            if fmt.is_jet4:
                names.append(_decode_text(raw_name))
            else:
                names.append(raw_name.decode("cp1252", errors="replace"))

        for raw, col_name in zip(raw_entries, names):
            flags = raw["flags"]
            columns.append(
                T.Column(
                    name=col_name,
                    index=raw["number"],
                    access_type=raw["type"],
                    length=raw["length"],
                    is_variable=not (flags & _COL_FLAG_FIXED),
                    is_auto_number=bool(flags & _COL_FLAG_AUTO_NUMBER),
                    is_fixed_width=bool(flags & _COL_FLAG_FIXED),
                    scale=raw["scale"],
                )
            )

        # 列を論理順(テーブル定義順)に並べ替える
        ordered = sorted(range(len(columns)), key=lambda i: columns[i].index)
        tdef = _TableDef(
            name=name,
            page=page_number,
            num_rows=num_rows,
            columns=[columns[i] for i in ordered],
            owned_pages_ref=owned_pages_ref,
            _entries=[raw_entries[i] for i in ordered],
            _num_var_cols=num_var_cols,
            _num_index_slots=num_index_slots,
        )
        self._tdef_cache[page_number] = tdef
        return tdef

    # -- データページの走査 ----------------------------------------------
    def _owned_pages(self, tdef: "_TableDef") -> list[int]:
        """テーブルが所有するデータページ番号の一覧。

        「ページ先頭の所有者フィールドを全ページ走査する」方法では、解放後に
        再利用されていない古いページ(所有者フィールドが残ったままのページ)を
        拾ってしまい行数が過剰になる。そのため使用状況マップ(usage map)を
        正式に辿る。
        """
        if tdef.page in self._owned_pages_cache:
            return self._owned_pages_cache[tdef.page]
        ref = tdef.owned_pages_ref
        pages = self._read_usage_map(ref >> 8, ref & 0xFF)
        # データページ以外(定義ページ・索引ページ)は除外する
        result = [p for p in pages if p < self._page_count and self._data[p * self.fmt.page_size] == PAGE_DATA]
        self._owned_pages_cache[tdef.page] = result
        return result

    def _read_usage_map(self, page_num: int, row_num: int) -> list[int]:
        """使用状況マップの行を読み、含まれるページ番号を返す。

        マップは 2 形式ある。

        * インライン形式(0x00): 行内のビットマップが ``開始ページ`` からの
          連続ページを表す。
        * 参照形式(0x01): 行内に並ぶページ番号がそれぞれビットマップページ
          (ページ種別 0x05)を指し、1 ページで ``(ページサイズ-4)*8`` ページ分
          を表す。
        """
        fmt = self.fmt
        if page_num >= self._page_count:
            return []
        page = self._page(page_num)
        row = self._row_bytes(page, row_num)
        if row is None or len(row) < 5:
            return []
        map_type = row[0]
        if map_type == 0x00:  # インライン形式: [種別][開始ページ 4byte][ビットマップ]
            start_page = _u32(row, 1)
            return _bitmap_pages(row[5:], start_page)
        if map_type == 0x01:  # 参照形式
            return self._read_reference_map(row)
        return []

    def _read_reference_map(self, row: bytes) -> list[int]:
        """参照形式の使用状況マップを展開する。

        行内にはビットマップページ番号が 4 バイトずつ並び、n 番目のページが
        ``n * (ページサイズ-4) * 8`` ページ目からの範囲を受け持つ。配列の開始
        位置はファイルによって 1 バイト目 / 5 バイト目のどちらもあり得るため、
        「実際にビットマップページ(種別 0x05)を指しているか」で判定する。
        """
        fmt = self.fmt
        span = (fmt.page_size - 4) * 8

        def collect(base_offset: int) -> list[int]:
            found: list[int] = []
            for slot, offset in enumerate(range(base_offset, len(row) - 3, 4)):
                map_page = _u32(row, offset)
                if not map_page or map_page >= self._page_count:
                    continue
                bitmap_page = self._page(map_page)
                if bitmap_page[0] != PAGE_USAGE_BITMAP:
                    continue
                found.append(map_page)
                self._reference_slots.append((slot * span, map_page))
            return found

        best: list[int] = []
        best_offset = 1
        for candidate in (1, 5):
            self._reference_slots = []
            pointers = collect(candidate)
            if len(pointers) > len(best):
                best = pointers
                best_offset = candidate
        self._reference_slots = []
        collect(best_offset)
        pages: list[int] = []
        for base, map_page in self._reference_slots:
            pages.extend(_bitmap_pages(self._page(map_page)[4:], base))
        self._reference_slots = []
        return pages

    def _row_bytes(self, page: bytes, row_num: int, ignore_flags: bool = False) -> bytes | None:
        """データページ上の行 ``row_num`` のバイト列を取り出す。"""
        fmt = self.fmt
        row_count = _u16(page, fmt.data_num_rows)
        if row_num >= row_count:
            return None
        offsets_base = fmt.data_num_rows + 2
        raw = _u16(page, offsets_base + row_num * 2)
        if not ignore_flags and (raw & (_ROW_DELETED | _ROW_OVERFLOW)):
            return None
        start = raw & _ROW_OFFSET_MASK
        end = self._row_end(page, row_num)
        if end <= start:
            return None
        return page[start:end]

    def _row_end(self, page: bytes, row_num: int) -> int:
        """行 ``row_num`` の終端(排他)を返す。"""
        fmt = self.fmt
        if row_num == 0:
            return fmt.page_size
        offsets_base = fmt.data_num_rows + 2
        return _u16(page, offsets_base + (row_num - 1) * 2) & _ROW_OFFSET_MASK

    def _iter_rows(self, tdef: "_TableDef") -> Iterator[dict[str, Any]]:
        fmt = self.fmt
        offsets_base = fmt.data_num_rows + 2
        for pno in self._owned_pages(tdef):
            page = self._page(pno)
            if _u32(page, 4) != tdef.page:
                # 所有者が一致しないページ(マップの取りこぼし)は無視する
                continue
            row_count = _u16(page, fmt.data_num_rows)
            for i in range(row_count):
                raw_offset = _u16(page, offsets_base + i * 2)
                if raw_offset & _ROW_OVERFLOW:
                    # 削除済み(0xC000)か、別ページへ移った行の移動元(0x4000)。
                    # 移動先(0x8000 だけ)をそのページで読むので、ここでは読まない
                    continue
                row_start = raw_offset & _ROW_OFFSET_MASK
                row_end = self._row_end(page, i)
                if row_end <= row_start:
                    continue
                row = self._parse_row(tdef, page, row_start, row_end - 1)
                if row is not None:
                    yield row

    def _parse_row(
        self, tdef: "_TableDef", page: bytes, row_start: int, row_end: int
    ) -> dict[str, Any] | None:
        """1 行分のバイト列を辞書へ展開する。

        レイアウト(Jet4/ACE, row_end は行末バイトの位置で内包)::

            [列数 2byte][固定長列データ...][可変長列データ...]
            [可変長列オフセット表 (n+1)*2byte][可変長列数 2byte][NULL マスク]
        """
        fmt = self.fmt
        try:
            if fmt.is_jet4:
                num_cols = _u16(page, row_start)
                data_start = row_start + 2
            else:
                num_cols = page[row_start]
                data_start = row_start + 1

            bitmask_size = (num_cols + 7) // 8
            null_mask_start = row_end - bitmask_size + 1
            null_mask = page[null_mask_start : null_mask_start + bitmask_size]

            if fmt.is_jet4:
                num_var_cols = _u16(page, row_end - bitmask_size - 1)
                var_table_end = row_end - bitmask_size - 2
                var_offsets = [
                    _u16(page, row_end - bitmask_size - 3 - i * 2)
                    for i in range(num_var_cols + 1)
                ]
            else:
                num_var_cols = page[row_end - bitmask_size]
                var_table_end = row_end - bitmask_size - 1
                var_offsets = [
                    page[row_end - bitmask_size - 1 - i] for i in range(num_var_cols + 1)
                ]
        except (IndexError, struct.error):
            return None

        del var_table_end  # レイアウト確認用(現状は未使用)

        result: dict[str, Any] = {}
        for col, entry in zip(tdef.columns, tdef._entries):
            col_index = col.index
            is_null = True
            if col_index < num_cols * 8:
                byte_index = col_index // 8
                if byte_index < len(null_mask):
                    is_null = not (null_mask[byte_index] & (1 << (col_index % 8)))

            if col.access_type == T.BOOL:
                # BOOL は NULL マスクのビットそのものが値
                result[col.name] = not is_null
                continue

            if is_null:
                result[col.name] = None
                continue

            try:
                if col.is_variable:
                    var_index = entry["var_index"]
                    if var_index + 1 >= len(var_offsets):
                        result[col.name] = None
                        continue
                    # オフセット表は末尾から前方に並ぶため昇順に読み替える
                    start = row_start + var_offsets[var_index]
                    end = row_start + var_offsets[var_index + 1]
                    if end < start:
                        start, end = end, start
                    raw = page[start:end]
                    result[col.name] = self._convert_variable(col, raw)
                else:
                    start = data_start + entry["fixed_offset"]
                    size = T.FIXED_SIZES.get(col.access_type, col.length)
                    raw = page[start : start + size]
                    result[col.name] = self._convert_fixed(col, raw)
            except (IndexError, struct.error, ValueError, OverflowError):
                result[col.name] = None

        return result

    # -- 値の変換 --------------------------------------------------------
    def _convert_fixed(self, col: T.Column, raw: bytes) -> Any:
        t = col.access_type
        if t == T.BYTE:
            return raw[0]
        if t == T.INT:
            return _i16(raw, 0)
        if t == T.LONG or t == T.COMPLEX:
            return _i32(raw, 0)
        if t == T.FLOAT:
            return _decode_single(raw)
        if t == T.DOUBLE:
            return struct.unpack_from("<d", raw, 0)[0]
        if t == T.MONEY:
            return struct.unpack_from("<q", raw, 0)[0] / 10000.0
        if t == T.DATETIME:
            serial = struct.unpack_from("<d", raw, 0)[0]
            try:
                return T.serial_to_datetime(serial)
            except (OverflowError, ValueError):
                return None
        if t == T.REPID:
            return str(_format_guid(raw))
        if t == T.NUMERIC:
            return _decode_numeric(raw, col.scale)
        return raw

    def _convert_variable(self, col: T.Column, raw: bytes) -> Any:
        t = col.access_type
        if t == T.TEXT:
            return _decode_text(raw)
        if t in (T.MEMO, T.OLE):
            payload = self._read_long_value(raw)
            if payload is None:
                return None
            if t == T.MEMO:
                return _decode_text(payload)
            return payload
        if t == T.BINARY:
            return raw
        return raw

    def _read_long_value(self, raw: bytes) -> bytes | None:
        """MEMO / OLE 列の LVAL を解決する。"""
        if len(raw) < 12:
            # ヘッダを持たない短いインライン値
            return raw
        length_and_flags = _u32(raw, 0)
        length = length_and_flags & 0x00FFFFFF
        flags = (length_and_flags >> 24) & 0xFF
        if flags & 0x80:  # インライン
            return raw[12 : 12 + length]
        pointer = _u32(raw, 4)
        row_num = pointer & 0xFF
        page_num = pointer >> 8
        if flags & 0x40:  # 単一 LVAL ページ
            return self._read_lval_page(page_num, row_num, length, chained=False)
        return self._read_lval_page(page_num, row_num, length, chained=True)

    def _read_lval_page(
        self, page_num: int, row_num: int, length: int, chained: bool
    ) -> bytes | None:
        fmt = self.fmt
        out = bytearray()
        seen: set[tuple[int, int]] = set()
        while page_num and len(out) < length:
            if (page_num, row_num) in seen or page_num >= self._page_count:
                break
            seen.add((page_num, row_num))
            page = self._page(page_num)
            if page[0] != PAGE_DATA:
                break
            row_count = _u16(page, fmt.data_num_rows)
            if row_num >= row_count:
                break
            offsets_base = fmt.data_num_rows + 2
            raw_offset = _u16(page, offsets_base + row_num * 2)
            start = raw_offset & _ROW_OFFSET_MASK
            if row_num == 0:
                end = fmt.page_size
            else:
                end = _u16(page, offsets_base + (row_num - 1) * 2) & _ROW_OFFSET_MASK
            chunk = page[start:end]
            if chained:
                if len(chunk) < 4:
                    break
                pointer = _u32(chunk, 0)
                out.extend(chunk[4:])
                row_num = pointer & 0xFF
                page_num = pointer >> 8
            else:
                out.extend(chunk)
                page_num = 0
        return bytes(out[:length]) if out else b""


def _bitmap_pages(bitmap: bytes, base_page: int) -> list[int]:
    """ビットマップの立っているビットをページ番号の一覧へ展開する。"""
    pages: list[int] = []
    for byte_index, byte in enumerate(bitmap):
        if not byte:
            continue
        for bit in range(8):
            if byte & (1 << bit):
                pages.append(base_page + byte_index * 8 + bit)
    return pages


def _format_guid(raw: bytes) -> str:
    if len(raw) < 16:
        return ""
    a = _u32(raw, 0)
    b = _u16(raw, 4)
    c = _u16(raw, 6)
    tail = raw[8:16].hex()
    return f"{{{a:08X}-{b:04X}-{c:04X}-{tail[:4].upper()}-{tail[4:].upper()}}}"


def _decode_single(raw: bytes) -> float:
    """単精度(Single・4 バイト)を、**Access が見せるのと同じ数**にする。

    そのまま Python の float にすると 2.9 が 2.9000000953674316 になり、その値で sqlite3 へ
    書いていた(Access で 2.9 と見えている値が別の数になる)。同じ 4 バイトに戻るいちばん短い
    10 進の書き方(最大 9 桁)にそろえる。
    """
    value = struct.unpack_from("<f", raw, 0)[0]
    if value != value or value in (float("inf"), float("-inf")):
        return value
    for digits in range(1, 10):
        text = f"{value:.{digits}g}"
        if struct.unpack("<f", struct.pack("<f", float(text)))[0] == value:
            return float(text)
    return value


def _decode_numeric(raw: bytes, scale: int = 0) -> float | int | None:
    """十進型(Decimal・17 バイト)を数にする。

    並び: 先頭 1 バイトが符号(0 以外で負)、続く 16 バイトが 4 バイトずつの整数 4 つ
    (それぞれは小さい側から、4 つは大きい桁から)。**小数点の位置(scale)は列の定義にある**
    (行の 2 バイト目ではない)。Jackcess・access_parser と同じ読み方。以前は scale を行の
    2 バイト目から取り、整数も違う位置から読んでいたので、2971.5 が桁違いの数になっていた。
    """
    if len(raw) < 17:
        return None
    from decimal import Decimal

    sign, n1, n2, n3, n4 = struct.unpack_from("<BIIII", raw, 0)
    number = Decimal((n1 << 96) | (n2 << 64) | (n3 << 32) | n4).scaleb(-int(scale or 0))
    if sign:
        number = -number
    return int(number) if not scale else float(number)


def read_table(path: str, table_name: str) -> T.Table:
    """単一テーブルを読み込む簡易ヘルパー。"""
    with AccdbReader(path) as db:
        return db.read_table(table_name)


def to_sqlite_value(value: Any) -> Any:
    """共有 SQLite へ入れる形にする(移行の変換 ``tools/accdb_to_sqlite.py`` と同じ)。

    日時は看板が文字列で比べる書き方 ``yyyy/mm/dd hh:nn:ss`` に、日付だけのものは
    ``yyyy/mm/dd`` にそろえる。**変換と中身の入れ替えで書き方が食い違わないよう、
    ここ 1 か所で決める。**
    """
    if isinstance(value, _dt.datetime):
        return value.strftime("%Y/%m/%d %H:%M:%S")
    if isinstance(value, _dt.date):
        return value.strftime("%Y/%m/%d")
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return value


def format_value(value: Any) -> Any:
    """SQLite へ格納できる素の値へ正規化する。

    日時は VBA 実装と同じ ``yyyy/mm/dd hh:nn:ss`` 文字列に統一する。
    """
    if isinstance(value, _dt.datetime):
        return value.strftime(T.DB_DATETIME_FORMAT)
    if isinstance(value, bytes):
        return value
    return value


@dataclass
class _TableDef:
    """内部用のテーブル定義。"""

    name: str
    page: int
    num_rows: int
    columns: list[T.Column]
    owned_pages_ref: int
    _entries: list[dict[str, int]]
    _num_var_cols: int
    _num_index_slots: int
