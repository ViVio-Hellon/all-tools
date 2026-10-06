#!/usr/bin/env python3
"""Access ファイルの中身を確認するための調査ツール。

トラブル対応時に「Access 側が実際にどうなっているか」を、Access を
インストールせずに確認するために使う。

使い方::

    python tools/dump_accdb.py 看板マスタ.accdb                 # テーブル一覧
    python tools/dump_accdb.py 看板マスタ.accdb 看板_LVC        # 内容を表示
    python tools/dump_accdb.py 看板マスタ.accdb 看板_LVC --csv  # CSV で出力
    python tools/dump_accdb.py 看板マスタ.accdb --check         # 全テーブルの件数検証
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kanban.accdb.reader import AccdbReader  # noqa: E402


def list_tables(db: AccdbReader, include_system: bool) -> None:
    for name in db.table_names(include_system=include_system):
        columns = db.columns(name)
        types = ", ".join(f"{c.name}:{c.type_name}" for c in columns)
        print(f"{name}\n    {types}")


def check_tables(db: AccdbReader, include_system: bool) -> int:
    """読み取り件数とテーブル定義上の件数を突き合わせる。"""
    mismatches = 0
    for name in db.table_names(include_system=include_system):
        tdef = db._table_def_by_name(name)
        rows = list(db._iter_rows(tdef))
        mark = ""
        if len(rows) != tdef.num_rows:
            mark = "  <<< 不一致"
            mismatches += 1
        print(f"{name:40s} 定義={tdef.num_rows:6d} 読取={len(rows):6d}{mark}")
    print(f"\n不一致: {mismatches} 件")
    return mismatches


def dump_table(db: AccdbReader, table_name: str, as_csv: bool, limit: int) -> None:
    table = db.read_table(table_name)
    names = table.column_names()
    rows = table.rows[:limit] if limit else table.rows

    if as_csv:
        writer = csv.DictWriter(sys.stdout, fieldnames=names, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("" if v is None else v) for k, v in row.items()})
        return

    print(f"# {table_name} ({len(table.rows)} 行)")
    print(" | ".join(names))
    print("-" * 80)
    for row in rows:
        print(" | ".join("" if row.get(n) is None else str(row.get(n)) for n in names))
    if limit and len(table.rows) > limit:
        print(f"... 他 {len(table.rows) - limit} 行")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Access ファイルの調査ツール")
    parser.add_argument("accdb", help="調べる .accdb / .mdb のパス")
    parser.add_argument("table", nargs="?", help="表示するテーブル名(省略時は一覧)")
    parser.add_argument("--csv", action="store_true", help="CSV 形式で出力する")
    parser.add_argument("--limit", type=int, default=0, help="表示する行数の上限")
    parser.add_argument("--system", action="store_true", help="システムテーブルも含める")
    parser.add_argument("--check", action="store_true", help="全テーブルの件数を検証する")
    args = parser.parse_args(argv)

    with AccdbReader(args.accdb) as db:
        print(
            f"# {args.accdb} (形式バージョン={db.version_byte:#x}, "
            f"ページ数={db.page_count})",
            file=sys.stderr,
        )
        if args.check:
            return 1 if check_tables(db, args.system) else 0
        if args.table:
            dump_table(db, args.table, args.csv, args.limit)
        else:
            list_tables(db, args.system)
    return 0


if __name__ == "__main__":
    sys.exit(main())
