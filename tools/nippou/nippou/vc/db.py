"""マスタDB (SQLite) への出入り

【前提】マスタは**共有フォルダに置いた1つの SQLite ファイル**を、複数の
ラインPCが読み、管理者が設定画面から直す。そのために守ること:

1. **接続を持ち続けない。** 要求のたびに開いて、終わったら閉じる。
   長時間放置した端末がロックやファイルハンドルを握ったままにならない
2. **WAL にしない。** WAL は共有メモリ(-shm)を使い、SMB の上では
   開けなくなる。書く接続は毎回 `journal_mode=DELETE` を確かめる
3. **書くときは最初に鍵を取る(`BEGIN IMMEDIATE`)。** 読んでから書くまでの
   あいだに他の端末が割り込むと、後から書いた側が黙って勝つ
4. **待つ・やり直す。** 他の端末が書いている最中は `busy_timeout` で待ち、
   それでも取れなければ間を空けて数回やり直す。だめなら理由を言って断る
   (`database is locked` をそのまま見せない)
5. **書いたら更新番号を上げる。** 各端末は番号だけを見て、変わったときだけ
   読み直す(`services/masters.py`)

Access(ACE)は使わない。VBA 側に Access への依存は無く(docs/vc/VBA解析.md §2)、
Python 標準の `sqlite3` だけで完結する。
"""
from __future__ import annotations

import getpass
import json
import os
import socket
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator, Optional

from ..logging_setup import get_logger
from . import seed

log = get_logger("vc.db")

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = "3"

# 他の端末の書き込みを待つ時間(秒)。書き込みは数ミリ秒で終わるので短くてよい。
# やり直し3回と合わせて、断るまで最長およそ11秒(長いと押した人が固まったと思う)
BUSY_TIMEOUT_SEC = 2.0
# 鍵が取れなかったときのやり直し(回数と、1回目の待ち秒。回ごとに伸ばす)
LOCK_RETRY = 3
LOCK_RETRY_WAIT_SEC = 0.5

# 控えの数。日に1つ、書く前に取る
BACKUP_KEEP = 30

META = "_メタ"
AUDIT = "変更履歴"


class DbError(Exception):
    """利用者に見せてよい文言を持つ失敗。"""

    def __init__(self, message: str, reason: str = "db_error") -> None:
        super().__init__(message)
        self.reason = reason


class DbLocked(DbError):
    def __init__(self, message: str = "") -> None:
        super().__init__(message or (
            "ほかの端末がマスタを書き込み中です。少し待ってから、もう一度押してください。"),
            "locked")


class DbUnavailable(DbError):
    def __init__(self, message: str) -> None:
        super().__init__(message, "unavailable")


def is_lock_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def quote(name: str) -> str:
    """識別子を二重引用符で囲む(表名・列名に日本語を使うため)。"""
    return '"' + str(name).replace('"', '""') + '"'


def now_text() -> str:
    return datetime.now().strftime("%Y/%m/%d %H:%M:%S")


def who() -> tuple[str, str]:
    """(端末名, ユーザー名)。変更履歴に残す。"""
    host = os.environ.get("COMPUTERNAME") or socket.gethostname()
    try:
        user = os.environ.get("USERNAME") or getpass.getuser()
    except Exception:                               # noqa: BLE001
        user = ""
    return host, user


# ------------------------------------------------------------------
# 開く
# ------------------------------------------------------------------
def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_SEC,
                           isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_SEC * 1000)}")
    return conn


def _require_file(path: Path) -> None:
    if not path.exists():
        raise DbUnavailable(f"マスタが見つかりません: {path}")


@contextmanager
def reading(path: Path) -> Iterator[sqlite3.Connection]:
    """読むだけの接続。**抜けたら必ず閉じる。**

    `mode=ro` にはしない ── 書き手が途中で落ちて残した journal を
    読み手が片づけられなくなり、以後だれも読めなくなる。代わりに
    `query_only` で、この接続からは書けないようにする。
    """
    _require_file(path)
    try:
        conn = _open(path)
    except sqlite3.Error as exc:
        raise DbUnavailable(f"マスタを開けません: {path} ({exc})") from exc
    try:
        conn.execute("PRAGMA query_only = ON")
        # 読み切るまで1つの取引にする。表ごとに読むあいだに他の端末が書くと、
        # 更新番号と中身が食い違った写しができる(共有ロックを最後まで持つ)
        conn.execute("BEGIN")
        yield conn
    except sqlite3.OperationalError as exc:
        if is_lock_error(exc):
            raise DbLocked("ほかの端末がマスタを書き込み中で、読めませんでした。"
                           "もう一度試してください。") from exc
        raise DbUnavailable(f"マスタを読めません: {exc}") from exc
    finally:
        try:
            conn.execute("ROLLBACK")             # 読むだけなので確定するものは無い
        except sqlite3.Error:
            pass
        conn.close()


# 同じプロセスの中の書き込みは1本ずつ(画面の二度押し・複数タブ)。
# 端末をまたぐ書き込みは SQLite の鍵(BEGIN IMMEDIATE)が並べる
_WRITE_LOCK = threading.RLock()
_active_writes = 0


def writing_now() -> bool:
    """いまマスタへ書いている最中か(自動終了・停止の判断に使う)。"""
    return _active_writes > 0


@contextmanager
def writing(path: Path) -> Iterator[sqlite3.Connection]:
    """書く取引。入口で書き込みの鍵を取り、抜けたら確定して閉じる。

    例外で抜けたら取り消す。鍵が取れなければ `DbLocked`。
    """
    global _active_writes
    _require_file(path)
    with _WRITE_LOCK:
        _active_writes += 1
        try:
            backup_daily(path)
            conn = _begin_immediate(path)
            try:
                yield conn
                _commit(conn)
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                conn.close()
        finally:
            _active_writes -= 1


def _begin_immediate(path: Path) -> sqlite3.Connection:
    last: Optional[BaseException] = None
    for attempt in range(LOCK_RETRY + 1):
        conn = None
        try:
            conn = _open(path)
            mode = conn.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
            if str(mode).lower() != "delete":
                log.warning("journal_mode を DELETE にできませんでした: %s", mode)
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("BEGIN IMMEDIATE")
            return conn
        except sqlite3.OperationalError as exc:
            if conn is not None:
                conn.close()
            if not is_lock_error(exc):
                raise DbUnavailable(f"マスタへ書けません: {exc}") from exc
            last = exc
            if attempt < LOCK_RETRY:
                wait = LOCK_RETRY_WAIT_SEC * (attempt + 1)
                log.info("書き込みの鍵が取れません。%.1f秒待ってやり直します(%d回目)",
                         wait, attempt + 1)
                time.sleep(wait)
        except sqlite3.Error as exc:
            if conn is not None:
                conn.close()
            raise DbUnavailable(f"マスタへ書けません: {exc}") from exc
    log.warning("書き込みの鍵を取れませんでした: %s", last)
    raise DbLocked()


def _commit(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("COMMIT")
    except sqlite3.OperationalError as exc:
        if is_lock_error(exc):
            # 読んでいる端末が居て確定できなかった。取り消して断る
            raise DbLocked() from exc
        raise


# ------------------------------------------------------------------
# 更新番号・履歴
# ------------------------------------------------------------------
def revision(conn: sqlite3.Connection) -> int:
    row = conn.execute(f"SELECT 値 FROM {quote(META)} WHERE キー = 'revision'").fetchone()
    try:
        return int(row[0]) if row else 0
    except (TypeError, ValueError):
        return 0


def bump_revision(conn: sqlite3.Connection) -> int:
    conn.execute(
        f"INSERT INTO {quote(META)} (キー, 値) VALUES ('revision', '1') "
        f"ON CONFLICT(キー) DO UPDATE SET 値 = CAST(CAST(値 AS INTEGER) + 1 AS TEXT)")
    return revision(conn)


def meta_get(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute(f"SELECT 値 FROM {quote(META)} WHERE キー = ?", [key]).fetchone()
    return None if row is None else str(row[0])


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(f"INSERT INTO {quote(META)} (キー, 値) VALUES (?, ?) "
                 f"ON CONFLICT(キー) DO UPDATE SET 値 = excluded.値", [key, value])


def record_change(conn: sqlite3.Connection, table: str, operation: str,
                  row: Optional[int], before: Optional[dict[str, Any]],
                  after: Optional[dict[str, Any]]) -> None:
    host, user = who()
    conn.execute(
        f"INSERT INTO {quote(AUDIT)} (日時, 端末, ユーザー, 表, 操作, 行, 変更前, 変更後)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [now_text(), host, user, table, operation, row,
         None if before is None else json.dumps(before, ensure_ascii=False),
         None if after is None else json.dumps(after, ensure_ascii=False)])


def note_edit(path: Path, table: str, operation: str, row: Optional[int],
              before: Optional[dict[str, Any]], after: Optional[dict[str, Any]]) -> None:
    """日報管理ツールのマスタ管理が1行書いたあと。**変更履歴と更新番号。**

    あちらは元のファイルへ1文ずつ書くので、ここは続けて1つの取引で
    「誰が・いつ・何を」を残し、更新番号を上げます。更新番号を上げないと、
    同じマスタを読んでいる vc-calculator の端末が古い写しで計算し続けます。
    """
    with _WRITE_LOCK:
        conn = _begin_immediate(path)
        try:
            record_change(conn, table, operation, row, before, after)
            bump_revision(conn)
            _commit(conn)
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()


#: 消せないときの案内(マスタ管理で子を1行ずつ消さなくてよい道)。v3.97.0
DELETE_HINT = ("VC長さ計算 →「設定」→ 早見表の品種 なら、枠をマスごと・VC品種を"
               "使っている枠ごと、1回で消せます。")


def integrity_message(operation: str, text: str, table: str = "") -> str:
    """表の決まり(UNIQUE / FOREIGN KEY / CHECK / NOT NULL)に当たったときの言い方。"""
    if "UNIQUE" in text:
        cols = text.split(":", 1)[-1].strip()
        names = "・".join(c.split(".")[-1] for c in cols.split(","))
        return f"「{names}」が同じ行がもうあります。"
    if "FOREIGN KEY" in text:
        if operation == "削除":
            children = {"VC品種": "VC内径選択肢・早見表ブロック(計算品種)",
                        "早見表ブロック": "早見表値"}.get(table, "VC内径選択肢・早見表値など")
            return (f"この行はほかの表から使われているので消せません({children} に"
                    f"同じ品種名の行があります)。{DELETE_HINT}")
        return "親の表に無い品種名です(先に VC品種 / 早見表ブロック へ品種を足してください)。"
    if "NOT NULL" in text:
        return f"「{text.split('.')[-1]}」は空にできません。"
    if "CHECK" in text:
        return "値の範囲が違います(VC厚・内径・長さは 0 より大きい数、有効は 0 か 1)。"
    return f"マスタへ書けませんでした: {text}"


# ------------------------------------------------------------------
# 控え
# ------------------------------------------------------------------
_backed_up: set[tuple[str, str]] = set()


def backup_dir(path: Path) -> Path:
    return path.parent / "backup"


def backup_daily(path: Path) -> Optional[Path]:
    """その日の最初の書き込みの前に、マスタの控えを1つ取る。

    控えはマスタの隣(`backup/`)に置く ── どの端末から直しても同じ場所に
    貯まり、壊れたときに管理者が1か所を見れば戻せる。取れなくても書き込みは
    止めない(控えの失敗で業務を止めない)。記録はログに残す。
    """
    day = date.today().strftime("%Y%m%d")
    key = (str(path), day)
    if key in _backed_up:
        return None
    target = backup_dir(path) / f"{path.stem}_{day}{path.suffix}"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            src = _open(path)
            try:
                dst = sqlite3.connect(str(target))
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
            log.info("マスタの控えを取りました: %s", target)
        _backed_up.add(key)
        _prune_backups(target.parent, path)
        return target
    except (OSError, sqlite3.Error) as exc:
        log.warning("マスタの控えを取れませんでした(書き込みは続けます): %s", exc)
        return None


def _prune_backups(folder: Path, path: Path) -> None:
    files = sorted(folder.glob(f"{path.stem}_????????{path.suffix}"))
    for old in files[:-BACKUP_KEEP]:
        try:
            old.unlink()
        except OSError:
            pass


# ------------------------------------------------------------------
# 作る
# ------------------------------------------------------------------
def create_schema(conn: sqlite3.Connection) -> None:
    """表を作る(あるものは触らない)。

    `executescript` は使わない ── 始めてある取引を**黙って確定してから**
    流すので、書き込みの鍵が途中で外れる。1文ずつ同じ取引の中で流す。
    """
    buffer = ""
    for line in SCHEMA_PATH.read_text(encoding="utf-8").splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            buffer = ""
            if statement:
                conn.execute(statement)


def insert_seed(conn: sqlite3.Connection) -> None:
    stamp = now_text()
    q = quote
    for order, name, vendor, vcatu, inside in seed.PRODUCTS:
        conn.execute(
            f"INSERT INTO {q('VC品種')} (表示順, 品種名, 業者名, VC厚, 内径, 有効, 備考, 更新日時)"
            " VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
            [order, name, vendor, vcatu, inside, seed.PRODUCT_NOTES.get(name), stamp])
    for name, order, label, inside in seed.INNER_CHOICES:
        conn.execute(
            f"INSERT INTO {q('VC内径選択肢')} (品種名, 表示順, 表示名, 内径, 更新日時)"
            " VALUES (?, ?, ?, ?, ?)", [name, order, label, inside, stamp])
    for order, key, label, length in seed.SHEETS:
        conn.execute(
            f"INSERT INTO {q('枚数定尺')} (表示順, 記号, 表示名, 長さmm, 有効, 更新日時)"
            " VALUES (?, ?, ?, ?, 1, ?)", [order, key, label, length, stamp])
    computed = {}
    for order, name, vendor, tone, product in seed.QUICK_BLOCKS:
        computed[name] = product is not None
        conn.execute(
            f"INSERT INTO {q('早見表ブロック')} (表示順, 品種名, 業者名, 枠色, 有効, 更新日時, 計算品種)"
            " VALUES (?, ?, ?, ?, 1, ?, ?)", [order, name, vendor, tone, stamp, product])
    for name, rows in seed.QUICK_VALUES.items():
        for inside, cells in rows.items():
            for thickness, length in cells:
                # 式で出す枠は長さを持たない(持つと、どちらが効くのか分からなくなる)
                conn.execute(
                    f"INSERT INTO {q('早見表値')} (品種名, 内径, 肉厚, 長さ, 更新日時)"
                    " VALUES (?, ?, ?, ?, ?)",
                    [name, inside, thickness, None if computed[name] else length, stamp])
    for key, value, note in seed.SETTINGS:
        conn.execute(
            f"INSERT OR IGNORE INTO {q('アプリ設定')} (キー, 値, 説明, 更新日時)"
            " VALUES (?, ?, ?, ?)", [key, value, note, stamp])
    meta_set(conn, "schema_version", SCHEMA_VERSION)
    meta_set(conn, "revision", "1")
    meta_set(conn, "created_at", stamp)


REQUIRED_TABLES = ("VC品種", "VC内径選択肢", "枚数定尺", "早見表ブロック", "早見表値",
                   "アプリ設定", AUDIT, META)


def ensure_database(path: Path) -> str:
    """マスタが無ければ初期値で作る。あれば足りない表・設定だけ足す。

    戻り値は何をしたか(ログと起動画面に出す)。

    **作るときは隣に一時ファイルを作ってから置く。** 2台が同時に初めて
    起動しても、中途半端なファイルを他の端末に読ませない。先に置けた
    ほうが勝ち、後の1台は自分の一時ファイルを捨てる。
    """
    if path.exists():
        return _upgrade(path)
    # **フォルダまでは作らない。** 参照用マスタのフォルダが無いのは設定の
    # 間違いか共有に届かないかで、ここで作ると黙って別の場所にマスタが
    # できる(日報管理ツールへ移したときに足した関門)
    if not path.parent.is_dir():
        raise DbUnavailable(f"VC計算マスタの置き場所(フォルダ)がありません: {path.parent}")
    # 一時ファイルは**要求(スレッド)ごとに別の名前。** 画面を開くと計算と設定の
    # 要求が同時に来る。同じ名前だと、同じ一時ファイルへ初期値を2度入れて
    # 片方が「UNIQUE constraint failed」で落ちていた(v3.96.0)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.creating")
    try:
        conn = sqlite3.connect(str(tmp), isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode = DELETE")
            conn.execute("BEGIN")
            create_schema(conn)
            insert_seed(conn)
            conn.execute("COMMIT")
        finally:
            conn.close()
        if _publish(tmp, path):
            log.info("マスタを初期値で作りました: %s", path)
            return "created"
        log.info("ほかの端末が先にマスタを作りました: %s", path)
        return "exists"
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _publish(tmp: Path, path: Path) -> bool:
    """`tmp` を `path` として置く。既にあれば置かずに False。"""
    try:
        if os.name == "nt":
            os.rename(tmp, path)                 # Windows は置き先があれば失敗する
        else:
            os.link(tmp, path)                   # POSIX は link が「あれば失敗」
        return True
    except FileExistsError:
        return False


def _upgrade(path: Path) -> str:
    with reading(path) as conn:
        have = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        settings = set()
        if "アプリ設定" in have:
            settings = {r[0] for r in conn.execute(f"SELECT キー FROM {quote('アプリ設定')}")}
        version = meta_get(conn, "schema_version") if META in have else None
    missing = [t for t in REQUIRED_TABLES if t not in have]
    missing_settings = seed.SETTING_KEYS - settings
    old_version = version is not None and version < SCHEMA_VERSION
    if not missing and not missing_settings and not old_version:
        return "ok"
    if not have:
        raise DbUnavailable(f"マスタのファイルに表が1つもありません: {path}")
    with writing(path) as conn:
        if version == "1":
            _migrate_1_to_2(conn)
        if version in ("1", "2"):
            _migrate_2_to_3(conn)
        create_schema(conn)
        stamp = now_text()
        for key, value, note in seed.SETTINGS:
            conn.execute(
                f"INSERT OR IGNORE INTO {quote('アプリ設定')} (キー, 値, 説明, 更新日時)"
                " VALUES (?, ?, ?, ?)", [key, value, note, stamp])
        meta_set(conn, "schema_version", SCHEMA_VERSION)
        bump_revision(conn)
    log.info("マスタに足りない表・設定を足しました: %s %s", missing, sorted(missing_settings))
    return "upgraded"


def _migrate_2_to_3(conn: sqlite3.Connection) -> None:
    """VER1.5.0 → 1.6.0。枚数定尺 の表示名を製品の呼び名(1×2 / 2×4 / 5×10)にする。

    表示名が記号のまま(初期値)の行だけを変える。管理者が直した表示名は残す。
    """
    q = quote
    stamp = now_text()
    for _order, key, label, _length in seed.SHEETS:
        row = conn.execute(f"SELECT rowid, 表示名 FROM {q('枚数定尺')} WHERE 記号 = ?",
                           [key]).fetchone()
        if row is None or (row[1] not in (None, "", key)):
            continue
        conn.execute(f"UPDATE {q('枚数定尺')} SET 表示名 = ?, 更新日時 = ? WHERE rowid = ?",
                     [label, stamp, row[0]])
        record_change(conn, "枚数定尺", "更新", row[0], {"表示名": row[1]}, {"表示名": label})
    record_change(conn, "_メタ", "スキーマ更新", None, {"schema_version": "2"},
                  {"schema_version": "3", "内容": "枚数定尺の表示名を 1×2 / 2×4 / 5×10 にする"})
    log.info("マスタを版3に更新しました(枚数定尺の表示名)")


def _migrate_1_to_2(conn: sqlite3.Connection) -> None:
    """VER1.0.0 → 1.1.0。早見表を**ブックの式から出す**形にする。

    - 早見表ブロック に「計算品種」を足し、同じ名前(VE系は ｽﾐﾛﾝVE系)の VC品種 を結ぶ
    - 早見表値.長さ を空欄を許す形に作り直し、式で出す枠の長さを空にする
    - 肉厚の逆算 を既定で使う(ブック「自動計算 長さから肉厚」と同じ)
    どれも同じ取引の中で行い、変更履歴に残す。
    """
    q = quote
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({q('早見表ブロック')})")}
    if "計算品種" not in cols:
        conn.execute(f"ALTER TABLE {q('早見表ブロック')} ADD COLUMN {q('計算品種')} TEXT"
                     f" REFERENCES {q('VC品種')}({q('品種名')})"
                     " ON UPDATE CASCADE ON DELETE RESTRICT")
    products = {r[0] for r in conn.execute(f"SELECT 品種名 FROM {q('VC品種')}")}
    for _order, name, _vendor, _tone, product in seed.QUICK_BLOCKS:
        if product and product in products:
            conn.execute(f"UPDATE {q('早見表ブロック')} SET 計算品種 = ?"
                         " WHERE 品種名 = ? AND 計算品種 IS NULL", [product, name])
    conn.execute(f"ALTER TABLE {q('早見表値')} RENAME TO {q('早見表値_v1')}")
    create_schema(conn)                               # 新しい形の 早見表値 を作る
    conn.execute(
        f"INSERT INTO {q('早見表値')} (品種名, 内径, 肉厚, 長さ, 更新日時)"
        f" SELECT v.品種名, v.内径, v.肉厚,"
        f" CASE WHEN b.計算品種 IS NULL THEN v.長さ END, v.更新日時"
        f" FROM {q('早見表値_v1')} v JOIN {q('早見表ブロック')} b ON b.品種名 = v.品種名")
    conn.execute(f"DROP TABLE {q('早見表値_v1')}")
    conn.execute(f"UPDATE {q('アプリ設定')} SET 値 = '1', 説明 = ?, 更新日時 = ?"
                 " WHERE キー = '肉厚の逆算' AND 値 = '0'",
                 [dict((k, d) for k, _v, d in seed.SETTINGS)["肉厚の逆算"], now_text()])
    record_change(conn, "_メタ", "スキーマ更新", None, {"schema_version": "1"},
                  {"schema_version": "2",
                   "内容": "早見表をブックの式から出す・肉厚の逆算を使う"})
    log.info("マスタを版2に更新しました(早見表を式から出す・肉厚の逆算を使う)")
