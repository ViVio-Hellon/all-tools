#!/usr/bin/env python3
"""共有の取り込み元 (sqlite3) から手元の SQLite へ取り込むコマンドライン版。

GUI を使わずに取り込みだけ行いたい場合や、
定期実行 (タスクスケジューラ等) で使う場合はこちらを使う。

使い方::

    # 保存用 DB とマスタ DB をまとめて取り込む
    python tools/import_source.py 連絡帳.sqlite3 梱包資材マスタ.sqlite3

    # 班員名簿を CSV から取り込む
    python tools/import_source.py --members-csv 班員名簿.csv

    # 中身を確認するだけ (SQLite へは書き込まない)
    python tools/import_source.py --inspect 連絡帳.sqlite3
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from calendar_app import db  # noqa: E402
from calendar_app.importer import (  # noqa: E402
    PendingChangesError,
    import_source,
    import_members_csv,
)
from calendar_app.dbkit import source_db  # noqa: E402
from calendar_app.logging_utils import configure_logging  # noqa: E402


def inspect(path: str) -> int:
    """取り込み元の構造と件数を表示する (取り込みは行わない)。

    **開けなかったときは理由と次の一手まで出す。** 「読めません」だけでは
    現場もこちらも直せない (``source_db.Probe.hint``)。
    """
    from pathlib import Path as _Path

    result = source_db.probe(_Path(path))
    print(f"ファイル : {path}")
    print(f"状態     : {result.describe()}")
    if not result.ok:
        hint = result.hint()
        if hint:
            print(f"対処     : {hint}")
        for way, why in result.attempts:
            if why:
                print(f"    {way}: {why}")
        return 1

    print(f"開き方   : {result.opened_by} / journal={result.journal}")
    if not result.tables:
        print("テーブルが見つかりませんでした。")
        return 1
    try:
        with source_db.connect(path, read_only=True) as source:
            for name in result.tables:
                rows = source.query(
                    f"SELECT COUNT(*) AS n FROM {source_db.quote_identifier(name)}")
                count = next(iter(rows[0].values()), 0) if rows else 0
                print(f"\n[{name}] 行数={count}")
                for column in source.columns(name):
                    print(f"    {column}")
    except source_db.SourceError as exc:
        print(f"解析エラー: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Access -> SQLite 取り込み")
    parser.add_argument("paths", nargs="*", help="取り込む .sqlite3 / .db ファイル")
    parser.add_argument(
        "--members-csv", metavar="CSV", help="班員名簿を CSV から取り込む"
    )
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="テーブル構造と件数を表示するだけで取り込まない",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Access へ未反映の変更があっても取り込みを強行する",
    )
    parser.add_argument("--db", help="SQLite ファイルのパス (既定は設定に従う)")
    args = parser.parse_args(argv)

    if not args.paths and not args.members_csv:
        parser.error("取り込むファイルを指定してください。")

    if args.inspect:
        return max((inspect(p) for p in args.paths), default=1)

    configure_logging()
    conn = db.connect(args.db)
    status = 0

    for path in args.paths:
        try:
            # 未反映の確かめはファイルごとに行う(名簿だけのファイルは断らない)
            result = import_source(conn, path, force=args.force)
            print(result.describe())
        except PendingChangesError as exc:
            print(f"エラー: {exc}", file=sys.stderr)
            print("--force で未反映の変更を破棄して取り込めます。", file=sys.stderr)
            return 1
        except (source_db.SourceError, OSError) as exc:
            print(f"取り込みエラー ({path}): {exc}", file=sys.stderr)
            status = 1

    if args.members_csv:
        try:
            count = import_members_csv(conn, args.members_csv)
            print(f"班員名簿を {count} 件取り込みました ({args.members_csv})")
        except (ValueError, OSError) as exc:
            print(f"CSV 取り込みエラー: {exc}", file=sys.stderr)
            status = 1

    return status


if __name__ == "__main__":
    raise SystemExit(main())
