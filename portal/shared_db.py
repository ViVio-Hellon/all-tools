"""共有の DB(タブ表示権限の表を置く sqlite3)を読む・書く

既定の置き場所は **python-web-tools と同じ** `梱包資材マスタ.sqlite3`
(`\\\\nlmfangyshrd\\各課共有\\0130_日軽稲沢\\梱包課\\AIM\\【■】_参照用ファイル`)。
アクセス権限と同じ DB に、**別の表**(`タブ表示権限`)として置く。

置き場所の決め方(先にあるものが優先):

    1. 大設定で決めたもの(`user_settings.json` の `shared_db_dir` / `shared_db_name`)
    2. 環境変数 `ALLTOOLS_SHARED_DB_DIR` / `ALLTOOLS_SHARED_DB_NAME`
    3. 既定(上)

【共有フォルダの sqlite3 の扱い】(python-web-tools の `source_db` と同じ注意)
- UNC(`\\\\サーバ\\共有\\x.sqlite3`)は `file:////サーバ/共有/x.sqlite3` の URI にする
  (`Path.as_uri()` の `file://サーバ/…` を SQLite は受け付けない)
- 読むときは読み取り専用で開く。開けなければ素のパス
- 書くときは短い取引(`BEGIN IMMEDIATE`)で、**読んだときと同じ行か**を確かめてから書く
  (2人が同時に直したとき、後の人が前の人の変更を知らずに消さない)
"""
from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from . import user_settings
from .logging_utils import get_logger
from .tab_rights import COLUMNS, EDITABLE, TABLE

log = get_logger("shared_db")

DEFAULT_DIR = r"\\nlmfangyshrd\各課共有\0130_日軽稲沢\梱包課\AIM\【■】_参照用ファイル"
DEFAULT_NAME = "梱包資材マスタ.sqlite3"

BUSY_TIMEOUT_SEC = 10
#: 共有に届くかどうかの確かめで、待つ上限(秒)。届かない共有フォルダは長く待たされる
REACH_TIMEOUT_SEC = 8

#: 表の行を指す隠しの鍵(sqlite の rowid)。画面との行き来に使う
ROW_KEY = "__行"


class SourceError(RuntimeError):
    """共有の DB を読めない・書けない。`code` で種類が分かる。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Location:
    folder: str
    name: str
    origin: str          # どこで決めたか(大設定 / 環境変数 / 既定)

    @property
    def path(self) -> Path:
        return Path(self.folder) / self.name

    def to_dict(self) -> dict:
        return {"folder": self.folder, "name": self.name, "path": str(self.path), "origin": self.origin}


def location() -> Location:
    folder = str(user_settings.get(user_settings.KEY_SHARED_DIR) or "").strip()
    name = str(user_settings.get(user_settings.KEY_SHARED_NAME) or "").strip()
    origin = "大設定"
    if not folder:
        folder = os.environ.get("ALLTOOLS_SHARED_DB_DIR", "").strip()
        origin = "環境変数" if folder else "既定"
    if not name:
        name = os.environ.get("ALLTOOLS_SHARED_DB_NAME", "").strip() or DEFAULT_NAME
    return Location(folder or DEFAULT_DIR, name, origin)


def to_uri(path: Path, *, read_only: bool = False) -> str:
    text = str(path)
    if text.startswith("\\\\") or text.startswith("//"):
        rest = text.replace("\\", "/").lstrip("/")
        uri = "file:////" + quote(rest, safe="/")
    else:
        uri = Path(text).resolve().as_uri()
    return uri + ("?mode=ro" if read_only else "")


def _connect(path: Path, *, read_only: bool) -> sqlite3.Connection:
    if not path.is_file():
        raise SourceError("no_source", f"共有の DB が見つかりません: {path}")
    last: Optional[Exception] = None
    # URI で開く(読むときは読み取り専用)→ 開けなければ素のパス
    ways = ((to_uri(path, read_only=read_only), True), (str(path), False))
    for target, as_uri in ways:
        try:
            conn = sqlite3.connect(target, uri=as_uri, timeout=BUSY_TIMEOUT_SEC,
                                   isolation_level=None, check_same_thread=False)
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_SEC * 1000}")
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
            return conn
        except sqlite3.Error as exc:
            last = exc
            log.info("共有の DB を開けませんでした(%s): %s", "URI" if as_uri else "パス", exc)
    raise SourceError("open_failed", f"共有の DB を開けません: {path}({last})")


def reachable(path: Path) -> bool:
    """共有の DB のファイルが見えるか(届かない共有で長く待たせない)。"""
    import threading

    result: list[bool] = []
    worker = threading.Thread(target=lambda: result.append(path.is_file()), daemon=True)
    worker.start()
    worker.join(REACH_TIMEOUT_SEC)
    return bool(result and result[0])


def stamp(path: Path) -> tuple[int, int]:
    """変わったかどうかの目印(大きさ・更新時刻)。"""
    st = path.stat()
    return st.st_size, st.st_mtime_ns


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def has_table(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()
    return row is not None


def read_rows(path: Path) -> Optional[list[dict[str, Any]]]:
    """表の行(`__行` 付き)。表が無ければ `None`。読めなければ `SourceError`。"""
    conn = _connect(path, read_only=True)
    try:
        if not has_table(conn):
            return None
        names = [r[1] for r in conn.execute(f"PRAGMA table_info({_quote(TABLE)})")]
        wanted = [c for c, _ in COLUMNS if c in names]
        cursor = conn.execute(
            f"SELECT rowid AS {_quote(ROW_KEY)}, {', '.join(_quote(c) for c in wanted)} "
            f"FROM {_quote(TABLE)} ORDER BY rowid")
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]
    except sqlite3.Error as exc:
        raise SourceError("read_failed", f"タブ表示権限を読めません: {exc}") from exc
    finally:
        conn.close()


def ddl() -> str:
    cols = ", ".join(f"{_quote(name)} {kind}" for name, kind in COLUMNS)
    return f"CREATE TABLE IF NOT EXISTS {_quote(TABLE)} ({cols})"


def create_table(path: Path) -> bool:
    """表を作る(あれば何もしない)。作ったら `True`。"""
    conn = _connect(path, read_only=False)
    try:
        if has_table(conn):
            return False
        conn.execute(ddl())
        log.info("タブ表示権限の表を作りました: %s", path)
        return True
    except sqlite3.Error as exc:
        raise SourceError("write_failed", f"表を作れません: {exc}") from exc
    finally:
        conn.close()


def _clean_values(values: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in EDITABLE:
        if key not in values:
            continue
        value = values[key]
        if key == "有効":
            out[key] = 1 if value in (True, 1, "1", "true", "True", "有効") else 0
        else:
            out[key] = "" if value is None else str(value).strip()
    return out


def _same(row: dict[str, Any], was: dict[str, Any]) -> bool:
    for key, expected in (was or {}).items():
        if key not in EDITABLE:
            continue
        got = row.get(key)
        if key == "有効":
            if int(got or 0) != int(expected or 0):
                return False
        elif str(got or "").strip() != str(expected or "").strip():
            return False
    return True


def _write(path: Path, work) -> Any:
    conn = _connect(path, read_only=False)
    try:
        for attempt in range(3):
            try:
                conn.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 2:
                    raise
                time.sleep(1 + attempt)
        try:
            result = work(conn)
            conn.execute("COMMIT")
            return result
        except Exception:
            conn.execute("ROLLBACK")
            raise
    except sqlite3.Error as exc:
        raise SourceError("write_failed", f"共有の DB に書けません: {exc}") from exc
    finally:
        conn.close()


def _current(conn: sqlite3.Connection, key: int) -> Optional[dict[str, Any]]:
    cursor = conn.execute(
        f"SELECT {', '.join(_quote(c) for c in EDITABLE)} FROM {_quote(TABLE)} WHERE rowid = ?", (key,))
    row = cursor.fetchone()
    return None if row is None else dict(zip(EDITABLE, row))


def insert(path: Path, values: dict[str, Any]) -> int:
    clean = _clean_values(values)

    def work(conn):
        if not has_table(conn):
            raise SourceError("no_table", "タブ表示権限の表がありません。先に「表を作る」を押してください。")
        cols = list(clean)
        cursor = conn.execute(
            f"INSERT INTO {_quote(TABLE)} ({', '.join(_quote(c) for c in cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)})", [clean[c] for c in cols])
        return int(cursor.lastrowid)

    return _write(path, work)


def update(path: Path, key: int, values: dict[str, Any], was: dict[str, Any]) -> None:
    clean = _clean_values(values)

    def work(conn):
        row = _current(conn, key)
        if row is None:
            raise SourceError("no_row", "その行はもうありません(ほかの人が消しました)。読み直してください。")
        if not _same(row, was):
            raise SourceError("stale_row", "その行はほかの人が先に直しています。読み直してから直してください。")
        cols = list(clean)
        conn.execute(f"UPDATE {_quote(TABLE)} SET {', '.join(f'{_quote(c)} = ?' for c in cols)} "
                     f"WHERE rowid = ?", [clean[c] for c in cols] + [key])

    _write(path, work)


def delete(path: Path, key: int, was: dict[str, Any]) -> None:
    def work(conn):
        row = _current(conn, key)
        if row is None:
            return
        if not _same(row, was):
            raise SourceError("stale_row", "その行はほかの人が先に直しています。読み直してから消してください。")
        conn.execute(f"DELETE FROM {_quote(TABLE)} WHERE rowid = ?", (key,))

    _write(path, work)
