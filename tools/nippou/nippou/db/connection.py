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


# ------------------------------------------------------------------
# 「無い」と「読めない」を分ける(共有のファイル・控え)
# ------------------------------------------------------------------
#: 読めなかったときに添える一言(現場の人が読む)
RETRY_LATER = "少しして自動でやり直します"


class SharedUnreadable(sqlite3.OperationalError):
    """ファイル(またはその置き場所)を**確かめられなかった**。「無い」とは別。

    共有フォルダに届かない・権限・ロック・0バイト・見えているのに開けない、など。
    これが出たら**作らない・書かない・覚えない**(次の回にやり直す)。
    sqlite3 のエラーの仲間なので、`except sqlite3.Error` の道はそのまま通る。
    """


def _unreadable(path, exc: object = "") -> SharedUnreadable:
    where = Path(path)
    detail = f"({exc})" if exc else ""
    return SharedUnreadable(
        f"共有フォルダを読めませんでした: {where}{detail}。{RETRY_LATER}")


def file_state(path, *, base=None) -> bool:
    """ファイルが**ある**(True)か、**確かめたうえで無い**(False)か。確かめられなければ `SharedUnreadable`。

    「無い」と言うのは、置き場所(親のフォルダ)に届いて一覧が取れ、そこに無いときだけ。
    親のフォルダが無い・届かないのは「読めない」です(Windows では、届かない共有も
    `FileNotFoundError`(WinError 53 など)になるので、`exists()` / `is_file()` が False でも
    「無い」とは言えません)。

    `base` を渡すと、`base` の下のフォルダが無いのも「確かめたうえで無い」に数えます
    (`base` そのものは届いて一覧が取れること。控えの LocalBackup とライン名の
    フォルダなど、こちらが作るフォルダのため)。
    """
    target = Path(path)
    try:
        target.stat()
        return True
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise _unreadable(target, exc) from exc
    stop = Path(base) if base is not None else None
    child, folder = target, target.parent
    while True:
        try:
            names = os.listdir(folder)
        except FileNotFoundError as exc:
            if stop is not None and folder != stop and stop in folder.parents:
                child, folder = folder, folder.parent
                continue
            raise _unreadable(folder, exc) from exc
        except OSError as exc:
            raise _unreadable(folder, exc) from exc
        if child.name.casefold() in {n.casefold() for n in names}:
            # 一覧には出ているのに開けない(差し替えの最中・権限など)
            raise _unreadable(target, "一覧には出ているのに開けません")
        return False


def file_uri(db_path, mode: str) -> str:
    """`mode` 付きの URI(`ro` 読むだけ / `rw` 書くが**無ければ作らない**)。共有(UNC)でも開ける形。"""
    text = os.fspath(db_path)
    for raw, escaped in (("%", "%25"), ("?", "%3f"), ("#", "%23")):
        text = text.replace(raw, escaped)
    if text.startswith("//"):
        text = "//" + text
    return f"file:{text}?mode={mode}"


def open_rw(db_path, *, create: bool = False, base=None,
            timeout: float = 10, **options) -> sqlite3.Connection:
    """書くために開く。**無いファイルは作らない**(`mode=rw`)。

    `create=True` のときだけ、**確かめたうえで無い**(`file_state`)ときに新しく作ります
    (初めての導入)。届かない・権限・0バイト・見えているのに開けない、は `SharedUnreadable`
    ── 作らない・開かない。**置き場所のフォルダが無ければ作らない**(`OperationalError`)。
    `base` を渡すと、`base` の下のフォルダに限って作る(`base` そのものは作らない)。
    `options` は `sqlite3.connect` へそのまま渡す(`check_same_thread` など)。

    以前は `sqlite3.connect(道)` で開いていたので、共有が一瞬見えなかったり差し替えの
    最中だったりすると、**空の新しい DB を共有に作って**そこへ書き始めていました。
    """
    path = Path(db_path)
    if file_state(path, base=base):
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise _unreadable(path, exc) from exc
        if size == 0:
            raise _unreadable(path, "ファイルの中身が空(0バイト)です")
        try:
            return sqlite3.connect(file_uri(path, "rw"), uri=True, timeout=timeout,
                                   **options)
        except sqlite3.OperationalError as first:
            # URI が通らない環境のために素の道でも試す。**素の道は無ければ作る**ので、
            # 直前にもう一度あることを確かめてから
            try:
                if path.stat().st_size == 0:
                    raise first
            except OSError:
                raise _unreadable(path, first) from first
            return sqlite3.connect(str(path), timeout=timeout, **options)
    if not create:
        raise sqlite3.OperationalError(f"{path.name} がありません: {path}")
    if not path.parent.is_dir():
        top = Path(base) if base is not None else None
        if top is None or not top.is_dir() or top not in path.parent.parents:
            raise sqlite3.OperationalError(f"置き場所のフォルダがありません: {path.parent}")
        path.parent.mkdir(parents=True, exist_ok=True)
    _create_new(path)
    return open_rw(path, timeout=timeout, **options)


def _create_new(path: Path) -> None:
    """空の DB を**隣の一時ファイルで作ってから置く**(あれば置かない)。

    その場で作ると、作りかけの0バイトのファイルをほかの端末が見て「読めない」になる
    (2台が同時に初めて共有へ保存したとき)。先に置けたほうが勝ち、後のほうは
    置かずに、置かれたものを開く(`vc.db.ensure_database` と同じ形)。
    """
    import threading

    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.creating")
    try:
        conn = sqlite3.connect(str(tmp))
        try:
            conn.execute("VACUUM")               # 見出しのページを書く(0バイトにしない)
        finally:
            conn.close()
        try:
            if os.name == "nt":
                os.rename(tmp, path)             # Windows は置き先があれば断る
            else:
                os.link(tmp, path)               # POSIX は link が「あれば断る」
        except FileExistsError:
            pass                                 # ほかの端末が先に作った
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


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
    return file_uri(db_path, "ro")


def connect_readonly(db_path, *, timeout: float = 10) -> sqlite3.Connection:
    """読むだけで開く(書けない・無ければ作らない)。共有の上のファイルでも開ける。

    **WAL の DB は、書かない約束(`query_only`)で普通に開き直す。** 読むだけの接続は
    閉じても -wal / -shm を片付けられず、共有に残る。古い付き添いの隣でファイルを
    差し替えると、新しいファイルに古い -wal が当たって壊れる。
    """
    conn = sqlite3.connect(readonly_uri(db_path), uri=True, timeout=timeout)
    try:
        wal = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
    except sqlite3.Error:
        wal = False         # 読めないなら、呼んだ側の最初の問い合わせで分かる
    if wal:
        conn.close()
        # 開き直すときも**無ければ作らない**(`open_rw`)── 素の道の connect は、
        # いま見えなくなったファイルの場所に空の DB を作る
        conn = open_rw(db_path, timeout=timeout)
        conn.execute("PRAGMA query_only = ON")
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
