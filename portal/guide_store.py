"""案内表示 ── 表を読む・直す・押した記録を残す(入口・大設定。決まりは `guide_notice.py`)

【表】共有の DB(タブ表示権限と同じファイル。既定 梱包資材マスタ)の「案内表示」。大設定で直す。
無ければ**初めて読むときに作り**、頼まれた案内(機側・2直の始まりに計量器の点検)を1行入れておく。
前の版で作った表に足りない列は足す。表があるかは読む形で確かめ、書く形で開くのは作る・足すときだけ。

【この端末のライン】大設定で決めた値 → 無ければ共有の DB の「アクセス権限」の表(この端末の行の
「権限」にあるライン名。日報と同じ読み方)。どちらも無ければ案内は出さない(大設定に理由を出す)。

【直の時刻】共有の DB と同じフォルダの「伝送用ファイル」の「時間用」(日報と同じ表)。読めなければ既定。

【押した人】名前は共有の DB の「班員名簿」(日報の作業者を選ぶと同じ名簿)から選ぶ。

【押した記録】
    共有の CSV(正)  <共有の DB のフォルダ>\\案内の確認\\<ライン>\\案内の確認_2026-11.csv
                      ほかの PC で押したぶんも入る。**消す・よそへ移すと、また出る**(試すとき)
    この端末         <手元の領域>\\data\\guide_notice.sqlite3。まだ共有へ書けていないぶん(次の見張りで
                      書く)・案内を初めて見た時刻(作る前の回は数えない)
共有のフォルダそのものに届かないときは、この端末の記録で決める(落ちただけで全部また出さない)。
"""
from __future__ import annotations

import csv
import re
import sqlite3
import threading
import time as _time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from . import app_config, guide_notice as rule, shared_db, user_settings
from .logging_utils import get_logger

log = get_logger("guide_store")

#: この端末のライン(大設定で決めたとき)。空なら「アクセス権限」の表から
KEY_LINE = "guide_line"
#: 時間用のあるファイル(共有の DB と同じフォルダ)
TRANSMISSION_NAMES = ("伝送用ファイル.sqlite3", "伝送用ファイル.db")
SHIFT_TABLE = "時間用"
STAFF_TABLE = "班員名簿"
ACCESS_TABLE = "アクセス権限"
RECORD_DIR = "案内の確認"
CSV_FIELDS = ["報告日", "直", "ライン", "案内", "押した人", "押した時刻", "PC", "押した直"]
#: 表・時刻・名簿を読み直すまでの秒数(見張りは1分ごと。共有へ毎回行かない)
CACHE_SEC = 30.0
#: 正規のライン名(日報の `constants.LINE_NAMES` と同じ。中板は 中板3 のように番号を続けてもよい)
LINE_NAMES = ("L-1", "LVC", "HVC", "トット", "バランサー", "AIM", "機側", "NS1", "中板")

_lock = threading.Lock()
_cache: dict[str, tuple[float, Any]] = {}


def reset() -> None:
    """読んだものを忘れる(試験・大設定で直したあと)。"""
    with _lock:
        _cache.clear()


def _cached(key: str, make):
    now = _time.monotonic()
    with _lock:
        got = _cache.get(key)
        if got is not None and now - got[0] < CACHE_SEC:
            return got[1]
    value = make()
    with _lock:
        _cache[key] = (now, value)
    return value


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def shared_path() -> Path:
    return shared_db.location(strict=True).path


# ----------------------------------------------------------------------
# 表「案内表示」
# ----------------------------------------------------------------------
def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({_q(table)})")]


def _tables(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]


def ensure_table(path: Path) -> bool:
    """表が無ければ作り、最初の1行を入れる。足りない列は足す。作った・足したら True。"""
    try:
        conn = shared_db._connect(path, read_only=True)
        try:
            have = (_table_columns(conn, rule.TABLE) if rule.TABLE in _tables(conn) else None)
        finally:
            conn.close()
    except (shared_db.SourceError, sqlite3.Error) as exc:
        log.warning("案内表示の表があるか確かめられませんでした: %s", exc)
        return False
    if have is not None and all(name in have for name, _ in rule.COLUMNS):
        return False

    def work(conn: sqlite3.Connection) -> bool:
        if rule.TABLE not in _tables(conn):
            cols = ", ".join(f"{_q(n)} {t}" for n, t in rule.COLUMNS)
            conn.execute(f"CREATE TABLE {_q(rule.TABLE)} ({cols})")
            names = list(rule.FIRST_ROW)
            conn.execute(f"INSERT INTO {_q(rule.TABLE)} ({', '.join(_q(n) for n in names)})"
                         f" VALUES ({', '.join('?' for _ in names)})",
                         [rule.FIRST_ROW[n] for n in names])
            log.info("共有の DB に「%s」の表を作り、最初の案内を入れました: %s", rule.TABLE, path)
            return True
        now = set(_table_columns(conn, rule.TABLE))
        missing = [(n, t) for n, t in rule.COLUMNS if n not in now]
        for name, kind in missing:
            conn.execute(f"ALTER TABLE {_q(rule.TABLE)} ADD COLUMN {_q(name)} {kind}")
        if missing:
            log.info("「%s」に列を足しました: %s", rule.TABLE, ", ".join(n for n, _ in missing))
        return bool(missing)

    try:
        made = shared_db._write(path, work)
    except shared_db.SourceError as exc:
        log.warning("案内表示の表を作れませんでした: %s", exc)
        return False
    reset()
    return made


def read_rows(path: Path) -> list[dict[str, Any]]:
    """表の行(`__行` 付き。並びは表の順)。表が無ければ空。読めなければ SourceError。"""
    conn = shared_db._connect(path, read_only=True)
    try:
        if rule.TABLE not in _tables(conn):
            return []
        cursor = conn.execute(f"SELECT rowid AS {_q(shared_db.ROW_KEY)}, * FROM {_q(rule.TABLE)}"
                              " ORDER BY rowid")
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]
    except sqlite3.Error as exc:
        raise shared_db.SourceError("read_failed", f"案内表示を読めません: {exc}") from exc
    finally:
        conn.close()


def notices() -> tuple[list[rule.Notice], str]:
    """(案内, 読めなかった理由)。理由が空でなければ読めなかった(案内は出さない)。"""
    def make():
        try:
            path = shared_path()
            ensure_table(path)
            return rule.parse(read_rows(path)), ""
        except shared_db.SourceError as exc:
            return [], str(exc)
    return _cached("notices", make)


EDIT_COLUMNS = tuple(n for n, _ in rule.COLUMNS)


def _clean(values: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    kinds = dict(rule.COLUMNS)
    for name in EDIT_COLUMNS:
        if name not in values:
            continue
        text = "" if values[name] is None else str(values[name]).strip()
        if kinds[name] == "INTEGER":
            out[name] = int(text) if re.fullmatch(r"-?\d+", text) else None
        else:
            out[name] = text
    return out


def check_values(values: dict[str, Any]) -> str:
    """書く前の確かめ。**選べる値以外・読めない形**を、選べる値を添えて断る。"""
    if not str(values.get("表示文") or "").strip():
        return "「表示文」を入れてください(大きく出す文)。"
    return rule.problem_of(values)


def _same(row: dict[str, Any], was: dict[str, Any]) -> bool:
    for name, expected in (was or {}).items():
        if name not in EDIT_COLUMNS:
            continue
        if str(row.get(name) if row.get(name) is not None else "").strip() != \
                str(expected if expected is not None else "").strip():
            return False
    return True


def insert(path: Path, values: dict[str, Any]) -> int:
    clean = _clean(values)
    ensure_table(path)

    def work(conn):
        cols = list(clean)
        cursor = conn.execute(f"INSERT INTO {_q(rule.TABLE)} ({', '.join(_q(c) for c in cols)})"
                              f" VALUES ({', '.join('?' for _ in cols)})", [clean[c] for c in cols])
        return int(cursor.lastrowid)

    key = shared_db._write(path, work)
    reset()
    return key


def _current(conn, key: int) -> Optional[dict[str, Any]]:
    cursor = conn.execute(f"SELECT * FROM {_q(rule.TABLE)} WHERE rowid = ?", (key,))
    row = cursor.fetchone()
    return None if row is None else dict(zip([d[0] for d in cursor.description], row))


def update(path: Path, key: int, values: dict[str, Any], was: dict[str, Any]) -> None:
    clean = _clean(values)

    def work(conn):
        row = _current(conn, key)
        if row is None:
            raise shared_db.SourceError("no_row", "その行はもうありません(ほかの人が消しました)。読み直してください。")
        if not _same(row, was):
            raise shared_db.SourceError("stale_row", "その行はほかの人が先に直しています。読み直してから直してください。")
        cols = list(clean)
        conn.execute(f"UPDATE {_q(rule.TABLE)} SET {', '.join(f'{_q(c)} = ?' for c in cols)}"
                     " WHERE rowid = ?", [clean[c] for c in cols] + [key])

    shared_db._write(path, work)
    reset()


def delete(path: Path, key: int, was: dict[str, Any]) -> None:
    def work(conn):
        row = _current(conn, key)
        if row is None:
            return
        if not _same(row, was):
            raise shared_db.SourceError("stale_row", "その行はほかの人が先に直しています。読み直してから消してください。")
        conn.execute(f"DELETE FROM {_q(rule.TABLE)} WHERE rowid = ?", (key,))

    shared_db._write(path, work)
    reset()


# ----------------------------------------------------------------------
# この端末のライン
# ----------------------------------------------------------------------
_SEPARATORS = re.compile(r"[\s,、，;；|｜/／・]+")


def _line_of(token: str) -> str:
    text = rule._text(token)
    if text in LINE_NAMES:
        return text
    if re.fullmatch(r"中板\d+", text):
        return text
    return ""


def access_lines() -> tuple[list[str], str]:
    """「アクセス権限」の表で、この端末の行にあるライン名(並び順)。(ライン, 読めなかった理由)。"""
    from . import remote_stop, stop_mode

    pc, login = remote_stop.identity()
    try:
        conn = shared_db._connect(shared_path(), read_only=True)
    except shared_db.SourceError as exc:
        return [], f"共有の DB を読めません: {exc}"
    try:
        table = next((t for t in _tables(conn) if stop_mode.fold(t) == stop_mode.fold(ACCESS_TABLE)), None)
        if table is None:
            return [], f"共有の DB に「{ACCESS_TABLE}」の表がありません"
        cursor = conn.execute(f"SELECT * FROM {_q(table)}")
        columns = [d[0] for d in cursor.description]
        rows = [dict(zip(columns, r)) for r in cursor.fetchall()]
    except sqlite3.Error as exc:
        return [], f"アクセス権限の表を読めません: {exc}"
    finally:
        conn.close()
    login_col = next((c for c in columns if stop_mode.fold(c) in stop_mode.LOGIN_COLUMNS), None)
    pc_col = next((c for c in columns if stop_mode.fold(c) in stop_mode.PC_COLUMNS), None)
    rights_col = next((c for c in columns if stop_mode.fold(c) == stop_mode.fold(stop_mode.RIGHTS_COLUMN)), None)
    valid_col = next((c for c in columns if stop_mode.fold(c) == stop_mode.fold(stop_mode.VALID_COLUMN)), None)
    if rights_col is None or (login_col is None and pc_col is None):
        return [], "アクセス権限の表に ログインID・PC名・権限 の列がありません"
    found: list[str] = []
    for row in rows:
        if valid_col is not None and stop_mode.fold(row.get(valid_col)) in stop_mode._OFF:
            continue
        lv = stop_mode.fold(row.get(login_col)) if login_col else ""
        pv = stop_mode.fold(row.get(pc_col)) if pc_col else ""
        if not lv and not pv:
            continue
        if lv and not stop_mode._same(lv, login):
            continue
        if pv and not stop_mode._same(pv, pc):
            continue
        for token in _SEPARATORS.split(str(row.get(rights_col) or "")):
            line = _line_of(token)
            if line and line not in found:
                found.append(line)
    return found, ""


@dataclass
class LineChoice:
    line: str
    origin: str          # 大設定 / アクセス権限 / ""(決まらない)
    why: str = ""

    def to_dict(self) -> dict:
        return {"line": self.line, "origin": self.origin, "why": self.why,
                "setting": str(user_settings.get(KEY_LINE) or "")}


def terminal_line() -> LineChoice:
    """この端末のライン。大設定で決めた値 → アクセス権限の表。"""
    chosen = rule._text(user_settings.get(KEY_LINE) or "")
    if chosen:
        return LineChoice(chosen, "大設定")
    lines, problem = _cached("access_lines", access_lines)
    if len(lines) == 1:
        return LineChoice(lines[0], "アクセス権限")
    if len(lines) > 1:
        return LineChoice("", "", f"アクセス権限のこの端末の行にラインが2つ以上あります({'・'.join(lines)})。"
                                  "大設定でこの端末のラインを決めてください")
    return LineChoice("", "", (problem + "。" if problem else "")
                      + "この端末のラインが決まっていません(アクセス権限のこの端末の行にライン名が無い)。"
                        "大設定でこの端末のラインを決めると、そのラインの案内が出ます")


# ----------------------------------------------------------------------
# 直の時刻・名簿
# ----------------------------------------------------------------------
def shift_times() -> tuple[dict, str]:
    """(直の時刻, どこから)。共有の DB と同じフォルダの伝送用ファイルの「時間用」。無ければ既定。"""
    def make():
        try:
            folder = shared_path().parent
        except shared_db.SourceError:
            return dict(rule.DEFAULT_TIMES), "既定(共有の DB の置き場所が読めない)"
        for name in TRANSMISSION_NAMES:
            path = folder / name
            try:
                if not path.is_file():
                    continue
                conn = shared_db._connect(path, read_only=True)
            except (OSError, shared_db.SourceError):
                continue
            try:
                if SHIFT_TABLE not in _tables(conn):
                    continue
                cursor = conn.execute(f"SELECT * FROM {_q(SHIFT_TABLE)}")
                cols = [d[0] for d in cursor.description]
                return rule.shift_times(dict(zip(cols, r)) for r in cursor.fetchall()), str(path)
            except sqlite3.Error:
                continue
            finally:
                conn.close()
        return dict(rule.DEFAULT_TIMES), "既定(伝送用ファイルの時間用が見つからない)"
    return _cached("times", make)


def roster() -> list[dict[str, Any]]:
    """班員名簿(班ごと)。読めなければ空(名前は打てる)。"""
    def make():
        try:
            conn = shared_db._connect(shared_path(), read_only=True)
        except shared_db.SourceError:
            return []
        try:
            if STAFF_TABLE not in _tables(conn):
                return []
            cursor = conn.execute(f"SELECT * FROM {_q(STAFF_TABLE)}")
            cols = [d[0] for d in cursor.description]
            teams: dict[str, list[str]] = {}
            for r in cursor.fetchall():
                row = dict(zip(cols, r))
                name = str(row.get("名前") or "").strip()
                if name:
                    teams.setdefault(str(row.get("班") or "").strip(), []).append(name)
            return [{"team": t, "names": n} for t, n in teams.items()]
        except sqlite3.Error:
            return []
        finally:
            conn.close()
    return _cached("roster", make)


# ----------------------------------------------------------------------
# 押した記録
# ----------------------------------------------------------------------
def _local() -> sqlite3.Connection:
    conn = sqlite3.connect(str(app_config.local_dir("data") / "guide_notice.sqlite3"))
    conn.execute("CREATE TABLE IF NOT EXISTS ack ("
                 " notice_id TEXT NOT NULL, report_date TEXT NOT NULL, line TEXT NOT NULL,"
                 " shift TEXT NOT NULL, worker TEXT, acked_at TEXT, pc TEXT, acked_shift TEXT,"
                 " shared INTEGER NOT NULL DEFAULT 0,"
                 " PRIMARY KEY (notice_id, report_date, line, shift))")
    conn.execute("CREATE TABLE IF NOT EXISTS seen (notice_id TEXT PRIMARY KEY, first_seen TEXT NOT NULL)")
    return conn


def record_folder() -> Optional[Path]:
    """押した記録の CSV を置く根(共有の DB のフォルダ\\案内の確認)。置き場所が読めなければ None。"""
    try:
        return shared_path().parent / RECORD_DIR
    except shared_db.SourceError:
        return None


def _safe(line: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", line) or "_"


def csv_path(line: str, month: str) -> Optional[Path]:
    root = record_folder()
    return None if root is None else root / _safe(line) / f"案内の確認_{month}.csv"


def first_seen(found: Iterable[rule.Notice], now: datetime) -> dict[str, datetime]:
    """案内ごとに、この端末が初めて見た時刻(作る前の回は数えない)。"""
    conn = _local()
    try:
        with conn:
            conn.executemany("INSERT OR IGNORE INTO seen (notice_id, first_seen) VALUES (?, ?)",
                             [(n.id, now.isoformat(timespec="seconds")) for n in found])
        out: dict[str, datetime] = {}
        for notice_id, at in conn.execute("SELECT notice_id, first_seen FROM seen"):
            try:
                out[str(notice_id)] = datetime.fromisoformat(str(at))
            except ValueError:
                continue
        return out
    finally:
        conn.close()


def _local_acks(line: str) -> list[dict[str, Any]]:
    keys = ("notice_id", "report_date", "line", "shift", "worker", "acked_at", "pc", "acked_shift", "shared")
    conn = _local()
    try:
        return [dict(zip(keys, r)) for r in conn.execute(
            f"SELECT {', '.join(keys)} FROM ack WHERE line=?", (line,))]
    finally:
        conn.close()


def _shared_acks(line: str, months: Iterable[str]) -> Optional[list[dict[str, Any]]]:
    root = record_folder()
    try:
        if root is None or not root.parent.is_dir():
            return None                           # 共有のフォルダそのものに届かない
    except OSError:
        return None
    out: list[dict[str, Any]] = []
    for month in sorted(set(months)):
        path = csv_path(line, month)
        try:
            with open(path, encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    out.append({"notice_id": row.get("案内", ""), "report_date": row.get("報告日", ""),
                                "line": row.get("ライン", ""), "shift": row.get("直", ""),
                                "worker": row.get("押した人", ""),
                                "acked_at": (row.get("押した時刻", "") or "").replace(" ", "T"),
                                "pc": row.get("PC", ""), "acked_shift": row.get("押した直", "")})
        except (OSError, UnicodeError, csv.Error, TypeError):
            continue
    return out


def acks(line: str, months: Iterable[str]) -> dict[tuple[str, str, str], dict[str, Any]]:
    """押した記録。**正は共有の CSV**。この端末の記録からは、まだ共有へ書けていないぶんだけ足す。
    共有のフォルダに届かないときは、この端末の記録で決める。"""
    shared = _shared_acks(line, months)
    local = _local_acks(line)
    rows = local if shared is None else shared + [r for r in local if not r.get("shared")]
    found: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        if row.get("line") and row["line"] != line:
            continue
        found.setdefault((str(row["notice_id"]), str(row["report_date"]), str(row["shift"])), row)
    return found


def share_pending() -> Optional[int]:
    """共有の CSV へまだ書いていない記録を書く。書いた数(書けなければ None。次の見張りでまた)。"""
    conn = _local()
    try:
        rows = conn.execute("SELECT notice_id, report_date, line, shift, worker, acked_at, pc, acked_shift"
                            " FROM ack WHERE shared=0 ORDER BY acked_at").fetchall()
        written = 0
        for notice_id, report_date, line, shift, worker, acked_at, pc, acked_shift in rows:
            path = csv_path(str(line), str(acked_at or "")[:7] or "不明")
            if path is None:
                return None
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                new = not path.exists()
                fields = CSV_FIELDS
                if not new:
                    with open(path, encoding="utf-8-sig", newline="") as f:
                        fields = next(csv.reader(f), CSV_FIELDS) or CSV_FIELDS
                values = {"報告日": report_date, "直": shift, "ライン": line, "案内": notice_id,
                          "押した人": worker or "", "押した時刻": str(acked_at or "").replace("T", " "),
                          "PC": pc or "", "押した直": acked_shift or shift}
                with open(path, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
                    writer = csv.writer(f)
                    if new:
                        writer.writerow(CSV_FIELDS)
                    writer.writerow([values.get(name, "") for name in fields])
            except OSError as exc:
                log.warning("案内の記録を共有の CSV に書けませんでした(次の見張りでまた): %s", exc)
                return None
            with conn:
                conn.execute("UPDATE ack SET shared=1 WHERE notice_id=? AND report_date=? AND line=?"
                             " AND shift=?", (notice_id, report_date, line, shift))
            written += 1
        return written
    finally:
        conn.close()


@dataclass
class Due:
    notice: Optional[rule.Notice]
    report_date: str = ""
    line: str = ""
    shift: str = ""
    carried: bool = False
    now_shift: str = ""
    problem: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"notice": self.notice.as_dict() if self.notice else None,
                "report_date": self.report_date, "line": self.line, "shift": self.shift,
                "carried": self.carried, "now_shift": self.now_shift, "problem": self.problem,
                "carried_text": (f"{self.report_date} {self.shift} に確認されていません。"
                                 "代わりに確認してください" if self.carried else "")}


def _months(slots: Iterable[rule.Slot], now: datetime) -> set[str]:
    months = {s.business.strftime("%Y-%m") for s in slots}
    months.add(now.strftime("%Y-%m"))
    return months


def _now_shift(times: dict, now: datetime) -> str:
    for slot in rule.shift_slots(times, now, back_days=1, ahead_days=0):
        if slot.shift in rule.MAIN_SHIFTS and slot.start <= now < slot.end:
            return slot.shift
    return ""


def due_now(now: Optional[datetime] = None, *, line: Optional[str] = None) -> Due:
    """この端末のラインで、いま出す案内(1つ。押せば次)。"""
    now = now or datetime.now()
    line = terminal_line().line if line is None else line
    if not line:
        return Due(None)
    try:
        share_pending()
    except Exception:                             # noqa: BLE001 - 次の見張りでまた
        log.exception("案内の記録を共有へ書き直せませんでした")
    found, problem = notices()
    times, _where = shift_times()
    slots = rule.shift_slots(times, now)
    done = set(acks(line, _months(slots, now)))
    hits = rule.due_all(found, line=line, now=now, slots=slots, acked=done,
                        since=first_seen(found, now))
    current = _now_shift(times, now)
    if not hits:
        return Due(None, line=line, now_shift=current, problem=problem)
    hit = hits[0]
    return Due(hit.notice, hit.slot.report_date, line, hit.slot.shift, hit.carried, current, problem)


def find(notice_id: str) -> Optional[rule.Notice]:
    return next((n for n in notices()[0] if n.id == notice_id), None)


def record(notice: rule.Notice, *, report_date: str, line: str, shift: str, worker: str,
           acked_shift: str = "", now: Optional[datetime] = None) -> tuple[bool, str]:
    """押した。この端末に残し(押し直しは上書き)、共有の CSV に書く。(共有に書けたか, 一言)。"""
    from . import remote_stop

    now = now or datetime.now()
    pc, _login = remote_stop.identity()
    conn = _local()
    try:
        with conn:
            conn.execute("INSERT OR REPLACE INTO ack (notice_id, report_date, line, shift, worker,"
                         " acked_at, pc, acked_shift, shared) VALUES (?,?,?,?,?,?,?,?,0)",
                         (notice.id, report_date, line, shift, worker,
                          now.isoformat(timespec="seconds"), pc, acked_shift or shift))
    finally:
        conn.close()
    log.info("案内を押しました %s %s %s %s(押した直 %s): %s", notice.id, report_date, line, shift,
             acked_shift or shift, worker)
    written = share_pending()
    if written is None:
        return False, f"記録しました({worker})。共有の記録には次の見張りで書きます"
    return True, f"記録しました({worker})"


def checks(*, line: Optional[str] = None, days: int = 14, now: Optional[datetime] = None
           ) -> list[dict[str, Any]]:
    """押した/押されていない一覧(新しい回が先)。"""
    now = now or datetime.now()
    line = terminal_line().line if line is None else line
    if not line:
        return []
    found, _problem = notices()
    times, _where = shift_times()
    slots = [s for s in rule.shift_slots(times, now, back_days=days, ahead_days=0)]
    done = acks(line, _months(slots, now))
    out: list[dict[str, Any]] = []
    for notice, slot in rule.windows(found, line=line, now=now, slots=slots,
                                     since=first_seen(found, now)):
        got = done.get((notice.id, slot.report_date, slot.shift))
        out.append({
            "notice": notice.id, "text": notice.text, "report_date": slot.report_date,
            "shift": slot.shift, "line": line, "acked": got is not None,
            "worker": (got or {}).get("worker", ""),
            "acked_at": str((got or {}).get("acked_at", "") or "").replace("T", " ")[:16],
            "acked_shift": (got or {}).get("acked_shift", "") or (slot.shift if got else ""),
            "now": slot.start <= now < slot.end,
        })
    return out
