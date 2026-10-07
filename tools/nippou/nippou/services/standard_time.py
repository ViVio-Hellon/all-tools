"""標準作業時間の蓄積 ── 共有の `標準作業時間.sqlite3` に実績を溜め、標準を出す

    標準作業時間.sqlite3を作成しデータ蓄積していきたい

計算そのものは `logic/standard_time.py`。ここは**いつ・どこへ**書くかです。

【どこへ】
共有(日報データ.sqlite3 と同じフォルダ)の `標準作業時間.sqlite3` に、
全ライン・全端末が書きます。**無ければ作ります**(共有保存の履歴と違い、
専用のファイルなので作ってよい)。フォルダごと無い・開けないときは作らず、
次の機会に回します。

    T_作業実績      1ロット行 = 1行(鍵: 報告日・ライン・直・ページ・行番号)
    T_標準作業時間  条件(ライン・班・用途コード・包装仕様NO・サイズ)ごとの標準

【いつ】
「共有へ保存」で**送れた直**のぶん(確定した中身だけ ── 打っている途中の
日報は入れない。2つ目の出力先と同じ考え)。共有が開けなければ手元の
`standard_time_pending` に残り、次の「共有へ保存」でまとめて写します。
過去ぶんは設定画面から期間を指定して入れられます(`backfill`)。

【直し直したら置き換える】
同じ直を直して送り直すと、その直の行を**いったん消してから**入れ直します。
行を消した日報の実績が残り続けないように。

【班】
その直の作業者(ページごとの作業者名を合わせたもの)を班員名簿で引きます
(`logic/standard_time.team_of`)。名簿が読めないときは全員「不明」で
残します ── 実績は失わず、あとで名簿が読めるようになれば
「標準作業時間に過去ぶんを入れる」で班を付け直せます。
"""
from __future__ import annotations

import socket
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional

from ..db.connection import connect_readonly
from ..db.models import PackingDetail, PackingReport
from ..db.repository import NippouRepository
from ..logging_setup import get_logger
from ..logic import standard_time as logic
from ..logic.shift import parse_business_date

log = get_logger("services.standard_time")

SAMPLE_TABLE = "T_作業実績"
STANDARD_TABLE = "T_標準作業時間"

#: T_作業実績 の列 (手元の名前, 共有の列名)
SAMPLE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("report_date", "報告日"), ("line", "ライン"), ("shift", "直"),
    ("page", "ページ"), ("row_no", "行番号"), ("team", "班"), ("worker", "作業者"),
    ("lot_no", "LOT"), ("purpose_code", "用途コード"), ("purpose_name", "用途名"),
    ("packing_spec_no", "包装仕様NO"), ("size", "サイズ"), ("dimension", "寸法(日報のまま)"),
    ("packages", "梱包数"), ("sheets", "枚数"), ("workers", "作業人数"),
    ("work_minutes", "作業時間(分)"), ("stop_minutes", "停止時間(分)"),
    ("person_minutes", "人分"), ("per_package", "梱包あたり人分"),
    ("per_sheet", "枚あたり人分"), ("usable", "標準に数える"),
    ("excluded_reason", "数えない理由"), ("terminal", "端末"), ("recorded_at", "記録日時"),
)
SAMPLE_KEY = ("報告日", "ライン", "直", "ページ", "行番号")

#: T_標準作業時間 の列
STANDARD_COLUMNS: tuple[tuple[str, str], ...] = (
    ("line", "ライン"), ("team", "班"), ("purpose_code", "用途コード"),
    ("purpose_name", "用途名"), ("packing_spec_no", "包装仕様NO"), ("size", "サイズ"),
    ("count", "件数"),
    ("pkg_median", "標準(人分/梱包)"), ("pkg_mean", "平均(人分/梱包)"),
    ("pkg_low", "最小(人分/梱包)"), ("pkg_high", "最大(人分/梱包)"),
    ("sheet_median", "標準(人分/枚)"), ("sheet_mean", "平均(人分/枚)"),
    ("sheet_low", "最小(人分/枚)"), ("sheet_high", "最大(人分/枚)"),
    ("provisional", "仮"), ("updated_at", "更新日時"),
)
STANDARD_KEY = ("ライン", "班", "用途コード", "包装仕様NO", "サイズ")


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _connect(path: Path) -> sqlite3.Connection:
    """開く(無ければ作る)。**フォルダが無ければ作らない**(共有が繋がって
    いないのに手元に見当違いのファイルを作らないため)。"""
    if not Path(path).parent.is_dir():
        raise sqlite3.OperationalError(f"フォルダがありません: {Path(path).parent}")
    from ..access_bridge import sqlite_backend

    return sqlite_backend._connect(Path(path))


def ensure_tables(conn: sqlite3.Connection) -> list[str]:
    """2つの表が無ければ作り、列が足りなければ足す。足した列名を返す。"""
    added: list[str] = []
    for table, columns, key in ((SAMPLE_TABLE, SAMPLE_COLUMNS, SAMPLE_KEY),
                                (STANDARD_TABLE, STANDARD_COLUMNS, STANDARD_KEY)):
        names = [shared for _, shared in columns]
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_quote(table)} ("
            + ", ".join(_quote(n) for n in names)
            + f", PRIMARY KEY ({', '.join(_quote(k) for k in key)}))")
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({_quote(table)})")}
        for name in names:
            if name not in have:
                conn.execute(f"ALTER TABLE {_quote(table)} ADD COLUMN {_quote(name)}")
                added.append(f"{table}.{name}")
    if added:
        log.info("標準作業時間の表に列を足しました: %s", ", ".join(added))
    return added


# ======================================================================
# 実績を作る
# ======================================================================
def load_teams() -> dict[str, str]:
    """班員名簿 `名前 → 班`。読めなければ空(全員「不明」になる)。"""
    from ..access_bridge import staff_master

    try:
        return {m.name: m.team for m in staff_master.load_staff_members() if m.team}
    except Exception:                             # noqa: BLE001 - 実績は残す
        log.exception("班員名簿を読めませんでした(班は「不明」で残します)")
        return {}


@dataclass
class Operators:
    """班員名簿のオペレーター区分(v4.15.0)。"""

    #: 名前 → 区分(AOP / ABOP / BOP)。読めた人だけ
    kinds: dict[str, str] = field(default_factory=dict)
    #: 読めなかった行(`logic/operator.inspect`)
    findings: list = field(default_factory=list)
    #: 名簿に「オペレーター」の列があるか
    has_column: bool = False

    def warning(self) -> str:
        """画面の頭に出す一言(読めない値があるとき・列が無いとき)。"""
        from ..logic import operator

        if not self.has_column:
            return ""
        if not self.findings:
            return ""
        shown = "、".join(f.describe() for f in self.findings[:5])
        more = f" ほか{len(self.findings) - 5}人" if len(self.findings) > 5 else ""
        return (f"班員名簿のオペレーターに読めない値があります({shown}{more})。"
                f"{operator.NO_KIND}として数えています ── マスタ管理の班員名簿で直せます。")


def load_operators() -> Operators:
    """班員名簿のオペレーター区分。**読めなければ空**(全員「区分なし」になる)。"""
    from ..access_bridge import staff_master
    from ..logic import operator

    try:
        members = staff_master.load_staff_members()
    except Exception:                             # noqa: BLE001 - 画面は出す
        log.exception("班員名簿を読めませんでした(オペレーターは「区分なし」で数えます)")
        return Operators()
    found = Operators(has_column=any(m.operator for m in members))
    for m in members:
        kind = operator.read(m.operator)
        if kind:
            found.kinds[m.name] = kind
        elif operator.problem(m.operator):
            found.findings.append(operator.Finding(name=m.name, value=m.operator,
                                                   reason=operator.problem(m.operator)))
    return found


def samples_of(repo: NippouRepository, report: PackingReport,
               details: Iterable[PackingDetail], teams: dict[str, str]
               ) -> list[logic.Sample]:
    """1直ぶんの集計明細 → 実績の行。"""
    worker = " ".join(repo.shift_workers(report.work_date, report.line_name,
                                          report.shift)) or report.worker_name
    team = logic.team_of(worker, teams)
    return [logic.sample_of(
        report_date=report.work_date, line=report.line_name, shift=report.shift,
        page=d.page, row_no=d.row_no, worker=worker, team=team, lot_no=d.lot_no,
        purpose_code=d.purpose_code, purpose_name=d.purpose_name,
        packing_spec_no=d.packing_spec_no, dimension=d.dimension,
        packages=d.packing_package_count, sheets=d.actual_quantity,
        workers=d.worker_count, work_minutes=d.work_time,
        stop_minutes=sum(s.stop_minutes for s in d.stops)) for d in details]


def _sample_values(s: logic.Sample, terminal: str, now: str) -> tuple:
    values = {
        "report_date": s.report_date, "line": s.line, "shift": s.shift,
        "page": s.page, "row_no": s.row_no, "team": s.team, "worker": s.worker,
        "lot_no": s.lot_no, "purpose_code": s.purpose_code,
        "purpose_name": s.purpose_name, "packing_spec_no": s.packing_spec_no,
        "size": s.size, "dimension": s.dimension, "packages": s.packages,
        "sheets": s.sheets, "workers": s.workers, "work_minutes": s.work_minutes,
        "stop_minutes": s.stop_minutes, "person_minutes": s.person_minutes,
        "per_package": s.per_package, "per_sheet": s.per_sheet,
        "usable": 1 if s.usable else 0, "excluded_reason": s.excluded_reason,
        "terminal": terminal, "recorded_at": now,
    }
    return tuple(values[local] for local, _ in SAMPLE_COLUMNS)


def _sample_from_row(row: sqlite3.Row) -> logic.Sample:
    return logic.Sample(
        report_date=row["報告日"] or "", line=row["ライン"] or "", shift=row["直"] or "",
        page=int(row["ページ"] or 0), row_no=int(row["行番号"] or 0),
        team=row["班"] or logic.TEAM_UNKNOWN, worker=row["作業者"] or "",
        lot_no=row["LOT"] or "", purpose_code=row["用途コード"] or "",
        purpose_name=row["用途名"] or "", packing_spec_no=row["包装仕様NO"] or "",
        size=row["サイズ"] or "", dimension=row["寸法(日報のまま)"] or "",
        packages=float(row["梱包数"] or 0), sheets=float(row["枚数"] or 0),
        workers=float(row["作業人数"] or 0), work_minutes=float(row["作業時間(分)"] or 0),
        stop_minutes=float(row["停止時間(分)"] or 0))


# ======================================================================
# 書く
# ======================================================================
@dataclass
class Outcome:
    """写した結果(知らせに載せる)。"""

    shifts: int = 0                 # 写した直の数
    rows: int = 0                   # 写した実績の行数
    standards: int = 0              # 作り直した標準の行数(そのライン)
    pending: int = 0                # まだ写せていない直の数
    error: str = ""
    path: str = ""
    lines: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error and not self.pending

    def as_dict(self) -> dict:
        return {"shifts": self.shifts, "rows": self.rows, "standards": self.standards,
                "pending": self.pending, "error": self.error, "path": self.path,
                "message": self.message}

    @property
    def message(self) -> str:
        if self.error:
            return (f"標準作業時間に写せませんでした({self.error})── "
                    f"直{self.pending}つぶんは次の「共有へ保存」で写します")
        if not self.shifts:
            return ""
        return (f"標準作業時間: 直{self.shifts}つぶん({self.rows}行)を蓄積し、"
                f"{'・'.join(self.lines)} の標準 {self.standards}件を出し直しました")


def after_push(repo: NippouRepository, pushed_keys: Iterable[tuple],
               db_path: Path) -> Outcome:
    """共有へ送れたページの直を、写す待ちに入れてから写す。"""
    keys = sorted({(str(k[0]), str(k[1]), str(k[2])) for k in pushed_keys})
    repo.queue_standard_time(keys)
    return flush(repo, db_path)


def flush(repo: NippouRepository, db_path: Path) -> Outcome:
    """写す待ちの直をぜんぶ共有へ写し、触ったラインの標準を出し直す。

    **失敗しても何も止めない。** 待ちはそのまま残り、次に写します。
    """
    pending = repo.standard_time_pending()
    out = Outcome(path=str(db_path), pending=len(pending))
    if not pending:
        return out
    teams = load_teams()
    terminal = socket.gethostname()
    now = datetime.now().isoformat(timespec="seconds")
    try:
        conn = _connect(db_path)
    except sqlite3.Error as exc:
        out.error = (f"{Path(db_path).name} を開けません"
                     f"(共有のフォルダに書けるか確かめてください): {exc}")
        log.warning("標準作業時間: %s", out.error)
        return out
    done: list[tuple[str, str, str]] = []
    try:
        with conn:
            ensure_tables(conn)
            for key in pending:
                work_date, line, shift = key
                report = repo.load_packing_report(work_date, line, shift)
                rows = (samples_of(repo, report, repo.packing_details(report.id), teams)
                        if report is not None else [])
                conn.execute(
                    f"DELETE FROM {_quote(SAMPLE_TABLE)} WHERE 報告日=? AND ライン=? AND 直=?",
                    key)
                if rows:
                    conn.executemany(
                        f"INSERT OR REPLACE INTO {_quote(SAMPLE_TABLE)} "
                        f"({', '.join(_quote(c) for _, c in SAMPLE_COLUMNS)}) "
                        f"VALUES ({', '.join('?' for _ in SAMPLE_COLUMNS)})",
                        [_sample_values(s, terminal, now) for s in rows])
                out.rows += len(rows)
                done.append(key)
            lines = sorted({ln for _, ln, _ in done})
            for line in lines:
                out.standards += _rebuild_standards(conn, line, now)
            out.lines = lines
    except sqlite3.Error as exc:
        out.error = f"{Path(db_path).name} に書けませんでした: {exc}"
        log.warning("標準作業時間: %s", out.error)
        return out
    finally:
        conn.close()
    repo.clear_standard_time_pending(done)
    out.shifts = len(done)
    out.pending = len(pending) - len(done)
    return out


def _rebuild_standards(conn: sqlite3.Connection, line: str, now: str) -> int:
    """そのラインの標準を、実績から作り直す(消して入れ直す)。"""
    conn.row_factory = sqlite3.Row
    samples = [_sample_from_row(r) for r in conn.execute(
        f"SELECT * FROM {_quote(SAMPLE_TABLE)} WHERE ライン=?", (line,))]
    found = logic.standards(samples)
    conn.execute(f"DELETE FROM {_quote(STANDARD_TABLE)} WHERE ライン=?", (line,))
    conn.executemany(
        f"INSERT OR REPLACE INTO {_quote(STANDARD_TABLE)} "
        f"({', '.join(_quote(c) for _, c in STANDARD_COLUMNS)}) "
        f"VALUES ({', '.join('?' for _ in STANDARD_COLUMNS)})",
        [_standard_values(st, now) for st in found])
    return len(found)


def _standard_values(st: logic.Standard, now: str) -> tuple:
    values = {
        "line": st.line, "team": st.team, "purpose_code": st.purpose_code,
        "purpose_name": st.purpose_name, "packing_spec_no": st.packing_spec_no,
        "size": st.size, "count": st.count,
        "pkg_median": st.per_package.median, "pkg_mean": st.per_package.mean,
        "pkg_low": st.per_package.low, "pkg_high": st.per_package.high,
        "sheet_median": st.per_sheet.median, "sheet_mean": st.per_sheet.mean,
        "sheet_low": st.per_sheet.low, "sheet_high": st.per_sheet.high,
        "provisional": 1 if st.provisional else 0, "updated_at": now,
    }
    return tuple(values[local] for local, _ in STANDARD_COLUMNS)


def backfill(repo: NippouRepository, start: date, end: date, line: str,
             db_path: Path) -> Outcome:
    """期間ぶんの集計(手元)を写す待ちに入れて写す。**過去ぶんを入れる**用。

    共有に送っていない直も入ります(手元の集計がぜんぶ)。名簿が読めるように
    なったあとで班を付け直すのにも、これを使います。
    """
    keys = [r.key() for r in repo.packing_reports_between(start, end, line=line)]
    repo.queue_standard_time(keys)
    return flush(repo, db_path)


# ======================================================================
# 読む(集計管理の表・CSV)
# ======================================================================
@dataclass
class Readout:
    standards: list[logic.Standard]
    samples: int = 0                # 実績の行数(標準に数えたもの)
    excluded: int = 0               # 実績のうち数えなかった行数
    source: str = ""
    warning: str = ""
    teams: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)


def read(db_path: Optional[Path]) -> Readout:
    """共有の標準をぜんぶ読む。ファイルが無ければ空(その旨を `source` に)。"""
    if db_path is None or not Path(db_path).exists():
        return Readout([], source=f"{Path(db_path).name if db_path else '標準作業時間.sqlite3'}"
                                  " はまだありません(共有へ保存すると作られます)")
    try:
        conn = connect_readonly(Path(db_path), timeout=5)
    except sqlite3.Error as exc:
        return Readout([], warning=f"{Path(db_path).name} を開けません: {exc}")
    try:
        conn.row_factory = sqlite3.Row
        have = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if STANDARD_TABLE not in have:
            return Readout([], source=f"{Path(db_path).name}(まだ表がありません)")
        found = [_standard_from_row(r) for r in conn.execute(
            f"SELECT * FROM {_quote(STANDARD_TABLE)}")]
        samples = excluded = 0
        if SAMPLE_TABLE in have:
            samples, excluded = conn.execute(
                f"SELECT COALESCE(SUM(標準に数える), 0), COALESCE(SUM(1 - 標準に数える), 0)"
                f" FROM {_quote(SAMPLE_TABLE)}").fetchone()
    except sqlite3.Error as exc:
        return Readout([], warning=f"{Path(db_path).name} を読めませんでした: {exc}")
    finally:
        conn.close()
    found.sort(key=lambda st: (st.line, st.purpose_code, st.packing_spec_no,
                               logic._size_sort_key(st.size),
                               st.team != logic.TEAM_ALL, st.team))
    teams = sorted({st.team for st in found if st.team != logic.TEAM_ALL})
    return Readout(found, samples=int(samples or 0), excluded=int(excluded or 0),
                   source=f"共有の {Path(db_path).name}(全端末)",
                   teams=teams, lines=sorted({st.line for st in found}))


def _standard_from_row(row: sqlite3.Row) -> logic.Standard:
    def stat(prefix: str) -> logic.Stat:
        med = row[f"標準(人分/{prefix})"]
        return logic.Stat(
            count=int(row["件数"] or 0) if med is not None else 0,
            median=med, mean=row[f"平均(人分/{prefix})"],
            low=row[f"最小(人分/{prefix})"], high=row[f"最大(人分/{prefix})"])
    return logic.Standard(
        line=row["ライン"] or "", team=row["班"] or "",
        purpose_code=row["用途コード"] or "", packing_spec_no=row["包装仕様NO"] or "",
        size=row["サイズ"] or "", count=int(row["件数"] or 0),
        per_package=stat("梱包"), per_sheet=stat("枚"),
        purpose_name=row["用途名"] or "")


def in_range(work_date: str, start: date, end: date) -> bool:
    day = parse_business_date(work_date)
    return day is not None and start <= day <= end


# ======================================================================
# 同条件の作業を抜き出す ── **AND / OR を選べる**(`logic.Criteria`)
# ======================================================================
@dataclass
class Works:
    samples: list[logic.Sample]
    #: 行ごとの比べる標準。鍵は (ライン, 用途コード, 包装仕様NO, サイズ)。
    #: 班を決めたときはその班の標準、問わないときは「全班」の標準
    standards: dict[tuple[str, str, str, str], logic.Standard] = field(default_factory=dict)
    #: 比べた標準の班(「全班」か、選んだ班)
    standard_team: str = logic.TEAM_ALL
    source: str = ""
    warning: str = ""

    def standard_for(self, s: logic.Sample) -> Optional[logic.Standard]:
        return self.standards.get((s.line, s.purpose_code, s.packing_spec_no, s.size))


def _order(s: logic.Sample) -> tuple:
    from ..logic.aggregation import SHIFT_ORDER

    return (parse_business_date(s.report_date) or date.min,
            SHIFT_ORDER.get(s.shift, 99), s.shift, s.page, s.row_no, s.line)


#: `Criteria` の欄 → `T_作業実績` の列
_CRITERIA_COLUMNS = {"purpose_code": "用途コード", "packing_spec_no": "包装仕様NO",
                     "size": "サイズ"}


def _criteria_sql(criteria: logic.Criteria) -> tuple[str, list]:
    """欄と欄は AND / OR、欄の中は IN(どれか)。**大文字小文字は問わない。**"""
    parts, params = [], []
    for key, _, values in criteria.fields:
        parts.append(f"UPPER({_quote(_CRITERIA_COLUMNS[key])}) IN "
                     f"({', '.join('?' for _ in values)})")
        params += [v.upper() for v in values]
    if not parts:
        return "", []
    word = " AND " if criteria.join == logic.JOIN_AND else " OR "
    return "(" + word.join(parts) + ")", params


def works(db_path: Optional[Path], criteria: logic.Criteria, *,
          line: str = "", team: str = "", start: Optional[date] = None,
          end: Optional[date] = None, include_excluded: bool = False) -> Works:
    """条件に当たる作業(`T_作業実績` の行)と、行ごとの比べる標準。

    ライン・班・期間は**いつも絞り込み**(空なら問わない)。欄の結び方は
    `criteria.join`。並びは 報告日 → 直 → ページ → 行(紙をめくる順)。
    """
    any_team = not team or team == logic.TEAM_ALL
    standard_team = logic.TEAM_ALL if any_team else team
    if db_path is None or not Path(db_path).exists():
        return Works([], standard_team=standard_team,
                     source=f"{Path(db_path).name if db_path else '標準作業時間.sqlite3'}"
                            " はまだありません(共有へ保存すると作られます)")
    where, params = ["1=1"], []
    if line:
        where.append("ライン=?")
        params.append(line)
    if not any_team:
        where.append("班=?")
        params.append(team)
    if not include_excluded:
        where.append("標準に数える=1")
    clause, extra = _criteria_sql(criteria)
    if clause:
        where.append(clause)
        params += extra
    try:
        conn = connect_readonly(Path(db_path), timeout=5)
    except sqlite3.Error as exc:
        return Works([], standard_team=standard_team,
                     warning=f"{Path(db_path).name} を開けません: {exc}")
    try:
        conn.row_factory = sqlite3.Row
        have = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if SAMPLE_TABLE not in have:
            return Works([], standard_team=standard_team,
                         source=f"{Path(db_path).name}(まだ表がありません)")
        found = [_sample_from_row(r) for r in conn.execute(
            f"SELECT * FROM {_quote(SAMPLE_TABLE)} WHERE {' AND '.join(where)}", params)]
        standards: dict[tuple[str, str, str, str], logic.Standard] = {}
        lines = sorted({s.line for s in found})
        if STANDARD_TABLE in have and lines:
            for row in conn.execute(
                    f"SELECT * FROM {_quote(STANDARD_TABLE)} WHERE 班=? AND ライン IN "
                    f"({', '.join('?' for _ in lines)})", [standard_team, *lines]):
                st = _standard_from_row(row)
                standards[(st.line, st.purpose_code, st.packing_spec_no, st.size)] = st
    except sqlite3.Error as exc:
        return Works([], standard_team=standard_team,
                     warning=f"{Path(db_path).name} を読めませんでした: {exc}")
    finally:
        conn.close()
    if start or end:
        lo, hi = start or date.min, end or date.max
        found = [s for s in found if in_range(s.report_date, lo, hi)]
    found.sort(key=_order)
    return Works(found, standards=standards, standard_team=standard_team,
                 source=f"共有の {Path(db_path).name}(全端末)")


def usable_samples(db_path: Optional[Path], line: Optional[str]) -> list[logic.Sample]:
    """標準に数えた実績ぜんぶ(`line` が None なら全ライン)。計算式を出すのに使う。"""
    if db_path is None or not Path(db_path).exists():
        return []
    try:
        conn = connect_readonly(Path(db_path), timeout=5)
    except sqlite3.Error:
        return []
    try:
        conn.row_factory = sqlite3.Row
        sql = f"SELECT * FROM {_quote(SAMPLE_TABLE)} WHERE 標準に数える=1"
        params: list = []
        if line:
            sql += " AND ライン=?"
            params.append(line)
        return [_sample_from_row(r) for r in conn.execute(sql, params)]
    except sqlite3.Error:
        return []
    finally:
        conn.close()
