#!/usr/bin/env python3
"""看板マスタ.accdb を共有 SQLite へ変換する(移行時に 1 回だけ使う)

Access をやめて共有 SQLite へ移るときの、最初で最後の一歩です。読み取りは
本パッケージ内蔵のリーダー(:mod:`kanban.accdb.reader`)で行うので、
**ACE OLEDB も ODBC も要りません**(Linux の検証機でも動きます)。

使い方::

    python tools/accdb_to_sqlite.py 看板マスタ.accdb 看板マスタ.sqlite3
    python tools/accdb_to_sqlite.py 看板マスタ.accdb 看板マスタ.sqlite3 --force
    python tools/accdb_to_sqlite.py 看板マスタ.accdb --check   # 中身を見るだけ

【変換するもの】
``看板_<ライン>`` と ``Form状態管理`` を、**列名も値もそのまま**移します。
列の型は Access の型カテゴリ(NUMBER / DATE / TEXT)を見て宣言し直します
── :class:`kanban.db.shared.SharedDb` が ``PRAGMA table_info`` から型
カテゴリを復元するので、ここで宣言しておかないと日時が文字列扱いになります。

【元のファイルは触りません】
読み取り専用で開き、出力先へ新しく作ります。切り戻したくなったら、設定の
接続先を .accdb へ戻すだけで済むようにしてあります(変換は何度でもやり直せます)。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from kanban import config  # noqa: E402
from kanban.accdb import reader as filereader  # noqa: E402
from kanban.accdb.types import (  # noqa: E402
    CATEGORY_DATE,
    CATEGORY_NUMBER,
    CATEGORY_TEXT,
)

#: 型カテゴリ -> SQLite の宣言型。``SharedDb._categories`` が読み戻す
_DECL = {
    CATEGORY_NUMBER: "NUMERIC",
    CATEGORY_DATE: "DATETIME",
    CATEGORY_TEXT: "TEXT",
}


def target_tables(names: list[str]) -> list[str]:
    """変換するテーブル。看板と状態管理だけで、他は持っていかない。"""
    out = [n for n in names if n.startswith(config.KANBAN_TABLE_PREFIX)]
    if config.TABLE_STATE in names:
        out.append(config.TABLE_STATE)
    return out


def describe(accdb_path: str) -> int:
    """中身を見るだけ(変換しない)。"""
    with filereader.AccdbReader(accdb_path) as db:
        names = db.table_names()
    picked = target_tables(names)
    print(f"Access: {accdb_path}")
    print(f"テーブル総数: {len(names)} / 変換対象: {len(picked)}")
    for name in picked:
        table = filereader.read_table(accdb_path, name)
        print(f"  {name:<20} 列={len(table.columns):<3} 行={len(table.rows)}")
    skipped = sorted(set(names) - set(picked))
    if skipped:
        print("\n変換しないもの(看板システムが使っていないテーブル):")
        for name in skipped:
            print(f"  {name}")
    return 0


def _out_state(out: Path) -> str:
    """出力先の様子。``"none"``(無いと確かめた)/ ``"file"`` / それ以外は**確かめられない理由**。

    「あるか」を ``exists()`` で見ると、確かめられない(共有フォルダに届かない・権限)ときに
    「無い」と答えることがある。そう取り違えると、あるファイルを --force 無しで作り直しに行く。
    """
    try:
        os.stat(out)
        return "file"
    except (FileNotFoundError, NotADirectoryError):
        pass
    except OSError as exc:
        return f"出力先を確かめられませんでした: {out}({exc})"
    try:
        if os.path.isdir(out.parent):
            return "none"
        os.stat(out.parent)
    except OSError as exc:
        return f"出力先のフォルダに届きません: {out.parent}({exc})"
    return f"出力先のフォルダが見つかりません: {out.parent}"


def convert(accdb_path: str, out_path: str, *, force: bool = False) -> int:
    out = Path(out_path)
    state = _out_state(out)
    if state == "file" and not force:
        print(f"[エラー] 出力先が既にあります: {out}\n"
              "  上書きしてよければ --force を付けてください。", file=sys.stderr)
        return 1
    if state not in ("file", "none"):
        # **フォルダを作りに行かない。** 共有フォルダが一瞬見えないだけのとき、そこへ空の
        # フォルダ・ファイルを作ってしまう
        print(f"[エラー] {state}\n  共有フォルダに届いているか確かめてから、もう一度実行してください。",
              file=sys.stderr)
        return 1

    with filereader.AccdbReader(accdb_path) as db:
        names = db.table_names()
    picked = target_tables(names)
    if not picked:
        print("[エラー] 看板テーブルが見つかりませんでした。", file=sys.stderr)
        return 1

    # **いまのファイルを消してから書かない。** 同じフォルダの一時ファイルへ書き上げてから
    # 置き換える。前は先に消して出力先へ直接書いていたので、変換の途中(Access を読み切れない・
    # 落ちた)には、ほかの PC から共有DBが「無い」「空」「途中まで」に見えた
    fd, name = tempfile.mkstemp(prefix=f"{out.stem}.", suffix=".converting", dir=str(out.parent))
    os.close(fd)
    tmp = Path(name)
    total = 0
    try:
        conn = sqlite3.connect(str(tmp))
        try:
            # **WAL にしない。** 共有フォルダに置く前提のファイルなので、
            # 共有メモリを使うモードにすると SMB の上で開けなくなる
            conn.execute("PRAGMA journal_mode = DELETE")
            for name in picked:
                table = filereader.read_table(accdb_path, name)
                rows = _copy_table(conn, table)
                total += rows
                print(f"  {name:<20} {rows} 行")
            conn.commit()
        finally:
            conn.close()
        _replace(tmp, out)
    except BaseException:
        for leftover in (tmp, Path(str(tmp) + "-journal")):
            try:
                leftover.unlink()
            except OSError:
                pass
        raise

    print(f"\n変換しました: {out}  (テーブル {len(picked)} / 行 {total})")
    print("\n次の手順:")
    print("  1. このファイルを共有フォルダへ置く")
    print("  2. 設定画面の「接続先」を、このファイルのパスに変える")
    print("  3. 全端末でアプリを開き直す")
    return 0


def _replace(src: Path, dst: Path, tries: int = 10, wait: float = 0.5) -> None:
    """置き換える。**Windows で一瞬断られたら、少し待ってやり直す**(ほかの PC・ウイルス対策が
    開いている瞬間は ``PermissionError`` になる)。"""
    for attempt in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt + 1 >= tries:
                raise
            time.sleep(wait)


def _copy_table(conn: sqlite3.Connection, table) -> int:
    """1 テーブルを作って中身を入れる。

    列名も値も**そのまま**移す。看板テーブルは ``管理番号`` を主キーに
    したいところだが、Access 側で重複や欠落があると変換自体が落ちるので、
    ここでは制約を付けずに写す(整合性の確認は取り込み側が行う)。
    """
    cols = table.columns
    decls = ", ".join(
        f"[{c.replace(']', ']]')}] {_DECL.get(table.categories.get(c, CATEGORY_TEXT), 'TEXT')}"
        for c in cols
    )
    ident = "[" + table.name.replace("]", "]]") + "]"
    conn.execute(f"CREATE TABLE {ident} ({decls})")

    placeholders = ", ".join("?" for _ in cols)
    names = ", ".join("[" + c.replace("]", "]]") + "]" for c in cols)
    conn.executemany(
        f"INSERT INTO {ident} ({names}) VALUES ({placeholders})",
        [tuple(_value(row.get(c)) for c in cols) for row in table.rows],
    )
    return len(table.rows)


def _value(value):
    """SQLite へ入れられる形にする(:func:`kanban.accdb.reader.to_sqlite_value`)。"""
    return filereader.to_sqlite_value(value)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="accdb_to_sqlite",
        description="看板マスタ.accdb を共有 SQLite へ変換する",
    )
    p.add_argument("accdb", help="変換元の .accdb")
    p.add_argument("out", nargs="?", help="出力先の .sqlite3")
    p.add_argument("--force", action="store_true", help="出力先を上書きする")
    p.add_argument("--check", action="store_true", help="中身を見るだけ(変換しない)")
    args = p.parse_args(argv)

    if not Path(args.accdb).is_file():
        print(f"[エラー] 見つかりません: {args.accdb}", file=sys.stderr)
        return 1
    if args.check:
        return describe(args.accdb)
    if not args.out:
        print("[エラー] 出力先を指定してください(--check なら不要)。", file=sys.stderr)
        return 1
    return convert(args.accdb, args.out, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
