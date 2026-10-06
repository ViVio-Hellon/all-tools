"""日報入力データの控え ── LocalBackup (v4.12.0)

    日報入力データがローカル保存とのことですが指定パスにも保存できるように
    (ローカルと両方)ローカルになければパスを見る / フォルダ名は LocalBackup
    権限 が Administrator のユーザーは LocalBackup すべてのラインデータを
    記録を見る で参照でき編集もできる(この機能がないと管理者が現地に行く必要がある)

【置き場所】

    <控えの置き場所>\\LocalBackup\\<ライン名>\\日報入力データ.sqlite3

控えの置き場所は設定・参照設定の「日報入力データの控えの置き場所」。空なら
**共有の日報データと同じフォルダ**です(どのPCからも届く場所でないと、管理者が
別のPCから直せないので)。中の形は手元のDBと同じで、入れるのは日報の
見出しと明細(`daily_header` / `daily_detail`)だけです。

【いつ写すか】
手元へ保存したとき(自動保存・打ちかけも)・共有へ送れたとき・紙を消したとき。
リポジトリが**保存と同じ取引で**写す待ち(`backup_pending`)に入れ、要求の
**応答を返したあと**に写します(`app/__init__._register_db`)── 共有のフォルダが
遅い・届かないときに、打っている人を待たせないため。届かなければ待ちに残り、
次の保存か起動で写ります(しばらく届かなければ、`RETRY_AFTER_SEC` は試さない)。

【どちらが新しいか】
同じページが手元と控えの両方にあるときは、**保存した時刻(`saved_at`)が新しい
ほう**を正とします。管理者が別のPCで直したぶんは控えのほうが新しいので:

    写すとき(手元 → 控え) … 控えのほうが新しければ上書きせず、手元へ戻す
    起動のとき(控え → 手元)… 手元に無い・控えのほうが新しいページを戻す

**控えの置き場所(親のフォルダ)が無ければ作りません。** 届かない共有を
「無い」と取り違えて、手元のどこかに作ってしまわないように。`LocalBackup` と
ライン名のフォルダは、親があれば作ります。

**ここで何が起きても、手元への保存は成功のまま**です(写す待ちに残るだけ)。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

from ..config import SETTINGS
from ..db.repository import NippouRepository
from ..db.schema import ensure_schema
from ..logging_setup import get_logger
from ..logic import line_names

log = get_logger("services.local_backup")

PageKey = tuple[str, str, str, int]
ShiftKey = tuple[str, str, str]

#: 写す表(見出しが先 ── 明細は見出しを外部キーで指す)
HEADER = "daily_header"
DETAIL = "daily_detail"
_WHERE = "report_date=? AND line=? AND shift=? AND page=?"

#: 届かなかったあと、次に試すまでの秒数(自動保存のたびに待たされないように)
RETRY_AFTER_SEC = 60.0

_flush_lock = threading.Lock()
_retry_at = 0.0


def _safe(line: str) -> str:
    from ..access_bridge.pusher import safe_line_name

    return safe_line_name(line)


def root() -> Path:
    return SETTINGS.local_backup_root


def path_for(line: str) -> Path:
    """そのラインの控えのファイル。"""
    return root() / _safe(line) / SETTINGS.local_backup_filename


def reachable() -> bool:
    """控えの置き場所(親のフォルダ)に届くか。"""
    try:
        return SETTINGS.local_backup_base.is_dir()
    except OSError:
        return False


def lines_available() -> list[str]:
    """控えのあるライン(ラインの並びで)。届かなければ空。"""
    try:
        if not root().is_dir():
            return []
        found = {p.parent.name for p in root().glob(f"*/{SETTINGS.local_backup_filename}")}
    except OSError:
        return []
    # 並びは定義の表の順(L-1・LVC・HVC・機側…)
    known = [ln for ln in line_names.codes() if _safe(ln) in found]
    return known + sorted(found - {_safe(ln) for ln in known})


# ----------------------------------------------------------------------
# 開く
# ----------------------------------------------------------------------
def _open_write(path: Path) -> sqlite3.Connection:
    """書くために開く。**WAL にしない**(共有のフォルダでは働かないことがある)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_schema(conn)
    return conn


@contextmanager
def reader(line: str) -> Iterator[Optional[NippouRepository]]:
    """そのラインの控えを**読むだけ**で開く。無ければ None。"""
    path = path_for(line)
    try:
        exists = path.is_file()
    except OSError:
        exists = False
    if not exists:
        yield None
        return
    # 共有(UNC)の上でも開ける形で(`db.connection.readonly_uri`)。以前は
    # `as_posix()` で `file://サーバ/…` になり「invalid uri authority」で断られていた
    from ..db.connection import connect_readonly

    conn = connect_readonly(path, timeout=10)
    try:
        yield NippouRepository(conn)
    finally:
        conn.close()


# ----------------------------------------------------------------------
# 1ページを写す
# ----------------------------------------------------------------------
def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def _stamp(conn: sqlite3.Connection, key: PageKey) -> Optional[tuple[str, str]]:
    """(保存した時刻, 指紋)。そのページが無ければ None。"""
    row = conn.execute(f"SELECT saved_at, content_hash FROM {HEADER} WHERE {_WHERE}",
                       key).fetchone()
    return None if row is None else (str(row[0] or ""), str(row[1] or ""))


def _newer(theirs: Optional[tuple[str, str]], mine: Optional[tuple[str, str]]) -> bool:
    """`theirs` が `mine` より新しい別の中身か(同じ時刻なら新しくない)。"""
    if theirs is None:
        return False
    if mine is None:
        return True
    return theirs[0] > mine[0] and theirs[1] != mine[1]


def _copy_page(src: sqlite3.Connection, dst: sqlite3.Connection, key: PageKey) -> bool:
    """`src` のそのページで `dst` を置き換える(取引は呼ぶ側)。`src` に無ければ消す。

    列は**両方にある列だけ**を写します(版の違うDBどうしでも写せるように)。
    """
    dst.execute(f"DELETE FROM {DETAIL} WHERE {_WHERE}", key)
    dst.execute(f"DELETE FROM {HEADER} WHERE {_WHERE}", key)
    for table in (HEADER, DETAIL):
        theirs = set(_columns(dst, table))
        cols = [c for c in _columns(src, table) if c in theirs]
        names = ", ".join(f'"{c}"' for c in cols)
        rows = src.execute(f"SELECT {names} FROM {table} WHERE {_WHERE}", key).fetchall()
        if table == HEADER and not rows:
            return False                          # 手元に無い = 消した
        dst.executemany(
            f"INSERT INTO {table} ({names}) VALUES ({', '.join('?' for _ in cols)})",
            [tuple(r) for r in rows])
    return True


@dataclass
class Result:
    """写した・戻した結果。"""

    written: int = 0                # 控えへ写したページ
    removed: int = 0                # 控えから消したページ
    pulled: int = 0                 # 控えから手元へ戻したページ
    pending: int = 0                # まだ写せていないページ
    error: str = ""
    shifts: list[ShiftKey] = field(default_factory=list)   # 戻した直(集計を作り直す)

    @property
    def message(self) -> str:
        if self.error:
            return f"控え(LocalBackup): {self.error}"
        parts = []
        if self.written:
            parts.append(f"{self.written}ページを写しました")
        if self.removed:
            parts.append(f"{self.removed}ページを消しました")
        if self.pulled:
            parts.append(f"{self.pulled}ページを手元へ戻しました")
        return "控え(LocalBackup): " + ("・".join(parts) if parts else "変わりありません")

    def as_dict(self) -> dict[str, Any]:
        return {"written": self.written, "removed": self.removed, "pulled": self.pulled,
                "pending": self.pending, "error": self.error, "message": self.message}


def _refresh(repo: NippouRepository, shifts: set[ShiftKey]) -> None:
    """戻した直の集計を作り直す(明細が正。集計はそこから何度でも作れる)。"""
    from . import summary

    for report_date, line, shift in sorted(shifts):
        try:
            summary.refresh_shift(repo, report_date, line, shift)
        except Exception:                         # noqa: BLE001 - 明細は戻っている
            log.exception("戻した直の集計を作り直せませんでした %s/%s/%s",
                          report_date, line, shift)


def _base_missing() -> str:
    return (f"控えの置き場所 {SETTINGS.local_backup_base} に届きません"
            "(届くようになったら写します)")


def flush(repo: NippouRepository, *, force: bool = False) -> Result:
    """写す待ちのページを控えへ写す。**同時には1つだけ走る。**"""
    global _retry_at
    if not _flush_lock.acquire(blocking=False):
        return Result(pending=len(repo.backup_pending()), error="ほかで写しています")
    try:
        pending = repo.backup_pending()
        result = Result(pending=len(pending))
        if not pending:
            return result
        if not force and time.monotonic() < _retry_at:
            result.error = _base_missing()
            return result
        if not reachable():
            _retry_at = time.monotonic() + RETRY_AFTER_SEC
            result.error = _base_missing()
            return result
        by_line: dict[str, list[PageKey]] = {}
        for key in pending:
            by_line.setdefault(key[1], []).append(key)
        pulled: set[ShiftKey] = set()
        for line, keys in by_line.items():
            try:
                dst = _open_write(path_for(line))
            except (OSError, sqlite3.Error) as exc:
                log.warning("控えを開けませんでした %s: %s", path_for(line), exc)
                result.error = f"{path_for(line)} を開けません: {exc}"
                continue
            try:
                done: list[PageKey] = []
                with dst:                         # 1ラインぶんを1つの取引で
                    for key in keys:
                        mine, theirs = _stamp(repo.conn, key), _stamp(dst, key)
                        # 手元に無い = このPCで消した(待ちに入れたのは消したとき)。
                        # **新しさを比べるのは両方にあるときだけ**
                        if mine is not None and _newer(theirs, mine):
                            # 控えのほうが新しい(別のPCで直された)── 上書きせず、手元へ戻す
                            with repo.conn:
                                _copy_page(dst, repo.conn, key)
                            result.pulled += 1
                            pulled.add(key[:3])
                        elif _copy_page(repo.conn, dst, key):
                            result.written += 1
                        elif theirs is not None:
                            result.removed += 1
                        done.append(key)
                repo.clear_backup_pending(done)
            except (OSError, sqlite3.Error) as exc:
                log.warning("控えへ写せませんでした %s: %s", line, exc)
                result.error = f"{line_names.label(line)} の控えへ写せませんでした: {exc}"
            finally:
                dst.close()
        _refresh(repo, pulled)
        result.shifts = sorted(pulled)
        result.pending = len(repo.backup_pending())
        if not result.error:
            _retry_at = 0.0
        return result
    finally:
        _flush_lock.release()


def flush_soon() -> None:
    """要求の応答を返したあとに写す(`call_on_close` から)。**自分の接続で。**"""
    from ..db.connection import connect

    try:
        conn = connect(SETTINGS.sqlite_path)
    except Exception:                             # noqa: BLE001 - 次の機会に写す
        log.exception("控えへ写すために手元のDBを開けませんでした")
        return
    try:
        result = flush(NippouRepository(conn))
        if result.written or result.pulled or result.removed:
            log.info("%s", result.message)
    except Exception:                             # noqa: BLE001 - 次の機会に写す
        log.exception("控えへ写す途中で予期しないエラー")
    finally:
        conn.close()


def reset() -> None:
    """届かなかった記憶を忘れる(テスト用)。"""
    global _retry_at
    _retry_at = 0.0


# ----------------------------------------------------------------------
# 起動のとき: 控え ⇄ 手元(この端末のライン)
# ----------------------------------------------------------------------
def pull(repo: NippouRepository, line: str) -> Result:
    """そのラインの控えと手元を**新しいほうに揃える**準備をする。

        控えにだけある / 控えのほうが新しい  → 手元へ戻す(ここで)
        手元にだけある / 手元のほうが新しい  → 写す待ちに入れる(`flush` が写す)

    2つ目は、この仕組みが入る前に打ったぶんも控えに入れるためです。
    """
    result = Result()
    if not reachable():
        result.error = _base_missing()
        return result
    pulled: set[ShiftKey] = set()
    to_send: list[PageKey] = []
    with reader(line) as backup:
        theirs: dict[PageKey, tuple[str, str]] = {}
        if backup is not None:
            for r in backup.conn.execute(
                    f"SELECT report_date, line, shift, page, saved_at, content_hash"
                    f" FROM {HEADER} WHERE line=?", (line,)):
                theirs[(r[0], r[1], r[2], int(r[3]))] = (str(r[4] or ""), str(r[5] or ""))
        mine = {(r[0], r[1], r[2], int(r[3])): (str(r[4] or ""), str(r[5] or ""))
                for r in repo.conn.execute(
                    f"SELECT report_date, line, shift, page, saved_at, content_hash"
                    f" FROM {HEADER} WHERE line=?", (line,))}
        for key, stamp in theirs.items():
            if _newer(stamp, mine.get(key)):
                with repo.conn:
                    _copy_page(backup.conn, repo.conn, key)
                result.pulled += 1
                pulled.add(key[:3])
        for key, stamp in mine.items():
            if key not in theirs or _newer(stamp, theirs[key]):
                to_send.append(key)
    repo.queue_backup(to_send)
    _refresh(repo, pulled)
    result.shifts = sorted(pulled)
    result.pending = len(repo.backup_pending())
    return result


# ----------------------------------------------------------------------
# 記録を見る: 控えの直を並べる / 手元へ入れる
# ----------------------------------------------------------------------
def list_shifts(line: str, limit: int = 60) -> list[dict[str, Any]]:
    """そのラインの控えにある直(新しい保存が先)。**直の単位で**、ページは添え物。"""
    from ..logic.shift import parse_business_date

    with reader(line) as backup:
        if backup is None:
            return []
        rows = backup.conn.execute(
            f"SELECT report_date, line, shift, page, saved_at, dirty, worker"
            f" FROM {HEADER} WHERE line=?", (line,)).fetchall()
    found: dict[ShiftKey, dict[str, Any]] = {}
    for r in rows:
        key = (r["report_date"], r["line"], r["shift"])
        got = found.setdefault(key, {"report_date": key[0], "line": key[1], "shift": key[2],
                                     "pages": [], "saved_at_raw": "", "synced": True,
                                     "workers": []})
        got["pages"].append(int(r["page"]))
        got["saved_at_raw"] = max(got["saved_at_raw"], str(r["saved_at"] or ""))
        got["synced"] = got["synced"] and not r["dirty"]
        for name in str(r["worker"] or "").split():
            if name not in got["workers"]:
                got["workers"].append(name)
    shifts = list(found.values())
    for s in shifts:
        s["pages"].sort()
        raw = s["saved_at_raw"]
        s["saved_at"] = f"{raw[5:10]} {raw[11:16]}" if len(raw) >= 16 else raw
        day = parse_business_date(s["report_date"])
        s["day"] = day.isoformat() if day else ""
    shifts.sort(key=lambda s: (s["day"], s["saved_at_raw"]), reverse=True)
    return shifts[:limit]


def import_shift(repo: NippouRepository, report_date: str, line: str, shift: str) -> Result:
    """控えのその直(全ページ)を手元へ入れる。**手元のほうが新しいページは触らない。**"""
    result = Result()
    with reader(line) as backup:
        if backup is None:
            result.error = f"{line} の控えがありません({path_for(line)})"
            return result
        pages = backup.saved_pages(report_date, line, shift)
        for page in pages:
            key = (report_date, line, shift, int(page))
            if _newer(_stamp(backup.conn, key), _stamp(repo.conn, key)):
                with repo.conn:
                    _copy_page(backup.conn, repo.conn, key)
                result.pulled += 1
    if result.pulled:
        _refresh(repo, {(report_date, line, shift)})
        result.shifts = [(report_date, line, shift)]
    return result


def pending_count(repo: NippouRepository) -> int:
    try:
        return len(repo.backup_pending())
    except sqlite3.Error:
        return 0


def status(repo: Optional[NippouRepository] = None) -> dict[str, Any]:
    """設定の画面に出す1かたまり。"""
    return {"base": str(SETTINGS.local_backup_base), "root": str(root()),
            "reachable": reachable(),
            "pending": pending_count(repo) if repo is not None else 0}
