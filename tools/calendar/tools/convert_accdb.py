#!/usr/bin/env python3
"""既存の Access (.accdb) を、取り込み元の sqlite3 へ変換する

**一度きりの移行のためのコマンド。** アプリ本体はもう .accdb を読まない
(取り込み元は sqlite3)。このツールだけが ``calendar_app.accdb.reader``
を使う。

使い方::

    # 連絡帳を変換する (同じフォルダに 連絡帳.sqlite3 ができる)
    python tools/convert_accdb.py 連絡帳.accdb

    # 出力先を指定する
    python tools/convert_accdb.py 連絡帳.accdb -o \\\\サーバ\\共有\\連絡帳.sqlite3

    # まとめて変換する
    python tools/convert_accdb.py 連絡帳.accdb 梱包資材マスタ.accdb -d 出力先フォルダ

    # 中身を見るだけ (書き込まない)
    python tools/convert_accdb.py --inspect 連絡帳.accdb

【何を変換するか】
カレンダーが読む3つの表だけ (``休み管理`` / ``削除履歴`` / ``班員名簿``)。
それ以外の表は「変換しなかった表」として報告する ── 黙って落とすと、
移行後に「あの表はどこへ行った」となる。

【変換後の形】
* 列名は .accdb と同じ (VBA からの引き継ぎ)
* ``ID`` は INTEGER PRIMARY KEY (Access のオートナンバーに対応)
* 日付・日時は **TEXT** で ``yyyy/mm/dd`` / ``yyyy/mm/dd hh:mm:ss``
  ── 手元の SQLite と同じ形にそろえる
* ``journal_mode=DELETE`` ── WAL は共有フォルダ(SMB)で開けない

【上書きしない】
出力先に既にファイルがあれば止まる。``--force`` を付けると、
**控えを取ってから**置き換える。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from calendar_app import config  # noqa: E402
from calendar_app.accdb.reader import AccdbError, AccdbReader  # noqa: E402
from calendar_app.logging_utils import configure_logging  # noqa: E402

#: 変換する表と、その作成 SQL。**カレンダーが読む3つだけ**
SCHEMA: dict[str, str] = {
    config.TABLE_DATA: f'''
        CREATE TABLE "{config.TABLE_DATA}" (
            "ID"        INTEGER PRIMARY KEY,
            "日付"       TEXT NOT NULL DEFAULT '',
            "区分"       TEXT NOT NULL DEFAULT '',
            "登録内容"    TEXT NOT NULL DEFAULT '',
            "識別コード"   TEXT NOT NULL DEFAULT '',
            "直"        TEXT NOT NULL DEFAULT '',
            "残業者"      TEXT NOT NULL DEFAULT '',
            "早出者"      TEXT NOT NULL DEFAULT '',
            "班"        TEXT NOT NULL DEFAULT '',
            "ライン"      TEXT NOT NULL DEFAULT ''
        )''',
    config.TABLE_DEL_HISTORY: f'''
        CREATE TABLE "{config.TABLE_DEL_HISTORY}" (
            "ID"        INTEGER PRIMARY KEY,
            "削除日時"    TEXT NOT NULL DEFAULT '',
            "削除実行者"   TEXT NOT NULL DEFAULT '',
            "対象日付"    TEXT NOT NULL DEFAULT '',
            "区分"       TEXT NOT NULL DEFAULT '',
            "登録内容"    TEXT NOT NULL DEFAULT '',
            "識別コード"   TEXT NOT NULL DEFAULT '',
            "班"        TEXT NOT NULL DEFAULT '',
            "ライン"      TEXT NOT NULL DEFAULT ''
        )''',
    config.TABLE_MEMBER: f'''
        CREATE TABLE "{config.TABLE_MEMBER}" (
            "管理番号"    TEXT PRIMARY KEY,
            "苗字"       TEXT NOT NULL DEFAULT '',
            "班"        TEXT NOT NULL DEFAULT '',
            "名前"       TEXT NOT NULL DEFAULT '',
            "読み"       TEXT NOT NULL DEFAULT '',
            "担当ライン"   TEXT NOT NULL DEFAULT ''
        )''',
}

#: 表ごとの列の順。**.accdb と同じ名前**で読む
COLUMNS: dict[str, tuple[str, ...]] = {
    config.TABLE_DATA: (
        "ID", "日付", "区分", "登録内容", "識別コード",
        "直", "残業者", "早出者", "班", "ライン"),
    config.TABLE_DEL_HISTORY: (
        "ID", "削除日時", "削除実行者", "対象日付", "区分",
        "登録内容", "識別コード", "班", "ライン"),
    config.TABLE_MEMBER: (
        "管理番号", "苗字", "班", "名前", "読み", "担当ライン"),
}

#: 日付として整える列 (値: 時刻も持つか)
DATE_COLUMNS: dict[str, dict[str, bool]] = {
    config.TABLE_DATA: {"日付": False},
    config.TABLE_DEL_HISTORY: {"削除日時": True, "対象日付": False},
}

#: 班員名簿は列名が環境で違うことがある。VBA は**列位置**で読んでいたので、
#: 名前で引けなければ同じ位置で拾う (元の importer と同じ扱い)
MEMBER_POSITIONS = ("管理番号", "苗字", "班", "名前", "読み", "担当ライン")


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _as_date(value: Any, *, with_time: bool) -> str:
    """Access の日付値を、手元の SQLite と同じ文字列にする。"""
    form = config.DATETIME_FORMAT if with_time else config.DATE_KEY_FORMAT
    if isinstance(value, _dt.datetime):
        return value.strftime(form)
    if isinstance(value, _dt.date):
        return _dt.datetime(value.year, value.month, value.day).strftime(form)
    return _text(value)


def _pick(row: dict[str, Any], name: str, names: list[str],
          position: int) -> Any:
    """名前で引き、駄目なら列位置で拾う (VBA の読み方)。"""
    if name in row:
        return row[name]
    if position < len(names):
        return row.get(names[position])
    return ""


def convert_table(reader: AccdbReader, out: sqlite3.Connection,
                  table: str) -> int:
    """表を1つ変換する。件数を返す。"""
    out.execute(SCHEMA[table])
    names = list(reader.table(table).column_names)
    dates = DATE_COLUMNS.get(table, {})
    columns = COLUMNS[table]
    marks = ", ".join("?" for _ in columns)
    cols = ", ".join(f'"{c}"' for c in columns)
    sql = f'INSERT OR REPLACE INTO "{table}" ({cols}) VALUES ({marks})'

    count = 0
    for row in reader.rows(table):
        values: list[Any] = []
        for position, column in enumerate(columns):
            if table == config.TABLE_MEMBER:
                raw = _pick(row, column, names,
                            MEMBER_POSITIONS.index(column))
            else:
                raw = row.get(column)

            if column == "ID":
                values.append(raw if isinstance(raw, int) else None)
            elif column in dates:
                values.append(_as_date(raw, with_time=dates[column]))
            else:
                values.append(_text(raw))

        if table == config.TABLE_MEMBER and not values[0]:
            # 管理番号が無い行は選択対象にできない (取り込みも捨てている)
            continue
        out.execute(sql, values)
        count += 1
    return count


def convert(source: Path, target: Path, *, force: bool = False) -> dict[str, Any]:
    """1つの .accdb を .sqlite3 へ変換する。"""
    if target.exists():
        if not force:
            raise SystemExit(
                f"[中止] 出力先が既にあります: {target}\n"
                "       上書きするなら --force を付けてください"
                "(控えを取ってから置き換えます)。")
        backup = target.with_name(
            f"{target.stem}_変換前_{_dt.datetime.now():%Y%m%d_%H%M%S}{target.suffix}")
        shutil.copyfile(target, backup)
        print(f"  控え: {backup}")
        target.unlink()

    report: dict[str, Any] = {"source": str(source), "target": str(target),
                              "tables": {}, "skipped": []}
    with AccdbReader(str(source)) as reader:
        available = set(reader.table_names())
        found = [t for t in SCHEMA if t in available]
        report["skipped"] = sorted(available - set(SCHEMA))
        if not found:
            raise SystemExit(
                f"[中止] {source.name} にカレンダーが使う表がありません。\n"
                f"       探した表: {', '.join(SCHEMA)}\n"
                f"       入っていた表: {', '.join(sorted(available)) or 'なし'}")

        out = sqlite3.connect(str(target))
        try:
            # **WAL にしない。** WAL は共有メモリを使うので、共有フォルダ
            # (SMB) に置いた瞬間そのファイルは誰からも開けなくなる
            out.execute("PRAGMA journal_mode = DELETE")
            with out:
                for table in found:
                    count = convert_table(reader, out, table)
                    report["tables"][table] = count
        finally:
            out.close()
    return report


def inspect(source: Path) -> None:
    """中身を見るだけ。書き込まない。"""
    with AccdbReader(str(source)) as reader:
        print(f"\n=== {source} ===")
        for name in reader.table_names():
            table = reader.table(name)
            count = sum(1 for _ in reader.rows(name))
            mark = "○" if name in SCHEMA else "  "
            print(f" {mark} {name}: {count} 行")
            print(f"      列: {', '.join(table.column_names)}")
        print("\n ○ = 変換する表")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Access(.accdb) を取り込み元の sqlite3 へ変換する")
    parser.add_argument("paths", nargs="+", help="変換する .accdb / .mdb")
    parser.add_argument("-o", "--out", metavar="FILE",
                        help="出力先のファイル (1つだけ変換するとき)")
    parser.add_argument("-d", "--out-dir", metavar="DIR",
                        help="出力先のフォルダ (既定は元ファイルと同じ場所)")
    parser.add_argument("--force", action="store_true",
                        help="出力先が既にあっても、控えを取って置き換える")
    parser.add_argument("--inspect", action="store_true",
                        help="中身を見るだけ (書き込まない)")
    args = parser.parse_args(argv)

    configure_logging()

    if args.out and len(args.paths) > 1:
        parser.error("--out は1つだけ変換するときに使ってください (複数なら --out-dir)")

    failed = False
    for raw in args.paths:
        source = Path(raw).expanduser()
        if not source.exists():
            print(f"[エラー] 見つかりません: {source}", file=sys.stderr)
            failed = True
            continue

        try:
            if args.inspect:
                inspect(source)
                continue

            if args.out:
                target = Path(args.out).expanduser()
            else:
                folder = Path(args.out_dir).expanduser() if args.out_dir else source.parent
                folder.mkdir(parents=True, exist_ok=True)
                target = folder / f"{source.stem}.sqlite3"

            print(f"\n変換: {source}\n   -> {target}")
            report = convert(source, target, force=args.force)
            for table, count in report["tables"].items():
                print(f"  {table}: {count} 件")
            if report["skipped"]:
                print(f"  変換しなかった表: {', '.join(report['skipped'])}")
        except AccdbError as exc:
            print(f"[エラー] {source.name} を読めません: {exc}", file=sys.stderr)
            failed = True
        except OSError as exc:
            print(f"[エラー] {source.name}: {exc}", file=sys.stderr)
            failed = True

    if not args.inspect and not failed:
        print("\n変換が終わりました。設定画面の「参照パス」に、"
              "出力先のフォルダを指定してください。")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
