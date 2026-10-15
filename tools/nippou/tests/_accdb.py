"""試験用の小さな .accdb を作る(Access 2007 以降の形 ── ACE12)

    「日報データ.accdb から写す」ボタンを作ってよいか → お願いします

**本番の 日報データ.accdb は試験に使いません**(利用者の本番データなので)。かといって、
Access の入っていないこの環境では Access で作ることもできません。そこで、写すボタンが
読む部品(`nippou/accdb/reader.py` ── 看板の読み取りの写し)が読むのと**同じ物理の形**を、
ここで1から書きます。架空の値だけで作るので、匿名化も要りません
(看板・カレンダーの `tests/data/sample.accdb` は本物を匿名化したもの)。

書くのは読むのに要るところだけです:

    0 ページ目   ファイルの頭(Standard ACE DB・版・RC4 で隠した頭の欄)
    1 ページ目   使用状況マップ(表ごとに「どのページを持っているか」)
    2 ページ目   MSysObjects の表の定義(表の名前 → 表の定義のページ)
    3 ページ目〜 日報の表の定義、そのあとにデータのページ

**mdbtools でも同じ中身が読める**ことを `tests/test_accdb_copy.py` で確かめます
(mdb-export が入っている環境だけ)。読み取りの部品だけに合わせた形ではない、という印です。

できること(試験で使う):

    ・文字(ASCII だけの値は Access と同じ「Unicode 圧縮」で、ほかは UTF-16LE)・長整数・日時
    ・NULL(NULL マスク)
    ・消した行(VBA が保存し直すと、消した行がページに残る ── 行の頭の印 0xC000)
    ・表の定義の行数をわざとずらす(読み落としを見つける守りの試験)
    ・古い形(Jet3)・暗号化の印・壊れたページ
"""
from __future__ import annotations

import datetime as _dt
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

PAGE = 4096

TEXT = 0x0A
LONG = 0x04
INT = 0x03
DATETIME = 0x08
OLE = 0x0B

_FIXED = {LONG: 4, INT: 2, DATETIME: 8}

#: ACE12(Access 2007)。本物の 日報データ.accdb も mdb-ver で ACE12
VERSION_ACE12 = 0x02
VERSION_JET3 = 0x00

#: ファイルの頭のうち、RC4 で隠してある欄(0x18 から 128 バイト)の鍵
_HEADER_KEY = struct.pack("<I", 0x6B39DAC7)

#: Access の日付の基準日
_EPOCH = _dt.datetime(1899, 12, 30)


@dataclass
class Table:
    """1つの表。`columns` は (列名, 型) の並び。行は {列名: 値}。"""

    name: str
    columns: Sequence[tuple[str, int]]
    rows: list[dict[str, Any]] = field(default_factory=list)
    #: 消した行(ページには残るが読まない)
    deleted: list[dict[str, Any]] = field(default_factory=list)
    #: 表の定義に書く行数。None なら本当の行数
    defined_rows: Optional[int] = None
    system: bool = False


# ----------------------------------------------------------------------
# 日報の表(VBA `NippouDB_EnsureTables` の形 ── 本物の 日報データ.accdb と同じ列)
# ----------------------------------------------------------------------
HEADER_COLUMNS: tuple[tuple[str, int], ...] = (
    ("報告日", TEXT), ("ライン", TEXT), ("直", TEXT), ("頁", LONG), ("担当者", TEXT),
    ("昼勤", TEXT), ("枚数", TEXT), ("重量Kg", TEXT), ("Lot数", TEXT), ("係数Lot数", TEXT),
    ("理由", TEXT), ("保存日時", DATETIME),
)
#: 明細。**作業停止④⑤(S4〜TH5)は無い**(VBA の表には無い)
DETAIL_COLUMNS: tuple[tuple[str, int], ...] = (
    ("報告日", TEXT), ("ライン", TEXT), ("直", TEXT), ("頁", LONG), ("行番号", LONG),
    *((name, TEXT) for name in (
        "LOT", "ZAI", "SIZ", "KEN", "KZ", "KH", "SZ", "SH", "HIT", "AI",
        "MAI", "TUT", "VC", "ET", "S", "TH", "SS", "THS", "STH", "THT",
        "CON", "WEI", "TIM", "UNI",
        "Others1", "Others2", "Others3", "Others4", "Others5", "Others6", "係数")),
)
STOPS45: tuple[tuple[str, int], ...] = (("S4", TEXT), ("TH4", TEXT), ("S5", TEXT), ("TH5", TEXT))


def renamed(columns: Sequence[tuple[str, int]], old: str, new: str) -> tuple[tuple[str, int], ...]:
    """列の名前を1つ言い換えた並び(「頁」の代わりに「ページ」の表を作る、など)。"""
    return tuple((new if name == old else name, kind) for name, kind in columns)


def nippou_page(line: str, *, day: str = "2026年7月22日", shift: str = "1直", page: int = 1,
                saved: Optional[_dt.datetime] = None, worker: str = "青木",
                lots: Iterable[str] = ("H6081A1",), page_column: str = "頁",
                extra: Optional[dict[str, Any]] = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """VBA が書いた1ページ(見出し1行 + 明細)。値は架空のもの。"""
    saved = saved or _dt.datetime(2026, 7, 22, 15, 30, 0)
    header = {"報告日": day, "ライン": line, "直": shift, page_column: page, "担当者": worker,
              "昼勤": "", "枚数": "120", "重量Kg": "350", "Lot数": "1", "係数Lot数": "1",
              "理由": None, "保存日時": saved}
    details = []
    for number, lot in enumerate(lots, 1):
        row = {"報告日": day, "ライン": line, "直": shift, page_column: page, "行番号": number,
               "LOT": lot, "ZAI": "A1100-H14", "SIZ": "1.0×1000×2000", "KEN": "40",
               "KZ": "8", "KH": "30", "SZ": "9", "SH": "15", "HIT": "2", "AI": "有",
               "MAI": "40", "TUT": "1", "VC": "両面", "S": None, "TH": None,
               "CON": "40", "WEI": "116", "TIM": "45", "UNI": "2.9", "係数": "1"}
        row.update(extra or {})
        details.append(row)
    return header, details


# ----------------------------------------------------------------------
# 書く
# ----------------------------------------------------------------------
def _rc4(key: bytes, data: bytes) -> bytes:
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) & 0xFF
        s[i], s[j] = s[j], s[i]
    out = bytearray()
    i = j = 0
    for byte in data:
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out.append(byte ^ s[(s[i] + s[j]) & 0xFF])
    return bytes(out)


def _text(value: str) -> bytes:
    """Access の文字。1バイトで書ける字だけなら「Unicode 圧縮」(頭が FF FE)、ほかは UTF-16LE。"""
    if value and all(0 < ord(ch) < 0x100 for ch in value):
        return b"\xff\xfe" + value.encode("latin-1")
    return value.encode("utf-16-le")


def _fixed(kind: int, value: Any) -> bytes:
    if kind == LONG:
        return struct.pack("<i", int(value))
    if kind == INT:
        return struct.pack("<h", int(value))
    if kind == DATETIME:
        return struct.pack("<d", (value - _EPOCH).total_seconds() / 86400.0)
    raise ValueError(kind)


@dataclass
class _Layout:
    """列の並び(固定長の置き場所・可変長の番号)。"""

    columns: Sequence[tuple[str, int]]

    def __post_init__(self) -> None:
        self.fixed: list[tuple[int, str, int, int]] = []     # (列番号, 名前, 型, 置き場所)
        self.var: list[tuple[int, str, int, int]] = []       # (列番号, 名前, 型, 可変長の番号)
        offset = 0
        for number, (name, kind) in enumerate(self.columns):
            if kind in _FIXED:
                self.fixed.append((number, name, kind, offset))
                offset += _FIXED[kind]
            else:
                self.var.append((number, name, kind, len(self.var)))
        self.fixed_size = offset

    def row(self, values: dict[str, Any]) -> bytes:
        count = len(self.columns)
        mask = bytearray((count + 7) // 8)
        fixed = bytearray(self.fixed_size)
        for number, name, kind, offset in self.fixed:
            value = values.get(name)
            if value is None:
                continue
            mask[number // 8] |= 1 << (number % 8)
            fixed[offset:offset + _FIXED[kind]] = _fixed(kind, value)
        data = bytearray()
        offsets = []
        start = 2 + self.fixed_size
        for number, name, _kind, _index in self.var:
            offsets.append(start + len(data))
            value = values.get(name)
            if value is None:
                continue
            mask[number // 8] |= 1 << (number % 8)
            data += _text(str(value))
        offsets.append(start + len(data))
        table = b"".join(struct.pack("<H", o) for o in reversed(offsets))
        return (struct.pack("<H", count) + bytes(fixed) + bytes(data) + table
                + struct.pack("<H", len(self.var)) + bytes(mask))

    def tdef(self, name: str, rows: int, used_map: int, free_map: int, system: bool) -> bytes:
        """表の定義(1ページに収まる大きさだけ)。索引は持たない(VBA の表も持っていない)。"""
        head = bytearray(63)
        head[0], head[1] = 0x02, 0x01
        struct.pack_into("<I", head, 16, rows)
        head[40] = 0x53 if system else 0x4E
        struct.pack_into("<H", head, 41, len(self.columns))
        struct.pack_into("<H", head, 43, len(self.var))
        struct.pack_into("<H", head, 45, len(self.columns))
        struct.pack_into("<I", head, 47, 0)            # 索引の数
        struct.pack_into("<I", head, 51, 0)            # 本当の索引の数
        struct.pack_into("<I", head, 55, used_map)
        struct.pack_into("<I", head, 59, free_map)
        entries = bytearray()
        var_index = {n: i for n, _, _, i in self.var}
        fixed_offset = {n: o for n, _, _, o in self.fixed}
        for number, (col_name, kind) in enumerate(self.columns):
            entry = bytearray(25)
            entry[0] = kind
            struct.pack_into("<H", entry, 5, number)
            struct.pack_into("<H", entry, 7, var_index.get(number, 0))
            struct.pack_into("<H", entry, 9, number)
            if kind == TEXT:
                struct.pack_into("<H", entry, 11, 0x0409)  # 並び順(一般)
            entry[15] = 0x03 if kind in _FIXED else 0x02
            struct.pack_into("<H", entry, 21, fixed_offset.get(number, 0))
            struct.pack_into("<H", entry, 23, _FIXED.get(kind, 510))
            entries += entry
        names = bytearray()
        for col_name, _kind in self.columns:
            raw = col_name.encode("utf-16-le")
            names += struct.pack("<H", len(raw)) + raw
        body = bytes(head) + bytes(entries) + bytes(names)
        if len(body) > PAGE:
            raise ValueError(f"表の定義が1ページに収まりません: {name}")
        page = bytearray(PAGE)
        page[:len(body)] = body
        struct.pack_into("<I", page, 8, len(body))
        return bytes(page)


def _data_pages(owner: int, rows: list[tuple[bytes, bool]]) -> list[bytes]:
    """行をデータのページへ詰める(行0がページの最後から)。`(行のバイト, 消したか)`。"""
    pages: list[bytes] = []
    chunk: list[tuple[bytes, bool]] = []
    used = 14

    def flush() -> None:
        page = bytearray(PAGE)
        page[0], page[1] = 0x01, 0x01
        struct.pack_into("<I", page, 4, owner)
        struct.pack_into("<H", page, 12, len(chunk))
        end = PAGE
        for number, (raw, deleted) in enumerate(chunk):
            start = end - len(raw)
            page[start:end] = raw
            struct.pack_into("<H", page, 14 + number * 2, start | (0xC000 if deleted else 0))
            end = start
        struct.pack_into("<H", page, 2, end - (14 + 2 * len(chunk)))
        pages.append(bytes(page))

    for raw, deleted in rows:
        if chunk and used + len(raw) + 2 > PAGE:
            flush()
            chunk, used = [], 14
        chunk.append((raw, deleted))
        used += len(raw) + 2
    if chunk or not pages:
        flush()
    return pages


def _map_row(start: int, pages: Iterable[int], size: int) -> bytes:
    """使用状況マップ(インラインの形 0x00): [0x00][頭のページ][ビット並び]。"""
    bitmap = bytearray((size + 7) // 8)
    for page in pages:
        bitmap[(page - start) // 8] |= 1 << ((page - start) % 8)
    return b"\x00" + struct.pack("<I", start) + bytes(bitmap)


def build(tables: Sequence[Table], *, version: int = VERSION_ACE12, db_key: int = 0) -> bytes:
    """ファイル1つぶんのバイト列。"""
    catalog = Table("MSysObjects", (("Id", LONG), ("ParentId", LONG), ("Name", TEXT),
                                    ("Type", INT), ("Flags", LONG),
                                    # mdbtools は列が無いと目録を読まない(値は NULL でよい)
                                    ("LvProp", OLE)), system=True)
    every = [catalog, *tables]
    tdef_page = {t.name: 2 + i for i, t in enumerate(every)}
    catalog.rows = [{"Id": tdef_page[t.name], "ParentId": 0x0F000001, "Name": t.name,
                     "Type": 1, "Flags": -0x80000000 if t.system else 0} for t in every]
    layouts = {t.name: _Layout(t.columns) for t in every}

    next_page = 2 + len(every)
    data: dict[str, list[bytes]] = {}
    owned: dict[str, list[int]] = {}
    for t in every:
        rows = [(layouts[t.name].row(r), False) for r in t.rows]
        rows += [(layouts[t.name].row(r), True) for r in t.deleted]
        pages = _data_pages(tdef_page[t.name], rows)
        data[t.name] = pages
        owned[t.name] = list(range(next_page, next_page + len(pages)))
        next_page += len(pages)
    total = next_page

    # 1 ページ目: 表ごとに2行(使っているページ・空きのページ)
    map_rows: list[bytes] = []
    refs: dict[str, tuple[int, int]] = {}
    for t in every:
        refs[t.name] = ((1 << 8) | len(map_rows), (1 << 8) | (len(map_rows) + 1))
        map_rows.append(_map_row(0, owned[t.name], total))
        map_rows.append(_map_row(0, (), total))
    map_page = _data_pages(0, [(r, False) for r in map_rows])
    if len(map_page) != 1:
        raise ValueError("使用状況マップが1ページに収まりません")

    out = bytearray(total * PAGE)
    head = bytearray(PAGE)
    head[0:4] = b"\x00\x01\x00\x00"
    head[4:20] = (b"Standard Jet DB\x00" if version == VERSION_JET3 else b"Standard ACE DB\x00")
    struct.pack_into("<I", head, 0x14, version)
    hidden = bytearray(128)
    struct.pack_into("<I", hidden, 0x3E - 0x18, db_key)
    head[0x18:0x18 + 128] = _rc4(_HEADER_KEY, bytes(hidden))
    out[0:PAGE] = head
    out[PAGE:2 * PAGE] = map_page[0]
    for t in every:
        rows = len(t.rows) if t.defined_rows is None else t.defined_rows
        used, free = refs[t.name]
        tdef = layouts[t.name].tdef(t.name, rows, used, free, t.system)
        out[tdef_page[t.name] * PAGE:(tdef_page[t.name] + 1) * PAGE] = tdef
        for number, page in zip(owned[t.name], data[t.name]):
            out[number * PAGE:(number + 1) * PAGE] = page
    return bytes(out)


def write(path: Path, tables: Sequence[Table], **kwargs: Any) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build(tables, **kwargs))
    return path


def nippou_tables(line: str, pages: Iterable[tuple[dict[str, Any], list[dict[str, Any]]]], *,
                  page_column: str = "頁", stops45: bool = False,
                  table_line: Optional[str] = None) -> list[Table]:
    """そのラインの見出しの表と明細の表(VBA の形)。`table_line` は表の名前に使う名前。"""
    name = table_line or line
    header_cols = renamed(HEADER_COLUMNS, "頁", page_column)
    detail_cols = renamed(DETAIL_COLUMNS, "頁", page_column) + (STOPS45 if stops45 else ())
    header = Table(f"T_日報ヘッダー_{name}", header_cols)
    detail = Table(f"T_日報明細_{name}", detail_cols)
    for head, rows in pages:
        header.rows.append(head)
        detail.rows.extend(rows)
    return [header, detail]
