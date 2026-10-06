"""コンバート ── 共有の日報データの、前の名前の表を正規の名前の表へ写す(v4.13.0)

    ・コードを正規の呼び名に置き換える
    ・コンバート(管理者だけが押せるボタン)
    これがあれば任意のタイミングで変えれるんですよね？
    このツールの機能として設置すると重くなりますか？

【どの表を】
VBA が書いている `T_日報ヘッダー_<前>` / `T_日報明細_<前>` を、`T_日報ヘッダー_<正規>` /
`T_日報明細_<正規>` へ。中の「ライン」列も正規へ揃えます。写すのは**前と正規で字が
違う5ライン**(L-1・機側・トット・バランサー・中板)だけです ── LVC・HVC・NS1・AIM は
表の名前も中身も同じなので、写すものがありません(VBA とこのツールが同じ表を使う)。

【前の表には触らない】
読むだけです。VBA はそのまま書き続けられ、コンバートは何度でもやり直せます。
ページごとの決め方は `logic/line_rename.decide`(写す・写し直す・このツールの分を残す…)。

【控えの表】
写したページと、そのときの保存日時を `T_ライン名変換` に残します(共有の日報データの中。
VBA は知らない表なので、触りません)。次に押したとき、「前に写したまま」かどうかを
これで見分けます。

【書かずに試す / 実行】
試すときは**共有のファイルを開かず、写しを読みます**(`source_db` ── ほかの読み取りと
同じ)。実行するときだけ共有のファイルを開き、**1ラインずつ1つのトランザクション**で
書きます(途中で失敗したラインは何も残さない。ほかのラインは進む)。書いたあと、
写したページの見出しと明細の数を前の表と突き合わせ、合わなければ**そのラインは取り消します。**

【重さ】
押したときだけ動きます(起動・保存のときは何もしません)。5年分・明細82万行の架空の
データで、1回目 2.7秒・2回目から 0.5秒でした(ローカルのディスク)。

【Access のとき】
日報データが `.accdb` のときは使えません(sqlite3 のときだけ)。読み取り(`backfill`)と
同じ線引きです。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ..access_bridge import pusher, sqlite_backend as backend
from ..config import SETTINGS
from ..logging_setup import get_logger
from ..logic import line_names
from ..logic import line_rename as rule

log = get_logger("services.line_rename")

#: 写したページの控え(共有の日報データの中)
TRACK_TABLE = "T_ライン名変換"
TRACK_COLUMNS: tuple[str, ...] = ("ライン", "報告日", "直", "ページ", "旧ライン", "保存日時",
                                  "写した日時")
TRACK_KEY: tuple[str, ...] = ("ライン", "報告日", "直", "ページ")

HEADER_PREFIX = "T_日報ヘッダー_"
DETAIL_PREFIX = "T_日報明細_"

@dataclass
class LineResult:
    """1つのラインの結果。"""

    official: str
    old: str
    found: bool = False                   # 前の表があったか
    old_pages: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    #: 前の表にあるが、ライン列が前の名前でない行(写さない)
    other_rows: int = 0
    #: 同じページ・行番号が2回ある明細(1つだけ写した)
    duplicates: int = 0
    #: 正規の表に無い列(写さなかった)
    dropped_columns: list[str] = field(default_factory=list)
    checked: str = ""
    error: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def tables(self) -> str:
        return f"{HEADER_PREFIX}{self.old} → {HEADER_PREFIX}{self.official}"

    def as_dict(self) -> dict[str, Any]:
        return {"official": self.official, "old": self.old, "found": self.found,
                "tables": self.tables, "old_pages": self.old_pages,
                "counts": dict(self.counts), "other_rows": self.other_rows,
                "duplicates": self.duplicates, "dropped_columns": list(self.dropped_columns),
                "checked": self.checked, "error": self.error, "notes": list(self.notes)}


@dataclass
class Report:
    """1回ぶんの結果(書かずに試した / 実行した)。"""

    dry_run: bool
    path: str = ""
    lines: list[LineResult] = field(default_factory=list)
    #: 前でも正規でもない名前の表(`T_日報ヘッダー_コイル` など)
    unknown_tables: list[str] = field(default_factory=list)
    #: 前と正規が同じ字のライン(写すものが無い)
    same_name: list[str] = field(default_factory=list)
    error: str = ""
    at: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and not any(line.error for line in self.lines)

    def total(self, kind: str) -> int:
        return sum(line.counts.get(kind, 0) for line in self.lines)

    def message(self) -> str:
        """1行で言う(画面の知らせ)。"""
        if self.error:
            return self.error
        head = "書かずに試しました" if self.dry_run else "コンバートしました"
        verb = "写すページ" if self.dry_run else "写したページ"
        parts = [f"{verb} {self.total(rule.COPY)}",
                 f"写し直し {self.total(rule.RECOPY)}",
                 f"写し済み {self.total(rule.SAME)}"]
        if self.total(rule.KEEP):
            parts.append(f"このツールの分(残す) {self.total(rule.KEEP)}")
        if self.total(rule.GONE):
            parts.append(f"VBA で消した {self.total(rule.GONE)}")
        text = f"{head}: " + "・".join(parts)
        failed = [line.official for line in self.lines if line.error]
        if failed:
            text += f"(できなかったライン: {'・'.join(failed)})"
        return text

    def as_dict(self) -> dict[str, Any]:
        return {"dry_run": self.dry_run, "ok": self.ok, "message": self.message(),
                "path": self.path, "at": self.at, "error": self.error,
                "kinds": list(rule.KINDS),
                "lines": [line.as_dict() for line in self.lines],
                "unknown_tables": list(self.unknown_tables),
                "same_name": list(self.same_name)}


def quote(name: str) -> str:
    return backend.quote(name)


# ==================================================================
# 読む
# ==================================================================
def _tables(conn) -> set[str]:
    return {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def _columns(conn, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({quote(table)})")]


def _rows(conn, table: str) -> list[dict[str, Any]]:
    cursor = conn.execute(f"SELECT * FROM {quote(table)}")
    names = [d[0] for d in cursor.description or []]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def unknown_tables(tables: set[str]) -> list[str]:
    """前でも正規でもない名前の日報の表。**黙って捨てない**(画面に並べる)。"""
    known = {name for d in line_names.DEFINITIONS for name in (d.official, d.old)}
    out = []
    for table in sorted(tables):
        for prefix in (HEADER_PREFIX, DETAIL_PREFIX):
            if table.startswith(prefix) and table[len(prefix):] not in known:
                out.append(table)
    return out


@dataclass
class _Old:
    """前の表から読んだもの(1つのライン)。"""

    headers: dict[rule.PageKey, dict[str, Any]] = field(default_factory=dict)
    details: dict[rule.PageKey, dict[object, dict[str, Any]]] = field(default_factory=dict)
    saved: dict[rule.PageKey, str] = field(default_factory=dict)
    other_rows: int = 0
    duplicates: int = 0
    dropped: list[str] = field(default_factory=list)


def _read_old(conn, d: line_names.LineName, tables: set[str]) -> _Old:
    out = _Old()
    header_table, detail_table = HEADER_PREFIX + d.old, DETAIL_PREFIX + d.old
    extra: set[str] = set()
    if header_table in tables:
        extra |= set(_columns(conn, header_table)) - set(backend.HEADER_COLUMNS)
        for row in _rows(conn, header_table):
            if line_names.same(row.get("ライン")) != d.old:
                out.other_rows += 1
                continue
            key = rule.page_key(row)
            out.headers[key] = row
            out.saved[key] = rule.text_of(row.get("保存日時"))
    if detail_table in tables:
        extra |= set(_columns(conn, detail_table)) - set(backend.DETAIL_COLUMNS)
        for row in _rows(conn, detail_table):
            if line_names.same(row.get("ライン")) != d.old:
                out.other_rows += 1
                continue
            key = rule.page_key(row)
            rows = out.details.setdefault(key, {})
            number = rule.number_of(row.get("行番号"))
            if number in rows:
                out.duplicates += 1            # 先に出たほうを写す
                continue
            rows[number] = row
            out.saved.setdefault(key, "")      # 見出しの無いページ
    out.dropped = sorted(extra)
    return out


def _read_new(conn, d: line_names.LineName, tables: set[str]) -> dict[rule.PageKey, str]:
    """正規の表のページ → 保存日時(明細だけのページは空)。"""
    found: dict[rule.PageKey, str] = {}
    header_table, detail_table = HEADER_PREFIX + d.official, DETAIL_PREFIX + d.official
    if header_table in tables:
        for row in conn.execute(
                f'SELECT "報告日", "直", "ページ", "保存日時" FROM {quote(header_table)}'
                ' WHERE "ライン"=?', (d.official,)):
            values = tuple(row)
            found[rule.page_key(dict(zip(("報告日", "直", "ページ"), values[:3])))] = \
                rule.text_of(values[3])
    if detail_table in tables:
        for row in conn.execute(
                f'SELECT DISTINCT "報告日", "直", "ページ" FROM {quote(detail_table)}'
                ' WHERE "ライン"=?', (d.official,)):
            found.setdefault(rule.page_key(dict(zip(("報告日", "直", "ページ"), row))), "")
    return found


def _read_copied(conn, d: line_names.LineName, tables: set[str]) -> dict[rule.PageKey, str]:
    if TRACK_TABLE not in tables:
        return {}
    return {rule.page_key({"報告日": row[0], "直": row[1], "ページ": row[2]}): rule.text_of(row[3])
            for row in conn.execute(
                f'SELECT "報告日", "直", "ページ", "保存日時" FROM {quote(TRACK_TABLE)}'
                ' WHERE "ライン"=?', (d.official,))}


def _survey(conn, d: line_names.LineName, tables: set[str]) -> tuple[LineResult, _Old, rule.Plan]:
    result = LineResult(official=d.official, old=d.old)
    result.found = (HEADER_PREFIX + d.old) in tables or (DETAIL_PREFIX + d.old) in tables
    old = _read_old(conn, d, tables) if result.found else _Old()
    plan = rule.decide(old.saved, _read_new(conn, d, tables), _read_copied(conn, d, tables))
    result.old_pages = len(old.saved)
    result.counts = plan.counts()
    result.other_rows, result.duplicates, result.dropped_columns = (
        old.other_rows, old.duplicates, old.dropped)
    if old.other_rows:
        result.notes.append(f"前の表にライン列が「{d.old}」でない行が {old.other_rows} 行あります"
                            "(写していません)")
    if old.duplicates:
        result.notes.append(f"同じページ・行番号の明細が {old.duplicates} 行重なっていました"
                            "(先の行を写しました)")
    if old.dropped:
        result.notes.append(f"このツールの表に無い列は写していません: {'・'.join(old.dropped)}")
    problem = rule.first_problem(plan)
    if problem:
        result.notes.append(problem)
    return result, old, plan


# ==================================================================
# 書く
# ==================================================================
def _ensure_track(conn) -> None:
    cols = ", ".join(quote(c) for c in TRACK_COLUMNS)
    key = ", ".join(quote(c) for c in TRACK_KEY)
    conn.execute(f"CREATE TABLE IF NOT EXISTS {quote(TRACK_TABLE)} ({cols}, PRIMARY KEY ({key}))")


def _key_values(official: str, key: rule.PageKey) -> tuple[Any, ...]:
    date, shift, page = key
    return (date, official, shift, page)


def _delete_page(conn, d: line_names.LineName, key: rule.PageKey) -> None:
    where = " AND ".join(f"{quote(c)}=?" for c in backend.HEADER_KEY)
    values = _key_values(d.official, key)
    conn.execute(f"DELETE FROM {quote(DETAIL_PREFIX + d.official)} WHERE {where}", values)
    conn.execute(f"DELETE FROM {quote(HEADER_PREFIX + d.official)} WHERE {where}", values)


def _converted(row: dict[str, Any], columns: tuple[str, ...], official: str) -> tuple[Any, ...]:
    """前の表の1行 → 正規の表の1行(ラインは正規、ページ・行番号は数に揃える)。"""
    out = []
    for column in columns:
        if column == "ライン":
            out.append(official)
        elif column in ("ページ", "行番号"):
            out.append(rule.number_of(row.get(column)))
        else:
            out.append(row.get(column))
    return tuple(out)


def _write(conn, d: line_names.LineName, old: _Old, plan: rule.Plan, now: str) -> rule.Check:
    """1ラインぶんを書く(呼ぶ側がトランザクションを持つ)。書いたページを突き合わせる。"""
    backend.ensure_tables(conn, HEADER_PREFIX + d.official, DETAIL_PREFIX + d.official)
    _ensure_track(conn)
    track_where = " AND ".join(f"{quote(c)}=?" for c in TRACK_KEY)
    for key in plan.keys(rule.RECOPY) + plan.keys(rule.GONE):
        _delete_page(conn, d, key)
    for key in plan.keys(rule.GONE):
        date, shift, page = key
        conn.execute(f"DELETE FROM {quote(TRACK_TABLE)} WHERE {track_where}",
                     (d.official, date, shift, page))

    header_sql = (f"INSERT INTO {quote(HEADER_PREFIX + d.official)}"
                  f" ({', '.join(quote(c) for c in backend.HEADER_COLUMNS)})"
                  f" VALUES ({', '.join('?' for _ in backend.HEADER_COLUMNS)})")
    detail_sql = (f"INSERT INTO {quote(DETAIL_PREFIX + d.official)}"
                  f" ({', '.join(quote(c) for c in backend.DETAIL_COLUMNS)})"
                  f" VALUES ({', '.join('?' for _ in backend.DETAIL_COLUMNS)})")
    track_sql = (f"INSERT OR REPLACE INTO {quote(TRACK_TABLE)}"
                 f" ({', '.join(quote(c) for c in TRACK_COLUMNS)})"
                 f" VALUES ({', '.join('?' for _ in TRACK_COLUMNS)})")
    written = plan.keys(rule.COPY) + plan.keys(rule.RECOPY)
    expected: dict[rule.PageKey, tuple[bool, int]] = {}
    for key in written:
        header = old.headers.get(key)
        rows = old.details.get(key, {})
        if header is not None:
            conn.execute(header_sql, _converted(header, backend.HEADER_COLUMNS, d.official))
        conn.executemany(detail_sql, [_converted(row, backend.DETAIL_COLUMNS, d.official)
                                      for row in rows.values()])
        date, shift, page = key
        conn.execute(track_sql, (d.official, date, shift, page, d.old,
                                 old.saved.get(key, ""), now))
        expected[key] = (header is not None, len(rows))
    return rule.check(expected, _written_counts(conn, d, set(written)))


def _written_counts(conn, d: line_names.LineName,
                    keys: set[rule.PageKey]) -> dict[rule.PageKey, tuple[bool, int]]:
    """正規の表にいまあるページ → (見出しがあるか, 明細の行数)。"""
    if not keys:
        return {}
    headers = {rule.page_key(dict(zip(("報告日", "直", "ページ"), row))) for row in conn.execute(
        f'SELECT "報告日", "直", "ページ" FROM {quote(HEADER_PREFIX + d.official)}'
        ' WHERE "ライン"=?', (d.official,))}
    rows: dict[rule.PageKey, int] = {}
    for row in conn.execute(
            f'SELECT "報告日", "直", "ページ", COUNT(*) FROM {quote(DETAIL_PREFIX + d.official)}'
            ' WHERE "ライン"=? GROUP BY "報告日", "直", "ページ"', (d.official,)):
        values = tuple(row)
        key = rule.page_key(dict(zip(("報告日", "直", "ページ"), values[:3])))
        rows[key] = rows.get(key, 0) + values[3]
    return {key: (key in headers, rows.get(key, 0)) for key in keys}


# ==================================================================
# 入口
# ==================================================================
def _target() -> tuple[Optional[Path], str]:
    """共有の日報データの道。使えなければ (None, 理由)。"""
    path = Path(SETTINGS.access_db_path)
    if not pusher.is_sqlite_target(path):
        return None, (f"日報データが Access のファイルです({path.name})。"
                      "コンバートは sqlite3 のときだけ使えます")
    try:
        if not path.is_file():
            return None, f"共有の日報データが見つかりません: {path}"
    except OSError as exc:
        return None, f"共有の日報データに届きません: {exc}"
    return path, ""


def _report(dry_run: bool, path: Optional[Path]) -> Report:
    return Report(dry_run=dry_run, path=str(path or SETTINGS.access_db_path),
                  same_name=[d.official for d in line_names.DEFINITIONS if not d.renamed],
                  at=datetime.now().isoformat(timespec="seconds"))


def survey() -> Report:
    """**書かずに試す。** 共有のファイルは開かず、写しを読んで数えるだけ。"""
    from .. import source_db

    path, why = _target()
    report = _report(True, path)
    if path is None:
        report.error = why
        return report
    try:
        with source_db.open_source(path) as conn:
            tables = _tables(conn)
            report.unknown_tables = unknown_tables(tables)
            for d in line_names.renamed():
                result, _, _ = _survey(conn, d, tables)
                report.lines.append(result)
    except Exception as exc:                      # noqa: BLE001 - 画面に出す
        log.exception("コンバートを試せませんでした")
        report.error = f"共有の日報データを読めませんでした: {exc}"
    return report


def run() -> Report:
    """**実行する。** 1ラインずつ1つのトランザクションで書き、数を突き合わせる。"""
    path, why = _target()
    report = _report(False, path)
    if path is None:
        report.error = why
        return report
    try:
        conn = backend.connect(path)
    except sqlite3.Error as exc:
        report.error = f"{path.name} を開けません: {exc}"
        return report
    now = report.at
    try:
        conn.isolation_level = None               # BEGIN は自分で出す
        report.unknown_tables = unknown_tables(_tables(conn))
        for d in line_names.renamed():
            result = LineResult(official=d.official, old=d.old)
            try:
                conn.execute("BEGIN IMMEDIATE")   # 読むところからロックを取る(VBA と重ならない)
                tables = _tables(conn)
                result, old, plan = _survey(conn, d, tables)
                if plan.writes:
                    found = _write(conn, d, old, plan, now)
                    result.checked = found.describe()
                    if not found.ok:
                        raise RuntimeError(f"写した数が合いません({found.describe()})")
                conn.execute("COMMIT")
            except Exception as exc:              # noqa: BLE001 - このラインだけ取り消す
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                log.exception("コンバートできませんでした line=%s", d.official)
                result.error = f"{exc}(このラインは書いていません)"
            report.lines.append(result)
            log.info("コンバート %s: %s %s", result.tables, result.counts,
                     result.error or result.checked)
    finally:
        conn.close()
    return report
