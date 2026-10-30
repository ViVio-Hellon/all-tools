"""過去の直を後から作る ── **事実を集めて `logic/backfill.decide` に渡す**

集めるもの:

    その直の終わりの時刻  … 時間マスタ(`ShiftCalculator.shift_end_at`)
    手元のページ          … 手元の日報(`saved_pages`)
    共有のページ          … 共有の日報データ(sqlite3 のときだけ読める)
    この端末が打てる直か  … 作業者を選んで固定した直が、まだ終わりの前か

共有は**手元へ写して読みます**(`source_db`。共有のファイルを開かない)。
Access のときや届かないときは読めないので None ── 判断の側が
「確かめられませんでした」と添えて通します。

共有の表のページの列は `頁`(VBA の形・v4.36.0 から作る表)か `ページ`(v4.35.3 まで)。
**その表にあるほうで読みます**(`sqlite_backend.page_column`)。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from ..logging_setup import get_logger
from ..logic import backfill as rule
from ..logic.shift import parse_business_date

log = get_logger("services.backfill")


def shared_pages(report_date: str, line: str, shift: str) -> Optional[list[int]]:
    """共有の日報データに入っているその直のページ。**確かめられなければ None。**"""
    from .. import source_db
    from ..access_bridge import pusher
    from ..config import SETTINGS

    path = SETTINGS.access_db_path
    try:
        if not pusher.is_sqlite_target(path):
            return None                          # Access は読めない
        from ..db.connection import file_state

        # 置き場所に届いて一覧が取れ、ファイルが無い = まだ1度も送っていない。
        # 届かない・権限・見えているのに開けない = 確かめられない(例外 → None)
        if not file_state(path):
            return []
        from ..access_bridge import sqlite_backend

        table = pusher.header_table_name(line)
        # **表の一覧・列を読めなければ確かめられない**(`strict`)。前は読めないと空の一覧に
        # なり「そのラインはまだ1度も送っていない」= 共有にページが無い、と答えていた
        if table not in source_db.list_tables(path, strict=True):
            return []                            # そのラインはまだ1度も送っていない
        page = sqlite_backend.PageColumn(
            sqlite_backend.page_names_of(source_db.columns(path, table, strict=True)))
        rows = source_db.read_query(
            path,
            f'SELECT {page.sql()} AS p FROM {source_db.quote_identifier(table)}'
            ' WHERE "報告日"=? AND "ライン"=? AND "直"=?',
            [report_date, line, shift])
    except Exception as exc:                     # noqa: BLE001 - 読めないなら確かめられない
        log.warning("共有の日報データを確かめられませんでした: %s", exc)
        return None
    out = []
    for row in rows:
        try:
            out.append(int(row["p"]))
        except (TypeError, ValueError):
            continue
    return sorted(set(out))


def check(ctx, calc, repo, report_date: str, line: str, shift: str, *,
          now: Optional[datetime] = None, writing: bool = False) -> rule.Decision:
    """その直を後から作ってよいか(`logic/backfill.decide`)。"""
    now = now or datetime.now()
    business = parse_business_date(report_date)
    shift_end = None
    if business is not None:
        try:
            shift_end = calc.shift_end_at(shift, business)
        except Exception:                        # noqa: BLE001 - 知らない直
            shift_end = None
    return rule.decide(
        admin=bool(ctx.admin), report_date=report_date, line=line, shift=shift,
        shift_end=shift_end, now=now,
        local_pages=repo.saved_pages(report_date, line, shift),
        shared_pages=shared_pages(report_date, line, shift) if ctx.admin else None,
        writing=writing)
