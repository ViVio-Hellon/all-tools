"""Access(ACE/Jet4) の型定義と共通ユーティリティ。

VBA 側の ``AdoTypeToCategory`` / ``GetFieldTypeCategory`` に相当する型分類を
Python 側でも 1 箇所に集約する。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any

# --- Access 内部の列型コード -------------------------------------------------
BOOL = 0x01
BYTE = 0x02
INT = 0x03
LONG = 0x04
MONEY = 0x05
FLOAT = 0x06
DOUBLE = 0x07
DATETIME = 0x08
BINARY = 0x09
TEXT = 0x0A
OLE = 0x0B
MEMO = 0x0C
REPID = 0x0F  # GUID
NUMERIC = 0x10
COMPLEX = 0x12

TYPE_NAMES = {
    BOOL: "BOOL",
    BYTE: "BYTE",
    INT: "INT",
    LONG: "LONG",
    MONEY: "MONEY",
    FLOAT: "FLOAT",
    DOUBLE: "DOUBLE",
    DATETIME: "DATETIME",
    BINARY: "BINARY",
    TEXT: "TEXT",
    OLE: "OLE",
    MEMO: "MEMO",
    REPID: "GUID",
    NUMERIC: "NUMERIC",
    COMPLEX: "COMPLEX",
}

# 固定長列のバイト数
FIXED_SIZES = {
    BOOL: 0,  # NULL ビットマスクに格納される
    BYTE: 1,
    INT: 2,
    LONG: 4,
    MONEY: 8,
    FLOAT: 4,
    DOUBLE: 8,
    DATETIME: 8,
    REPID: 16,
    NUMERIC: 17,
    COMPLEX: 4,
}

#: VBA の ``GetFieldTypeCategory`` と同じ 3 分類
CATEGORY_NUMBER = "NUMBER"
CATEGORY_DATE = "DATE"
CATEGORY_TEXT = "TEXT"

_NUMBER_TYPES = frozenset({BYTE, INT, LONG, MONEY, FLOAT, DOUBLE, NUMERIC, COMPLEX})
_DATE_TYPES = frozenset({DATETIME})


def type_category(access_type: int) -> str:
    """Access 型コードを NUMBER / DATE / TEXT に分類する。

    VBA の ``AdoTypeToCategory`` と同じ分類規則。BOOL は Access では数値
    (0/-1) として比較されるため NUMBER 扱いにする。
    """
    if access_type in _NUMBER_TYPES or access_type == BOOL:
        return CATEGORY_NUMBER
    if access_type in _DATE_TYPES:
        return CATEGORY_DATE
    return CATEGORY_TEXT


def sqlite_affinity(access_type: int) -> str:
    """Access 型コードに対応する SQLite の列宣言型。

    日付は VBA 実装が ``yyyy/mm/dd hh:nn:ss`` の文字列比較でソート・抽出して
    いるため、SQLite 側でも TEXT として保持する（書式は 1 箇所で統一）。
    """
    if access_type in (BYTE, INT, LONG, BOOL, COMPLEX):
        return "INTEGER"
    if access_type in (FLOAT, DOUBLE, MONEY, NUMERIC):
        return "REAL"
    if access_type in (BINARY, OLE):
        return "BLOB"
    return "TEXT"


# Access(OLE Automation) の日付シリアル値の基準日
ACCESS_EPOCH = _dt.datetime(1899, 12, 30)

#: DB に書き込む日時書式。VBA の ``NowDBString`` と一致させる
DB_DATETIME_FORMAT = "%Y/%m/%d %H:%M:%S"


def serial_to_datetime(serial: float) -> _dt.datetime:
    """OLE Automation 日付シリアル値を datetime へ変換する。"""
    return ACCESS_EPOCH + _dt.timedelta(days=serial)


def datetime_to_serial(value: _dt.datetime) -> float:
    """datetime を OLE Automation 日付シリアル値へ変換する。"""
    return (value - ACCESS_EPOCH).total_seconds() / 86400.0


@dataclass(frozen=True)
class Column:
    """テーブルの 1 列の定義。"""

    name: str
    index: int
    """テーブル定義上の列順(0 起点)。VBA の列インデックスに対応する。"""

    access_type: int
    length: int = 0
    is_variable: bool = False
    is_auto_number: bool = False
    is_fixed_width: bool = False
    scale: int = 0
    """十進型(Decimal)の小数点の位置。ほかの型では 0。"""

    @property
    def type_name(self) -> str:
        return TYPE_NAMES.get(self.access_type, f"UNKNOWN({self.access_type:#x})")

    @property
    def category(self) -> str:
        return type_category(self.access_type)

    @property
    def sqlite_type(self) -> str:
        return sqlite_affinity(self.access_type)


@dataclass
class Table:
    """テーブル 1 つ分の読み取り結果。"""

    name: str
    columns: list[Column] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)

    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    def column(self, name: str) -> Column | None:
        for c in self.columns:
            if c.name == name:
                return c
        return None
