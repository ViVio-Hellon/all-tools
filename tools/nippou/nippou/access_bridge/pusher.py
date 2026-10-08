"""Phase ③ of the architecture: SQLite (dirty rows) -> generated
DELETE+INSERT SQL -> Access, executed as one ADODB transaction per
report key via a generated VBScript.

Ports ``NippouDB_Save``'s SQL-building (``NippouDB_BuildHeaderSQL`` /
``NippouDB_BuildDetailSQL`` / the DELETE-then-INSERT idiom) and
``ExecuteSQLTransaction``. Every (report_date, line, shift, page) key is
pushed as its own transaction so one bad/conflicting key can't block the
rest of the batch -- matching the original per-key atomicity.

【``dbkit.outbox_sync`` を使わない理由】
``dbkit`` にはSQLite->Access書き戻し用の汎用エンジン
(``outbox_sync.write_back``)もあるが、そちらは「Access側はオートナンバー
主キーの追記専用テーブルで、同じ行を二重INSERTしないよう送信IDを
Access側の一意インデックスに賭ける」という前提で作られている。

このアプリの反映(③)は逆の形をしている: Access側のキーは
(報告日,ライン,直,ページ)の複合キーで、同じキーへの再保存は
「DELETEしてから今の内容をINSERTし直す」ことで**上書き**を表現する
(1日に同じページを何度保存し直しても構わない)。DELETE+INSERTはそれ自体が
冪等 -- 同じキーに対して何度実行しても最終状態は同じになる -- なので、
``outbox_sync`` が解決する「二重送信でAccess側に行が増殖する」問題が
そもそも起きない。ここへ送信ID/claim機構を持ち込むと、追記が前提の
Access側スキーマ変更(送信ID列+一意インデックスの追加)を本来不要な
テーブルに強いるだけで得るものがない。そのため反映(③)は今後も
このモジュール(またはODBC版の :mod:`.odbc_backend`)のDELETE+INSERT方式を
使う。``outbox_sync`` は、他のVBA移行プロジェクトで「追記専用テーブルを
Accessへ送る」ような場面向けに ``dbkit`` 側に残してある。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..config import SETTINGS
from ..db.models import DetailRecord, HeaderRecord
from ..db.repository import NippouRepository
from ..logging_setup import get_logger
from . import script_gen
from .errors import AccessBridgeError, ErrorKind
from .lock import check_concurrent_access
from .runner import ScriptRunner, run_with_retry

_logger = get_logger("access_bridge.pusher")

_UNSAFE_CHARS = str.maketrans({"[": "(", "]": ")", ".": "_", "!": "_", "`": "_"})


def is_sqlite_target(path: Path) -> bool:
    """反映先が sqlite3 か。**道の拡張子で決める**(振り分けはここ1か所)。"""
    from .. import source_db

    return Path(path).suffix.lower() in source_db.SUFFIXES


def safe_line_name(line: str) -> str:
    """Port of ``NippouDB_SafeLineName``."""
    s = line.strip().translate(_UNSAFE_CHARS)
    return s or "Unknown"


def header_table_name(line: str) -> str:
    return SETTINGS.access_header_table_template.format(line=safe_line_name(line))


def detail_table_name(line: str) -> str:
    return SETTINGS.access_detail_table_template.format(line=safe_line_name(line))


def summary_table_name(line: str) -> str:
    """直ごとの集計の表。**VBAには無い**(あちらは集計シートだった)。"""
    return SETTINGS.access_summary_table_template.format(line=safe_line_name(line))


def agg_detail_table_name(line: str) -> str:
    """集計のロット行(`Agg_OutPut` が並べていたもの)。"""
    return SETTINGS.access_agg_detail_table_template.format(
        line=safe_line_name(line))


def stop_detail_table_name(line: str) -> str:
    """停止1つ1行(横持ち→縦持ち)。記号ごとの合計は GROUP BY で出せます。"""
    return SETTINGS.access_stop_detail_table_template.format(
        line=safe_line_name(line))


def _lit(value: str) -> str:
    return script_gen.sql_literal(value, "TEXT")


def build_header_statements(header: HeaderRecord) -> list[str]:
    key_where = (
        f"[報告日]={_lit(header.report_date)} AND [ライン]={_lit(header.line)} "
        f"AND [直]={_lit(header.shift)} AND [ページ]={header.page}"
    )
    delete_sql = f"DELETE FROM [{header_table_name(header.line)}] WHERE {key_where}"
    insert_sql = (
        f"INSERT INTO [{header_table_name(header.line)}] "
        "([報告日],[ライン],[直],[ページ],[担当者],[昼勤],[枚数],[重量Kg],[Lot数],[係数Lot数],[理由],[保存日時]) "
        "VALUES ("
        f"{_lit(header.report_date)},{_lit(header.line)},{_lit(header.shift)},{header.page},"
        f"{_lit(header.worker)},{_lit(header.day_shift)},{_lit(header.count)},{_lit(header.weight_kg)},"
        f"{_lit(header.lot_count)},{_lit(header.coefficient_lot_count)},{_lit(header.reason)},Now())"
    )
    return [delete_sql, insert_sql]


def build_detail_statements(header: HeaderRecord, details: list[DetailRecord]) -> list[str]:
    key_where = (
        f"[報告日]={_lit(header.report_date)} AND [ライン]={_lit(header.line)} "
        f"AND [直]={_lit(header.shift)} AND [ページ]={header.page}"
    )
    statements = [f"DELETE FROM [{detail_table_name(header.line)}] WHERE {key_where}"]

    columns = (
        "[報告日],[ライン],[直],[ページ],[行番号],"
        "[LOT],[ZAI],[SIZ],[KEN],[KZ],[KH],[SZ],[SH],[HIT],[AI],"
        "[MAI],[TUT],[VC],[ET],[S],[TH],[SS],[THS],[STH],[THT],"
        "[CON],[WEI],[TIM],[UNI],"
        "[Others1],[Others2],[Others3],[Others4],[Others5],[Others6],[係数],"
        "[S4],[TH4],[S5],[TH5]"
    )
    for d in details:
        values = ",".join(
            [
                _lit(header.report_date), _lit(header.line), _lit(header.shift), str(header.page), str(d.row_no),
                _lit(d.lot), _lit(d.zai), _lit(d.siz), _lit(d.ken), _lit(d.kz), _lit(d.kh), _lit(d.sz), _lit(d.sh),
                _lit(d.hit), _lit(d.ai), _lit(d.mai), _lit(d.tut), _lit(d.vc), _lit(d.et), _lit(d.s), _lit(d.th),
                _lit(d.ss), _lit(d.ths), _lit(d.sth), _lit(d.tht), _lit(d.con), _lit(d.wei), _lit(d.tim), _lit(d.uni),
                _lit(d.others1), _lit(d.others2), _lit(d.others3), _lit(d.others4), _lit(d.others5), _lit(d.others6),
                _lit(d.keisu),
                _lit(d.s4), _lit(d.th4), _lit(d.s5), _lit(d.th5),
            ]
        )
        statements.append(f"INSERT INTO [{detail_table_name(header.line)}] ({columns}) VALUES ({values})")
    return statements


@dataclass
class PushOutcome:
    key: tuple[str, str, str, int]
    success: bool
    error: Optional[AccessBridgeError] = None


@dataclass
class PushSummary:
    succeeded: list[tuple[str, str, str, int]] = field(default_factory=list)
    failed: list[PushOutcome] = field(default_factory=list)
    concurrency_warning: str = ""
    #: 一緒に送った直ごとの集計の数(書き先が sqlite3 のときだけ)
    summaries: int = 0
    #: 「中止」で送らずに残したページの数(次に押せば続きから)
    stopped: int = 0


def push_pending(
    repo: NippouRepository,
    accdb_path: Path,
    runner: Optional[ScriptRunner] = None,
) -> PushSummary:
    """Port of the write side of ``NippouDB_Save`` + ``ExecuteSQLTransaction``,
    replayed against every locally dirty header. Returns a summary the UI
    can surface (e.g. "3 saved, 1 failed: 現在他の人が使用中です")."""
    summary = PushSummary()

    # **`.laccdb` は Access のしくみ。** sqlite3 にそれは無く、同時に書こうと
    # しても待てば通る(`busy_timeout`)ので、ここで警告を出す意味がない ──
    # 出すと「誰かが使っています」が毎回出て、本当に困ったときに読まれなくなる
    if not is_sqlite_target(accdb_path):
        status = check_concurrent_access(accdb_path)
        if status.others_may_be_editing:
            summary.concurrency_warning = status.message()
            _logger.warning("concurrent access detected before push: %s",
                            status.message())

    # **進み具合を置く**(共有へ保存の棒。走っていなければ何もしない)
    from .. import job_progress
    from ..logic import progress

    headers = list(repo.pending_sync_headers())
    job_progress.step(phase=progress.PHASE_SEND, total=len(headers), done=0)
    sqlite_target = is_sqlite_target(accdb_path)
    size = BATCH_PAGES if sqlite_target else 1
    done = 0
    for start in range(0, len(headers), size):
        # **「中止」は束の切れ目で受ける。** 送った分は共有に入っていて、
        # 残りは未送信のまま(次に押せば続きから)
        if job_progress.stop_requested():
            summary.stopped = len(headers) - done
            break
        chunk = headers[start:start + size]
        job_progress.step(label=_chunk_label(chunk))
        if sqlite_target:
            _push_batch(repo, Path(accdb_path), chunk, summary)
        else:
            _push_guarded(repo, accdb_path, chunk[0], runner, summary)
        done += len(chunk)
        job_progress.step(done=done)

    if not summary.stopped:
        summary.summaries = _push_summaries(repo, accdb_path)
    return summary


#: 1回開いて1回で確定するページ数(共有が sqlite3 のとき)。
#: 多すぎると1つの確定が長くなり、そのあいだ他の端末が書けない。
#: 25ページ(=300行)なら共有フォルダの上でも数秒で終わる
BATCH_PAGES = 25
#: 集計の束(直の数)。1直で3つの表を入れ替えるので、ページより小さく
BATCH_SHIFTS = 10


def _chunk_label(chunk: list[HeaderRecord]) -> str:
    first, last = chunk[0], chunk[-1]
    head = f"{first.report_date} {first.line} {first.shift} {first.page}ページ"
    if len(chunk) == 1:
        return head
    return f"{head} 〜 {last.report_date} {last.shift} {last.page}ページ"


def _push_guarded(repo: NippouRepository, accdb_path: Path, header: HeaderRecord,
                  runner: Optional[ScriptRunner], summary: "PushSummary") -> None:
    """1ページを送る。**想定外の例外でも、ほかのページは止めない。**"""
    # 1キー分の反映処理を個別にtry/exceptで保護する。
    # 「他ライン(=他キー)を巻き込んで処理全体を止めない」ことが目的で、
    # SQL文組み立て時の想定外のデータ型エラーなど、run_with_retry の
    # リトライ対象にならない異常もここで確実に食い止める。
    try:
        _push_one(repo, accdb_path, header, runner, summary)
    except Exception as exc:  # noqa: BLE001 - 他キーの処理を止めないための意図的な広い捕捉
        _logger.exception("反映処理中に予期しないエラーが発生しました key=%s", header.key())
        repo.log_sync("push", header_table_name(header.line), "error", f"予期しないエラー: {exc}")
        summary.failed.append(
            PushOutcome(
                key=header.key(),
                success=False,
                error=AccessBridgeError(kind=ErrorKind.UNKNOWN, err_number=None, message=str(exc)),
            )
        )


def _push_batch(repo: NippouRepository, accdb_path: Path,
                chunk: list[HeaderRecord], summary: "PushSummary") -> None:
    """何ページかを1回で送る。**駄目なら1ページずつ送り直して、悪いページだけ残す。**"""
    from .sqlite_backend import push_records_batch

    items = []
    for header in chunk:
        loaded = repo.load(header.report_date, header.line, header.shift, header.page)
        if loaded is None:
            continue
        _stored, details = loaded
        items.append((header, details, header_table_name(header.line),
                      detail_table_name(header.line)))
    if not items:
        return
    try:
        result = push_records_batch(accdb_path, items)
    except Exception:  # noqa: BLE001 - 1ページずつに切り替えて切り分ける
        _logger.exception("束で送れませんでした(1ページずつ送り直します)")
        result = None
    if result is not None and result.success:
        for header, *_ in items:
            repo.mark_synced(header.key())
            summary.succeeded.append(header.key())
        for line in sorted({h.line for h, *_ in items}):
            repo.log_sync("push", header_table_name(line), "success",
                          f"{sum(1 for h, *_ in items if h.line == line)}ページ")
        return
    # 束のどれかが悪い(または共有がロック中)。**1ページずつ**送り直して、
    # 送れるものは送り、送れないものだけを失敗として返す
    for header, *_ in items:
        _push_guarded(repo, accdb_path, header, None, summary)


def _push_summaries(repo: NippouRepository, accdb_path: Path) -> int:
    """直ごとの集計も共有へ置く。**送れた数を返す。**

    VBA には無い表です ── あちらは集計を Excel のシートに書いていました。
    値で残すと決めたので、共有にも**手元と同じ3階層**で置きます
    (合計 / ロット行 / 停止)。ツールが無くても中身を読めることが、
    残す意味の半分です。

    **日報の反映が主で、こちらは従。** 集計は明細から何度でも作り直せる
    ので、ここで失敗しても日報の反映結果は返します(記録だけ残す)。
    書き先が Access のときは飛ばします(あちらに表を増やしません)。
    """
    if not is_sqlite_target(accdb_path):
        return 0
    from .. import job_progress
    from ..logic import progress
    from . import sqlite_backend

    reports = list(repo.pending_packing_reports())
    job_progress.step(phase=progress.PHASE_SEND_SUMMARY, total=len(reports), done=0)
    sent = 0
    for start in range(0, len(reports), BATCH_SHIFTS):
        if job_progress.stop_requested():
            break
        chunk = reports[start:start + BATCH_SHIFTS]
        first = chunk[0]
        job_progress.step(label=f"{first.work_date} {first.line_name} {first.shift}"
                                + (f" ほか{len(chunk) - 1}直" if len(chunk) > 1 else ""))
        items = [(r, repo.packing_details(r.id), summary_table_name(r.line_name),
                  agg_detail_table_name(r.line_name), stop_detail_table_name(r.line_name))
                 for r in chunk]
        try:
            result = sqlite_backend.push_summary_batch(Path(accdb_path), items)
        except Exception:                         # noqa: BLE001 - 1直ずつへ
            _logger.exception("集計を束で送れませんでした(1直ずつ送り直します)")
            result = None
        if result is not None and result.success:
            for record in chunk:
                repo.mark_packing_report_synced(record.key())
            sent += len(chunk)
        else:
            sent += _push_summaries_one_by_one(repo, Path(accdb_path), items)
        job_progress.step(done=min(len(reports), start + len(chunk)))
    return sent


def _push_summaries_one_by_one(repo: NippouRepository, accdb_path: Path,
                               items: list) -> int:
    """束で送れなかった集計を1直ずつ。送れた数を返す(悪い直だけ残る)。"""
    from . import sqlite_backend

    sent = 0
    for item in items:
        record = item[0]
        key = record.key()
        table = item[2]
        try:
            result = sqlite_backend.push_summary_batch(accdb_path, [item])
        except Exception as exc:                  # noqa: BLE001 - 日報の反映は返す
            _logger.exception("集計の反映で予期しないエラー key=%s", key)
            repo.log_sync("push", table, "error", str(exc))
            continue
        if result.success:
            repo.mark_packing_report_synced(key)
            sent += 1
        else:
            repo.log_sync("push", table, "error",
                          result.err_desc or "集計の反映に失敗しました")
    return sent


#: あとから増えた明細の列(Access の表に無ければ足す)。作業停止④⑤(v4.24.0)
ADDED_DETAIL_COLUMNS: tuple[str, ...] = ("S4", "TH4", "S5", "TH5")
#: このプロセスで列を足し終えた(書き先, 表)。**1つの表には1回だけ**流す
_columns_ensured: set[tuple[str, str]] = set()


def add_columns_statements(table: str) -> list[str]:
    return [f"ALTER TABLE [{table}] ADD COLUMN [{c}] TEXT(255)" for c in ADDED_DETAIL_COLUMNS]


def _ensure_access_columns(accdb_path: Path, line: str,
                           runner: Optional[ScriptRunner]) -> None:
    """共有の Access の明細表に、作業停止④⑤の列を足す(もう有れば何もしない)。

    足さないと、日報の INSERT が「列がありません」で断られ、共有へ保存
    できなくなります。失敗しても日報の送信は続けます(断られた理由が
    そちらで出るので)。
    """
    table = detail_table_name(line)
    key = (str(accdb_path), table)
    if key in _columns_ensured:
        return
    statements = add_columns_statements(table)
    try:
        if SETTINGS.access_backend == "odbc":
            from .odbc_backend import add_columns_odbc
            add_columns_odbc(accdb_path, statements)
        else:
            script = script_gen.build_add_columns_script(str(accdb_path), statements)
            (runner or ScriptRunner()).run(script)
        _columns_ensured.add(key)
    except Exception:  # noqa: BLE001 - 日報の送信は続ける
        _logger.exception("Access の表に作業停止④⑤の列を足せませんでした table=%s", table)


def _push_one(
    repo: NippouRepository,
    accdb_path: Path,
    header: HeaderRecord,
    runner: Optional[ScriptRunner],
    summary: "PushSummary",
) -> None:
    """1キー分の DELETE+INSERT を生成・実行し、成否を ``summary`` に積む。

    ``SETTINGS.access_backend == "odbc"`` の場合は VBScript/cscript.exe
    を経由せず、``dbkit.access_odbc`` のトランザクションで反映する
    (``runner`` はVBScript経路専用なのでODBC経路では使われない)。
    """
    loaded = repo.load(header.report_date, header.line, header.shift, header.page)
    if loaded is None:
        return
    _, details = loaded

    # **書き先が sqlite3 ならそちらへ回す。** 反映先が Access から sqlite3 へ
    # 移っても、呼び出し側(設定画面の「Accessへ反映」)は変わらない
    if is_sqlite_target(accdb_path):
        from .sqlite_backend import push_records
        result = push_records(
            Path(accdb_path), header, details,
            header_table_name(header.line), detail_table_name(header.line))
    elif SETTINGS.access_backend == "odbc":
        from .odbc_backend import push_statements_odbc
        _ensure_access_columns(accdb_path, header.line, runner)
        statements = (build_header_statements(header)
                      + build_detail_statements(header, details))
        result = push_statements_odbc(accdb_path, statements)
    else:
        _ensure_access_columns(accdb_path, header.line, runner)
        statements = (build_header_statements(header)
                      + build_detail_statements(header, details))
        result = run_with_retry(
            lambda: script_gen.build_push_script(str(accdb_path), statements),
            runner=runner,
        )

    if result.success:
        repo.mark_synced(header.key())
        repo.log_sync("push", header_table_name(header.line), "success")
        summary.succeeded.append(header.key())
    else:
        repo.log_sync("push", header_table_name(header.line), "error", str(result.error))
        summary.failed.append(PushOutcome(key=header.key(), success=False, error=result.error))
