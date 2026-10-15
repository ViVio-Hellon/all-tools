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

【書き先が Access のとき ── VBA の表に合わせて書く(v4.36.0)】

    VBA の日報をまだしばらく使い続けるか → 使います

書き先の .accdb は VBA が書き続けている表です。**こちらの形に表を変えません。**

    ・ページの列は**その表にある名前**で書く(VBA の表は `頁`。`build_header_statements`)
    ・作業停止④⑤(S4〜TH5)の列が無い表には④⑤を書かない。**列を ALTER TABLE で足さない**
      (VBA が列名を書かずに INSERT していると、列が増えた表で壊れるので)。④⑤を打った
      ページは「Access の表に作業停止④⑤の列が無いので、④⑤は共有へ出ていません」と
      知らせる(`PushSummary.notices`)。手元には残っています

表の列は、ACE を使わない内蔵の読み取り(`nippou/accdb`)で .accdb のファイルを読んで
知ります(`access_table`)。読めなかったときは VBA の形(`頁`・④⑤なし)とみなします。
v4.35.3 までは、送る前に明細表へ④⑤の列を ALTER TABLE で足していました(やめました)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from ..config import SETTINGS
from ..db.models import DetailRecord, HeaderRecord
from ..db.repository import NippouRepository
from ..logging_setup import get_logger
from . import script_gen
from .sqlite_backend import (
    DETAIL_COLUMNS,
    HEADER_COLUMNS,
    PAGE,
    STOP45_COLUMNS,
    PageColumn,
    page_names_of,
)
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


# ------------------------------------------------------------------
# 書き先が Access のときの表の形(v4.36.0)
# ------------------------------------------------------------------
@dataclass(frozen=True)
class AccessTable:
    """書き先の Access の表の列。`columns` が None なら**見られなかった**(VBA の形とみなす)。"""

    columns: Optional[tuple[str, ...]] = None

    @property
    def page(self) -> PageColumn:
        """ページの列。見られなかったときは VBA の `頁`。"""
        if self.columns is None:
            return PageColumn((PAGE,))
        return PageColumn(page_names_of(self.columns) or (PAGE,))

    def has(self, column: str) -> bool:
        """その列に書くか。見られなかったときは VBA の表にある列だけ(④⑤は書かない)。"""
        if self.columns is None:
            return column not in STOP45_COLUMNS
        return column in self.columns

    @property
    def stops45(self) -> bool:
        """作業停止④⑤の列が全部あるか。"""
        return all(self.has(c) for c in STOP45_COLUMNS)


#: VBA `NippouDB_EnsureTables` の形(`頁`・④⑤なし)。表を見られなかったときもこれ
VBA_TABLE = AccessTable()

#: 書き先(.accdb)ごとの「表の名前 → 列」。**1回の共有へ保存で1回だけ読む**
#: (`push_pending` の頭で忘れる)。読めなかったファイルは None
_access_columns: dict[str, Optional[dict[str, tuple[str, ...]]]] = {}


def _read_access_columns(accdb_path: Path) -> Optional[dict[str, tuple[str, ...]]]:
    """.accdb の表ごとの列(内蔵の読み取り。ACE を使わない)。読めなければ None。"""
    from ..accdb import reader

    try:
        with reader.AccdbReader(str(accdb_path)) as db:
            return {name: tuple(c.name for c in db.columns(name))
                    for name in db.table_names()}
    except Exception as exc:                      # noqa: BLE001 - VBA の形とみなして続ける
        _logger.warning("書き先の Access の表の列を読めませんでした(VBA の形とみなします)"
                        " %s: %s", accdb_path, exc)
        return None


def access_table(accdb_path: Path, table: str) -> AccessTable:
    """書き先の Access の表の形。ファイルを読めない・表が無いときは VBA の形。"""
    key = str(accdb_path)
    if key not in _access_columns:
        _access_columns[key] = _read_access_columns(Path(accdb_path))
    found = _access_columns[key]
    if found is None or table not in found:
        return VBA_TABLE
    return AccessTable(found[table])


def forget_access_tables() -> None:
    _access_columns.clear()


def _header_literals(header: HeaderRecord) -> dict[str, str]:
    """見出しの1行を Access の SQL の値に(列の名前は中の呼び名 ── `HEADER_COLUMNS`)。"""
    return {
        "報告日": _lit(header.report_date), "ライン": _lit(header.line),
        "直": _lit(header.shift), PAGE: str(header.page),
        "担当者": _lit(header.worker), "昼勤": _lit(header.day_shift),
        "枚数": _lit(header.count), "重量Kg": _lit(header.weight_kg),
        "Lot数": _lit(header.lot_count), "係数Lot数": _lit(header.coefficient_lot_count),
        "理由": _lit(header.reason), "保存日時": "Now()",
    }


def _detail_literals(header: HeaderRecord, d: DetailRecord) -> dict[str, str]:
    from .sqlite_backend import _detail_values

    out = {}
    for column, value in zip(DETAIL_COLUMNS, _detail_values(header, d)):
        out[column] = str(value) if column in (PAGE, "行番号") else _lit(value)
    return out


def _key_where(header: HeaderRecord, table: AccessTable) -> str:
    """そのページを指す WHERE。ページは**主の列**で比べる(Access の SQL に COALESCE は無い)。"""
    return (
        f"[報告日]={_lit(header.report_date)} AND [ライン]={_lit(header.line)} "
        f"AND [直]={_lit(header.shift)} AND [{table.page.main}]={header.page}"
    )


def _insert(table_name: str, logical: tuple[str, ...], literals: dict[str, str],
            table: AccessTable) -> str:
    """その表にある列だけで INSERT する。ページの列はその表の名前(両方あれば両方)。"""
    page = table.page
    columns: list[str] = []
    values: list[str] = []
    for column in logical:
        if column == PAGE:
            for name in page.write_names:
                columns.append(name)
                values.append(literals[PAGE])
        elif table.has(column):
            columns.append(column)
            values.append(literals[column])
    return (f"INSERT INTO [{table_name}] ({','.join(f'[{c}]' for c in columns)}) "
            f"VALUES ({','.join(values)})")


def build_header_statements(header: HeaderRecord,
                            table: AccessTable = VBA_TABLE) -> list[str]:
    """1ページの見出しを入れ替える2文。`table` は書き先の表の形(既定は VBA の形)。"""
    name = header_table_name(header.line)
    delete_sql = f"DELETE FROM [{name}] WHERE {_key_where(header, table)}"
    return [delete_sql, _insert(name, HEADER_COLUMNS, _header_literals(header), table)]


def build_detail_statements(header: HeaderRecord, details: list[DetailRecord],
                            table: AccessTable = VBA_TABLE) -> list[str]:
    """1ページの明細を入れ替える文。**その表に無い列(VBA の表の④⑤)は書かない。**"""
    name = detail_table_name(header.line)
    statements = [f"DELETE FROM [{name}] WHERE {_key_where(header, table)}"]
    for d in details:
        statements.append(_insert(name, DETAIL_COLUMNS, _detail_literals(header, d), table))
    return statements


def has_stops45(details: list[DetailRecord]) -> bool:
    """作業停止④⑤を打った行があるか。"""
    return any((d.s4 or "").strip() or (d.th4 or "").strip()
               or (d.s5 or "").strip() or (d.th5 or "").strip() for d in details)


#: ④⑤を書けなかったときの知らせ(利用者に決めてもらった言い方のまま)
NO_STOPS45_NOTICE = "Access の表に作業停止④⑤の列が無いので、④⑤は共有へ出ていません"


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
    #: 送れたが、知らせておくこと(書き先が Access で④⑤を書けなかった、など。v4.36.0)
    notices: list[str] = field(default_factory=list)
    #: ④⑤を書けなかったページ(知らせの中身。`notices` はここから作る)
    no_stops45: list[tuple[str, str, str, int]] = field(default_factory=list)


def push_pending(
    repo: NippouRepository,
    accdb_path: Path,
    runner: Optional[ScriptRunner] = None,
    *,
    keys: Optional[Iterable[tuple[str, str, str]]] = None,
) -> PushSummary:
    """Port of the write side of ``NippouDB_Save`` + ``ExecuteSQLTransaction``,
    replayed against every locally dirty header. Returns a summary the UI
    can surface (e.g. "3 saved, 1 failed: 現在他の人が使用中です").

    `keys` を渡すと、**その直 (報告日, ライン, 直) のページだけ**を送ります
    (v4.35.0 ── いまの直 / 過去分のボタン、`logic/push_scope`)。送らなかった
    直の集計も送りません(:func:`_push_summaries`)。渡さなければ未送信ぜんぶ。
    """
    summary = PushSummary()
    wanted = None if keys is None else {tuple(k) for k in keys}
    # 書き先が Access のとき、表の列は**押すたびに読み直す**(VBA の側で表が変わるかもしれない)
    forget_access_tables()

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

    pending = list(repo.pending_sync_headers())
    headers = [h for h in pending
               if wanted is None or (h.report_date, h.line, h.shift) in wanted]
    # 範囲の外で未送信のまま残す直。**その直の集計も共有へ出さない**
    held = {(h.report_date, h.line, h.shift) for h in pending} - (
        {(h.report_date, h.line, h.shift) for h in headers})
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
        summary.summaries = _push_summaries(repo, accdb_path, held=held)
    if summary.no_stops45:
        first = summary.no_stops45[0]
        where = f"{first[0]} {first[1]} {first[2]} {first[3]}ページ"
        more = len(summary.no_stops45) - 1
        summary.notices.append(
            f"{NO_STOPS45_NOTICE}(手元には残っています。{where}"
            + (f" ほか{more}ページ" if more else "") + ")")
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


def _push_summaries(repo: NippouRepository, accdb_path: Path, *,
                    held: Optional[set] = None) -> int:
    """直ごとの集計も共有へ置く。**送れた数を返す。**

    VBA には無い表です ── あちらは集計を Excel のシートに書いていました。
    値で残すと決めたので、共有にも**手元と同じ3階層**で置きます
    (合計 / ロット行 / 停止)。ツールが無くても中身を読めることが、
    残す意味の半分です。

    **日報の反映が主で、こちらは従。** 集計は明細から何度でも作り直せる
    ので、ここで失敗しても日報の反映結果は返します(記録だけ残す)。
    書き先が Access のときは飛ばします(あちらに表を増やしません)。

    `held` は**今回は送らなかった直**(範囲の外・関門で止めた)。その直の集計も
    出しません ── 日報は手元に残したのに、集計だけ共有へ出ると食い違います
    (いまの直の途中の数が、過去分のボタンで共有へ出てしまう、など)。
    """
    if not is_sqlite_target(accdb_path):
        return 0
    from .. import job_progress
    from ..logic import progress
    from . import sqlite_backend

    reports = [r for r in repo.pending_packing_reports()
               if not held or tuple(r.key()) not in held]
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
    else:
        # **VBA の表に合わせて書く**(列を足さない ── 冒頭)
        head_table = access_table(accdb_path, header_table_name(header.line))
        rows_table = access_table(accdb_path, detail_table_name(header.line))
        statements = (build_header_statements(header, head_table)
                      + build_detail_statements(header, details, rows_table))
        if SETTINGS.access_backend == "odbc":
            from .odbc_backend import push_statements_odbc
            result = push_statements_odbc(accdb_path, statements)
        else:
            result = run_with_retry(
                lambda: script_gen.build_push_script(str(accdb_path), statements),
                runner=runner,
            )
        if result.success and not rows_table.stops45 and has_stops45(details):
            summary.no_stops45.append(header.key())

    if result.success:
        repo.mark_synced(header.key())
        repo.log_sync("push", header_table_name(header.line), "success")
        summary.succeeded.append(header.key())
    else:
        repo.log_sync("push", header_table_name(header.line), "error", str(result.error))
        summary.failed.append(PushOutcome(key=header.key(), success=False, error=result.error))
