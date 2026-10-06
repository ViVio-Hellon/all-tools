"""SQLite への読み書き。複数端末(複数ライン)からの同時アクセスを前提とする。

VBA 版は Access ファイルを共有し、ロック競合を ``ExecuteSQLWithRetry`` の
リトライで凌いでいた。SQLite 版では次の 4 段構えで競合を扱う。

1. ``busy_timeout``
   ロックが解けるまで SQLite 自体が待つ。
2. ``BEGIN IMMEDIATE``
   書き込みトランザクションは最初から書き込みロックを取り、読み取り途中で
   昇格できずに失敗する(``SQLITE_BUSY``)のを防ぐ。
3. リトライ
   それでも ``database is locked`` になった場合は VBA と同じ回数・待ち時間で
   やり直す。
4. 楽観ロック(``rev`` 列)
   「読んで判断してから書く」までの間に他端末が同じ行を変えていた場合は
   更新件数 0 で検出し、画面を再読込させる。VBA の ``recAffected = 0``
   チェックと同じ考え方で、こちらは行バージョンで確実に判定する。

ネットワーク共有(UNC パス)上では WAL が使えないため、共有先を見て
ジャーナルモードを切り替える。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

from dbkit import sqlite_toolkit

from .. import applog, config
from ..domain.models import MUTABLE_FIELDS, KanbanItem

SCHEMA_VERSION = 6

#: コメントの種類(手元・共有DBとも同じ文字)
COMMENT = "コメント"
COMMENT_CLOSE = "片付け"
#: 確認の区切り。黄・緑などで「ここまで確認済み」になった印(**片付けない**。やり取りは
#: 届く(赤と緑を消す)まで開いたまま見える)。本文は何をして確認済みになったか
COMMENT_CONFIRM = "確認"
#: 既読の印。**側(現場 / 倉庫)ごとに共有する**(看板コメントの 1 行として共有DBへ送る)。
#: 本文は、その側が読んだ相手側のコメントの uid を空白区切りで並べたもの。
#: 現場の印はそのラインの端末にだけ、倉庫の印は倉庫の端末に効く(ラインごと・側ごと)
COMMENT_READ = "既読"
_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

#: バージョン 1 -> 2、2 -> 3、3 -> 4 で追加された列。開発中に作られた
#: 既存の SQLite ファイル(``CREATE TABLE IF NOT EXISTS`` では追加され
#: ない)向けの軽量マイグレーション。バージョン 3 で追加した ``reason``
#: 列は ``hold_at`` に置き換わったため、以後は追加しない(既存ファイルに
#: 残っていても未使用のまま無害)。
_MIGRATION_COLUMNS = {
    "kanban_item": {
        "dirty_columns": "TEXT NOT NULL DEFAULT ''",
        "hold": "TEXT NOT NULL DEFAULT ''",
        "hold_at": "TEXT NOT NULL DEFAULT ''",
    },
    "line_status": {
        "remote_status": "TEXT NOT NULL DEFAULT ''",
        "remote_stamp": "TEXT NOT NULL DEFAULT ''",
        "remote_host": "TEXT NOT NULL DEFAULT ''",
        "seen_at": "TEXT NOT NULL DEFAULT ''",
    },
}

#: 論理名 -> Access の既定列名
DEFAULT_COLUMN_MAP = {
    "mgmt_no": config.COL_KEY,
    "material": config.COL_MATERIAL,
    "size": config.COL_SIZE,
    "want": config.COL_WANT,
    "unwant": config.COL_UNWANT,
    "ordered_at": config.COL_ORDERED_AT,
    "shipped": config.COL_SHIPPED,
    "confirmed_at": config.COL_CONFIRMED_AT,
    "permanent": config.COL_PERMANENT,
    "hold": config.COL_HOLD,
    "hold_at": config.COL_HOLD_AT,
}

_ITEM_COLUMNS = (
    "line",
    "mgmt_no",
    "material",
    "size",
    "want",
    "unwant",
    "ordered_at",
    "shipped",
    "confirmed_at",
    "permanent",
    "hold",
    "hold_at",
    "row_order",
    "rev",
    "updated_at",
    "updated_by",
)

#: 書き戻しを諦める失敗回数
MAX_SYNC_ATTEMPTS = 5

#: 共有フォルダへの書き込みを URI の形で開いていたころの失敗(``kanban.db.shared``)。
#: アプリ側の不具合なので、この理由で諦めた行は起動時に送り直す
#: (:meth:`Store.requeue_failures`)
URI_AUTHORITY_FAILURE = "invalid uri authority"

#: 「この端末のモード」を meta テーブルへ記録するときのキー。
#: config.json ではなくここへ保存する理由は Store.get_device_mode 参照。
_META_DEVICE_MODE = "device_mode"


#: 書き戻し対象になりうる状態列(論理名)。dirty_columns にはこの部分集合が入る
STATE_COLUMNS = MUTABLE_FIELDS


def _merge_dirty_columns(existing: str, new_keys: Iterable[str]) -> str:
    """既存の未反映列とこの操作で変わる列を、重複なく合わせる。

    複数の操作が書き戻し前に重なった場合(例: 発注操作の直後、まだ
    Access へ送る前にもう一度操作した場合)でも、最初の操作で変わった列を
    見失わないようにするための素朴な集合演算。
    """
    current = {c for c in (existing or "").split(",") if c}
    current.update(new_keys)
    return ",".join(sorted(current))


class ConflictError(Exception):
    """他端末が同じ行を先に更新していた場合。

    画面を再読込してからやり直す必要がある。VBA の
    「更新対象が見つかりませんでした。他端末で変更された可能性があります。」
    に相当する。
    """


class LockTimeout(Exception):
    """リトライしてもロックが解けなかった場合。"""


#: SQLite のロック系エラーかどうか。プロジェクト固有の判定ではないため
#: :mod:`dbkit.sqlite_toolkit` の実装をそのまま使う。
is_lock_error = sqlite_toolkit.is_lock_error


class Store:
    """SQLite データストア。

    スレッドごとに接続を持つため、UI スレッドと書き戻しスレッドから
    同時に使ってよい。
    """

    def __init__(
        self,
        path: str,
        host_name: str = "",
        busy_timeout_ms: int = 8000,
        max_retry: int = 3,
    ):
        self.path = str(path)
        self.host_name = host_name or config.Config().host_name
        self.busy_timeout_ms = busy_timeout_ms
        self.max_retry = max_retry
        self._local = threading.local()
        self._journal_mode: str | None = None
        self.records_seen = False
        """取り込みで「倉庫に表示」の出来事を書くか(倉庫モードの端末だけ True)。

        出した看板が倉庫の端末に**初めて出た時刻**。出した → 倉庫の画面に出る → 発送 を
        分けて、時間が掛かっているのが「倉庫が気付くまで」(端末が閉じていた)なのか
        「準備」なのかを見るため(:mod:`kanban.presenters.stats`)。"""
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)

    # -- 接続管理 --------------------------------------------------------
    @property
    def connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000.0,
            isolation_level=None,  # トランザクションは明示的に制御する
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
        conn.execute("PRAGMA foreign_keys = ON")
        # 共有フォルダ上での破損を避けるため書き込みは確実に flush する
        conn.execute("PRAGMA synchronous = FULL")
        mode = self._preferred_journal_mode()
        applied = conn.execute(f"PRAGMA journal_mode = {mode}").fetchone()
        actual = (applied[0] if applied else "").lower()
        if mode == "wal" and actual != "wal":
            # UNC 判定をすり抜けた共有でも WAL が張れないことがある
            conn.execute("PRAGMA journal_mode = TRUNCATE")
            actual = "truncate"
        self._journal_mode = actual
        return conn

    def _preferred_journal_mode(self) -> str:
        """ジャーナルモードを決める。

        WAL は共有メモリファイル(``-shm``)を必要とし、SMB 共有では使えない
        (``disk I/O error`` や破損の原因になる)。UNC パスやネットワーク
        ドライブでは TRUNCATE を使う。
        """
        raw = self.path.replace("/", "\\")
        if raw.startswith("\\\\"):
            return "TRUNCATE"
        return "wal"

    @property
    def journal_mode(self) -> str:
        if self._journal_mode is None:
            self.connection  # 接続して確定させる
        return self._journal_mode or ""

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -- トランザクションとリトライ --------------------------------------
    @contextmanager
    def write_transaction(self) -> Iterator[sqlite3.Connection]:
        """書き込みトランザクション(``BEGIN IMMEDIATE``)。"""
        conn = self.connection
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def retry(self, operation: Callable[[], Any], caller: str = "SQL") -> Any:
        """ロック競合時にリトライしながら ``operation`` を実行する。

        待ち時間は VBA と同じく 1 秒・2 秒・3 秒…と伸ばす。
        """
        attempt = 0
        while True:
            try:
                return operation()
            except sqlite3.OperationalError as exc:
                if not is_lock_error(exc) or attempt >= self.max_retry:
                    if is_lock_error(exc):
                        applog.error("%s: ロック競合で最終失敗 (%s)", caller, exc)
                        raise LockTimeout(str(exc)) from exc
                    raise
                attempt += 1
                applog.warning("%s: ロック競合 リトライ %d 回目 (%s)", caller, attempt, exc)
                time.sleep(attempt)

    # -- スキーマ --------------------------------------------------------
    def ensure_schema(self) -> None:
        """テーブルを作成する(既にあれば何もしない)。既存ファイルへの列追加も行う。

        **開くときの手入れもここでやります。** 操作履歴
        (:meth:`prune_operation_log`)は押すたびに 1 行増える一方で、
        これを呼ぶ場所がどこにも無く、**端末を使い続けるかぎり増え続けて
        いました**。捨ててよい古い分をここで落とします ── 開くときに 1 回
        だけ通る場所で、起動の入口(``start_app.py`` / ``main.py``)の
        どちらからも必ず通ります。
        """

        def _apply() -> None:
            # executescript は暗黙にコミットするため、明示トランザクションで
            # 囲まずにそのまま実行する
            self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
            self._migrate_columns()

        self.retry(_apply, "ensure_schema")
        self.set_meta("schema_version", str(SCHEMA_VERSION))
        try:
            removed = self.prune_operation_log()
            if removed:
                applog.info("古い操作履歴を %d 件 片付けました", removed)
            self.prune_sent_events()
        except Exception:  # noqa: BLE001 - 手入れの失敗で起動を止めない
            applog.exception("操作履歴の片付けでエラー")
        try:
            requeued = self.requeue_failures(URI_AUTHORITY_FAILURE)
            if requeued:
                applog.info(
                    "共有フォルダへ書けなかった不具合で止まっていた操作を %d 件 送り直します",
                    requeued,
                )
        except Exception:  # noqa: BLE001 - 手入れの失敗で起動を止めない
            applog.exception("送り直しの準備でエラー")

    def requeue_failures(self, marker: str) -> int:
        """失敗の記録に ``marker`` を含む未反映の行を、もう一度送る列に戻す。

        **こちらの不具合で失敗した行を、諦めたままにしないため。** 書き戻しは
        :data:`MAX_SYNC_ATTEMPTS` 回失敗すると諦め(要確認)、以後は二度と
        送りません。原因がデータではなくアプリの側にあったなら、直したあとに
        送り直さないと、押した操作が共有DBへ永久に届きません。
        """

        def _apply() -> int:
            with self.write_transaction() as conn:
                cursor = conn.execute(
                    "UPDATE kanban_item SET sync_attempts = 0, sync_error = ''"
                    " WHERE dirty = 1 AND sync_error LIKE ?",
                    (f"%{marker}%",),
                )
                return cursor.rowcount

        return int(self.retry(_apply, "requeue_failures"))

    def _migrate_columns(self) -> None:
        """``CREATE TABLE IF NOT EXISTS`` では追加されない列を後から足す。"""
        for table, columns in _MIGRATION_COLUMNS.items():
            existing = {
                row["name"]
                for row in self.connection.execute(f"PRAGMA table_info({table})")
            }
            for column, definition in columns.items():
                if column not in existing:
                    applog.info("ensure_schema: %s に列 %s を追加します", table, column)
                    self.connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                    )

    # -- メタ情報 --------------------------------------------------------
    def get_meta(self, key: str, default: str = "") -> str:
        row = self.connection.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        def _apply() -> None:
            with self.write_transaction() as conn:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, str(value)),
                )

        self.retry(_apply, "set_meta")

    def get_device_mode(self) -> str:
        """この端末が起動する画面(``site``/``warehouse``/``view``)。

        config.json ではなくローカル SQLite に保存する。config.json は
        「導入時に配る雛形」の性質が強く複数端末で使い回されがちな一方、
        画面モードは**端末ごとに個別に選ぶ値**だから。

        未設定なら空文字を返す。呼ぶ側はそのとき現場モードを既定として
        書き込む(``start_app.py``)── 台数がいちばん多いのは現場の端末
        なので、そのまま使い始められるほうが手間が少ない。

        設定画面から変えられるが、**効くのは次に開いたとき**。モードは
        ポートとロックに結びついているので、走ったまま入れ替えられない
        (:mod:`launch_guard` / ``app/routes/settings.py`` の ``set_mode``)。
        """
        return self.get_meta(_META_DEVICE_MODE, "")

    def set_device_mode(self, mode: str) -> None:
        self.set_meta(_META_DEVICE_MODE, mode)

    # -- 取り込み --------------------------------------------------------
    def import_line(
        self,
        line: str,
        table_name: str,
        key_column: str,
        key_category: str,
        column_map: dict[str, str],
        categories: dict[str, str],
        rows: Sequence[dict[str, Any]],
        source_path: str,
        keep_local_changes: bool = True,
    ) -> tuple[int, int, int]:
        """Access から読んだ 1 ラインぶんを取り込む。

        ``keep_local_changes`` が True の場合、まだ Access へ書き戻せていない行
        (``dirty = 1``)の状態列は上書きしない。書き戻し前に再取り込みを行っても
        現場の操作が消えないようにするための保護。

        戻り値は ``(追加件数, 更新件数, 保護した件数)``。
        """
        now = datetime.now().strftime("%Y/%m/%d %H:%M:%S")

        def _apply() -> tuple[int, int, int]:
            inserted = updated = protected = 0
            dropped: list[str] = []
            with self.write_transaction() as conn:
                conn.execute(
                    "INSERT INTO line_source(line, table_name, key_column, key_category,"
                    " column_map, categories, source_path, imported_at)"
                    " VALUES(?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(line) DO UPDATE SET"
                    "   table_name = excluded.table_name,"
                    "   key_column = excluded.key_column,"
                    "   key_category = excluded.key_category,"
                    "   column_map = excluded.column_map,"
                    "   categories = excluded.categories,"
                    "   source_path = excluded.source_path,"
                    "   imported_at = excluded.imported_at",
                    (
                        line,
                        table_name,
                        key_column,
                        key_category,
                        json.dumps(column_map, ensure_ascii=False),
                        json.dumps(categories, ensure_ascii=False),
                        source_path,
                        now,
                    ),
                )

                existing = {
                    row["mgmt_no"]: dict(row)
                    for row in conn.execute(
                        "SELECT mgmt_no, dirty, dirty_columns, material, size, want,"
                        " unwant, ordered_at, shipped, confirmed_at, permanent,"
                        " hold, hold_at, row_order"
                        " FROM kanban_item WHERE line = ?",
                        (line,),
                    )
                }
                seen: set[str] = set()
                # その回(出した日時)が倉庫の端末に初めて出た。**このラインを前に取り込んで
                # いたときだけ**書く ── 初めて取り込んだときは、いつから出ていたか分からない
                newly_seen: list[dict[str, Any]] = []

                for order, row in enumerate(rows):
                    mgmt_no = str(row.get("mgmt_no", "")).strip()
                    if not mgmt_no:
                        continue
                    seen.add(mgmt_no)
                    current = existing.get(mgmt_no)
                    if (self.records_seen and existing
                            and str(row.get("want", "")).strip() == config.MARK_ON
                            and (current is None
                                 or str(current.get("want") or "").strip() != config.MARK_ON
                                 or str(current.get("ordered_at") or "") != str(row.get("ordered_at", "")))):
                        newly_seen.append({"mgmt_no": mgmt_no, **row})
                    if current is None:
                        conn.execute(
                            "INSERT INTO kanban_item(line, mgmt_no, material, size, want,"
                            " unwant, ordered_at, shipped, confirmed_at, permanent,"
                            " hold, hold_at,"
                            " row_order, rev, updated_at, updated_by, dirty)"
                            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,0)",
                            (
                                line,
                                mgmt_no,
                                row.get("material", ""),
                                row.get("size", ""),
                                row.get("want", ""),
                                row.get("unwant", ""),
                                row.get("ordered_at", ""),
                                row.get("shipped", ""),
                                row.get("confirmed_at", ""),
                                row.get("permanent", ""),
                                row.get("hold", ""),
                                row.get("hold_at", ""),
                                order,
                                now,
                                "import",
                            ),
                        )
                        inserted += 1
                    elif keep_local_changes and current["dirty"]:
                        if self._refresh_protected_row(
                            conn, line, mgmt_no, order, row, current, now
                        ):
                            protected += 1
                    else:
                        if self._refresh_clean_row(conn, line, mgmt_no, order, row, current, now):
                            updated += 1

                # **共有DBから消された看板は、手元からも取り除く。未反映の操作が
                # 残っていても取り除く。**
                #
                # 以前は未反映の行だけ残していたが、送り先の行がもう無いので
                # **永久に送れない**。看板画面に消したはずの看板が出続け、「要確認」
                # からも消えなかった。そのうえ同じ番号で看板を足し直すと、残って
                # いた古い発注がその新しい看板に乗って見えた(マスタ管理で消して
                # 足し直したときに起きる)。捨てた操作はログに残す
                from ..domain import events as _events

                for r in newly_seen:
                    conn.execute(
                        "INSERT INTO kanban_event(uid, at, line, mgmt_no, kind, material, size,"
                        " ordered_at, host) VALUES(?,?,?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, now, line, r["mgmt_no"], _events.SEEN,
                         r.get("material", ""), r.get("size", ""), r.get("ordered_at", "") or "",
                         self.host_name),
                    )
                removable = [mgmt_no for mgmt_no in existing if mgmt_no not in seen]
                dropped = [m for m in removable if existing[m]["dirty"]]
                for mgmt_no in removable:
                    conn.execute(
                        "DELETE FROM kanban_item WHERE line = ? AND mgmt_no = ?",
                        (line, mgmt_no),
                    )
            if dropped:
                _log_dropped(line, dropped)
            return inserted, updated, protected

        return self.retry(_apply, "import_line")

    def _refresh_clean_row(
        self,
        conn: sqlite3.Connection,
        line: str,
        mgmt_no: str,
        order: int,
        row: dict[str, Any],
        current: dict[str, Any],
        now: str,
    ) -> bool:
        """未反映の操作が無い行を Access の内容で更新する。

        値が実際に変わった場合だけ ``rev`` を進める。無条件に ``rev`` を
        進めると、定期取り込み(:class:`kanban.db.sync.Importer`)が走る
        たびに画面が保持している ``expected_rev`` が本当は何も変わって
        いないのに古くなり、ボタン操作が無関係な理由で競合エラーになって
        しまう。戻り値は実際に更新したか。
        """
        incoming = {
            "material": row.get("material", ""),
            "size": row.get("size", ""),
            "want": row.get("want", ""),
            "unwant": row.get("unwant", ""),
            "ordered_at": row.get("ordered_at", ""),
            "shipped": row.get("shipped", ""),
            "confirmed_at": row.get("confirmed_at", ""),
            "permanent": row.get("permanent", ""),
            "hold": row.get("hold", ""),
            "hold_at": row.get("hold_at", ""),
        }
        changed = order != current["row_order"] or any(
            current[key] != value for key, value in incoming.items()
        )
        if not changed:
            return False

        conn.execute(
            "UPDATE kanban_item SET material = ?, size = ?, want = ?,"
            " unwant = ?, ordered_at = ?, shipped = ?, confirmed_at = ?,"
            " permanent = ?, hold = ?, hold_at = ?, row_order = ?, rev = rev + 1,"
            " updated_at = ?, updated_by = ?, dirty = 0, dirty_columns = '',"
            " sync_attempts = 0, sync_error = ''"
            " WHERE line = ? AND mgmt_no = ?",
            (
                incoming["material"],
                incoming["size"],
                incoming["want"],
                incoming["unwant"],
                incoming["ordered_at"],
                incoming["shipped"],
                incoming["confirmed_at"],
                incoming["permanent"],
                incoming["hold"],
                incoming["hold_at"],
                order,
                now,
                "import",
                line,
                mgmt_no,
            ),
        )
        return True

    def _refresh_protected_row(
        self,
        conn: sqlite3.Connection,
        line: str,
        mgmt_no: str,
        order: int,
        row: dict[str, Any],
        current: dict[str, Any],
        now: str,
    ) -> bool:
        """未反映の操作がある行を、その列だけ保護しつつ Access の内容で更新する。

        ``dirty_columns`` に載っている列(この端末がまだ Access へ送って
        いない状態列)は一切触らない。それ以外の状態列(例: 他端末が
        書き戻した発送)とマスタ列(資材名・サイズ等)は Access の最新値へ
        更新する。これにより「現場が発注中の行を倉庫が発送する」といった
        列単位でしか重ならない同時編集が、互いを消し合わずに両立する。

        ``dirty_columns`` が空(スキーマ移行前の古い行など)の場合は、
        安全側に倒して状態列を一切更新しない(従来の挙動)。
        """
        dirty_cols = {c for c in (current["dirty_columns"] or "").split(",") if c}

        set_clauses = ["material = ?", "size = ?", "permanent = ?", "row_order = ?"]
        params: list[Any] = [
            row.get("material", ""),
            row.get("size", ""),
            row.get("permanent", ""),
            order,
        ]
        changed = (
            current["material"] != row.get("material", "")
            or current["size"] != row.get("size", "")
            or current["permanent"] != row.get("permanent", "")
            or current["row_order"] != order
        )

        if dirty_cols:
            for col in STATE_COLUMNS:
                if col in dirty_cols:
                    continue  # ローカルの未反映変更を保護(触らない)
                value = row.get(col, "")
                set_clauses.append(f"{col} = ?")
                params.append(value)
                if current[col] != value:
                    changed = True

        if changed:
            set_clauses.extend(["rev = rev + 1", "updated_at = ?", "updated_by = ?"])
            params.extend([now, "import"])

        params.extend([line, mgmt_no])
        conn.execute(
            f"UPDATE kanban_item SET {', '.join(set_clauses)} WHERE line = ? AND mgmt_no = ?",
            params,
        )
        return True

    def line_source(self, line: str) -> dict[str, Any] | None:
        """取り込み元テーブルの情報を返す。"""
        row = self.connection.execute(
            "SELECT * FROM line_source WHERE line = ?", (line,)
        ).fetchone()
        if row is None:
            return None
        info = dict(row)
        info["column_map"] = json.loads(info["column_map"])
        info["categories"] = json.loads(info["categories"])
        return info

    def imported_lines(self) -> list[str]:
        return [
            row["line"]
            for row in self.connection.execute(
                "SELECT line FROM line_source ORDER BY line"
            )
        ]

    # -- 参照 ------------------------------------------------------------
    def items(self, line: str) -> list[KanbanItem]:
        """1 ラインの全項目を Access 上の並び順で返す。"""
        rows = self.connection.execute(
            f"SELECT {', '.join(_ITEM_COLUMNS)} FROM kanban_item"
            " WHERE line = ? ORDER BY row_order, mgmt_no",
            (line,),
        ).fetchall()
        return [KanbanItem(**dict(row)) for row in rows]

    def item(self, line: str, mgmt_no: str) -> KanbanItem | None:
        row = self.connection.execute(
            f"SELECT {', '.join(_ITEM_COLUMNS)} FROM kanban_item"
            " WHERE line = ? AND mgmt_no = ?",
            (line, mgmt_no),
        ).fetchone()
        return KanbanItem(**dict(row)) if row else None

    def change_token(self, line: str | None = None) -> str:
        """画面の自動更新用トークン。

        内容が変わると値が変わる。ポーリングで比較して、変化があったときだけ
        再描画する。
        """
        if line:
            row = self.connection.execute(
                "SELECT COUNT(*) AS c, COALESCE(SUM(rev), 0) AS s,"
                " COALESCE(MAX(updated_at), '') AS m FROM kanban_item WHERE line = ?",
                (line,),
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT COUNT(*) AS c, COALESCE(SUM(rev), 0) AS s,"
                " COALESCE(MAX(updated_at), '') AS m FROM kanban_item"
            ).fetchone()
        # コメントが届いた・読んだ、でも描き直す(未読の印と帯を出し入れする)
        extra = self.connection.execute(
            "SELECT (SELECT COUNT(*) FROM kanban_comment) || '/' ||"
            " (SELECT COALESCE(MAX(id), 0) FROM kanban_comment) || '/' ||"
            " (SELECT COALESCE(SUM(read_id), 0) FROM comment_read)"
        ).fetchone()[0]
        return f"{row['c']}:{row['s']}:{row['m']}:{extra}"

    # -- 更新 ------------------------------------------------------------
    def apply_transition(
        self,
        line: str,
        mgmt_no: str,
        compute: Callable[[KanbanItem], dict[str, str]],
        operation: str,
        expected_rev: int | None = None,
    ) -> KanbanItem:
        """1 行の状態遷移を原子的に適用する。

        ``compute`` は現在の状態を受け取り、変更する列を返す純粋関数
        (:mod:`kanban.domain.models` の ``*_changes``)。読み取りから書き込みまで
        1 つの ``BEGIN IMMEDIATE`` の中で行うため、他端末との競合で
        中途半端な状態にはならない。

        ``expected_rev`` を渡した場合、画面が見ていた版と食い違っていれば
        :class:`ConflictError` を送出する。
        """

        def _apply() -> KanbanItem:
            with self.write_transaction() as conn:
                row = conn.execute(
                    f"SELECT {', '.join(_ITEM_COLUMNS)}, dirty_columns FROM kanban_item"
                    " WHERE line = ? AND mgmt_no = ?",
                    (line, mgmt_no),
                ).fetchone()
                if row is None:
                    raise ConflictError(
                        f"対象の項目が見つかりません(ライン={line}, 管理番号={mgmt_no})。"
                        "画面を更新してからやり直してください。"
                    )
                row_dict = dict(row)
                existing_dirty_columns = row_dict.pop("dirty_columns")
                item = KanbanItem(**row_dict)
                if expected_rev is not None and item.rev != expected_rev:
                    raise ConflictError(
                        "他の端末が同じ項目を更新しました。"
                        "画面を更新してからやり直してください。"
                    )

                changes = compute(item)
                if not changes:
                    return item

                now = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
                # 実際に変更を意図した列だけを dirty_columns へ積む(既存の
                # 未反映列があれば合流させる)。これにより書き戻しはこの列
                # だけを送り、この行の他の列(例: 他端末が触る発送)は
                # 巻き込まない。詳細は schema.sql のコメントを参照
                merged_dirty_columns = _merge_dirty_columns(
                    existing_dirty_columns, changes.keys()
                )
                assignments = ", ".join(f"{col} = ?" for col in changes)
                params: list[Any] = list(changes.values())
                params.extend(
                    [now, self.host_name, merged_dirty_columns, line, mgmt_no, item.rev]
                )
                cursor = conn.execute(
                    f"UPDATE kanban_item SET {assignments}, rev = rev + 1,"
                    " updated_at = ?, updated_by = ?, dirty = 1, dirty_columns = ?,"
                    " sync_attempts = 0, sync_error = ''"
                    " WHERE line = ? AND mgmt_no = ? AND rev = ?",
                    params,
                )
                if cursor.rowcount == 0:
                    raise ConflictError(
                        "更新対象が見つかりませんでした。"
                        "他の端末で変更された可能性があります。"
                    )
                self._log(conn, line, mgmt_no, operation, json.dumps(changes, ensure_ascii=False))
                self._record_events(conn, item, changes, now)
                updated = conn.execute(
                    f"SELECT {', '.join(_ITEM_COLUMNS)} FROM kanban_item"
                    " WHERE line = ? AND mgmt_no = ?",
                    (line, mgmt_no),
                ).fetchone()
                return KanbanItem(**dict(updated))

        return self.retry(_apply, f"apply_transition({operation})")

    def apply_batch(
        self,
        line: str,
        select: Callable[[KanbanItem], bool],
        changes_for: Callable[[KanbanItem], dict[str, str]],
        operation: str,
    ) -> list[KanbanItem]:
        """条件に合う行をまとめて更新する(一括リセット / 一括発送)。

        対象の選定と更新を 1 トランザクションで行うため、選定後に他端末が
        変更した行を取りこぼしたり二重に処理したりしない。戻り値は更新後の
        行の一覧。
        """

        def _apply() -> list[KanbanItem]:
            with self.write_transaction() as conn:
                rows = conn.execute(
                    f"SELECT {', '.join(_ITEM_COLUMNS)}, dirty_columns FROM kanban_item"
                    " WHERE line = ? ORDER BY row_order, mgmt_no",
                    (line,),
                ).fetchall()
                dirty_columns_by_key: dict[str, str] = {}
                targets: list[KanbanItem] = []
                for r in rows:
                    row_dict = dict(r)
                    dirty_columns_by_key[row_dict["mgmt_no"]] = row_dict.pop(
                        "dirty_columns"
                    )
                    targets.append(KanbanItem(**row_dict))
                targets = [item for item in targets if select(item)]
                if not targets:
                    return []

                now = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
                updated: list[KanbanItem] = []
                for item in targets:
                    changes = changes_for(item)
                    if not changes:
                        continue
                    merged_dirty_columns = _merge_dirty_columns(
                        dirty_columns_by_key.get(item.mgmt_no, ""), changes.keys()
                    )
                    assignments = ", ".join(f"{col} = ?" for col in changes)
                    params: list[Any] = list(changes.values())
                    params.extend(
                        [now, self.host_name, merged_dirty_columns, line, item.mgmt_no, item.rev]
                    )
                    cursor = conn.execute(
                        f"UPDATE kanban_item SET {assignments}, rev = rev + 1,"
                        " updated_at = ?, updated_by = ?, dirty = 1, dirty_columns = ?,"
                        " sync_attempts = 0, sync_error = ''"
                        " WHERE line = ? AND mgmt_no = ? AND rev = ?",
                        params,
                    )
                    if cursor.rowcount == 0:
                        # 同一トランザクション内では起きないが念のため
                        continue
                    self._record_events(conn, item, changes, now)
                    updated.append(item.with_changes(changes))
                self._log(
                    conn,
                    line,
                    "",
                    operation,
                    f"対象 {len(updated)} 件",
                )
                return updated

        return self.retry(_apply, f"apply_batch({operation})")

    # -- フォーム状態(Form状態管理) --------------------------------------
    #
    # **ここは読むだけです。** 「開いています」を共有DBへ書くのは
    # :mod:`kanban.presence` の心拍だけで、手元には溜めません。
    #
    # 以前は「手元に ``dirty`` で溜めて、書き戻しのついでに送る」経路も
    # ありましたが、外しました ── 同じマスに書き手が 2 つある形になるうえ、
    # 溜まっても「未反映 N 件」(``pending_count`` は ``kanban_item`` しか
    # 数えない)に出ないので、**詰まっても誰にも見えません**。開閉は
    # 届かなければ次の心拍で送り直せば済むので、溜める価値がありません。
    def line_statuses(self) -> list[dict[str, Any]]:
        """全ラインの開閉状態(表示順)。**共有DBで見えた値だけ**を返す。"""
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT line, sort_order, remote_status, remote_stamp,"
                " remote_host, seen_at"
                " FROM line_status ORDER BY sort_order, line"
            )
        ]

    def max_status_sort_order(self) -> int:
        """``Form状態管理`` の ``管理番号`` の最大値。

        ラインの行がまだ無いときに、**共有DBを読み直さずに**番号を決める
        ために使います(手元は取り込み済みの写しを持っている)。
        """
        row = self.connection.execute(
            "SELECT MAX(sort_order) AS m FROM line_status"
        ).fetchone()
        return int(row["m"] or 0)

    def upsert_line_status_rows(self, rows: Iterable[dict[str, Any]]) -> None:
        """共有DBから取り込んだ ``Form状態管理`` を反映する。

        **ここは観測の記録です。** 共有DBにそう書いてあった、というだけを
        持ちます(書く側は :mod:`kanban.presence` の心拍だけ)。守るべき
        「自分の未反映」がこの表には無いので、そのまま上書きします。

        ``seen_at`` は ``remote_stamp`` が**変わったとき**だけ打ち直します。
        相手の時計とこちらの時計を比べずに「生きているか」を判じるための
        基準です(工場の端末どうしで時刻が揃っている保証はありません)。
        """

        def _apply() -> None:
            now = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
            with self.write_transaction() as conn:
                for row in rows:
                    line = row["line"]
                    status = row.get("status", "")
                    sort_order = row.get("sort_order", 0)
                    stamp = row.get("stamp", "")
                    host = row.get("host", "")
                    existing = conn.execute(
                        "SELECT remote_stamp, seen_at FROM line_status WHERE line = ?",
                        (line,),
                    ).fetchone()
                    # 値が変わっていなければ seen_at は動かさない ── 動かすと
                    # 打たれ続けているように見えて、落ちた端末を見逃す
                    seen_at = now
                    if existing is not None and existing["seen_at"]:
                        if existing["remote_stamp"] == stamp:
                            seen_at = existing["seen_at"]
                    conn.execute(
                        "INSERT INTO line_status(line, sort_order, remote_status,"
                        " remote_stamp, remote_host, seen_at) VALUES(?,?,?,?,?,?)"
                        " ON CONFLICT(line) DO UPDATE SET"
                        "   sort_order = excluded.sort_order,"
                        "   remote_status = excluded.remote_status,"
                        "   remote_stamp = excluded.remote_stamp,"
                        "   remote_host = excluded.remote_host,"
                        "   seen_at = excluded.seen_at",
                        (line, sort_order, status, stamp, host, seen_at),
                    )

        self.retry(_apply, "upsert_line_status_rows")

    # -- 書き戻し(SQLite -> Access) --------------------------------------
    def pending_items(self, limit: int = 500) -> list[dict[str, Any]]:
        """共有DBへ未反映の行を返す。

        ``dirty_columns`` にはこの端末が実際に変更を意図した列(論理名、
        カンマ区切り)が入っており、書き戻しはこの列だけを送る。空の場合
        (スキーマ移行前の古い行など)は、呼び出し側が安全側に倒して
        状態列全部を送る。
        """
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT line, mgmt_no, rev, want, unwant, ordered_at, shipped,"
                " confirmed_at, hold, hold_at, sync_attempts, dirty_columns"
                " FROM kanban_item"
                " WHERE dirty = 1 AND sync_attempts < ?"
                " ORDER BY line, row_order LIMIT ?",
                (MAX_SYNC_ATTEMPTS, limit),
            )
        ]

    def mark_items_synced(self, keys: Sequence[tuple[str, str, int]]) -> int:
        """書き戻しに成功した行の ``dirty`` を下ろす。

        ``rev`` が一致する行だけを対象にするため、書き戻し中に現場が操作した
        行は未反映のまま残り、次回の書き戻しで再送される。
        """
        if not keys:
            return 0

        def _apply() -> int:
            cleared = 0
            with self.write_transaction() as conn:
                for line, mgmt_no, rev in keys:
                    cursor = conn.execute(
                        "UPDATE kanban_item SET dirty = 0, dirty_columns = '',"
                        " sync_attempts = 0, sync_error = ''"
                        " WHERE line = ? AND mgmt_no = ? AND rev = ?",
                        (line, mgmt_no, rev),
                    )
                    cleared += cursor.rowcount
            return cleared

        return self.retry(_apply, "mark_items_synced")

    def mark_items_failed(
        self, keys: Sequence[tuple[str, str]], message: str
    ) -> None:
        """書き戻しに失敗した行に失敗回数とエラーを記録する。"""
        if not keys:
            return

        def _apply() -> None:
            with self.write_transaction() as conn:
                for line, mgmt_no in keys:
                    conn.execute(
                        "UPDATE kanban_item SET sync_attempts = sync_attempts + 1,"
                        " sync_error = ? WHERE line = ? AND mgmt_no = ?",
                        (message[:500], line, mgmt_no),
                    )

        self.retry(_apply, "mark_items_failed")

    def pending_details(self, limit: int = 50) -> list[dict[str, Any]]:
        """未反映の**中身**。数だけでは何も分からない。

        「未反映 8 件」とだけ出ていても、何が届いていないのか確かめる手段が
        どこにもありませんでした。どの看板の・どの操作が・いつから残って
        いるのかを返します。古いものから(困っているのはたいてい古いほう)。
        """
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT line, mgmt_no, material, size, dirty_columns,"
                " sync_attempts, sync_error, updated_at"
                " FROM kanban_item WHERE dirty = 1"
                " ORDER BY updated_at, line, row_order LIMIT ?",
                (limit,),
            )
        ]

    def sync_failures(self) -> list[dict[str, Any]]:
        """書き戻しを諦めた行(要確認)。"""
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT line, mgmt_no, sync_attempts, sync_error FROM kanban_item"
                " WHERE dirty = 1 AND sync_attempts >= ?",
                (MAX_SYNC_ATTEMPTS,),
            )
        ]

    def discard_failed(self) -> list[dict[str, Any]]:
        """**送れなかった操作を捨てる**(1 回以上失敗した未反映の行)。捨てたものを返す。

        共有DBの側で行が消された・接続先が変わった、などで送り先が無い操作は、
        待っても減りません(「要確認」)。未反映の印だけを下ろすので、続けて
        取り込めば、共有DBにある看板は共有DBの今の状態に戻り、共有DBに無い
        看板は手元からも消えます(:meth:`import_line`)。捨てた中身はログに残す。
        """
        rows = [dict(r) for r in self.connection.execute(
            "SELECT line, mgmt_no, material, size, dirty_columns, want, shipped, hold,"
            " sync_attempts, sync_error, updated_at FROM kanban_item"
            " WHERE dirty = 1 AND sync_attempts > 0 ORDER BY line, row_order")]
        if not rows:
            return []

        def _apply() -> None:
            with self.write_transaction() as conn:
                conn.executemany(
                    "UPDATE kanban_item SET dirty = 0, dirty_columns = '', sync_attempts = 0,"
                    " sync_error = '' WHERE line = ? AND mgmt_no = ?",
                    [(r["line"], r["mgmt_no"]) for r in rows],
                )
                for r in rows:
                    self._log(conn, r["line"], r["mgmt_no"], "discard_unsent",
                              json.dumps(r, ensure_ascii=False))

        self.retry(_apply, "discard_failed")
        for r in rows:
            applog.warning("送れない操作を捨てました: %s/%s 列=%s 理由=%s", r["line"], r["mgmt_no"],
                           r["dirty_columns"], r["sync_error"])
        return rows

    def retry_failed(self) -> int:
        """諦めた行(要確認)を、もう一度送る列に戻す。戻した数。"""

        def _apply() -> int:
            with self.write_transaction() as conn:
                return conn.execute(
                    "UPDATE kanban_item SET sync_attempts = 0 WHERE dirty = 1 AND sync_attempts > 0"
                ).rowcount

        return int(self.retry(_apply, "retry_failed"))

    def pending_count(self) -> int:
        """共有DBへ届いていない行の数。**諦めた行も含みます。**

        画面の「未反映」はこちらを出します ── 届いていないことに変わりは
        ないので、数から外すと「0 件なのにデータが合わない」になります。
        「あと何件送れる見込みか」は :meth:`retryable_pending_count`。
        """
        row = self.connection.execute(
            "SELECT COUNT(*) AS c FROM kanban_item WHERE dirty = 1"
        ).fetchone()
        return int(row["c"])

    def retryable_pending_count(self) -> int:
        """**まだ送る見込みのある**未反映の数(諦めた行を除く)。

        「いま止めてよいか」はこちらで判断します
        (``start_app._busy_reason``)。:meth:`pending_count` で判断すると、
        :data:`MAX_SYNC_ATTEMPTS` 回で諦めた行 ── 共有DB側から消された行
        (要確認)など ── が**永久に残り続けるので、二度と終われなく
        なります**:

        * タブを閉じてもアプリが終わらない(:mod:`kanban.idle_exit` が待ち続ける)
        * 「終了」ボタンが毎回 409 で断られる
        * ``stop.bat`` が ``--force`` 無しでは効かない

        送りようのない行を待っても減りません。**待って意味のあるものだけ**を
        数えます。諦めた行は ``sync_failures``(要確認)として画面に出るので、
        見えなくなるわけではありません。
        """
        row = self.connection.execute(
            "SELECT COUNT(*) AS c FROM kanban_item"
            " WHERE dirty = 1 AND sync_attempts < ?",
            (MAX_SYNC_ATTEMPTS,),
        ).fetchone()
        return int(row["c"])

    # -- 端末間ロック ----------------------------------------------------
    def acquire_lock(self, name: str, ttl_sec: int = 120) -> bool:
        """名前付きロックを取得する。

        ``ttl_sec`` を超えて心拍が更新されていないロックは、異常終了した端末が
        残したものとみなして奪う。
        """

        def _apply() -> bool:
            now = datetime.now()
            stale_before = (now - timedelta(seconds=ttl_sec)).strftime("%Y/%m/%d %H:%M:%S")
            now_text = now.strftime("%Y/%m/%d %H:%M:%S")
            with self.write_transaction() as conn:
                row = conn.execute(
                    "SELECT owner, heartbeat_at FROM app_lock WHERE name = ?", (name,)
                ).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO app_lock(name, owner, acquired_at, heartbeat_at)"
                        " VALUES(?,?,?,?)",
                        (name, self.host_name, now_text, now_text),
                    )
                    return True
                if row["owner"] == self.host_name or row["heartbeat_at"] < stale_before:
                    conn.execute(
                        "UPDATE app_lock SET owner = ?, heartbeat_at = ?,"
                        " acquired_at = CASE WHEN owner = ? THEN acquired_at ELSE ? END"
                        " WHERE name = ?",
                        (self.host_name, now_text, self.host_name, now_text, name),
                    )
                    return True
                return False

        return bool(self.retry(_apply, "acquire_lock"))

    def heartbeat_lock(self, name: str) -> None:
        def _apply() -> None:
            now_text = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
            with self.write_transaction() as conn:
                conn.execute(
                    "UPDATE app_lock SET heartbeat_at = ? WHERE name = ? AND owner = ?",
                    (now_text, name, self.host_name),
                )

        self.retry(_apply, "heartbeat_lock")

    def release_lock(self, name: str) -> None:
        def _apply() -> None:
            with self.write_transaction() as conn:
                conn.execute(
                    "DELETE FROM app_lock WHERE name = ? AND owner = ?",
                    (name, self.host_name),
                )

        self.retry(_apply, "release_lock")

    # -- 看板の出来事(集計のための記録) --------------------------------
    def _record_events(
        self, conn: sqlite3.Connection, before: KanbanItem, changes: dict[str, str], now: str
    ) -> None:
        """状態が変わったら、その出来事(出した・発送・届いた…)を積む。

        **ボタンの操作と同じトランザクションで積む。** 別に積むと、操作は
        通ったのに記録だけ抜ける(集計が実際より少なく出る)ことがある。
        """
        from ..domain import events

        kinds = events.classify(before, changes)
        # **ボタンで状態が変わったら、それまでのコメントは確認済み**(区切りの印を積む)。
        # 押す前に画面が中身を見せている(views/board.js の確認の小窓)。**前の回へ移すのは
        # 届いた・取り消したときだけ**で、それまではやり取りを開いたまま見せる。共有DBへも送る
        self._close_comments_on(conn, before, kinds, now)
        for kind in kinds:
            ordered_at = (
                changes.get("ordered_at") or now if kind == events.ORDERED
                else before.ordered_at
            )
            conn.execute(
                "INSERT INTO kanban_event(uid, at, line, mgmt_no, kind, material, size,"
                " ordered_at, host) VALUES(?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, now, before.line, before.mgmt_no, kind,
                 before.material, before.size, ordered_at or "", self.host_name),
            )

    def _close_comments_on(
        self, conn: sqlite3.Connection, before: KanbanItem, kinds: Sequence[str], now: str
    ) -> None:
        """ボタン操作(出来事)で、その看板のいまのコメントを確認済みにする。

        * 届いた(赤と緑を消した)・取り消した … **片付ける**(前の回へ移す)
        * それ以外(黄・緑・外した…) … **確認の区切り**だけ積む(やり取りは開いたまま)

        印には**何をして確認済みになったか**を残す。押した側が相手のコメントを読んだことにも
        する(押す前に中身を見せているので)。区切りの後ろに新しいコメントが無ければ何も積まない
        (押すたびに行を増やさない)。
        """
        from ..domain import comments as rules

        reason = rules.close_reason(kinds)
        if not reason:
            return
        closing = rules.closes_round(kinds)
        last_mark = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM kanban_comment WHERE line = ? AND mgmt_no = ? AND kind IN (?, ?)",
            (before.line, before.mgmt_no, COMMENT_CLOSE, COMMENT_CONFIRM)).fetchone()[0]
        has_open = conn.execute(
            "SELECT 1 FROM kanban_comment c WHERE c.line = ? AND c.mgmt_no = ?"
            f" AND {self._OPEN_COMMENT} LIMIT 1", (before.line, before.mgmt_no)).fetchone()
        # 前の区切りのあとに書かれた(この端末に届いた)コメントがあるか。同じ秒でも取り違えないよう番号で見る
        has_new = conn.execute(
            "SELECT 1 FROM kanban_comment WHERE line = ? AND mgmt_no = ? AND kind = ? AND id > ? LIMIT 1",
            (before.line, before.mgmt_no, COMMENT, last_mark)).fetchone()
        if not has_open or (not closing and not has_new):
            return
        actor = rules.actor_of(kinds)
        if actor:
            self._share_read(conn, before.line, before.mgmt_no, actor, now)
        conn.execute(
            "INSERT INTO kanban_comment(uid, at, line, mgmt_no, kind, side, host, body)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, now, before.line, before.mgmt_no,
             COMMENT_CLOSE if closing else COMMENT_CONFIRM, actor, self.host_name, reason),
        )

    def unsent_events(self, limit: int = 500) -> list[dict[str, Any]]:
        """まだ共有DBへ送っていない出来事(古い順)。"""
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT id, uid, at, line, mgmt_no, kind, material, size, ordered_at, host"
                " FROM kanban_event WHERE sent = 0 ORDER BY id LIMIT ?",
                (limit,),
            )
        ]

    def mark_events_sent(self, ids: Sequence[int]) -> None:
        if not ids:
            return

        def _apply() -> None:
            with self.write_transaction() as conn:
                conn.executemany(
                    "UPDATE kanban_event SET sent = 1 WHERE id = ?", [(i,) for i in ids]
                )

        self.retry(_apply, "mark_events_sent")

    def unsent_event_count(self) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) FROM kanban_event WHERE sent = 0"
        ).fetchone()
        return int(row[0])

    def prune_sent_events(self, keep_days: int = 90) -> int:
        """送り終えた出来事を片付ける(共有DBに残っているので手元には要らない)。"""
        cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y/%m/%d %H:%M:%S")

        def _apply() -> int:
            with self.write_transaction() as conn:
                cursor = conn.execute(
                    "DELETE FROM kanban_event WHERE sent = 1 AND at < ?", (cutoff,)
                )
                return cursor.rowcount

        return int(self.retry(_apply, "prune_sent_events"))

    # -- コメント(倉庫 ⇔ 現場のやり取り)---------------------------------
    #
    # 1 回のやり取りは「前の片付け(届いた・取り消した)のあと」に書かれたもの。
    # 日時は "YYYY/MM/DD HH:MM:SS" の文字列なので、そのまま大小を比べられる。
    _OPEN_COMMENT = (
        "c.kind = '" + "コメント" + "' AND c.at > COALESCE((SELECT MAX(x.at) FROM kanban_comment x"
        " WHERE x.line = c.line AND x.mgmt_no = c.mgmt_no AND x.kind = '" + "片付け" + "'), '')"
    )

    def add_comment(self, line: str, mgmt_no: str, side: str, body: str) -> dict[str, Any]:
        """コメントを 1 件書く(手元に積み、書き戻しと一緒に共有DBへ送る)。"""
        now = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
        uid = uuid.uuid4().hex

        def _apply() -> None:
            with self.write_transaction() as conn:
                cursor = conn.execute(
                    "INSERT INTO kanban_comment(uid, at, line, mgmt_no, kind, side, host, body)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (uid, now, line, mgmt_no, COMMENT, side, self.host_name, body),
                )
                # 書いたのは、それまでのやり取りを見たうえで(開いた画面から書く)
                self._mark_read(conn, line, mgmt_no, cursor.lastrowid)
                self._share_read(conn, line, mgmt_no, side, now)

        self.retry(_apply, "add_comment")
        return {"uid": uid, "at": now, "side": side, "body": body, "host": self.host_name}

    def close_comments(self, line: str, mgmt_no: str) -> None:
        """その看板のやり取りを片付ける(マスタで看板を消したとき)。

        片付けないと、**同じ番号で看板を足し直したとき、消した看板のコメントが
        新しい看板に出る**(未読の帯にも出る)。片付けの印は共有DBへも送る。
        """
        now = datetime.now().strftime("%Y/%m/%d %H:%M:%S")

        def _apply() -> None:
            with self.write_transaction() as conn:
                conn.execute(
                    "INSERT INTO kanban_comment(uid, at, line, mgmt_no, kind, host, body)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, now, line, mgmt_no, COMMENT_CLOSE, self.host_name,
                     "マスタで看板を消した"),
                )

        self.retry(_apply, "close_comments")

    def comment_thread(self, line: str, mgmt_no: str, history: int = 30) -> dict[str, list[dict[str, Any]]]:
        """いまのやり取り(open。確認の区切りを含む)と、片付いた前の分(closed。コメント ``history`` 件まで)。

        各コメントに ``read`` を添える ── **相手側が読んだか**(いちばん早く読んだ印の
        日時と端末)。読まれていなければ空。
        """
        rows = [dict(r) for r in self.connection.execute(
            "SELECT uid, at, kind, side, host, body FROM kanban_comment"
            " WHERE line = ? AND mgmt_no = ? ORDER BY at, id",
            (line, mgmt_no),
        )]
        last_close = max((r["at"] for r in rows if r["kind"] == COMMENT_CLOSE), default="")
        marks = [r for r in rows if r["kind"] == COMMENT_READ]
        comments = [r for r in rows if r["kind"] == COMMENT]
        for c in comments:
            seen = [m for m in marks if m["side"] != c["side"] and c["uid"] in m["body"].split()]
            first = min(seen, key=lambda m: m["at"]) if seen else None
            c["read"] = {"at": first["at"], "side": first["side"], "host": first["host"]} if first else None
            c.pop("uid", None)
        # 「何をして確認済みになったか」(確認の区切り・片付けの印)をコメントの間に並べる。
        # いまの回は区切りごと開いたまま見せ(届くまで畳まない)、前の回は片付けの印まで。
        # 並びは書かれた順(日時 → 番号。同じ秒でも順番どおり)のまま
        for r in rows:
            if r["kind"] in (COMMENT_CLOSE, COMMENT_CONFIRM):
                r["body"] = r["body"] or "片付けた"
                r.pop("uid", None)
        shown = [r for r in rows if r["kind"] in (COMMENT, COMMENT_CLOSE, COMMENT_CONFIRM)]
        now_open = [r for r in shown if r["at"] > last_close]
        while now_open and now_open[0]["kind"] != COMMENT:   # いまの回はコメントから始める
            now_open.pop(0)
        older = [r for r in shown if r["at"] <= last_close]
        firsts = [i for i, r in enumerate(older) if r["kind"] == COMMENT]   # 前の回は新しいコメント history 件まで
        keep = firsts[-history:] if history > 0 else []
        older = older[keep[0]:] if keep else []
        return {"open": now_open, "closed": older}

    def comment_counts(self, line: str, my_side: str) -> dict[str, tuple[int, int]]:
        """看板ごとの ``(いまのコメント数, 未読数)``。

        未読 = 相手側が書いて、**この端末でも、同じ側のほかの端末でも**まだ開いていないもの
        (同じ側の既読の印は共有する。現場はそのライン、倉庫は倉庫の端末どうし)。
        """
        out: dict[str, tuple[int, int]] = {}
        for r in self.connection.execute(
            "SELECT c.mgmt_no, COUNT(*) AS n,"
            " SUM(CASE WHEN c.side <> ? AND c.id > COALESCE(r.read_id, 0)"
            f" AND NOT ({self._READ_BY_SIDE}) THEN 1 ELSE 0 END) AS u"
            " FROM kanban_comment c LEFT JOIN comment_read r"
            " ON r.line = c.line AND r.mgmt_no = c.mgmt_no"
            f" WHERE c.line = ? AND {self._OPEN_COMMENT} GROUP BY c.mgmt_no",
            (my_side, my_side, line),
        ):
            out[str(r["mgmt_no"])] = (int(r["n"]), int(r["u"] or 0))
        return out

    #: その側(``?``)の既読の印に、コメント ``c`` の uid が入っているか
    _READ_BY_SIDE = (
        "EXISTS (SELECT 1 FROM kanban_comment m WHERE m.kind = '" + "既読" + "'"
        " AND m.side = ? AND m.line = c.line AND m.mgmt_no = c.mgmt_no"
        " AND instr(' ' || m.body || ' ', ' ' || c.uid || ' ') > 0)"
    )

    def mark_comments_read(self, line: str, mgmt_no: str, share_side: str = "") -> bool:
        """開いた。そのときこの端末に届いていたコメントまでを読んだことにする。

        ``share_side``(現場 / 倉庫)を渡すと、**同じ側のほかの端末にも既読を知らせる**
        印を積む(共有DBへ送る)。倉庫参照は渡さない ── 見るだけの端末が開いても、
        倉庫が読んだことにはしない。印を積んだら True(すぐ送ってもらうため)。
        """
        row = self.connection.execute(
            "SELECT MAX(id) FROM kanban_comment WHERE line = ? AND mgmt_no = ? AND kind = ?",
            (line, mgmt_no, COMMENT),
        ).fetchone()
        last = int(row[0]) if row and row[0] else 0
        if not last:
            return False
        now = datetime.now().strftime("%Y/%m/%d %H:%M:%S")

        def _apply() -> bool:
            with self.write_transaction() as conn:
                self._mark_read(conn, line, mgmt_no, last)
                return self._share_read(conn, line, mgmt_no, share_side, now) if share_side else False

        return bool(self.retry(_apply, "mark_comments_read"))

    def _share_read(self, conn: sqlite3.Connection, line: str, mgmt_no: str, side: str, now: str) -> bool:
        """``side`` がまだ読んでいない相手側のいまのコメントがあれば、既読の印を 1 行積む。

        **読んでいないものが無ければ積まない**(開くたびに行を増やさない)。印には相手側の
        いまのコメントの uid をすべて並べる(どれを読んだかを端末の順番に頼らず言えるように)。
        """
        if not side:
            return False
        open_uids = [r[0] for r in conn.execute(
            "SELECT c.uid FROM kanban_comment c"
            f" WHERE c.line = ? AND c.mgmt_no = ? AND c.side <> ? AND {self._OPEN_COMMENT}"
            " ORDER BY c.at, c.id", (line, mgmt_no, side))]
        if not open_uids:
            return False
        unread = conn.execute(
            "SELECT COUNT(*) FROM kanban_comment c"
            f" WHERE c.line = ? AND c.mgmt_no = ? AND c.side <> ? AND {self._OPEN_COMMENT}"
            f" AND NOT ({self._READ_BY_SIDE})", (line, mgmt_no, side, side)).fetchone()[0]
        if not unread:
            return False
        conn.execute(
            "INSERT INTO kanban_comment(uid, at, line, mgmt_no, kind, side, host, body)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, now, line, mgmt_no, COMMENT_READ, side, self.host_name,
             " ".join(open_uids)),
        )
        return True

    @staticmethod
    def _mark_read(conn: sqlite3.Connection, line: str, mgmt_no: str, read_id: int) -> None:
        conn.execute(
            "INSERT INTO comment_read(line, mgmt_no, read_id) VALUES(?,?,?)"
            " ON CONFLICT(line, mgmt_no) DO UPDATE SET read_id = MAX(read_id, excluded.read_id)",
            (line, mgmt_no, read_id),
        )

    def unsent_comments(self, limit: int = 500) -> list[dict[str, Any]]:
        return [dict(r) for r in self.connection.execute(
            "SELECT id, uid, at, line, mgmt_no, kind, side, host, body FROM kanban_comment"
            " WHERE sent = 0 ORDER BY id LIMIT ?", (limit,),
        )]

    def mark_comments_sent(self, ids: Sequence[int]) -> None:
        if not ids:
            return

        def _apply() -> None:
            with self.write_transaction() as conn:
                conn.executemany("UPDATE kanban_comment SET sent = 1 WHERE id = ?", [(i,) for i in ids])

        self.retry(_apply, "mark_comments_sent")

    def unsent_comment_count(self) -> int:
        return int(self.connection.execute(
            "SELECT COUNT(*) FROM kanban_comment WHERE sent = 0").fetchone()[0])

    def merge_comments(self, rows: Sequence[dict[str, Any]]) -> int:
        """共有DBから取り込んだコメント・片付けの印を足す(同じ uid は入れない)。足した件数。"""
        if not rows:
            return 0

        def _apply() -> int:
            with self.write_transaction() as conn:
                before = conn.total_changes
                conn.executemany(
                    "INSERT OR IGNORE INTO kanban_comment(uid, at, line, mgmt_no, kind, side, host, body, sent)"
                    " VALUES(:uid, :at, :line, :mgmt_no, :kind, :side, :host, :body, 1)",
                    rows,
                )
                return conn.total_changes - before

        return int(self.retry(_apply, "merge_comments"))

    # -- 操作履歴 --------------------------------------------------------
    def _log(
        self,
        conn: sqlite3.Connection,
        line: str,
        mgmt_no: str,
        operation: str,
        detail: str = "",
    ) -> None:
        conn.execute(
            "INSERT INTO operation_log(at, line, mgmt_no, operation, detail, host)"
            " VALUES(?,?,?,?,?,?)",
            (
                datetime.now().strftime("%Y/%m/%d %H:%M:%S"),
                line,
                mgmt_no,
                operation,
                detail,
                self.host_name,
            ),
        )

    def recent_operations(self, limit: int = 50) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT at, line, mgmt_no, operation, detail, host FROM operation_log"
                " ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        ]

    def prune_operation_log(self, keep_days: int = 30) -> int:
        """古い操作履歴を削除する。"""
        cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y/%m/%d %H:%M:%S")

        def _apply() -> int:
            with self.write_transaction() as conn:
                cursor = conn.execute("DELETE FROM operation_log WHERE at < ?", (cutoff,))
                return cursor.rowcount

        return int(self.retry(_apply, "prune_operation_log"))


def _log_dropped(line: str, mgmt_nos: list[str]) -> None:
    """共有DBから消された看板に残っていた未反映の操作を捨てたことを残す。"""
    applog.warning(
        "共有DBから消された看板の未反映の操作を捨てました(送り先がありません): %s / 管理番号 %s",
        line, "、".join(mgmt_nos),
    )
