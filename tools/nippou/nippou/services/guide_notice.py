"""案内表示 ── 表を読む・押した記録を残す (v4.47.0。決まりは `logic/guide_notice.py`)

【表はどこにあるか】伝送用ファイル(停止理由・時間用と同じファイル)の「案内表示」。マスタ管理で
直します。sqlite3 のファイルに表が無ければ、**初めて読むときに作り**、頼まれた案内(機側・2直の始まりに
計量器の点検)を1行入れておきます。Access のファイルなら作りません(Access で表を作ってもらう)。

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
from typing import Any, Optional

from ..config import SETTINGS
from ..logging_setup import get_logger
from ..logic import guide_notice as rule

log = get_logger("services.guide_notice")

ACK_TABLE = "guide_notice_ack"
#: 表を読み直すまでの秒数(見張りは1分ごと。共有へ毎回行かない)
CACHE_SEC = 30.0
#: 記録のCSVの列
CSV_FIELDS = ["報告日", "直", "ライン", "案内", "押した人", "押した時刻", "PC"]

_lock = threading.Lock()
_cache: Optional[tuple[float, str, list[rule.Notice], str]] = None


def reset() -> None:
    """読んだ表を忘れる(試験・マスタを直したあと)。"""
    global _cache
    with _lock:
        _cache = None


# ----------------------------------------------------------------------
# 表を読む
# ----------------------------------------------------------------------
def master_path() -> Path:
    return SETTINGS.transmission_master_path


def ensure_table(path: Path) -> bool:
    """sqlite3 のマスタに表が無ければ作り、最初の1行を入れる。作ったら True。"""
    from .. import source_db

    path = Path(path)
    if not path.exists() or not source_db.looks_like_sqlite(path):
        return False
    try:
        with source_db.connect(path) as src:
            if rule.TABLE in src.table_names():
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
                 " shared INTEGER NOT NULL DEFAULT 0,"
                 " PRIMARY KEY (notice_id, report_date, line, shift))")


def acked(repo, report_date: str, line: str, shift: str) -> set[str]:
    """その直で押した案内の ID。"""
    _ack_table(repo.conn)
    return {str(r[0]) for r in repo.conn.execute(
        f"SELECT notice_id FROM {ACK_TABLE} WHERE report_date=? AND line=? AND shift=?",
        (report_date, line, shift))}


def history(repo, limit: int = 50) -> list[dict[str, Any]]:
    """この端末で押した記録(新しい順)。"""
    _ack_table(repo.conn)
    rows = repo.conn.execute(
        f"SELECT notice_id, report_date, line, shift, worker, acked_at, pc, shared FROM {ACK_TABLE}"
        " ORDER BY acked_at DESC LIMIT ?", (limit,)).fetchall()
    keys = ("notice_id", "report_date", "line", "shift", "worker", "acked_at", "pc", "shared")
    return [dict(zip(keys, r)) for r in rows]


@dataclass
class Acked:
    ok: bool
    message: str
    shared_path: str = ""


def record(repo, notice: rule.Notice, *, report_date: str, line: str, shift: str,
           worker: str, now: Optional[datetime] = None) -> Acked:
    """押した。この端末のDBに残し(同じ直・同じ案内は1つ)、共有のCSVと出来事の記録にも書く。"""
    from ..db.repository import this_pc
    from . import event_log

    now = now or datetime.now()
    at = now.isoformat(timespec="seconds")
    with repo.conn:
        _ack_table(repo.conn)
        repo.conn.execute(
            f"INSERT OR IGNORE INTO {ACK_TABLE}"
            " (notice_id, report_date, line, shift, worker, acked_at, pc, shared)"
            " VALUES (?,?,?,?,?,?,?,0)",
            (notice.id, report_date, line, shift, worker, at, this_pc()))
    try:
        event_log.note(f"案内「{notice.id}」を押しました: {worker or '(名前なし)'}",
                       notice=notice.id, report_date=report_date, line=line, shift=shift,
                       worker=worker)
    except Exception:                             # noqa: BLE001 - 記録は DB に残っている
        log.exception("出来事の記録に残せませんでした")
    log.info("案内を押しました %s %s %s %s: %s", notice.id, report_date, line, shift, worker)
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
    """共有のCSVへまだ書いていない記録を書く。書いた数(書けなければ None。次の見張りでまた)。"""
    _ack_table(repo.conn)
    rows = repo.conn.execute(
        f"SELECT notice_id, report_date, line, shift, worker, acked_at, pc FROM {ACK_TABLE}"
        " WHERE shared=0 ORDER BY acked_at").fetchall()
    written = 0
    for notice_id, report_date, line, shift, worker, acked_at, pc in rows:
        path = csv_path(str(line), str(acked_at or "")[:7] or "不明")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            new = not path.exists()
            with open(path, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
                writer = csv.writer(f)
                if new:
                    writer.writerow(CSV_FIELDS)
                writer.writerow([report_date, shift, line, notice_id, worker or "",
                                 str(acked_at or "").replace("T", " "), pc or ""])
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

    def as_dict(self) -> dict[str, Any]:
        return {"notice": self.notice.as_dict() if self.notice else None,
                "report_date": self.report_date, "line": self.line, "shift": self.shift,
                "worker": self.worker, "problem": self.problem}


def where_now(calc, now: datetime, *, force_day_shift: bool = False
              ) -> Optional[tuple[str, str, datetime, datetime, Any]]:
    """(報告日の字, 直, 始まり, 終わり, 報告日) ── 時計の直。分からなければ None。"""
    from .nippou_service import format_business_date

    shift = calc.time_check(now, force_day_shift)
    business = calc.today_check(now, force_day_shift)
    start_t = calc.current_shift_start_time(shift)
    end_at = calc.shift_end_at(shift, business)
    if start_t is None or end_at is None:
        return None
    return format_business_date(business), shift, datetime.combine(business, start_t), end_at, business


def due_now(repo, calc, line: str, now: Optional[datetime] = None, *,
            force_day_shift: bool = False) -> Due:
    """この端末のライン(ライン設定)・時計の直で、いま出す案内(1つ。押せば次)。"""
    now = now or datetime.now()
    if not line:
        return Due(None)
    try:
        share_pending(repo)                       # 前に書けなかった記録
    except Exception:                             # noqa: BLE001
        log.exception("案内の記録を共有へ書き直せませんでした")
    found, problem = notices()
    where = where_now(calc, now, force_day_shift=force_day_shift)
    if where is None:
        return Due(None, line=line, problem=problem)
    report_date, shift, start, end, business = where
    hits = rule.due(found, line=line, shift=shift, business_date=business, now=now,
                    shift_start=start, shift_end=end,
                    acked=acked(repo, report_date, line, shift))
    if not hits:
        return Due(None, report_date, line, shift, problem=problem)
    return Due(hits[0], report_date, line, shift, worker=_worker(repo, report_date, line, shift),
               problem=problem)


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
