"""SQLite connection helper.

WAL journal mode is enabled so waitress's request threads and any
background sync work (Access import/push) can read/write concurrently
without the
"database is locked" errors that plain rollback-journal mode is prone to
under this app's autosave-every-focus-loss access pattern. WAL setup goes
through :mod:`dbkit.sqlite_toolkit` so the "fall back quietly if a shared
folder can't provide WAL" handling is shared with the other VBA migration
projects instead of re-implemented here.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from dbkit.sqlite_toolkit import enable_wal

from .schema import ensure_schema


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        conn.row_factory = sqlite3.Row
        enable_wal(conn)
        conn.execute("PRAGMA foreign_keys=ON")
        ensure_schema(conn)
    except BaseException:
        # **開いたまま投げない。** Windows では開いているファイルをよけられない
        # (壊れた作業用DBを脇へよける `set_aside_broken` が続く)
        conn.close()
        raise
    return conn


#: 作業用DBの付き添い(よけるときは一緒に)
SIDECARS = ("-wal", "-shm", "-journal")


def is_broken_db_error(exc: BaseException) -> bool:
    """作業用DBのファイルが壊れている(SQLite のファイルではない・中が傷んでいる)か。

    「使用中(locked)」「開けない」は壊れていない(`OperationalError`)ので数えない。
    """
    return isinstance(exc, sqlite3.DatabaseError) and not isinstance(exc, sqlite3.OperationalError)


def set_aside_broken(db_path: Path) -> Path:
    """壊れた作業用DBを**消さずに**脇へよける(`<名前>.broken-<日時>`)。よけた先を返す。

    よけたあとは空の作業用DBで動き出せる(起動ごと止まらない)。よけたファイルは
    残すので、送っていなかった分を取り出せるかもしれない。名前を変えられないとき
    (Windows で、ほかのプログラムが開いている)は OSError のまま投げる。
    """
    import gc
    import time

    gc.collect()  # 失敗した接続が残っていれば閉じる(開いたままでは名前を変えられない)
    target = db_path.with_name(f"{db_path.name}.broken-{time.strftime('%Y%m%d-%H%M%S')}")
    os.replace(db_path, target)
    for suffix in SIDECARS:
        side = Path(str(db_path) + suffix)
        if side.exists():
            os.replace(side, Path(str(target) + suffix))
    return target


def readonly_uri(db_path) -> str:
    """**読むだけ**で開く URI(`?mode=ro`)。共有フォルダ(UNC)でも開ける形。

        AIM の控えを読めませんでした: invalid uri authority: nlmfangyshrd

    `file:` の後ろが `//` で始まると、SQLite はその次をサーバ名(authority)と
    読み、空か `localhost` 以外は断ります。`Path.as_posix()` は UNC を
    `//サーバ/共有/…` にするので、そのまま付けると上のとおり断られていました
    (記録を見る ── 控えの置き場所が共有のとき)。

    道はそのまま書き(Windows では `\\サーバ\共有\…` のまま ── 標準作業時間が
    同じ形で共有を読んでいます)、**`//` で始まる道だけ空の authority を前に足し
    ます**(`file:////サーバ/共有/…`)。URI で意味を持つ `%` `?` `#` は % で書きます。
    """
    text = os.fspath(db_path)
    for raw, escaped in (("%", "%25"), ("?", "%3f"), ("#", "%23")):
        text = text.replace(raw, escaped)
    if text.startswith("//"):
        text = "//" + text
    return f"file:{text}?mode=ro"


def connect_readonly(db_path, *, timeout: float = 10) -> sqlite3.Connection:
    """読むだけで開く(書けない・無ければ作らない)。共有の上のファイルでも開ける。"""
    conn = sqlite3.connect(readonly_uri(db_path), uri=True, timeout=timeout)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Explicit transaction context: commits on success, rolls back on
    any exception (mirrors the atomic DELETE+INSERT the VBA
    ``NippouDB_Save`` performed via ``ExecuteSQLTransaction``)."""
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
