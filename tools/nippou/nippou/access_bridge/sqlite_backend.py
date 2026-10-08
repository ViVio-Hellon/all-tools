"""③反映の書き先が sqlite3 のとき

【なぜ要るのか】
参照用マスタ4つは読むだけなので `source_db`(手元へ写して読む)で
sqlite3 に移せました。残るのが **`日報管理` への書き込み**です ── このアプリ
唯一の「外のファイルへ書く」処理で、ここが Access のままだと

    ・Windows と ACE ドライバが要る(cscript.exe / ODBC)
    ・`.laccdb` の排他に当たると反映できない
    ・書けたかどうかが VBScript の標準出力1行でしか分からない

が残り続けます。sqlite3 なら標準ライブラリだけで書けて、失敗も例外で
はっきり分かります。

【文字列ではなく、値として渡す】
Access経路は SQL文を組み立てて VBScript に渡します(そうするしかない)。
こちらは**プレースホルダで渡します** ── 担当者名や理由に `'` が入った
だけで壊れる、という事故の芽をそもそも作りません。

【1キー=1トランザクション】
VBA ``ExecuteSQLTransaction`` と同じで、(報告日,ライン,直,ページ)の1キーぶんを
まとめて確定します。1つのキーが失敗しても、ほかのキーは反映されます。

【表は無ければ作る】
VBA ``NippouDB_EnsureTables`` と同じ役目。列の並び・名前・主キーは
あちらに揃えてあります ── 同じファイルを Access 版と行き来させる可能性を
残すためで、揃っていないと片方から読めなくなります。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from ..db.models import (
    DetailRecord,
    HeaderRecord,
    PackingDetail,
    PackingReport,
)
from ..logging_setup import get_logger
from .errors import AccessBridgeError, ErrorKind
from .runner import ScriptResult

_logger = get_logger("access_bridge.sqlite_backend")

# 共有フォルダの上でロックを待つ時間(ミリ秒)。Access の `.laccdb` と違い、
# sqlite3 は待てば通ることが多い ── すぐ諦めない
BUSY_TIMEOUT_MS = 10_000

# ヘッダーの列。**並びも名前も VBA `NippouDB_EnsureTables` のまま**
HEADER_COLUMNS: tuple[str, ...] = (
    "報告日", "ライン", "直", "ページ", "担当者", "昼勤", "枚数", "重量Kg",
    "Lot数", "係数Lot数", "理由", "保存日時",
)

# 明細の列。同上
DETAIL_COLUMNS: tuple[str, ...] = (
    "報告日", "ライン", "直", "ページ", "行番号",
    "LOT", "ZAI", "SIZ", "KEN", "KZ", "KH", "SZ", "SH", "HIT", "AI",
    "MAI", "TUT", "VC", "ET", "S", "TH", "SS", "THS", "STH", "THT",
    "CON", "WEI", "TIM", "UNI",
    "Others1", "Others2", "Others3", "Others4", "Others5", "Others6", "係数",
)

# ------------------------------------------------------------------
# 集計フォーマットの3つ (VBA の「集計シート」を表にしたもの)
#
# 共有にも**手元と同じ3階層**で置きます。ツールが無くても、Excel でも
# DB Browser でも開けることが、残す意味の半分なので。
#
#   T_日報集計_<ライン>      1作業日1ライン1直
#   T_日報集計明細_<ライン>  1ロット1行(紙に載らない欄もここ)
#   T_日報停止明細_<ライン>  1停止1行(横持ち→縦持ち)
#
# 記号ごとの合計は `GROUP BY 記号` で出せるので、**丸めた表は置きません**
# ── 同じ数字を2か所に置くと、片方だけ古くなったときに困ります。
# ------------------------------------------------------------------
SUMMARY_COLUMNS: tuple[str, ...] = (
    "報告日", "ライン", "直", "ページ数", "担当者", "昼勤", "理由",
    "枚数", "重量Kg", "Lot数", "係数Lot数",
    "作業時間分", "操業時間分",
    "管理ロス停止分", "突発停止分", "ハンドリング停止分",
    "稼働時間分", "稼働率", "生産性",
    "保存日時",
)

AGG_DETAIL_COLUMNS: tuple[str, ...] = (
    "報告日", "ライン", "直", "ページ", "行番号",
    "ロット番号", "材調質", "寸法", "検入枚数",
    "開始時", "開始分", "終了時", "終了分",
    "作業人数", "合紙", "個装単位枚数", "梱包単位包数", "VC種別",
    "実績枚数", "実績重量Kg", "作業時間分", "単重", "係数処理ロット数",
    "用途コード", "用途名", "納入先", "包装仕様NO", "etc",
    "コイル縦割", "コイル横縦割", "保存日時",
)

STOP_DETAIL_COLUMNS: tuple[str, ...] = (
    "報告日", "ライン", "直", "ページ", "行番号", "停止番号",
    "記号", "内訳", "分類", "時間分", "保存日時",
)

# 主キー。Access側の CONSTRAINT と同じ組み合わせ
HEADER_KEY: tuple[str, ...] = ("報告日", "ライン", "直", "ページ")
DETAIL_KEY: tuple[str, ...] = ("報告日", "ライン", "直", "ページ", "行番号")
SUMMARY_KEY: tuple[str, ...] = ("報告日", "ライン", "直")
AGG_DETAIL_KEY: tuple[str, ...] = ("報告日", "ライン", "直", "ページ", "行番号")
STOP_DETAIL_KEY: tuple[str, ...] = ("報告日", "ライン", "直", "ページ", "行番号",
                                    "停止番号")


def quote(name: str) -> str:
    """識別子を囲む。日本語のテーブル名・列名がそのまま出てくるので必須。"""
    return '"' + str(name).replace('"', '""') + '"'


def _create_sql(table: str, columns: tuple[str, ...],
                key: tuple[str, ...]) -> str:
    """表を作る1文。

    **型は付けません。** sqlite3 は型を強く見ないうえ、Access側は
    `TEXT(255)` のような桁つきで作られています。桁を写しても守られないので、
    書かないほうが「守られているつもり」を作りません(値はすべて文字で
    入れるので、読む側も文字として読めます)。
    """
    cols = ", ".join(quote(c) for c in columns)
    pk = ", ".join(quote(c) for c in key)
    return f"CREATE TABLE IF NOT EXISTS {quote(table)} ({cols}, PRIMARY KEY ({pk}))"


def ensure_tables(conn: sqlite3.Connection, header_table: str,
                  detail_table: str) -> None:
    """表が無ければ作る(VBA ``NippouDB_EnsureTables`` 相当)。"""
    conn.execute(_create_sql(header_table, HEADER_COLUMNS, HEADER_KEY))
    conn.execute(_create_sql(detail_table, DETAIL_COLUMNS, DETAIL_KEY))


def _connect(path: Path) -> sqlite3.Connection:
    """書き込み用に開く。**共有フォルダの上に置かれる前提。**

    WAL には**しません**。WAL は共有メモリ(`-shm`)を使い、SMB の上では
    そもそも開けないことがあります(`source_db` の説明と同じ理由)。
    手元のDBと違って、ここは共有に置かれるほうです。
    """
    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000)
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


def connect(path: Path) -> sqlite3.Connection:
    """書き込み用に開く(コンバート `services/line_rename` も同じ開き方をする)。"""
    return _connect(path)


def _header_values(header: HeaderRecord, saved_at: str) -> tuple[Any, ...]:
    return (header.report_date, header.line, header.shift, header.page,
            header.worker, header.day_shift, header.count, header.weight_kg,
            header.lot_count, header.coefficient_lot_count, header.reason,
            saved_at)


def _detail_values(header: HeaderRecord, d: DetailRecord) -> tuple[Any, ...]:
    return (header.report_date, header.line, header.shift, header.page, d.row_no,
            d.lot, d.zai, d.siz, d.ken, d.kz, d.kh, d.sz, d.sh, d.hit, d.ai,
            d.mai, d.tut, d.vc, d.et, d.s, d.th, d.ss, d.ths, d.sth, d.tht,
            d.con, d.wei, d.tim, d.uni,
            d.others1, d.others2, d.others3, d.others4, d.others5, d.others6,
            d.keisu)


def _marks(columns: tuple[str, ...]) -> str:
    return ", ".join("?" for _ in columns)


def _write_records(conn: sqlite3.Connection, header: HeaderRecord,
                   details: list[DetailRecord], header_table: str,
                   detail_table: str, saved_at: str) -> None:
    """1キーぶんの DELETE と INSERT(確定は呼ぶ側の `with conn`)。"""
    key = (header.report_date, header.line, header.shift, header.page)
    key_where = " AND ".join(f"{quote(c)}=?" for c in HEADER_KEY)
    conn.execute(f"DELETE FROM {quote(detail_table)} WHERE {key_where}", key)
    conn.execute(f"DELETE FROM {quote(header_table)} WHERE {key_where}", key)
    conn.execute(
        f"INSERT INTO {quote(header_table)}"
        f" ({', '.join(quote(c) for c in HEADER_COLUMNS)})"
        f" VALUES ({_marks(HEADER_COLUMNS)})",
        _header_values(header, saved_at))
    conn.executemany(
        f"INSERT INTO {quote(detail_table)}"
        f" ({', '.join(quote(c) for c in DETAIL_COLUMNS)})"
        f" VALUES ({_marks(DETAIL_COLUMNS)})",
        [_detail_values(header, d) for d in details])


def push_records(db_path: Path, header: HeaderRecord,
                 details: list[DetailRecord],
                 header_table: str, detail_table: str,
                 saved_at: str = "") -> ScriptResult:
    """1キーぶんを DELETE してから INSERT する。**まとめて確定する。**

    途中で失敗したら何も残しません(`with conn` がロールバックする)。
    ヘッダーだけ入って明細が入っていない、という中途半端な形を
    共有のDBに残さないためです。

    戻り値は Access経路と同じ :class:`ScriptResult` ── 呼び出し側
    (`pusher._push_one`)がどちらの経路かを気にしなくて済むように。
    """
    return push_records_batch(db_path, [(header, details, header_table, detail_table)],
                              saved_at=saved_at)


#: 1つの束(`push_records_batch`)の中身。(ヘッダ, 明細, ヘッダの表, 明細の表)
RecordItem = tuple[HeaderRecord, list[DetailRecord], str, str]


def push_records_batch(db_path: Path, items: list[RecordItem],
                       saved_at: str = "") -> ScriptResult:
    """何ページかを**1回開いて1回で確定する。**

        共有へ保存しています [2/4] 共有へ送っています:
        プログレスも動かずにとまったままになった

    1ページごとに共有のファイルを開いて確定していました。共有フォルダの
    上の sqlite3 は確定のたびにファイルへ書き切るのを待つので、取り込んだ
    何百ページを送ると、それだけで何十分もかかっていました。束ねれば、
    開くのも確定も束に1回です。

    **束の中のどれかが失敗したら、束ごと何も残しません**(ロールバック)。
    どれが悪いかは呼ぶ側(`pusher`)が1ページずつ送り直して切り分けます。
    """
    from datetime import datetime

    if not items:
        return ScriptResult(success=True, rows=0)
    saved_at = saved_at or datetime.now().isoformat(timespec="seconds")
    try:
        conn = _connect(db_path)
    except sqlite3.Error as exc:
        return _failure(f"{db_path.name} を開けません: {exc}", exc)

    rows = 0
    try:
        with conn:                                   # 例外ならロールバック
            made: set[tuple[str, str]] = set()
            for header, details, header_table, detail_table in items:
                if (header_table, detail_table) not in made:
                    ensure_tables(conn, header_table, detail_table)
                    made.add((header_table, detail_table))
                _write_records(conn, header, details, header_table, detail_table,
                               saved_at)
                rows += len(details) + 1
    except sqlite3.Error as exc:
        keys = [(h.report_date, h.line, h.shift, h.page) for h, *_ in items]
        _logger.warning("sqlite3 への反映に失敗しました keys=%s: %s", keys, exc)
        return _failure(str(exc), exc)
    finally:
        conn.close()

    _logger.info("push ok (sqlite3) pages=%d rows=%d", len(items), rows)
    return ScriptResult(success=True, rows=rows)


def _summary_values(report: PackingReport, saved_at: str) -> tuple[Any, ...]:
    return (report.work_date, report.line_name, report.shift, report.pages,
            report.worker_name, report.daytime_operation, report.reason,
            report.total_quantity, report.total_weight,
            report.total_lot_count, report.coefficient_lot_count,
            report.work_time, report.operation_time,
            report.equipment_stop_total, report.setup_stop_total,
            report.handling_stop_total, report.operating_time,
            report.operating_rate, report.productivity, saved_at)


def _agg_detail_values(key: tuple[str, str, str], d: PackingDetail,
                       saved_at: str) -> tuple[Any, ...]:
    return (*key, d.page, d.row_no,
            d.lot_no, d.material_condition, d.dimension, d.incoming_quantity,
            d.start_hour, d.start_minute, d.end_hour, d.end_minute,
            d.worker_count, d.interleaf, d.packing_quantity,
            d.packing_package_count, d.vc_type,
            d.actual_quantity, d.actual_weight, d.work_time, d.unit_weight,
            d.coefficient_lot_count,
            d.purpose_code, d.purpose_name, d.delivery_destination,
            d.packing_spec_no, d.etc,
            d.coil_vertical_split, d.coil_horizontal_split, saved_at)


def _write_summary(conn: sqlite3.Connection, report: PackingReport,
                   details: list[PackingDetail], summary_table: str,
                   detail_table: str, stop_table: str, saved_at: str) -> int:
    """直1つぶんの3つの表を入れ替える(確定は呼ぶ側)。入れた停止の数を返す。"""
    key = report.key()
    key_where = " AND ".join(f"{quote(c)}=?" for c in SUMMARY_KEY)
    for table in (stop_table, detail_table, summary_table):
        conn.execute(f"DELETE FROM {quote(table)} WHERE {key_where}", key)
    conn.execute(
        f"INSERT INTO {quote(summary_table)}"
        f" ({', '.join(quote(c) for c in SUMMARY_COLUMNS)})"
        f" VALUES ({_marks(SUMMARY_COLUMNS)})",
        _summary_values(report, saved_at))
    conn.executemany(
        f"INSERT INTO {quote(detail_table)}"
        f" ({', '.join(quote(c) for c in AGG_DETAIL_COLUMNS)})"
        f" VALUES ({_marks(AGG_DETAIL_COLUMNS)})",
        [_agg_detail_values(key, d, saved_at) for d in details])
    stops = [(*key, d.page, d.row_no, s.stop_no, s.stop_code,
              s.stop_reason, s.stop_kind, s.stop_minutes, saved_at)
             for d in details for s in d.stops]
    conn.executemany(
        f"INSERT INTO {quote(stop_table)}"
        f" ({', '.join(quote(c) for c in STOP_DETAIL_COLUMNS)})"
        f" VALUES ({_marks(STOP_DETAIL_COLUMNS)})", stops)
    return len(stops)


def _ensure_summary_tables(conn: sqlite3.Connection, summary_table: str,
                           detail_table: str, stop_table: str) -> None:
    conn.execute(_create_sql(summary_table, SUMMARY_COLUMNS, SUMMARY_KEY))
    conn.execute(_create_sql(detail_table, AGG_DETAIL_COLUMNS, AGG_DETAIL_KEY))
    conn.execute(_create_sql(stop_table, STOP_DETAIL_COLUMNS, STOP_DETAIL_KEY))


def push_summary(db_path: Path, report: PackingReport,
                 details: list[PackingDetail],
                 summary_table: str, detail_table: str, stop_table: str,
                 saved_at: str = "") -> ScriptResult:
    """直1つぶんの集計を共有へ置き換える。**3つの表をまとめて。**

    日報の値と同じ扱いにします ── 集計もツールを通さずに読める表と
    して残したいので、書き先は同じファイルの別の表です。

    3つを1つの `with conn:`(=1トランザクション)で入れ替えます。
    途中で落ちて「合計だけ新しくて明細が古い」形になると、数が
    合わない原因を探すのがいちばん難しくなるので。
    """
    return push_summary_batch(
        db_path, [(report, details, summary_table, detail_table, stop_table)],
        saved_at=saved_at)


#: (集計, 明細, 合計の表, 明細の表, 停止の表)
SummaryItem = tuple[PackingReport, list[PackingDetail], str, str, str]


def push_summary_batch(db_path: Path, items: list[SummaryItem],
                       saved_at: str = "") -> ScriptResult:
    """何直ぶんかの集計を**1回開いて1回で確定する**(`push_records_batch` と同じ理由)。"""
    from datetime import datetime

    if not items:
        return ScriptResult(success=True, rows=0)
    saved_at = saved_at or datetime.now().isoformat(timespec="seconds")
    try:
        conn = _connect(db_path)
    except sqlite3.Error as exc:
        return _failure(f"{db_path.name} を開けません: {exc}", exc)

    rows = 0
    try:
        with conn:
            made: set[tuple[str, str, str]] = set()
            for report, details, summary_table, detail_table, stop_table in items:
                tables = (summary_table, detail_table, stop_table)
                if tables not in made:
                    _ensure_summary_tables(conn, *tables)
                    made.add(tables)
                stops = _write_summary(conn, report, details, *tables, saved_at)
                rows += len(details) + stops + 1
    except sqlite3.Error as exc:
        keys = [r.key() for r, *_ in items]
        _logger.warning("集計の反映に失敗しました keys=%s: %s", keys, exc)
        return _failure(str(exc), exc)
    finally:
        conn.close()

    _logger.info("push ok (集計) shifts=%d rows=%d", len(items), rows)
    return ScriptResult(success=True, rows=rows)


def _failure(message: str, exc: Exception) -> ScriptResult:
    """失敗の形を1つに揃える。

    ロック競合だけは種別を分けます ── 呼び出し側が「時間をおけば通る」と
    判断できるようにするため(Access経路の `IsLockError` と同じ役目)。
    """
    from dbkit.sqlite_toolkit import is_lock_error

    kind = ErrorKind.LOCKED if is_lock_error(exc) else ErrorKind.UNKNOWN
    return ScriptResult(
        success=False,
        error=AccessBridgeError(kind=kind, err_number=None, message=message))


def read_table(db_path: Path, table: str) -> list[dict[str, Any]]:
    """反映先を読み返す。**確かめるためのもの。**

    参照用マスタと違って、ここは自分が書いた先なので写しは作りません
    (写すと、いま書いたものが見えない)。
    """
    conn = _connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(f"SELECT * FROM {quote(table)}").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def table_exists(db_path: Path, table: str) -> bool:
    if not Path(db_path).exists():
        return False
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone()
        return row is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()
