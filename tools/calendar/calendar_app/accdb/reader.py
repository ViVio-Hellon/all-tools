"""Access (.accdb / .mdb) ファイルを Python 標準ライブラリのみで直接読み取るリーダ。

VBA 版は ADODB + ACE OLEDB プロバイダで Access に接続していたが、
移行先の環境では Access / ACE ドライバに接続できない。
そのため「事前に SQLite へ取り込む」方式を採用し、その取り込み元として
Access ファイルのバイナリを直接解析する。

対応フォーマット
  * Jet4    (Access 2000/2002/2003 : .mdb)
  * ACE     (Access 2007 以降       : .accdb)
  いずれもページサイズ 4096 バイトで、テーブル定義/データページの構造は共通。

制限
  * 暗号化(パスワード付き)DB は非対応 -> AccdbError を送出する
  * 添付ファイル型/複数値フィールドは未対応 (値は None になる)
  読み取り専用であり、このモジュールが .accdb を書き換えることは一切ない。
"""

from __future__ import annotations

import datetime as _dt
import os
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

__all__ = ["AccdbError", "AccdbReader", "Column", "Table"]


class AccdbError(Exception):
    """Access ファイルの解析に失敗した場合に送出する。"""


# --- ページ種別 -------------------------------------------------------------
PAGE_DB_DEF = 0x00
PAGE_DATA = 0x01
PAGE_TABLE_DEF = 0x02
PAGE_INDEX_NODE = 0x03
PAGE_INDEX_LEAF = 0x04
PAGE_USAGE_BITMAP = 0x05

# --- Jet 列型 ---------------------------------------------------------------
TYPE_BOOL = 0x01
TYPE_BYTE = 0x02
TYPE_INT = 0x03
TYPE_LONGINT = 0x04
TYPE_MONEY = 0x05
TYPE_FLOAT = 0x06
TYPE_DOUBLE = 0x07
TYPE_DATETIME = 0x08
TYPE_BINARY = 0x09
TYPE_TEXT = 0x0A
TYPE_OLE = 0x0B
TYPE_MEMO = 0x0C
TYPE_REPID = 0x0F
TYPE_NUMERIC = 0x10
TYPE_COMPLEX = 0x12

#: 固定長列のバイト数。ここに無い型は可変長として扱う。
_FIXED_SIZE = {
    TYPE_BOOL: 0,  # 値は NULL ビットマスク上に格納される
    TYPE_BYTE: 1,
    TYPE_INT: 2,
    TYPE_LONGINT: 4,
    TYPE_MONEY: 8,
    TYPE_FLOAT: 4,
    TYPE_DOUBLE: 8,
    TYPE_DATETIME: 8,
    TYPE_REPID: 16,
    TYPE_NUMERIC: 17,
    TYPE_COMPLEX: 4,
}

# --- Jet4 / ACE テーブル定義ページのオフセット定数 --------------------------
_TAB_NUM_ROWS = 16
_TAB_NUM_COLS = 45
_TAB_NUM_IDXS = 47
_TAB_NUM_RIDXS = 51
_TAB_USAGE_MAP = 55
_TAB_COLS_START = 63
_TAB_RIDX_ENTRY_SIZE = 12
_COL_ENTRY_SIZE = 25
_COL_TYPE = 0
_COL_NUM = 5
_COL_VAR_OFFSET = 7
_COL_FLAGS = 15
_COL_FIXED_OFFSET = 21
_COL_SIZE = 23

#: 列フラグ: 固定長列であることを示すビット
_COL_FLAG_FIXED = 0x01

#: 行オフセット表のマスクとフラグ。
#: 実データとの突合により、0x4000 が立っている行は削除済み(再利用待ち)であることを確認済み。
_ROW_OFFSET_MASK = 0x1FFF
_ROW_FLAG_DELETED = 0x4000

#: Access の 1 テーブルあたりの最大列数。行の妥当性検査に使う。
_MAX_COLUMNS = 255

#: Access の DATETIME は 1899-12-30 を 0 とする日数(小数部が時刻)
_ACCESS_EPOCH = _dt.datetime(1899, 12, 30)

PAGE_SIZE = 4096


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
    """Jet のテキスト列をデコードする。

    Jet4 以降のテキストは原則 UTF-16LE だが、先頭に 0xFF 0xFE が付いている場合は
    「圧縮形式」となる。圧縮形式では 1 バイト 1 文字 (U+00xx) で格納され、
    ``0x00`` バイトが圧縮/非圧縮モードの切替エスケープとして働く。

    例 : "PETバンド" は  FF FE  'P' 'E' 'T'  00  D0 30 F3 30 C9 30
         -> "PET" までが圧縮、00 で非圧縮に切り替わり以降は UTF-16LE

    そのため ASCII と日本語が混在する値では、単純に全体を 1 バイト単位で
    読んでしまうと文字化けする。

    制限 : 非圧縮区間で下位バイトが 0x00 の文字 (U+3000 の全角空白など) が
    文字境界に現れると切替と区別できない。Access はそのような値に対して
    圧縮を適用しないため実用上は問題にならない。
    """
    if raw[:2] != b"\xff\xfe":
        if len(raw) % 2:
            raw = raw[:-1]
        return raw.decode("utf-16-le", errors="replace").rstrip("\x00")

    data = raw[2:]
    chars: list[str] = []
    compressed = True
    i = 0
    while i < len(data):
        if data[i] == 0x00:
            compressed = not compressed
            i += 1
        elif compressed:
            chars.append(chr(data[i]))
            i += 1
        elif i + 1 < len(data):
            chars.append(chr(data[i] | (data[i + 1] << 8)))
            i += 2
        else:
            break
    return "".join(chars).rstrip("\x00")


def _decode_datetime(raw: bytes) -> _dt.datetime | None:
    """Access の DATETIME (1899-12-30 起点の日数を表す倍精度) を復元する。

    シリアル値は浮動小数点であり ±0.5 秒程度の誤差を含む
    (元の VBA にも「シリアル値は[±0.5秒]の誤差範囲で認識されます」という
    注意書きがあった)。そのまま換算すると 10:35:05 が 10:35:04.962 のように
    なってしまうため、Access の表示と同じく秒単位に丸める。
    """
    (serial,) = struct.unpack_from("<d", raw, 0)
    try:
        value = _ACCESS_EPOCH + _dt.timedelta(days=serial)
    except (OverflowError, ValueError):
        return None
    if value.microsecond:
        value = value.replace(microsecond=0) + _dt.timedelta(
            seconds=1 if value.microsecond >= 500_000 else 0
        )
    return value


def _decode_numeric(raw: bytes) -> float | None:
    """NUMERIC(17 バイト) を float として復元する。"""
    if len(raw) < 17:
        return None
    sign = -1 if raw[0] & 0x80 else 1
    scale = raw[1]
    # 12 バイトのリトルエンディアン整数 (4 バイト x 3 ワードの並びを考慮)
    lo = int.from_bytes(raw[5:9], "little")
    mid = int.from_bytes(raw[9:13], "little")
    hi = int.from_bytes(raw[13:17], "little")
    value = lo | (mid << 32) | (hi << 64)
    return sign * value / (10**scale) if scale else sign * float(value)


@dataclass
class Column:
    """テーブルの 1 列分の定義。"""

    name: str
    index: int
    type_code: int
    size: int
    fixed: bool
    fixed_offset: int
    var_index: int

    @property
    def type_name(self) -> str:
        return {
            TYPE_BOOL: "BOOL",
            TYPE_BYTE: "BYTE",
            TYPE_INT: "INT",
            TYPE_LONGINT: "LONG",
            TYPE_MONEY: "MONEY",
            TYPE_FLOAT: "FLOAT",
            TYPE_DOUBLE: "DOUBLE",
            TYPE_DATETIME: "DATETIME",
            TYPE_BINARY: "BINARY",
            TYPE_TEXT: "TEXT",
            TYPE_OLE: "OLE",
            TYPE_MEMO: "MEMO",
            TYPE_REPID: "GUID",
            TYPE_NUMERIC: "NUMERIC",
            TYPE_COMPLEX: "COMPLEX",
        }.get(self.type_code, f"UNKNOWN(0x{self.type_code:02x})")

    @property
    def sqlite_type(self) -> str:
        """SQLite 側で使用する型親和性を返す。"""
        if self.type_code in (TYPE_BYTE, TYPE_INT, TYPE_LONGINT, TYPE_BOOL):
            return "INTEGER"
        if self.type_code in (TYPE_FLOAT, TYPE_DOUBLE, TYPE_MONEY, TYPE_NUMERIC):
            return "REAL"
        if self.type_code in (TYPE_BINARY, TYPE_OLE):
            return "BLOB"
        return "TEXT"


@dataclass
class Table:
    """テーブル定義。"""

    name: str
    tdef_page: int
    row_count: int
    columns: list[Column] = field(default_factory=list)

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]


class AccdbReader:
    """.accdb / .mdb を読み取り専用で開くリーダ。

    使い方::

        with AccdbReader("連絡帳.accdb") as db:
            for row in db.rows("休み管理"):
                print(row["ID"], row["登録内容"])
    """

    def __init__(self, path: str) -> None:
        self.path = str(path)
        self._data = _read_stable(self.path)
        self._validate_header()
        self._tables: dict[str, Table] | None = None
        self._tdef_cache: dict[int, Table] = {}

    @staticmethod
    def lock_file(path: str | Path) -> Path | None:
        """Access が使用中に作るロックファイルがあれば、そのパスを返す。

        .accdb なら ``.laccdb``、.mdb なら ``.ldb`` が隣に作られる。
        存在する場合は誰かが開いている (= 書き込みの可能性がある) 目印になる。
        """
        target = Path(path)
        suffix = ".laccdb" if target.suffix.lower() == ".accdb" else ".ldb"
        candidate = target.with_suffix(suffix)
        return candidate if candidate.exists() else None

    @staticmethod
    def in_use(path: str | Path) -> bool:
        """他の利用者が Access でこのファイルを開いているとみられるか。"""
        return AccdbReader.lock_file(path) is not None

    # -- コンテキストマネージャ (ファイルは既に読み込み済みなので解放のみ) --
    def __enter__(self) -> "AccdbReader":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._data = b""
        self._tables = None
        self._tdef_cache.clear()

    # ------------------------------------------------------------------
    # ヘッダ検証
    # ------------------------------------------------------------------
    def _validate_header(self) -> None:
        d = self._data
        if len(d) < PAGE_SIZE:
            raise AccdbError(f"ファイルが小さすぎます ({len(d)} バイト): {self.path}")
        if d[0:4] != b"\x00\x01\x00\x00":
            raise AccdbError(f"Access ファイルではありません: {self.path}")
        magic = d[4:19]
        if not (magic.startswith(b"Standard Jet") or magic.startswith(b"Standard ACE")):
            raise AccdbError(f"未知のフォーマット識別子です: {magic!r}")
        self.jet_version = _u32(d, 0x14)
        if self.jet_version == 0:
            raise AccdbError(
                "Jet3 (Access 97) 形式は未対応です。Access 2000 以降の形式へ変換してください。"
            )
        # 暗号化(DB パスワード)されたファイルはページ 0 以外が復号できないため弾く。
        # 平文の DB ではページ 2 に必ず MSysObjects の TDEF が存在する。
        if len(d) >= 3 * PAGE_SIZE and d[2 * PAGE_SIZE] != PAGE_TABLE_DEF:
            raise AccdbError(
                "暗号化されているか破損している可能性があります "
                "(システムテーブルを読み取れません)。"
                "パスワード付き DB の場合は Access で解除してから再実行してください。"
            )

    # ------------------------------------------------------------------
    # ページアクセス
    # ------------------------------------------------------------------
    @property
    def page_count(self) -> int:
        return len(self._data) // PAGE_SIZE

    def _page(self, number: int) -> bytes:
        if number < 0 or number >= self.page_count:
            raise AccdbError(f"ページ番号が範囲外です: {number}")
        return self._data[number * PAGE_SIZE : (number + 1) * PAGE_SIZE]

    def _tdef_bytes(self, page: int) -> bytes:
        """テーブル定義は複数ページに連鎖することがあるため連結して返す。"""
        chunks: list[bytes] = []
        seen: set[int] = set()
        current = page
        while current and current not in seen:
            seen.add(current)
            buf = self._page(current)
            if buf[0] != PAGE_TABLE_DEF:
                raise AccdbError(f"ページ {current} はテーブル定義ページではありません")
            nxt = _u32(buf, 4)
            # 継続ページは 8 バイトのヘッダを読み飛ばして連結する
            chunks.append(buf if not chunks else buf[8:])
            current = nxt
        return b"".join(chunks)

    # ------------------------------------------------------------------
    # テーブル定義の解析
    # ------------------------------------------------------------------
    def _parse_tdef(self, page: int, name: str = "") -> Table:
        if page in self._tdef_cache:
            table = self._tdef_cache[page]
            if name and not table.name:
                table.name = name
            return table

        b = self._tdef_bytes(page)
        row_count = _u32(b, _TAB_NUM_ROWS)
        num_cols = _u16(b, _TAB_NUM_COLS)
        num_real_idx = _u32(b, _TAB_NUM_RIDXS)

        cursor = _TAB_COLS_START + num_real_idx * _TAB_RIDX_ENTRY_SIZE
        needed = cursor + num_cols * _COL_ENTRY_SIZE
        if needed > len(b):
            raise AccdbError(
                f"テーブル定義が壊れています (page={page}, 必要={needed}, 実際={len(b)})"
            )

        columns: list[Column] = []
        for i in range(num_cols):
            off = cursor + i * _COL_ENTRY_SIZE
            flags = _u8(b, off + _COL_FLAGS)
            columns.append(
                Column(
                    name="",
                    index=_u16(b, off + _COL_NUM),
                    type_code=_u8(b, off + _COL_TYPE),
                    size=_u16(b, off + _COL_SIZE),
                    fixed=bool(flags & _COL_FLAG_FIXED),
                    fixed_offset=_u16(b, off + _COL_FIXED_OFFSET),
                    var_index=_u16(b, off + _COL_VAR_OFFSET),
                )
            )

        # 列定義の直後に、列名が (長さ2バイト + UTF-16LE) の並びで続く
        pos = cursor + num_cols * _COL_ENTRY_SIZE
        for col in columns:
            if pos + 2 > len(b):
                raise AccdbError(f"列名の読み取りに失敗しました (page={page})")
            length = _u16(b, pos)
            pos += 2
            col.name = _decode_text(b[pos : pos + length])
            pos += length

        table = Table(name=name, tdef_page=page, row_count=row_count, columns=columns)
        self._tdef_cache[page] = table
        return table

    # ------------------------------------------------------------------
    # システムカタログ (MSysObjects) からテーブル一覧を得る
    # ------------------------------------------------------------------
    def _load_catalog(self) -> dict[str, Table]:
        if self._tables is not None:
            return self._tables

        catalog = self._parse_tdef(2, "MSysObjects")
        tables: dict[str, Table] = {}
        for row in self._iter_rows(catalog):
            obj_type = row.get("Type")
            name = row.get("Name")
            obj_id = row.get("Id")
            if not isinstance(name, str) or obj_id is None:
                continue
            # Type==1 がローカルテーブル。Id の下位 24 ビットが TDEF ページ番号。
            if obj_type != 1:
                continue
            tdef_page = int(obj_id) & 0x00FFFFFF
            if tdef_page <= 0 or tdef_page >= self.page_count:
                continue
            try:
                if self._page(tdef_page)[0] != PAGE_TABLE_DEF:
                    continue
                tables[name] = self._parse_tdef(tdef_page, name)
            except AccdbError:
                continue

        self._tables = tables
        return tables

    # ------------------------------------------------------------------
    # 公開 API
    # ------------------------------------------------------------------
    def table_names(self, include_system: bool = False) -> list[str]:
        """テーブル名の一覧を返す (既定ではシステムテーブルを除外)。"""
        names = sorted(self._load_catalog())
        if include_system:
            return names
        return [n for n in names if not n.startswith(("MSys", "~"))]

    def table(self, name: str) -> Table:
        tables = self._load_catalog()
        if name not in tables:
            raise AccdbError(
                f"テーブル '{name}' が見つかりません。"
                f"存在するテーブル: {', '.join(self.table_names()) or '(なし)'}"
            )
        return tables[name]

    def rows(self, name: str) -> Iterator[dict[str, Any]]:
        """指定テーブルの全レコードを dict で列挙する。"""
        yield from self._iter_rows(self.table(name))

    # ------------------------------------------------------------------
    # データページ走査
    # ------------------------------------------------------------------
    def _data_pages(self, tdef_page: int) -> list[int]:
        """指定テーブルに属するデータページを列挙する。

        テーブル定義が持つ「使用ページマップ」を第一の根拠とする。
        全ページ走査 (所属 TDEF 番号が一致するページを拾う) だけでは、
        テーブルから切り離された古いページまで拾ってしまい
        件数が実際より多くなるため、必ずマップと突き合わせる。
        """
        mapped = self._usage_map_pages(tdef_page)
        if mapped is None:
            # マップを解釈できない場合のみ、全ページ走査へ退避する
            return [
                pg
                for pg in range(self.page_count)
                if self._is_data_page_of(pg, tdef_page)
            ]
        return [pg for pg in sorted(mapped) if self._is_data_page_of(pg, tdef_page)]

    def _is_data_page_of(self, pg: int, tdef_page: int) -> bool:
        """ページ pg が、指定テーブルのデータページかどうか。"""
        if pg <= 0 or pg >= self.page_count:
            return False
        base = pg * PAGE_SIZE
        return self._data[base] == PAGE_DATA and _u32(self._data, base + 4) == tdef_page

    def _usage_map_pages(self, tdef_page: int) -> set[int] | None:
        """使用ページマップからページ番号の集合を返す。解釈できなければ None。"""
        try:
            b = self._tdef_bytes(tdef_page)
            pointer = _u32(b, _TAB_USAGE_MAP)
            # 下位 8 ビットが行番号、上位 24 ビットがページ番号
            row_num, page_num = pointer & 0xFF, pointer >> 8
            row = self._read_page_row(page_num, row_num)
            if row is None:
                return None
            return self._parse_usage_map(row)
        except (AccdbError, struct.error, IndexError):
            return None

    def _read_page_row(self, page_num: int, row_num: int) -> bytes | None:
        """データページ上の指定行のバイト列を取り出す (使用ページマップ用)。"""
        if page_num <= 0 or page_num >= self.page_count:
            return None
        page = self._page(page_num)
        num_rows = _u16(page, 0x0C)
        if row_num >= num_rows or 0x0E + num_rows * 2 > PAGE_SIZE:
            return None
        offsets = [_u16(page, 0x0E + i * 2) for i in range(num_rows)]
        start = offsets[row_num] & _ROW_OFFSET_MASK
        end = PAGE_SIZE if row_num == 0 else (offsets[row_num - 1] & _ROW_OFFSET_MASK)
        if not (0 < start < end <= PAGE_SIZE):
            return None
        return page[start:end]

    def _parse_usage_map(self, raw: bytes) -> set[int] | None:
        """使用ページマップ 1 行分を解釈してページ番号の集合を返す。

        先頭 1 バイトが種別::

            0x00 : インライン形式。続く 4 バイトが開始ページ番号、
                   その後がページ使用状況のビットマップ。
            0x01 : 参照形式。続く 4 バイトずつがビットマップページ(種別 0x05)への
                   ポインタで、i 番目が担当するページ範囲は
                   i * (ページサイズ - 4) * 8 から始まる。
        """
        if not raw:
            return None
        pages: set[int] = set()

        if raw[0] == 0x00:
            if len(raw) < 5:
                return None
            start_page = _u32(raw, 1)
            pages.update(_bitmap_pages(raw[5:], start_page))
            return pages

        if raw[0] == 0x01:
            bits_per_page = (PAGE_SIZE - 4) * 8
            for i in range((len(raw) - 1) // 4):
                bitmap_page = _u32(raw, 1 + i * 4)
                if not bitmap_page or bitmap_page >= self.page_count:
                    continue
                page = self._page(bitmap_page)
                if page[0] != PAGE_USAGE_BITMAP:
                    continue
                pages.update(_bitmap_pages(page[4:], i * bits_per_page))
            return pages

        return None

    def _iter_rows(self, table: Table) -> Iterator[dict[str, Any]]:
        for pg in self._data_pages(table.tdef_page):
            page = self._page(pg)
            num_rows = _u16(page, 0x0C)
            # 行オフセット表がページに収まらない場合は壊れたページとして無視する
            if num_rows <= 0 or 0x0E + num_rows * 2 > PAGE_SIZE:
                continue
            offsets = [_u16(page, 0x0E + i * 2) for i in range(num_rows)]
            for i, raw_off in enumerate(offsets):
                if raw_off & _ROW_FLAG_DELETED:
                    continue  # 削除済みの行 (領域は再利用待ち)
                start = raw_off & _ROW_OFFSET_MASK
                end = PAGE_SIZE if i == 0 else (offsets[i - 1] & _ROW_OFFSET_MASK)
                if not (0 < start < end <= PAGE_SIZE):
                    continue
                row = self._parse_row(page[start:end], table)
                if row is not None:
                    yield row

    def _parse_row(self, raw: bytes, table: Table) -> dict[str, Any] | None:
        """1 行分のバイト列を dict に変換する。

        Jet4 / ACE の行レイアウト::

            [0:2]                       列数 (int16)
            [2 + col.fixed_offset ...]  固定長列のデータ
            ...                         可変長列のデータ
            [.. 逆順の可変長オフセット表 (可変長列数+1 個の int16) ..]
            [末尾-bitmask-2 : 末尾-bitmask]  可変長列数 (int16)
            [末尾-bitmask : 末尾]            NULL ビットマスク (1=非NULL)
        """
        n = len(raw)
        if n < 4:
            return None
        num_cols = _u16(raw, 0)
        # 行が持つ列数はテーブル定義の列数と一致しないことがある。
        #   * 列を削除した場合 : 既存行には削除前の列数が残る (定義より多い)
        #   * 列を追加した場合 : 既存行は追加前の列数のまま (定義より少ない)
        # そのため一致を要求せず、Access の上限(255列)による妥当性検査だけ行う。
        # これにより、壊れた行や旧スキーマの残骸は弾きつつ、正常な行は取りこぼさない。
        if not 0 < num_cols <= _MAX_COLUMNS:
            return None

        bitmask_size = (num_cols + 7) // 8
        var_count_pos = n - bitmask_size - 2
        if var_count_pos < 2:
            return None
        null_mask = raw[n - bitmask_size : n]
        num_var = _u16(raw, var_count_pos)

        var_offsets: list[int] = []
        if num_var:
            table_start = var_count_pos - (num_var + 1) * 2
            if table_start < 2:
                return None
            # オフセット表は逆順に格納されている
            for i in range(num_var + 1):
                var_offsets.append(_u16(raw, var_count_pos - (i + 1) * 2))

        def is_null(col_index: int) -> bool:
            byte_i, bit_i = divmod(col_index, 8)
            if byte_i >= len(null_mask):
                return True
            return not (null_mask[byte_i] & (1 << bit_i))

        result: dict[str, Any] = {}
        for col in table.columns:
            # この行が書かれた後に追加された列は、行に値を持たない
            if col.index >= num_cols:
                result[col.name] = None
                continue
            try:
                result[col.name] = self._read_value(
                    raw, col, var_offsets, is_null(col.index)
                )
            except (struct.error, IndexError, ValueError):
                result[col.name] = None
        return result

    def _read_value(
        self, raw: bytes, col: Column, var_offsets: list[int], null: bool
    ) -> Any:
        # BOOL は値そのものを NULL ビットマスクで表現する (ビットが立っていれば True)
        if col.type_code == TYPE_BOOL:
            return not null
        if null:
            return None

        if col.fixed:
            size = _FIXED_SIZE.get(col.type_code, col.size)
            start = 2 + col.fixed_offset
            data = raw[start : start + size]
            if len(data) < size:
                return None
            return self._decode_fixed(col, data)

        # 可変長列: var_index 番目のオフセット範囲を切り出す
        idx = col.var_index
        if idx + 1 >= len(var_offsets):
            return None
        start, end = var_offsets[idx], var_offsets[idx + 1]
        if not (0 <= start <= end <= len(raw)):
            return None
        data = raw[start:end]
        if not data:
            return "" if col.type_code == TYPE_TEXT else None
        return self._decode_variable(col, data)

    def _decode_fixed(self, col: Column, data: bytes) -> Any:
        t = col.type_code
        if t == TYPE_BYTE:
            return data[0]
        if t == TYPE_INT:
            return _i16(data, 0)
        if t == TYPE_LONGINT or t == TYPE_COMPLEX:
            return _i32(data, 0)
        if t == TYPE_FLOAT:
            return struct.unpack_from("<f", data, 0)[0]
        if t == TYPE_DOUBLE:
            return struct.unpack_from("<d", data, 0)[0]
        if t == TYPE_MONEY:
            return struct.unpack_from("<q", data, 0)[0] / 10000.0
        if t == TYPE_DATETIME:
            return _decode_datetime(data)
        if t == TYPE_NUMERIC:
            return _decode_numeric(data)
        if t == TYPE_REPID:
            return str(_format_guid(data))
        return data

    def _decode_variable(self, col: Column, data: bytes) -> Any:
        t = col.type_code
        if t == TYPE_TEXT:
            return _decode_text(data)
        if t in (TYPE_MEMO, TYPE_OLE):
            return self._read_lval(data, as_text=(t == TYPE_MEMO))
        if t == TYPE_BINARY:
            return data
        # 数値/日付などのスカラ型が可変長領域に置かれることがある
        # (システムテーブルの MSysNavPaneObjectIDs.Id など)。
        # 格納場所ではなく列の型に従ってデコードする。
        if t in _FIXED_SIZE:
            size = _FIXED_SIZE[t]
            if len(data) >= size:
                return self._decode_fixed(col, data[:size])
            return None
        return data

    # ------------------------------------------------------------------
    # LVAL (メモ型 / OLE 型) の読み取り
    # ------------------------------------------------------------------
    def _read_lval(self, descriptor: bytes, as_text: bool) -> Any:
        """メモ型/OLE 型の値を復元する。

        先頭 4 バイトが長さ + フラグ。
          0x80000000 : 12 バイト目以降にインライン格納
          0x40000000 : LVAL ページ 1 枚に格納
          それ以外   : LVAL ページを連鎖して格納
        """
        if len(descriptor) < 12:
            return _decode_text(descriptor) if as_text else descriptor
        header = _u32(descriptor, 0)
        length = header & 0x00FFFFFF
        payload: bytes

        if header & 0x80000000:
            payload = descriptor[12 : 12 + length]
        else:
            # ページ参照は「下位 8 ビット = 行番号 / 上位 24 ビット = ページ番号」
            # (使用ページマップのポインタと同じ形式)
            pointer = _u32(descriptor, 4)
            row_num = pointer & 0xFF
            page_num = pointer >> 8
            single = bool(header & 0x40000000)
            try:
                payload = self._read_lval_pages(page_num, row_num, length, single)
            except AccdbError:
                return None

        return _decode_text(payload) if as_text else payload

    def _read_lval_pages(
        self, page_num: int, row_num: int, length: int, single: bool
    ) -> bytes:
        out = bytearray()
        guard = 0
        while page_num and len(out) < length and guard < 4096:
            guard += 1
            page = self._page(page_num)
            num_rows = _u16(page, 0x0C)
            if row_num >= num_rows:
                break
            offsets = [_u16(page, 0x0E + i * 2) for i in range(num_rows)]
            start = offsets[row_num] & _ROW_OFFSET_MASK
            end = PAGE_SIZE if row_num == 0 else (offsets[row_num - 1] & _ROW_OFFSET_MASK)
            chunk = page[start:end]
            if single:
                out += chunk
                break
            # 連鎖形式では先頭 4 バイトが次の格納先へのポインタ
            if len(chunk) < 4:
                break
            nxt = _u32(chunk, 0)
            out += chunk[4:]
            row_num, page_num = nxt & 0xFF, nxt >> 8
        return bytes(out[:length])


def _read_stable(path: str, attempts: int = 4) -> bytes:
    """ファイル全体を読み込む。読んでいる途中で更新された場合は読み直す。

    VBA は ADO 経由だったため Jet のファイルロックに従っていたが、
    このリーダはファイルを直接読むため、他の PC が書き込んでいる最中だと
    ページ単位で不整合な内容を読んでしまう可能性がある。
    そこで「読む前後で 更新日時とサイズが変わっていないこと」を確認し、
    変わっていた場合は少し待って読み直す。
    """
    for attempt in range(attempts):
        before = os.stat(path)
        with open(path, "rb") as fh:
            data = fh.read()
        after = os.stat(path)
        if (before.st_mtime_ns, before.st_size) == (after.st_mtime_ns, after.st_size):
            return data
        # 書き込み中とみられるので、間隔を空けて読み直す
        time.sleep(0.2 * (attempt + 1))
    raise AccdbError(
        f"読み取り中にファイルが更新され続けています: {path}\n"
        "他の利用者が Access で編集している可能性があります。"
        "しばらく待ってからもう一度取り込んでください。"
    )


def _bitmap_pages(bitmap: bytes, start_page: int) -> Iterator[int]:
    """使用状況ビットマップで立っているビットを、ページ番号として列挙する。"""
    for index, byte in enumerate(bitmap):
        if not byte:
            continue
        for bit in range(8):
            if byte & (1 << bit):
                yield start_page + index * 8 + bit


def _format_guid(data: bytes) -> str:
    if len(data) < 16:
        return ""
    d1 = _u32(data, 0)
    d2 = _u16(data, 4)
    d3 = _u16(data, 6)
    rest = data[8:16].hex()
    return f"{{{d1:08X}-{d2:04X}-{d3:04X}-{rest[:4].upper()}-{rest[4:].upper()}}}"
