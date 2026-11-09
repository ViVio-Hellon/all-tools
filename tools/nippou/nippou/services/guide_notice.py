"""案内表示 ── 表を読む・押した記録を残す (v4.47.0。決まりは `logic/guide_notice.py`)

【表はどこにあるか】伝送用ファイル(停止理由・時間用と同じファイル)の「案内表示」。マスタ管理で
直します。表が無ければ、**初めて読むときに作り**、頼まれた案内(機側・2直の始まりに計量器の点検)を
1行入れておきます。sqlite3 はその場で、Access(.accdb)は共有へ保存と同じ道(VBScript か ODBC)で作ります
(Access は10分に1回まで確かめる。ほかの人が開いていて作れなければ10分後にもう一度)。

【押した記録】押したら3か所に残します。

    この端末のDB(`guide_notice_ack`)  その直でもう押したか(押すまで毎分出す・押したら出さない)
    出力先(共有)の CSV                 <出力先>\\<ライン>\\案内の確認\\案内の確認_2026-11.csv
                                        (誰がいつ押したかを、あとで誰でも開いて確かめられる)
    出来事の記録                          ログの画面にも並ぶ

CSV に書けなかったぶん(共有に届かない)は覚えておき、次の見張りで書きます。
"""
from __future__ import annotations

import csv
import threading
import time as _time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from ..config import SETTINGS
from ..logging_setup import get_logger
from ..logic import guide_notice as rule

log = get_logger("services.guide_notice")

ACK_TABLE = "guide_notice_ack"
#: 表を読み直すまでの秒数(見張りは1分ごと。共有へ毎回行かない)
CACHE_SEC = 30.0
#: 記録のCSVの列
CSV_FIELDS = ["報告日", "直", "ライン", "案内", "押した人", "押した時刻", "PC", "押した直"]

_lock = threading.Lock()
_cache: Optional[tuple[float, str, list[rule.Notice], str]] = None


def reset() -> None:
    """読んだ表を忘れる(試験・マスタを直したあと)。"""
    global _cache
    with _lock:
        _cache = None
        _access_tried.clear()


# ----------------------------------------------------------------------
# 表を読む
# ----------------------------------------------------------------------
def master_path() -> Path:
    return SETTINGS.transmission_master_path


#: Access のマスタで表を作ろうとした時刻(道ごと)。cscript を毎回は走らせない
_access_tried: dict[str, float] = {}
#: Access で作れなかったとき、もう一度試すまでの秒数(ほかの人が開いていて錠が取れない など)
ACCESS_RETRY_SEC = 600.0
ACCESS_SUFFIXES = (".accdb", ".mdb")


def _access_literal(value: Any) -> str:
    if value is None or value == "":
        return "NULL"
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def access_statements() -> list[str]:
    """Access で表を作り、最初の1行を入れる SQL。長くなる文は MEMO(255字を超えてよい)。"""
    long_text = {"表示文", "確認の文"}
    cols = ", ".join(
        f"[{name}] " + ("INTEGER" if kind == "INTEGER" else "MEMO" if name in long_text else "TEXT(255)")
        for name, kind in rule.COLUMNS)
    names = ", ".join(f"[{name}]" for name, _ in rule.COLUMNS)
    values = ", ".join(_access_literal(rule.FIRST_ROW.get(name)) for name, _ in rule.COLUMNS)
    return [f"CREATE TABLE [{rule.TABLE}] ({cols})",
            f"INSERT INTO [{rule.TABLE}] ({names}) VALUES ({values})"]


def ensure_access_table(path: Path, *, runner=None, now: Optional[float] = None) -> bool:
    """Access のマスタに表が無ければ作り、最初の1行を入れる(共有へ保存と同じ道 ── VBScript か ODBC)。
    作ったら True。表があるか確かめるのは10分に1回まで(cscript を毎回走らせない)。"""
    from ..access_bridge import script_gen
    from ..access_bridge.importer import import_table
    from ..access_bridge.runner import run_with_retry

    path = Path(path)
    if path.suffix.lower() not in ACCESS_SUFFIXES or not path.exists():
        return False
    t = _time.monotonic() if now is None else now
    with _lock:
        tried = _access_tried.get(str(path))
        if tried is not None and t - tried < ACCESS_RETRY_SEC:
            return False
        _access_tried[str(path)] = t
    try:
        found = import_table(path, rule.TABLE, runner=runner)
        if found.success:
            # もうある。**前の版で作った表に足りない列を足す**(曜日・過ぎても出す)。列は行から
            # 読むので、行が1つも無い表では分からない(そのときは足さない ── 空の列は既定どおり)
            have = set(found.rows[0]) if found.rows else set()
            missing = [(n, k) for n, k in rule.COLUMNS if have and n not in have]
            if not missing:
                return False
            statements = [f"ALTER TABLE [{rule.TABLE}] ADD COLUMN [{n}] "
                          + ("INTEGER" if k == "INTEGER" else "TEXT(255)") for n, k in missing]
        else:
            statements = access_statements()
        if SETTINGS.access_backend == "odbc":
            from ..access_bridge.odbc_backend import push_statements_odbc
            result = push_statements_odbc(path, statements)
        else:
            result = run_with_retry(lambda: script_gen.build_push_script(str(path), statements),
                                    runner=runner)
    except Exception:                             # noqa: BLE001 - 作れなくても日報は止めない
        log.exception("案内表示の表を作れませんでした(Access): %s", path)
        return False
    if not result.success:
        log.warning("案内表示の表を作れませんでした(Access。10分後にもう一度): %s: %s",
                    path, getattr(result, "error", "") or getattr(result, "err_desc", ""))
        return False
    log.info("伝送用ファイル(Access)に「%s」の表を作り、最初の案内を入れました: %s", rule.TABLE, path)
    return True


def ensure_table(path: Path) -> bool:
    """マスタに表が無ければ作り、最初の1行を入れる。作ったら True。
    sqlite3 はここで、Access は `ensure_access_table`(共有へ保存と同じ道)で作る。"""
    from .. import source_db

    path = Path(path)
    if path.suffix.lower() in ACCESS_SUFFIXES:
        return ensure_access_table(path)
    if not path.exists() or not source_db.looks_like_sqlite(path):
        return False
    # **まず写しで見る**(共有のファイルは、作る・足すときだけ開く ── `source_db` の約束。
    # 見張りのたびに全PCが共有のマスタを書ける形で開かない)
    try:
        if rule.TABLE in source_db.list_tables(path, strict=True):
            have = set(source_db.columns(path, rule.TABLE, strict=True))
            if all(name in have for name, _ in rule.COLUMNS):
                return False
    except Exception:                             # noqa: BLE001 - 読めなければ作らない(次の回)
        log.exception("案内表示の表があるか確かめられませんでした: %s", path)
        return False
    try:
        with source_db.connect(path) as src:
            if rule.TABLE in src.table_names():
                # **前の版で作った表に、あとから増えた列を足す**(「過ぎても出す」v4.47.2)。
                # 空のままなら決まり(`logic/guide_notice`)の既定どおりに読む
                have = {r["name"] for r in src.query(
                    f"PRAGMA table_info({source_db.quote_identifier(rule.TABLE)})")}
                missing = [(n, t) for n, t in rule.COLUMNS if n not in have]
                for name, kind in missing:
                    src.execute(f"ALTER TABLE {source_db.quote_identifier(rule.TABLE)}"
                                f" ADD COLUMN {source_db.quote_identifier(name)} {kind}")
                if not missing:
                    return False
                source_db.forget(path)
                log.info("「%s」に列を足しました: %s", rule.TABLE, ", ".join(n for n, _ in missing))
                return False
            cols = ", ".join(f"{source_db.quote_identifier(n)} {t}" for n, t in rule.COLUMNS)
            src.execute(f"CREATE TABLE IF NOT EXISTS {source_db.quote_identifier(rule.TABLE)} ({cols})")
            if not src.query(f"SELECT 1 FROM {source_db.quote_identifier(rule.TABLE)} LIMIT 1"):
                src.insert(rule.TABLE, dict(rule.FIRST_ROW))
    except Exception:                             # noqa: BLE001 - 作れなくても日報は止めない
        log.exception("案内表示の表を作れませんでした: %s", path)
        return False
    source_db.forget(path)
    log.info("伝送用ファイルに「%s」の表を作り、最初の案内を入れました: %s", rule.TABLE, path)
    return True


def notices() -> tuple[list[rule.Notice], str]:
    """(案内, 読めなかった理由)。理由が空でなければ読めなかった(案内は出さない)。"""
    global _cache
    from ..access_bridge.importer import import_table

    path = master_path()
    now = _time.monotonic()
    with _lock:
        if _cache is not None and _cache[1] == str(path) and now - _cache[0] < CACHE_SEC:
            return list(_cache[2]), _cache[3]
    found: list[rule.Notice] = []
    problem = ""
    try:
        ensure_table(path)
        result = import_table(path, rule.TABLE, missing_ok=True)
        if result.success:
            found = rule.parse(result.rows)
        else:
            problem = str(result.error or f"{rule.TABLE} を読めませんでした")
    except Exception as exc:                      # noqa: BLE001 - 案内が出ないだけ
        log.exception("案内表示を読めませんでした: %s", path)
        problem = f"{rule.TABLE} を読めませんでした: {exc}"
    with _lock:
        _cache = (now, str(path), found, problem)
    return list(found), problem


# ----------------------------------------------------------------------
# 押した記録
# ----------------------------------------------------------------------
def _ack_table(conn) -> None:
    conn.execute(f"CREATE TABLE IF NOT EXISTS {ACK_TABLE} ("
                 " notice_id TEXT NOT NULL, report_date TEXT NOT NULL, line TEXT NOT NULL,"
                 " shift TEXT NOT NULL, worker TEXT, acked_at TEXT, pc TEXT,"
                 " shared INTEGER NOT NULL DEFAULT 0, acked_shift TEXT,"
                 " PRIMARY KEY (notice_id, report_date, line, shift))")
    names = {r[1] for r in conn.execute(f"PRAGMA table_info({ACK_TABLE})")}
    if "acked_shift" not in names:                # v4.47.0 で作った表(押した直が無い)
        conn.execute(f"ALTER TABLE {ACK_TABLE} ADD COLUMN acked_shift TEXT")


def _local_acks(repo, line: str) -> list[dict[str, Any]]:
    _ack_table(repo.conn)
    keys = ("notice_id", "report_date", "line", "shift", "worker", "acked_at", "pc", "shared",
            "acked_shift")
    return [dict(zip(keys, r)) for r in repo.conn.execute(
        f"SELECT {', '.join(keys)} FROM {ACK_TABLE} WHERE line=?", (line,))]


def _shared_acks(line: str, months: Iterable[str]) -> Optional[list[dict[str, Any]]]:
    """共有のCSVの記録(**ほかのPCで押したぶんも**入る)。月のファイルが無ければその月は空。
    **出力先そのものに届かなければ None**(共有が落ちているだけで、押した記録が消えたとみなさない)。"""
    try:
        if not Path(SETTINGS.report_output_dir).is_dir():
            return None
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
        except (OSError, UnicodeError, csv.Error):
            continue
    return out


def acks(repo, line: str, months: Iterable[str]) -> dict[tuple[str, str, str], dict[str, Any]]:
    """押した記録 (案内ID, 報告日, 直) → 1件。

    **正は共有のCSV**(ほかのPCで押したぶんも入る)。この端末のDBからは、まだ共有へ書けていない
    ぶんだけを足す ── 共有へ書けたぶんは CSV を見る。だから**CSV を消す・よそへ移すと、また出る**:

        案内の確認内のCSVを消しても再案内はしないんですね
        テストがやりにくいのでファイルを消したり退避させたら再表示するようにしてください

    出力先そのものに届かないとき(共有が落ちている)は、この端末のDBで決める(落ちただけで
    全部また出さない)。"""
    shared = _shared_acks(line, months)
    local = _local_acks(repo, line)
    rows = local if shared is None else shared + [r for r in local if not r.get("shared")]
    found: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        if row.get("line") and row["line"] != line:
            continue
        found.setdefault((str(row["notice_id"]), str(row["report_date"]), str(row["shift"])), row)
    return found


SEEN_TABLE = "guide_notice_seen"


def first_seen(repo, notices_: Iterable[rule.Notice], now: datetime) -> dict[str, datetime]:
    """案内ごとに、この端末が**初めて見た時刻**(無ければ `now` として覚える)。

        月曜に案内を作ったのに、作る前の週の火曜の分まで「押されていません」と出た(v4.47.4)

    それより前に終わった回は、あとの直へ回さない・押されていない一覧に入れない。"""
    with repo.conn:
        repo.conn.execute(f"CREATE TABLE IF NOT EXISTS {SEEN_TABLE} ("
                          " notice_id TEXT PRIMARY KEY, first_seen TEXT NOT NULL)")
        repo.conn.executemany(
            f"INSERT OR IGNORE INTO {SEEN_TABLE} (notice_id, first_seen) VALUES (?, ?)",
            [(n.id, now.isoformat(timespec="seconds")) for n in notices_])
    out: dict[str, datetime] = {}
    for notice_id, at in repo.conn.execute(f"SELECT notice_id, first_seen FROM {SEEN_TABLE}"):
        try:
            out[str(notice_id)] = datetime.fromisoformat(str(at))
        except ValueError:
            continue
    return out


def acked(repo, report_date: str, line: str, shift: str) -> set[str]:
    """その直で押した案内の ID(この端末のDB)。"""
    return {r["notice_id"] for r in _local_acks(repo, line)
            if (r["report_date"], r["shift"]) == (report_date, shift)}


def history(repo, limit: int = 50) -> list[dict[str, Any]]:
    """この端末で押した記録(新しい順)。"""
    _ack_table(repo.conn)
    rows = repo.conn.execute(
        f"SELECT notice_id, report_date, line, shift, worker, acked_at, pc, shared, acked_shift"
        f" FROM {ACK_TABLE} ORDER BY acked_at DESC LIMIT ?", (limit,)).fetchall()
    keys = ("notice_id", "report_date", "line", "shift", "worker", "acked_at", "pc", "shared",
            "acked_shift")
    return [dict(zip(keys, r)) for r in rows]


@dataclass
class Acked:
    ok: bool
    message: str
    shared_path: str = ""


def record(repo, notice: rule.Notice, *, report_date: str, line: str, shift: str,
           worker: str, acked_shift: str = "", now: Optional[datetime] = None) -> Acked:
    """押した。この端末のDBに残し(同じ直・同じ案内は1つ)、共有のCSVと出来事の記録にも書く。
    `shift` はその案内の直(2直)、`acked_shift` は押した直(2直の案内を3直で押したなら3直)。"""
    from ..db.repository import this_pc
    from . import event_log

    now = now or datetime.now()
    at = now.isoformat(timespec="seconds")
    with repo.conn:
        _ack_table(repo.conn)
        # **押し直しは上書き**(CSV を消して出し直した案内を押したら、押した人・時刻を新しくして
        # もう一度共有へ書く)
        repo.conn.execute(
            f"INSERT OR REPLACE INTO {ACK_TABLE}"
            " (notice_id, report_date, line, shift, worker, acked_at, pc, shared, acked_shift)"
            " VALUES (?,?,?,?,?,?,?,0,?)",
            (notice.id, report_date, line, shift, worker, at, this_pc(), acked_shift or shift))
    try:
        event_log.note(f"案内「{notice.id}」を押しました: {worker or '(名前なし)'}",
                       notice=notice.id, report_date=report_date, line=line, shift=shift,
                       acked_shift=acked_shift or shift, worker=worker)
    except Exception:                             # noqa: BLE001 - 記録は DB に残っている
        log.exception("出来事の記録に残せませんでした")
    log.info("案内を押しました %s %s %s %s(押した直 %s): %s", notice.id, report_date, line, shift,
             acked_shift or shift, worker)
    written = share_pending(repo)
    path = csv_path(line, now.strftime("%Y-%m"))
    if written is None:
        return Acked(True, f"記録しました({worker})。共有の記録には次の見張りで書きます", "")
    return Acked(True, f"記録しました({worker})", str(path))


def csv_path(line: str, month: str) -> Path:
    from ..reporting import csv_export

    return (csv_export.line_dir(SETTINGS.report_output_dir, line, "案内の確認")
            / f"案内の確認_{month}.csv")


def share_pending(repo) -> Optional[int]:
    """共有のCSVへまだ書いていない記録を書く。書いた数(書けなければ None。次の見張りでまた)。

    月のファイルは**押した月**。前の版で作ったファイル(押した直の列が無い)には、列を足さずに
    そのまま書き足す(列の数が違う行が混ざらないよう、そのファイルの見出しに合わせる)。"""
    _ack_table(repo.conn)
    rows = repo.conn.execute(
        f"SELECT notice_id, report_date, line, shift, worker, acked_at, pc, acked_shift FROM {ACK_TABLE}"
        " WHERE shared=0 ORDER BY acked_at").fetchall()
    written = 0
    for notice_id, report_date, line, shift, worker, acked_at, pc, acked_shift in rows:
        path = csv_path(str(line), str(acked_at or "")[:7] or "不明")
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
            log.warning("案内の記録を共有のCSVに書けませんでした(次の見張りでまた): %s", exc)
            return None
        with repo.conn:
            repo.conn.execute(
                f"UPDATE {ACK_TABLE} SET shared=1 WHERE notice_id=? AND report_date=? AND line=?"
                " AND shift=?", (notice_id, report_date, line, shift))
        written += 1
    return written


# ----------------------------------------------------------------------
# 直の回
# ----------------------------------------------------------------------
#: 過ぎた直を何日さかのぼって見るか(あとの直で出す)。**1週間**: 曜日を決めた案内(月曜だけ など)は
#: 次の同じ曜日・同じ直が始まるまで出すので、2日では足りない(月曜のぶんを木曜に開くと出なかった ──
#: v4.47.4)。毎日の案内は、いちばん近い回だけを出すので長くしても変わらない
LOOK_BACK_DAYS = 7


def _slot(calc, business, shift: str) -> Optional[rule.Slot]:
    from .nippou_service import format_business_date

    start_t = calc.current_shift_start_time(shift)
    end_at = calc.shift_end_at(shift, business)
    if start_t is None or end_at is None:
        return None
    return rule.Slot(format_business_date(business), business, shift,
                     datetime.combine(business, start_t), end_at)


def slots(calc, now: datetime, *, days: int = LOOK_BACK_DAYS, force_day_shift: bool = False
          ) -> tuple[Optional[rule.Slot], list[rule.Slot]]:
    """(いまの直の回, 過ぎた直の回)。過ぎた回は `days` 日前の報告日から。"""
    from datetime import timedelta

    from ..logic.shift import SHIFT_NAMES

    shift = calc.time_check(now, force_day_shift)
    business = calc.today_check(now, force_day_shift)
    current = _slot(calc, business, shift)
    past: list[rule.Slot] = []
    for back in range(days, -1, -1):
        day = business - timedelta(days=back)
        for name in SHIFT_NAMES:
            got = _slot(calc, day, name)
            if got is not None and got.end <= now and (current is None or got.key != current.key):
                past.append(got)
    return current, past


def _months(slots_: Iterable[rule.Slot], now: datetime) -> set[str]:
    months = {s.business.strftime("%Y-%m") for s in slots_}
    months.add(now.strftime("%Y-%m"))             # 押した月のファイル(過ぎた回を今月押した)
    return months


# ----------------------------------------------------------------------
# いま出すもの
# ----------------------------------------------------------------------
@dataclass
class Due:
    notice: Optional[rule.Notice]
    report_date: str = ""
    line: str = ""
    shift: str = ""
    worker: str = ""
    problem: str = ""
    #: 過ぎた直のぶんを、いまの直で出している
    carried: bool = False
    #: いまの直(押した直として残す)
    now_shift: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"notice": self.notice.as_dict() if self.notice else None,
                "report_date": self.report_date, "line": self.line, "shift": self.shift,
                "worker": self.worker, "problem": self.problem, "carried": self.carried,
                "now_shift": self.now_shift,
                "carried_text": (f"{self.report_date} {self.shift} に確認されていません。"
                                 "代わりに確認してください" if self.carried else "")}


def where_now(calc, now: datetime, *, force_day_shift: bool = False
              ) -> Optional[tuple[str, str, datetime, datetime, Any]]:
    """(報告日の字, 直, 始まり, 終わり, 報告日) ── 時計の直。分からなければ None。"""
    current, _past = slots(calc, now, days=0, force_day_shift=force_day_shift)
    if current is None:
        return None
    return current.report_date, current.shift, current.start, current.end, current.business


def due_now(repo, calc, line: str, now: Optional[datetime] = None, *,
            force_day_shift: bool = False) -> Due:
    """この端末のライン(ライン設定)・時計の直で、いま出す案内(1つ。押せば次)。
    いまの直のぶんと、押されないまま過ぎた直のぶん(`rule.due_slots`)。"""
    now = now or datetime.now()
    if not line:
        return Due(None)
    try:
        share_pending(repo)                       # 前に書けなかった記録
    except Exception:                             # noqa: BLE001
        log.exception("案内の記録を共有へ書き直せませんでした")
    found, problem = notices()
    current, past = slots(calc, now, force_day_shift=force_day_shift)
    if current is None:
        return Due(None, line=line, problem=problem)
    every = ([current] if current else []) + past
    done = set(acks(repo, line, _months(every, now)))
    hits = rule.due_slots(found, line=line, now=now, current=current, past=past, acked=done,
                          since=first_seen(repo, found, now))
    if not hits:
        return Due(None, current.report_date, line, current.shift, problem=problem,
                   now_shift=current.shift)
    hit = hits[0]
    return Due(hit.notice, hit.slot.report_date, line, hit.slot.shift,
               worker=_worker(repo, current.report_date, line, current.shift),
               problem=problem, carried=hit.carried, now_shift=current.shift)


def checks(repo, calc, line: str, *, days: int = 14, now: Optional[datetime] = None,
           force_day_shift: bool = False) -> list[dict[str, Any]]:
    """押した/押されていない一覧(新しい回が先)。`days` 日前の報告日から、いまの直まで。"""
    now = now or datetime.now()
    found, _problem = notices()
    current, past = slots(calc, now, days=days, force_day_shift=force_day_shift)
    every = ([current] if current else []) + past
    done = acks(repo, line, _months(every, now))
    out: list[dict[str, Any]] = []
    for notice, slot in rule.windows(found, line=line, now=now, slots=every,
                                     since=first_seen(repo, found, now)):
        got = done.get((notice.id, slot.report_date, slot.shift))
        out.append({
            "notice": notice.id, "text": notice.text, "report_date": slot.report_date,
            "date_key": slot.business.isoformat(), "shift": slot.shift, "line": line,
            "acked": got is not None,
            "worker": (got or {}).get("worker", ""),
            "acked_at": str((got or {}).get("acked_at", "") or "").replace("T", " ")[:16],
            "acked_shift": (got or {}).get("acked_shift", "") or (slot.shift if got else ""),
            "now": current is not None and slot.key == current.key,
        })
    return out


def _worker(repo, report_date: str, line: str, shift: str) -> str:
    """その直で選んである作業者(最初の1人)。まだなら空。"""
    try:
        for page in repo.saved_pages(report_date, line, shift):
            loaded = repo.load(report_date, line, shift, page)
            if loaded and loaded[0].worker:
                return str(loaded[0].worker).split()[0]
    except Exception:                             # noqa: BLE001 - 選んだ状態にしないだけ
        log.exception("作業者を読めませんでした")
    return ""


def find(notice_id: str) -> Optional[rule.Notice]:
    for notice in notices()[0]:
        if notice.id == notice_id:
            return notice
    return None
