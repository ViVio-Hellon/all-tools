"""Access と SQLite の同期。

ローカル SQLite は「この端末専用の作業用の写し + 送信待ちの一時置き場」で
あり、正式なデータは常に Access 側にある(``sqlite_path`` は端末ごとの
ローカルディスクを指すのが既定であり、SQLite 自体を複数端末で共有はしない)。
このため同期は 2 方向を回し続ける。

* 起動時 + 定期的: Access(.accdb) -> SQLite (:func:`import_all` / :class:`Importer`)
  他ラインや倉庫が Access へ書き戻した内容をこの端末へ反映する。
* 操作直後 + 定期的: SQLite -> Access(.accdb) (:func:`export_pending` / :class:`Exporter`)
  この端末での操作を Access へ反映する。

書き戻しは「更新した列だけを UPDATE する」方式で、VBA 版と同じく全列上書きに
よるロストアップデートを避ける。取り込みは未反映(``dirty``)の行の状態列を
上書きしない(:meth:`kanban.db.store.Store.import_line` 参照)ため、この
2 方向を同時に回しても自分の未送信の操作が消えることはない。
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Sequence

from dbkit.periodic import PeriodicTask

from .. import applog, config
from ..accdb.types import CATEGORY_NUMBER, CATEGORY_TEXT
from .shared import SharedDb, TableSnapshot, build_update
from ..domain.models import MUTABLE_FIELDS
from .store import DEFAULT_COLUMN_MAP, Store

EXPORT_LOCK_NAME = "accdb_export"
#: 1 回の書き戻しで送る SQL の最大数(長すぎる IN 句や巨大トランザクションを避ける)
EXPORT_CHUNK_SIZE = 100

#: ``Form状態管理`` に実際にあった列(カンマ区切り)を覚えておく meta キー。
#:
#: 心拍(:mod:`kanban.presence`)が「``更新日時`` を書いてよいか」を、
#: **共有DBを読み直さずに**決めるために使う。読む側は列が無くても既定値で
#: 済ませているので、書く側も同じだけ寛容にしておく。
META_STATE_COLUMNS = "state_table_columns"


@dataclass
class LineImportResult:
    """1 ラインの取り込み結果。"""

    line: str
    table_name: str
    ok: bool
    inserted: int = 0
    updated: int = 0
    protected: int = 0
    message: str = ""

    @property
    def total(self) -> int:
        return self.inserted + self.updated + self.protected


@dataclass
class ImportResult:
    """取り込み全体の結果。"""

    lines: list[LineImportResult] = field(default_factory=list)
    status_rows: int = 0
    started_at: str = ""
    finished_at: str = ""
    access_mode: str = ""

    @property
    def ok_lines(self) -> list[LineImportResult]:
        return [r for r in self.lines if r.ok]

    @property
    def failed_lines(self) -> list[LineImportResult]:
        return [r for r in self.lines if not r.ok]

    def summary(self) -> str:
        ok = len(self.ok_lines)
        ng = len(self.failed_lines)
        total = sum(r.total for r in self.ok_lines)
        text = f"取り込み完了: {ok} ライン / {total} 件"
        if ng:
            names = ", ".join(r.table_name for r in self.failed_lines)
            text += f" (取得できなかったテーブル: {names})"
        return text


@dataclass
class ExportResult:
    """書き戻しの結果。"""

    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped_reason: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failed == 0 and not self.skipped_reason


# ---------------------------------------------------------------------------
# 取り込み(Access -> SQLite)
# ---------------------------------------------------------------------------
def import_all(
    store: Store,
    gateway: SharedDb,
    lines: Sequence[str] | None = None,
    keep_local_changes: bool = True,
    progress: Callable[[str, int, int], None] | None = None,
) -> ImportResult:
    """Access の 看板テーブル群と ``Form状態管理`` を SQLite へ取り込む。

    テーブルが存在しないラインは、VBA 版と同じく警告を出さずに読み飛ばす
    (未実装のラインがあるため)。
    """
    # 書き戻しと重ねない(:attr:`kanban.db.store.Store.sync_lock`)
    with store.sync_lock:
        return _import_all(store, gateway, lines, keep_local_changes, progress)


def _import_all(
    store: Store,
    gateway: SharedDb,
    lines: Sequence[str] | None,
    keep_local_changes: bool,
    progress: Callable[[str, int, int], None] | None,
) -> ImportResult:
    codes = list(lines) if lines is not None else config.all_line_codes()
    # **読み始める前の書き戻しの番号を控える。** 別のプロセス(``main.py --export-only``)の
    # 書き戻しは錠では止められないので、読んだあとに送れた行はこれで見分ける
    synced_before = store.sync_generation()
    result = ImportResult(
        started_at=datetime.now().strftime("%Y/%m/%d %H:%M:%S"),
        access_mode=gateway.mode,
    )
    applog.info(
        "import_all: 開始 (経路=%s, 対象=%s)", gateway.mode, ",".join(codes)
    )

    available: set[str] | None = None
    try:
        available = set(gateway.table_names())
    except Exception as exc:  # テーブル一覧が取れなくても個別読み取りは試す
        applog.warning("import_all: テーブル一覧の取得に失敗 (%s)", exc)

    total = len(codes)
    for index, code in enumerate(codes, start=1):
        table_name = config.table_name_for(code)
        if progress:
            progress(code, index, total)
        if available is not None and table_name not in available:
            applog.info("import_all: %s は存在しないため読み飛ばし", table_name)
            result.lines.append(
                LineImportResult(
                    line=code,
                    table_name=table_name,
                    ok=False,
                    message="テーブルが存在しません",
                )
            )
            continue
        result.lines.append(
            _import_line(store, gateway, code, table_name, keep_local_changes, synced_before)
        )

    result.status_rows = _import_line_status(store, gateway)
    if available is not None:
        # 共有DBに看板の表が無いライン。看板の画面はそれを見て帯を出し、ボタンを止める
        # (押しても共有DBへ届かない)。**今回見たラインだけ**書き換える
        missing = {r.line for r in result.lines if not r.ok and r.message == "テーブルが存在しません"}
        before = {c for c in store.get_meta("missing_line_tables", "").split(",") if c}
        store.set_meta("missing_line_tables", ",".join(sorted((before - set(codes)) | missing)))
    # 倉庫 ⇔ 現場のコメント(取り込んだラインの分)
    import_comments(store, gateway, [line.line for line in result.ok_lines])
    result.finished_at = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
    store.set_meta("last_import_at", result.finished_at)
    store.set_meta("last_import_source", gateway.path)
    applog.info("import_all: %s", result.summary())
    return result


def _import_line(
    store: Store,
    gateway: SharedDb,
    code: str,
    table_name: str,
    keep_local_changes: bool,
    synced_before: int | None = None,
) -> LineImportResult:
    try:
        snapshot = gateway.read_table(table_name)
    except Exception as exc:
        applog.warning("import_all: %s の読み取りに失敗 (%s)", table_name, exc)
        return LineImportResult(
            line=code, table_name=table_name, ok=False, message=str(exc)
        )

    missing = [c for c in config.REQUIRED_COLUMNS if c not in snapshot.columns]
    if missing:
        message = f"必要な列がありません: {', '.join(missing)}"
        applog.warning("import_all: %s %s", table_name, message)
        return LineImportResult(
            line=code, table_name=table_name, ok=False, message=message
        )

    column_map = {
        logical: actual
        for logical, actual in DEFAULT_COLUMN_MAP.items()
        if actual in snapshot.columns
    }
    key_column = _resolve_key_column(snapshot)
    key_category = snapshot.categories.get(key_column, CATEGORY_TEXT)

    rows: list[dict[str, Any]] = []
    for raw in snapshot.rows:
        row = {
            logical: _as_text(raw.get(actual, ""))
            for logical, actual in column_map.items()
        }
        row["mgmt_no"] = _as_text(raw.get(key_column, "")).strip()
        if not row["mgmt_no"]:
            continue
        rows.append(row)

    inserted, updated, protected = store.import_line(
        line=code,
        table_name=table_name,
        key_column=key_column,
        key_category=key_category,
        column_map=column_map,
        categories=snapshot.categories,
        rows=rows,
        source_path=gateway.path,
        keep_local_changes=keep_local_changes,
        synced_before=synced_before,
    )
    applog.info(
        "import_all: %s 取り込み成功 (追加=%d 更新=%d 保護=%d)",
        table_name,
        inserted,
        updated,
        protected,
    )
    return LineImportResult(
        line=code,
        table_name=table_name,
        ok=True,
        inserted=inserted,
        updated=updated,
        protected=protected,
    )


def _resolve_key_column(snapshot: TableSnapshot) -> str:
    """キー列を決める。

    VBA 版は「``SELECT *`` の先頭列」をキーとして扱っていた。しかし
    ``看板_L1`` や ``看板_大板小板`` のように後から ``管理番号`` を追加した
    テーブルでは列順が変わっており、先頭列(``資材``)をキーにすると同じ資材の
    複数行をまとめて書き換えてしまう。ここでは列名で ``管理番号`` を探し、
    見つからない場合だけ先頭列に戻す。
    """
    if config.COL_KEY in snapshot.columns:
        return config.COL_KEY
    applog.warning(
        "%s に %s 列がないため先頭列 %s をキーとして使用します",
        snapshot.name,
        config.COL_KEY,
        snapshot.key_column(),
    )
    return snapshot.key_column()


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _import_line_status(store: Store, gateway: SharedDb) -> int:
    """``Form状態管理`` を取り込む。

    **開閉だけでなく、更新日時とホスト名も持ち帰ります。** 「いま誰が開いて
    いるか」を出すのに要ります(:mod:`kanban.presence`)。VBA 版は開いた印を
    付けるだけだったので、異常終了すると開いたままになりましたが、更新日時が
    あれば「印は立っているが、もう誰も打っていない」と分かります。
    """
    try:
        snapshot = gateway.read_table(config.TABLE_STATE)
    except Exception as exc:
        applog.warning("import_all: %s の読み取りに失敗 (%s)", config.TABLE_STATE, exc)
        return 0

    # 書く側(心拍)が、この表にどの列があるかを知るために残す。ここで
    # 覚えておけば、書くたびに共有DBを読み直さずに済む
    store.set_meta(META_STATE_COLUMNS, ",".join(snapshot.columns))

    rows: list[dict[str, Any]] = []
    for order, raw in enumerate(snapshot.rows):
        line = _as_text(raw.get("ライン名", "")).strip()
        if not line:
            continue
        status = _as_text(raw.get("状態", "")).strip() or config.STATE_CLOSED
        try:
            sort_order = int(_as_text(raw.get(config.COL_KEY, order)) or order)
        except ValueError:
            sort_order = order
        rows.append(
            {
                "line": line,
                "status": status,
                "sort_order": sort_order,
                "stamp": _as_text(raw.get(config.COL_STATE_UPDATED_AT, "")).strip(),
                "host": _as_text(raw.get(config.COL_STATE_HOST, "")).strip(),
            }
        )
    store.upsert_line_status_rows(rows)
    return len(rows)


# ---------------------------------------------------------------------------
# 書き戻し(SQLite -> Access)
# ---------------------------------------------------------------------------
def export_pending(
    store: Store, gateway: SharedDb, use_lock: bool = True
) -> ExportResult:
    """未反映の変更を Access へ書き戻す。"""
    result = ExportResult()

    if not gateway.can_write:
        result.skipped_reason = "Access への書き戻しができない環境です(ADO 不可)"
        return result

    # 取り込みと重ねない(:attr:`kanban.db.store.Store.sync_lock`)。重なると、取り込みが
    # 送る前に読んだ値で、送り終えた行を戻してしまう
    with store.sync_lock:
        return _export_pending(store, gateway, use_lock, result)


def _export_pending(store: Store, gateway: SharedDb, use_lock: bool, result: ExportResult) -> ExportResult:
    pending = store.pending_items()
    if not pending:
        return result

    if use_lock and not store.acquire_lock(EXPORT_LOCK_NAME):
        result.skipped_reason = "他の端末が書き戻し中のため待機します"
        applog.debug("export_pending: %s", result.skipped_reason)
        return result

    try:
        result = _export_items(store, gateway, pending, result)
        store.set_meta("last_export_at", datetime.now().strftime("%Y/%m/%d %H:%M:%S"))
    finally:
        if use_lock:
            store.release_lock(EXPORT_LOCK_NAME)
    return result


def _key_value(value: Any, category: str) -> Any:
    """キー列の値を、その列の型に合わせて渡す。

    ``管理番号`` は Access では数値のことがあり、手元の SQLite では文字列
    として持っている(``mgmt_no`` は TEXT)。数値列に文字列で問い合わせると、
    型親和性しだいで**一致せず 0 件更新**になりうる ── 更新できないのに
    エラーにもならないので、いちばん気づきにくい壊れ方をする。ここで
    明示的にそろえる。
    """
    if category != CATEGORY_NUMBER:
        return value
    text = str(value).strip()
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            # 数値列のはずが数値でない。そのまま渡して、結果(0 件更新)を
            # 呼び出し側の失敗扱いに任せる
            return value


def _missing_row_reason(gateway: SharedDb, table: str, key_column: str, mgmt_no: str) -> str:
    """送り先の行が見つからない理由。**行が無い**のか、**番号の書き方が違う**のか。"""
    from .. import keys

    try:
        found = [r["k"] for r in gateway.select(
            f"SELECT {_quote(key_column)} AS k FROM {_quote(table)}")]
    except Exception:  # noqa: BLE001 - 理由が分からなくても失敗の記録はする
        return "対象行なし"
    near = [k for k in found if keys.same(k, mgmt_no)]
    if near:
        return (f"対象行なし: 共有DBの {table} では{key_column}が「{near[0]}」と書かれていて"
                f"(文字の種類・空白・型の違い)、この端末の「{mgmt_no}」と一致しません")
    return (f"対象行なし: 共有DBの {table} に{key_column} {mgmt_no} の行がありません"
            "(マスタで消された・Access から差し替えた・接続先が変わった など)")


#: 論理名 → 共有DBでの列名。**利用者に見せるのは共有DB側の言葉**
#: (``hold`` ではなく ``保留``)── 直しに行くのは共有DBのほうなので。
def _actual_names(column_map: dict[str, str], logicals: Sequence[str]) -> list[str]:
    return [column_map.get(name) or DEFAULT_COLUMN_MAP.get(name, name) for name in logicals]


def _export_items(
    store: Store,
    gateway: SharedDb,
    pending: Sequence[dict[str, Any]],
    result: ExportResult,
) -> ExportResult:
    by_line: dict[str, list[dict[str, Any]]] = {}
    for row in pending:
        by_line.setdefault(row["line"], []).append(row)

    for line, rows in by_line.items():
        source = store.line_source(line)
        if source is None:
            message = f"{line}: 取り込み元テーブルの情報がありません"
            applog.error("export_pending: %s", message)
            result.errors.append(message)
            result.failed += len(rows)
            store.mark_items_failed(
                [(r["line"], r["mgmt_no"]) for r in rows], message
            )
            continue

        column_map: dict[str, str] = source["column_map"]
        categories: dict[str, str] = source["categories"]
        table_name: str = source["table_name"]
        key_column: str = source["key_column"]
        key_category: str = source["key_category"]

        for chunk in _chunks(rows, EXPORT_CHUNK_SIZE):
            statements: list[tuple[str, list[Any]]] = []
            keys: list[tuple[str, str, int]] = []
            for row in chunk:
                # dirty_columns にはこの端末が実際に変更を意図した列だけが
                # 入っている(kanban/db/store.py 参照)。ここだけを送ることで、
                # 他端末がこの行の別の列(例: 発送)を書き戻していても
                # 巻き込んで上書きしない。空の場合(スキーマ移行前の古い行)
                # だけ安全側に倒して状態列全部を送る
                dirty_columns = [
                    c for c in (row.get("dirty_columns") or "").split(",") if c
                ]
                target_columns = dirty_columns or list(MUTABLE_FIELDS)
                assignments: dict[str, tuple[Any, str]] = {}
                unmapped: list[str] = []
                for logical in target_columns:
                    actual = column_map.get(logical)
                    if not actual or logical not in row:
                        unmapped.append(logical)
                        continue
                    assignments[actual] = (
                        row[logical],
                        categories.get(actual, CATEGORY_TEXT),
                    )
                if not assignments:
                    # **送る先が 1 つも無い。** 黙って飛ばしてはいけない ──
                    # 飛ばすと成功にも失敗にもならず、``dirty`` が落ちないまま
                    # 永久に残る。「未反映 N 件」が減らなくなり、
                    # ``_busy_reason`` がそれを見てアプリを終わらせなくなる。
                    #
                    # 起きるのは、共有DB側にその列が無いとき。``保留`` と
                    # ``注文中日時`` は任意列なので(``config.REQUIRED_COLUMNS``)、
                    # 持たない看板テーブルが実際にありうる
                    message = (
                        f"共有DBの {table_name} に "
                        f"{', '.join(_actual_names(column_map, unmapped))} 列が"
                        "ありません。この変更は送れません"
                    )
                    applog.error("export_pending: %s/%s: %s", line, row["mgmt_no"], message)
                    result.errors.append(f"{line}/{row['mgmt_no']}: {message}")
                    result.failed += 1
                    store.mark_items_failed([(line, row["mgmt_no"])], message)
                    continue
                try:
                    statements.append(
                        build_update(
                            table_name,
                            {c: v for c, (v, _cat) in assignments.items()},
                            key_column,
                            _key_value(row["mgmt_no"], key_category),
                        )
                    )
                except ValueError as exc:
                    message = f"{line}/{row['mgmt_no']}: {exc}"
                    applog.error("export_pending: %s", message)
                    result.errors.append(message)
                    result.failed += 1
                    store.mark_items_failed([(line, row["mgmt_no"])], str(exc))
                    continue
                keys.append((row["line"], row["mgmt_no"], row["rev"]))

            if not statements:
                continue

            result.attempted += len(statements)
            try:
                # **1 文ずつ独立にコミットされる**(SharedDb.execute)。
                # まとめて巻き戻されると、実際には届いていない更新を
                # 「成功」として dirty を落としてしまう
                statement_results = gateway.execute(statements)
            except Exception as exc:
                message = f"{line}: 書き戻しに失敗 ({exc})"
                applog.error("export_pending: %s", message)
                result.errors.append(message)
                result.failed += len(statements)
                store.mark_items_failed([(k[0], k[1]) for k in keys], str(exc))
                continue

            synced: list[tuple[str, str, int]] = []
            for key, statement_result in zip(keys, statement_results):
                if statement_result.ok and statement_result.affected > 0:
                    synced.append(key)
                elif statement_result.ok:
                    # 条件に一致する行が共有DBに無い。**なぜ無いのか**まで書く
                    # (画面の「要確認」にそのまま出る。「対象行なし」だけでは直しようがない)
                    why = _missing_row_reason(gateway, table_name, key_column, key[1])
                    message = f"{key[0]}/{key[1]}: {why}"
                    applog.warning("export_pending: %s", message)
                    result.errors.append(message)
                    result.failed += 1
                    store.mark_items_failed([(key[0], key[1])], why)
                else:
                    # (Access の頃の ``error_number`` はもう無い。参照すると
                    # 例外で書き戻しの周期ごと止まり、失敗も記録されなかった)
                    message = f"{key[0]}/{key[1]}: {statement_result.error_message}"
                    applog.error("export_pending: %s", message)
                    result.errors.append(message)
                    result.failed += 1
                    store.mark_items_failed(
                        [(key[0], key[1])], statement_result.error_message
                    )

            cleared = store.mark_items_synced(synced)
            result.succeeded += cleared
            if cleared != len(synced):
                applog.info(
                    "export_pending: %d 件は書き戻し中に再操作されたため次回送信します",
                    len(synced) - cleared,
                )
    return result


# ---------------------------------------------------------------------------
# 看板の出来事(集計のための記録)
# ---------------------------------------------------------------------------
#: 出来事を置く共有DBの表(kanban/domain/events.py)。看板の表(看板_<ライン>)と
#: 見分けるため、``看板_`` で始めない
HISTORY_TABLE = "看板履歴"

#: 共有DBの [看板履歴] の列(画面の集計・CSV もこの名前で読む)
HISTORY_COLUMNS = (
    ("ID", "uid"), ("日時", "at"), ("ライン", "line"), ("管理番号", "mgmt_no"),
    ("出来事", "kind"), ("資材", "material"), ("サイズ", "size"),
    ("出した日時", "ordered_at"), ("端末", "host"),
)


@dataclass
class HistoryTableResult:
    """:func:`ensure_history_table` の結果。"""

    ok: bool = True
    created: bool = False
    added_columns: list[str] = field(default_factory=list)
    message: str = ""


def ensure_history_table(gateway: SharedDb) -> HistoryTableResult:
    """共有DBの ``看板履歴`` を用意する。**無ければ作り、足りない列があれば足す。**

    起動したとき・接続先を変えたとき・出来事を送る前に呼ぶ。表が無いままだと
    集計タブが「まだ記録がありません」としか言えず、列が欠けていると(手で
    作った表・古い版が作った表)送るたびに断られて記録が端末に溜まり続ける。

    足すだけで、**消したり並べ替えたりはしない**(余分な列はそのまま残す)。
    足した列は空文字で埋まる。

    ``gateway`` が看板履歴.sqlite3(:class:`kanban.db.history.HistoryDb`)なら、
    ファイルが無ければ作る(看板の表が無いのが正しいファイルなので、その確かめはしない)。
    """
    if getattr(gateway, "role", "") == "history":
        from . import history

        try:
            created = history.create_if_missing(gateway)
        except Exception as exc:  # noqa: BLE001 - 届かないときは次の機会にまた試す
            return HistoryTableResult(ok=False, message=f"看板履歴のファイルを作れませんでした: {exc}")
        result = ensure_shared_table(gateway, HISTORY_TABLE, [n for n, _ in HISTORY_COLUMNS],
                                     "idx_看板履歴_ID", require_kanban=False, where="看板履歴.sqlite3")
        if created and result.ok:
            result.created = True
            result.message = f"看板履歴のファイルを作りました: {gateway.path}"
        return result
    return ensure_shared_table(gateway, HISTORY_TABLE, [n for n, _ in HISTORY_COLUMNS], "idx_看板履歴_ID")


def ensure_comment_table(gateway: SharedDb) -> HistoryTableResult:
    """共有DBの ``看板コメント`` を用意する(看板履歴と同じ扱い)。"""
    return ensure_shared_table(gateway, COMMENT_TABLE, [n for n, _ in COMMENT_COLUMNS], "idx_看板コメント_ID")


def ensure_shared_table(gateway: SharedDb, name: str, column_names: Sequence[str], index: str,
                        *, require_kanban: bool = True, where: str = "共有DB") -> HistoryTableResult:
    """共有DBに記録用の表を用意する。無ければ作り、足りない列があれば足す(消さない)。"""
    table = _quote(name)
    if not gateway.can_write:
        return HistoryTableResult(ok=False, message=f"{where}に書き込めないので用意しません")
    try:
        existing = gateway.column_names(name)
    except Exception as exc:  # noqa: BLE001 - 届かないときは次の機会にまた試す
        return HistoryTableResult(ok=False, message=f"{name} を確かめられませんでした: {exc}")

    result = HistoryTableResult()
    statements: list[tuple[str, list]] = []
    if not existing:
        # **看板マスタでなければ作らない。** 接続先が別のツールの sqlite3
        # (梱包資材マスタなど)を指していると、そこへこのツールの表を作ってしまう
        try:
            is_kanban = gateway.has_kanban_tables() if require_kanban else True
        except Exception as exc:  # noqa: BLE001
            return HistoryTableResult(ok=False, message=f"{name} を確かめられませんでした: {exc}")
        if not is_kanban:
            return HistoryTableResult(
                ok=False,
                message=f"共有DBに看板の表が無いので {name} は作りません(別のファイルを指していませんか): {gateway.path}",
            )
        columns = ", ".join(f"[{c}] TEXT NOT NULL DEFAULT ''" for c in column_names)
        statements.append((f"CREATE TABLE IF NOT EXISTS {table} ({columns})", []))
        result.created = True
    else:
        have = {c.casefold() for c in existing}
        for c in column_names:
            if c.casefold() not in have:
                statements.append((f"ALTER TABLE {table} ADD COLUMN [{c}] TEXT NOT NULL DEFAULT ''", []))
                result.added_columns.append(c)
    statements.append((f"CREATE INDEX IF NOT EXISTS [{index}] ON {table} ([ID])", []))

    outcomes = gateway.execute(statements)
    failed = [r for r in outcomes if not r.ok]
    if failed:
        return HistoryTableResult(
            ok=False, created=False, added_columns=[],
            message=f"{name} を用意できませんでした: {failed[0].error_message}",
        )
    if result.created:
        result.message = f"{where}に {name} を作りました"
        applog.info("%s", result.message)
    elif result.added_columns:
        result.message = f"{name} に足りない列を足しました: {'、'.join(result.added_columns)}"
        applog.info("%s", result.message)
    else:
        result.message = f"{name} は揃っています"
    return result


COMMENT_TABLE = "看板コメント"

#: 共有DBの [看板コメント] の列(集計の明細 CSV もこの名前で読む)
COMMENT_COLUMNS = (
    ("ID", "uid"), ("日時", "at"), ("ライン", "line"), ("管理番号", "mgmt_no"),
    ("種類", "kind"), ("書いた側", "side"), ("端末", "host"), ("本文", "body"),
)

#: 取り込むコメントの古さの上限(日)。片付いたやり取りをいつまでも読みに行かない
COMMENT_IMPORT_DAYS = 180


def export_comments(store: Store, gateway: SharedDb, limit: int = 500) -> int:
    """手元に積んだコメント・片付けの印を共有DBの ``看板コメント`` へ送る(二重に入れない)。"""
    rows = store.unsent_comments(limit)
    if not rows or not gateway.can_write:
        return 0
    table = _quote(COMMENT_TABLE)
    names = ", ".join(f"[{name}]" for name, _ in COMMENT_COLUMNS)
    marks = ", ".join("?" for _ in COMMENT_COLUMNS)
    statements = [
        (f"INSERT INTO {table} ({names}) SELECT {marks}"
         f" WHERE NOT EXISTS (SELECT 1 FROM {table} WHERE [ID] = ?)",
         [row[key] for _, key in COMMENT_COLUMNS] + [row["uid"]])
        for row in rows
    ]
    results = gateway.execute(statements)
    sent = [row["id"] for row, r in zip(rows, results) if r.ok]
    store.mark_comments_sent(sent)
    return len(sent)


def import_comments(store: Store, gateway: SharedDb, lines: Sequence[str]) -> int:
    """他の端末が書いたコメント・片付けの印を受け取る。表が無ければ何もしない。"""
    if not lines:
        return 0
    try:
        if not gateway.column_names(COMMENT_TABLE):
            return 0
        since = (datetime.now() - timedelta(days=COMMENT_IMPORT_DAYS)).strftime("%Y/%m/%d %H:%M:%S")
        marks = ", ".join("?" for _ in lines)
        rows = gateway.select(
            f"SELECT {', '.join(f'[{n}] AS {k}' for n, k in COMMENT_COLUMNS)}"
            f" FROM {_quote(COMMENT_TABLE)} WHERE [ライン] IN ({marks}) AND [日時] >= ?"
            # **共有DBに届いた順に足す。** いまの回か前の回かは、この端末に届いた順でも
            # 決める(:data:`kanban.db.store.Store._OPEN_COMMENT`)ので、片付けの印と
            # その前後のコメントの順番を崩さない
            " ORDER BY rowid",
            [*lines, since],
        )
    except Exception as exc:  # noqa: BLE001 - 看板の取り込みは止めない
        applog.warning("コメントを取り込めませんでした: %s", exc)
        return 0
    clean = [
        {k: ("" if r.get(k) is None else str(r.get(k))) for _, k in COMMENT_COLUMNS}
        for r in rows if r.get("uid")
    ]
    return store.merge_comments(clean)


def export_events(store: Store, gateway: SharedDb, limit: int = 500) -> int:
    """手元に溜まった出来事を共有DBの ``看板履歴`` へ送る。送れた件数を返す。

    **同じ出来事を 2 回入れない。** 送ったあと手元に「送った」と付ける前に
    落ちると、次の周期で同じものをもう一度送ることになる。出来事ごとの
    ``ID``(端末で振った uid)が無いときだけ入れる。
    """
    events = store.unsent_events(limit)
    if not events or not gateway.can_write:
        return 0
    # 表を作るのは ensure_history_table だけ(看板マスタかどうかをそこで見る)
    if not gateway.column_names(HISTORY_TABLE):
        ready = ensure_history_table(gateway)
        if not ready.ok:
            raise RuntimeError(ready.message)
    table = _quote(HISTORY_TABLE)
    statements: list[tuple[str, list]] = []
    names = ", ".join(f"[{name}]" for name, _ in HISTORY_COLUMNS)
    marks = ", ".join("?" for _ in HISTORY_COLUMNS)
    for event in events:
        statements.append((
            f"INSERT INTO {table} ({names}) SELECT {marks}"
            f" WHERE NOT EXISTS (SELECT 1 FROM {table} WHERE [ID] = ?)",
            [event[key] for _, key in HISTORY_COLUMNS] + [event["uid"]],
        ))
    results = gateway.execute(statements)
    sent = [event["id"] for event, r in zip(events, results) if r.ok]
    store.mark_events_sent(sent)
    return len(sent)


def _quote(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


# ``Form状態管理`` の開閉状態は、ここ(未反映の待ち行列)からは書きません。
#
# **書き手は :mod:`kanban.presence` ただ 1 つ**です。以前は待ち行列にも
# 同じ列を書く経路があり、2 つの書き手が同じマスを別々の都合で触る形に
# なっていました。しかも待ち行列に溜まった状態は「未反映 N 件」
# (``pending_count`` は ``kanban_item`` しか数えない)に出てこないので、
# 詰まっても**誰にも見えません**。
#
# 開閉は「いま生きているか」を伝えるだけの情報で、届かなければ次の心拍で
# 送り直せば済みます ── 溜めて再送する価値がないので、待ち行列から外しました。


def _chunks(items: Sequence[Any], size: int) -> list[Sequence[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


# ---------------------------------------------------------------------------
# 定期書き戻し
# ---------------------------------------------------------------------------
class Exporter:
    """一定間隔で書き戻しを行うバックグラウンドスレッド。

    UI をブロックしないよう別スレッドで動かす。結果は ``on_result`` で
    受け取れる(画面表示用)。周期実行そのものは業務に依存しないため
    :class:`dbkit.periodic.PeriodicTask` に任せ、ここでは「何を呼ぶか」
    (``export_pending``)と kanban 固有の停止時挙動(``final_export``)
    だけを持つ。
    """

    def __init__(
        self,
        store: Store,
        gateway: SharedDb,
        interval_sec: int = 60,
        on_result: Callable[[ExportResult], None] | None = None,
        history_gateway: SharedDb | None = None,
    ):
        self.store = store
        self.gateway = gateway
        self.history_gateway = history_gateway
        """出来事(看板履歴)の送り先。看板履歴.sqlite3(:mod:`kanban.db.history`)。
        無ければ共有DBの ``看板履歴``(以前の置き場所。試験など)。"""
        self.interval_sec = max(0, int(interval_sec))
        self.on_result = on_result
        self._history_checked: SharedDb | None = None
        self._comments_checked: SharedDb | None = None
        self._last_run = 0.0
        self._retry_stop = threading.Event()
        self._retry_thread: threading.Thread | None = None
        self.retries = 0
        """共有DBが見えたのを見て、周期を待たずに送り直した回数(調べもの用)。"""
        self._task: PeriodicTask[ExportResult] = PeriodicTask(
            run=self._run,
            interval_sec=self.interval_sec,
            name="kanban-exporter",
            on_result=on_result,
            on_error=lambda exc: ExportResult(failed=1, errors=[str(exc)]),
        )

    #: 送れていないものがあるあいだ、共有DBが見えるかを確かめる間隔(秒)。
    #: 見えたら周期(既定 60 秒)を待たずに送る。0 なら確かめない(周期だけで送る)
    RETRY_SEC = 5.0
    #: 「届いていない」を画面に出すために、この端末の SQLite に覚えておく鍵
    META_UNDELIVERED_SINCE = "undelivered_since"
    META_UNDELIVERED_WHY = "undelivered_why"
    META_UNDELIVERED_EVENTS_SINCE = "undelivered_events_since"

    def unsent(self) -> int:
        """**送り直せば減る**もの(看板の状態・コメント・出来事)の数。手元の SQLite だけを見る。

        諦めた行(要確認)は数えない。以前は数えていたので、諦めた行があるあいだ、見張りが
        5 秒ごとに書き戻しを回し続けた(送り直しても減らない)。
        """
        try:
            left = self.store.unsent()
            return left.retryable_items + left.comments + left.events
        except Exception:  # noqa: BLE001 - 数えられないときは送り直しを急がない
            return 0

    def _run(self) -> ExportResult:
        self._last_run = time.monotonic()
        try:
            return self._run_export()
        finally:
            self._note_delivery()

    def _note_delivery(self) -> None:
        """送れていないものが残っていれば「いつから・なぜ」を覚える(帯に出す。`/api/status`)。

        共有フォルダが見えない間に押したものは、この端末に預かったまま。以前は帯の
        「未送信 N」だけで、**倉庫に届いていないことが画面からは分からなかった。**
        数えるのは相手の画面に出るもの(看板の状態・コメント)。出来事(看板履歴)は
        相手の画面には出ない集計の記録で、置き場所も別のファイルなので、帯では別に
        数える(`/api/status` の ``undelivered_events``)。
        """
        try:
            if self.store.pending_count() or self.store.unsent_comment_count():
                if not self.store.get_meta(self.META_UNDELIVERED_SINCE, ""):
                    self.store.set_meta(self.META_UNDELIVERED_SINCE,
                                        datetime.now().strftime("%Y/%m/%d %H:%M:%S"))
                why = ("共有DBが見えません" if not self.gateway.exists()
                       else "共有DBへ書けませんでした(ほかの端末が使っている・ロック)")
                self.store.set_meta(self.META_UNDELIVERED_WHY, why)
            elif self.store.get_meta(self.META_UNDELIVERED_SINCE, ""):
                self.store.set_meta(self.META_UNDELIVERED_SINCE, "")
                self.store.set_meta(self.META_UNDELIVERED_WHY, "")
            # 出来事(看板履歴)は別に覚える。**送ってみて残ったときだけ**(押した直後の、
            # まだ送っていないだけの数秒で帯を出さない)
            events_since = self.store.get_meta(self.META_UNDELIVERED_EVENTS_SINCE, "")
            if self.store.unsent_event_count():
                if not events_since:
                    self.store.set_meta(self.META_UNDELIVERED_EVENTS_SINCE,
                                        datetime.now().strftime("%Y/%m/%d %H:%M:%S"))
            elif events_since:
                self.store.set_meta(self.META_UNDELIVERED_EVENTS_SINCE, "")
        except Exception:  # noqa: BLE001 - 覚えられなくても送るほうは続ける
            pass

    def _retry_loop(self) -> None:
        """送れていないものがあるあいだ、共有DBが見えたら周期を待たずに送る。

        共有フォルダが落ちているあいだに押したものは、押した直後の書き戻しが届かず、
        次の周期(既定 60 秒)まで送り直さなかった(点検: 共有を戻してから倉庫に出るまで 37 秒)。
        手元の数を数えるだけで、共有フォルダへは**残りがあるときだけ**大きさを見に行く。
        """
        while not self._retry_stop.wait(self.RETRY_SEC):
            try:
                left = self.store.unsent()
            except Exception:  # noqa: BLE001 - 数えられないときは周期に任せる
                continue
            if not (left.to_shared_db() or left.to_history()):
                continue
            if time.monotonic() - self._last_run < self.RETRY_SEC:
                continue                      # いま送ったばかり(押した直後など)
            # 送り先が見えたときだけ。**出来事の送り先は看板履歴のファイル**(共有DBではない)
            history = self.history_gateway or self.gateway
            if ((left.to_shared_db() and self.gateway.exists())
                    or (left.to_history() and history.exists())):
                self.retries += 1
                self._task.request_now()

    def _run_export(self) -> ExportResult:
        # コメント・出来事も取り込みと重ねない(コメントは取り込みが同じ表から足す)
        with self.store.sync_lock:
            return self._run_export_locked()

    def _run_export_locked(self) -> ExportResult:
        result = export_pending(self.store, self.gateway)
        # **出来事(集計のための記録)も同じ周期で送る。** 失敗しても書き戻しの
        # 結果には混ぜない ── 看板の状態を送れたかどうかと、記録を送れたか
        # どうかは別の話で、記録は次の周期にまた送ればよい
        try:
            self._send_events()
        except Exception as exc:  # noqa: BLE001
            applog.warning("出来事を看板履歴へ送れませんでした(次の周期でまた送ります): %s", exc)
        try:
            self._send_comments()
        except Exception as exc:  # noqa: BLE001
            applog.warning("コメントを共有DBへ送れませんでした(次の周期でまた送ります): %s", exc)
        return result

    def _send_comments(self) -> None:
        # 看板コメントも看板履歴と同じ扱い(表を確かめるのは接続先ごとに 1 回と、送り残しのあと)
        gateway = self.gateway
        if not self.store.unsent_comment_count():
            return
        if self._comments_checked is not gateway:
            if ensure_comment_table(gateway).ok:
                self._comments_checked = gateway
        export_comments(self.store, gateway)
        if self.store.unsent_comment_count():
            # 送れなかった。表が消された・列が欠けたのかもしれない ── その場で
            # 確かめ直して 1 回だけ送り直す(次の周期まで待たせない)
            self._comments_checked = None
            if ensure_comment_table(gateway).ok:
                self._comments_checked = gateway
                export_comments(self.store, gateway)

    def _send_events(self) -> None:
        # 看板履歴(表と列)を確かめるのは、接続先ごとに 1 回と、送り残しが
        # 出たあとだけ(毎周期 共有DBの列を読みに行かない)
        gateway = self.history_gateway or self.gateway
        if not self.store.unsent_event_count():
            return
        if self._history_checked is not gateway:
            if ensure_history_table(gateway).ok:
                self._history_checked = gateway
        export_events(self.store, gateway)
        if self.store.unsent_event_count():
            # 断られた。表から確かめ直して 1 回だけ送り直す(次の周期まで待たせない)
            self._history_checked = None
            if ensure_history_table(gateway).ok:
                self._history_checked = gateway
                export_events(self.store, gateway)

    @property
    def last_result(self) -> ExportResult | None:
        return self._task.last_result

    def start(self) -> None:
        self._task.start()
        if self.interval_sec > 0 and self.RETRY_SEC > 0 and self._retry_thread is None:
            self._retry_stop.clear()
            self._retry_thread = threading.Thread(
                target=self._retry_loop, name="kanban-export-retry", daemon=True)
            self._retry_thread.start()

    def request_now(self) -> None:
        """次の周期を待たずに書き戻しを行う。"""
        self._task.request_now()

    def stop(self, final_export: bool = True, timeout: float = 30.0) -> None:
        """停止する。``final_export`` が True なら最後に 1 回書き戻す。"""
        self._retry_stop.set()
        if self._retry_thread is not None:
            self._retry_thread.join(timeout=5)
            self._retry_thread = None
        self._task.stop(timeout=timeout)
        if final_export:
            self.run_once()

    def run_once(self) -> ExportResult:
        """1 回だけ書き戻しを実行する(同じスレッドで実行)。"""
        return self._task.run_once()


# ---------------------------------------------------------------------------
# 定期取り込み
# ---------------------------------------------------------------------------
class Importer:
    """一定間隔で Access -> SQLite の再取り込みを行うバックグラウンドスレッド。

    ローカル SQLite は他端末と共有していないため、これが無いと他ラインや
    倉庫が Access へ書き戻した内容は、この端末を再起動するまで画面に
    反映されない(起動時の 1 回きりの取り込みだけでは足りない)。

    ``Exporter`` と対になる構成。UI をブロックしないよう別スレッドで動かし、
    未反映(``dirty``)の行は上書きしない(:func:`import_all` の
    ``keep_local_changes`` 既定 True)ため、書き戻し前の自分の操作が
    再取り込みで消えることはない。周期実行そのものは
    :class:`dbkit.periodic.PeriodicTask` に任せる(``Exporter`` と同じ理由)。

    【変わったらすぐ取り込む】(``watch_sec``)
    周期(既定 30 秒)だけに頼ると、相手の端末が書いたコメント・発送が画面に出るまで
    最大 30 秒かかった(押さなくても出るが、遅い)。そこで ``watch_sec`` ごとに共有DBの
    **大きさと更新時刻だけ**を見て(中身は写さない。共有フォルダへの負担は小さい)、
    変わっていたら周期を待たずに取り込む。Windows の共有フォルダは大きさ・更新時刻を
    数秒覚えて返すので、それでも数秒は遅れることがある。
    """

    #: 共有DBが変わったかを見る間隔(秒)。0 なら見ない(周期だけで取り込む)
    WATCH_SEC = 2.0

    def __init__(
        self,
        store: Store,
        gateway: SharedDb,
        lines: Callable[[], Sequence[str]] | Sequence[str],
        interval_sec: int = 30,
        on_result: Callable[[ImportResult], None] | None = None,
        watch_sec: float | None = None,
    ):
        self.store = store
        self.gateway = gateway
        self._lines = lines
        """取り込み対象のライン。呼び出しごとに変わりうるため関数も渡せる

        (現場モードでライン変更した場合など)。"""
        self.interval_sec = max(0, int(interval_sec))
        self.on_result = on_result
        self._task: PeriodicTask[ImportResult] = PeriodicTask(
            run=self._run_import,
            interval_sec=self.interval_sec,
            name="kanban-importer",
            on_result=on_result,
            on_error=self._error_result,
        )
        self.watch_sec = self.WATCH_SEC if watch_sec is None else max(0.0, float(watch_sec))
        self._watch_stop = threading.Event()
        self._watch_thread: threading.Thread | None = None
        self.wakes = 0
        """共有DBが変わったのを見て、周期を待たずに取り込んだ回数(調べもの用)。"""

    @property
    def last_result(self) -> ImportResult | None:
        return self._task.last_result

    def _resolve_lines(self) -> Sequence[str]:
        if callable(self._lines):
            return self._lines()
        return self._lines

    def _run_import(self) -> ImportResult:
        lines = list(self._resolve_lines())
        return import_all(self.store, self.gateway, lines=lines or None)

    def _error_result(self, exc: BaseException) -> ImportResult:
        return ImportResult(
            started_at=datetime.now().strftime("%Y/%m/%d %H:%M:%S"),
            finished_at=datetime.now().strftime("%Y/%m/%d %H:%M:%S"),
            access_mode=self.gateway.mode,
        )

    def start(self) -> None:
        self._task.start()
        if self.interval_sec > 0 and self.watch_sec > 0 and self._watch_thread is None:
            self._watch_stop.clear()
            self._watch_thread = threading.Thread(
                target=self._watch, name="kanban-import-watch", daemon=True)
            self._watch_thread.start()

    def _stamp(self) -> tuple[int, int] | None:
        """共有DBの (大きさ, 更新時刻)。届かなければ ``None``(周期の取り込みに任せる)。"""
        try:
            st = os.stat(self.gateway.path)
        except OSError:
            return None
        return (st.st_size, st.st_mtime_ns)

    def _watch(self) -> None:
        """共有DBが変わったら、周期を待たずに取り込ませる。"""
        last = self._stamp()
        last_path = self.gateway.path
        while not self._watch_stop.wait(self.watch_sec):
            if self.gateway.path != last_path:      # 接続先を変えた(取り込みは向こうが頼む)
                last_path, last = self.gateway.path, self._stamp()
                continue
            now = self._stamp()
            if now is not None and last is not None and now != last:
                self.wakes += 1
                self.request_now()
            if now is not None:
                last = now

    def request_now(self) -> None:
        """次の周期を待たずに再取り込みを行う(手動更新ボタン・共有DBが変わったとき)。"""
        self._task.request_now()

    def stop(self, timeout: float = 30.0) -> None:
        self._watch_stop.set()
        thread = self._watch_thread
        if thread is not None:
            thread.join(timeout=5)
        self._watch_thread = None
        self._task.stop(timeout=timeout)

    def run_once(self) -> ImportResult:
        """1 回だけ再取り込みを実行する(呼び出したスレッドで実行)。

        手動更新(ダブルクリック等)からは、このメソッドを直接同期呼び出し
        することで「今すぐ Access の最新を見る」を実現する。
        """
        return self._task.run_once()
