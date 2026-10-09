"""SQLite -> Access 書き戻し汎用エンジン

特定のテーブル名や業務に一切依存しない。「送信したい行が増えるSQLite
テーブル」と「送り先のAccessテーブル」の対応を `WriteBackSpec` で渡す
だけで、以下をまとめて面倒みる。

    - 二重取得の防止(claim/予約): 背景スレッドが同時に走っても、
      同じ行を2度は拾わない
    - 中断からの再開: 送信の途中で落ちて「送信中」のまま残った予約は、
      一定時間後に拾い直す。そのとき送信IDは新規採番せず引き継ぐ
    - 二重登録の防止: 送信IDをAccess側の一意インデックスに賭ける。
      再送で同じ行がもう一度INSERTされても、Access側が弾いてくれる
      (弾かれたら「失敗」ではなく「既に届いていた」として扱う)
    - Access側に送信ID列を用意できない環境でも、書き戻し自体は
      止めない(その場合は旧来どおり「予約による二重防止」だけで動く)
    - 1行の失敗で全体を止めない

他のVBA移行ツールでも、`WriteBackSpec` のリストを用意して
`write_back(conn, access, specs)` を呼ぶだけで使い回せる。
このファイル自体は業務固有のテーブル名を一切知らない。

【Access側の送信ID列について】
`write_back` は呼ぶたびに `ensure_op_id_column` で列とインデックスの
有無を確かめるが、結果(使えた/使えなかった)は端末内でキャッシュする。
ALTER TABLE / CREATE INDEX は失敗しうる操作なので、毎回リトライして
無駄な失敗ログを積み上げたり、ロック競合を増やしたりしないためである。
ロック競合など「今回はたまたま判定できなかった」場合だけキャッシュ
せず、次回また試す。
"""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Iterable, Optional

from . import access_odbc
from .logging_utils import get_logger

log = get_logger("outbox_sync")

# 同期記録テーブル(このエンジンが自分のSQLite内に持つ管理テーブル)。
# 業務テーブルの名前は一切ここに出てこない
SYNC_LOG_TABLE = "Access同期記録"

# 同期記録の状態。送信前に「送信中」で予約し、送れたら「済」にする
SYNC_SENDING = "送信中"
SYNC_DONE = "済"

# 「送信中」のまま放置された予約を、何分たったら拾い直すか。
# アプリが落ちる・端末が落ちる等で予約だけ残ることがある
STALE_CLAIM_MINUTES = 10

# Access側に足す送信ID列の既定名。テーブルごとに変えたい理由がなければ
# このままでよい
DEFAULT_OP_ID_COLUMN = "送信ID"


@dataclass(frozen=True)
class WriteBackSpec:
    """書き戻し対象1テーブル分の定義。呼び出し側(アプリ層)が組み立てる。

    sqlite_table   : ローカルSQLite側のテーブル名(行が増えていく方)
    access_table   : 送り先のAccessテーブル名
    key_column     : 両テーブルで共通の主キー列名(整数、オートナンバー等)
    op_id_column   : Access側に足す送信ID列の名前
    use_op_id_guard: Access側テーブルに送信ID列/一意インデックスを
                     作成してよいか。他チーム管理のテーブル等、
                     スキーマを触りたくない送り先では False にする
                     (その場合は予約による二重防止だけで動く)
    """
    sqlite_table: str
    access_table: str
    key_column: str
    op_id_column: str = DEFAULT_OP_ID_COLUMN
    use_op_id_guard: bool = True


@dataclass
class WriteBackResult:
    sent: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    skipped_reason: str = ""

    @property
    def total(self) -> int:
        return sum(self.sent.values())

    @property
    def ok(self) -> bool:
        return not self.errors and not self.skipped_reason

    def summary(self) -> str:
        if self.skipped_reason:
            return self.skipped_reason
        if not self.total and not self.errors:
            return "Accessに送る新しいデータはありませんでした。"
        lines = [f"Accessへ{self.total}件を反映しました。"]
        for table, count in self.sent.items():
            lines.append(f"  {table}: {count}件")
        if self.errors:
            lines.append("")
            lines.append("送れなかったもの:")
            lines.extend(f"  {e}" for e in self.errors)
        return "\n".join(lines)


# ------------------------------------------------------------------
# 同期記録テーブル
# ------------------------------------------------------------------
def ensure_sync_table(conn: sqlite3.Connection) -> None:
    """同期済みを覚えておく表。無ければ作る。全テーブル共通で1つだけ持つ。

    **呼んだ側のトランザクションを勝手に閉じない。** 取り込みは
    ``BEGIN IMMEDIATE`` を取ってから「送信待ちが0件か」をここ経由で
    数え直す(``importer.import_source``)。ここが ``commit()`` すると
    その錠がほどけ、数え直した直後に入った登録を総入れ替えで消してしまう
    (「打った行が消えることがありました」── とんでもない話である)。
    """
    owned = not conn.in_transaction
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS [{SYNC_LOG_TABLE}] ("
        " テーブル名 TEXT NOT NULL,"
        " 行ID       INTEGER NOT NULL,"
        " 送信ID     TEXT,"
        " 同期日時   TEXT NOT NULL DEFAULT (datetime('now','localtime')),"
        f" 状態       TEXT NOT NULL DEFAULT '{SYNC_DONE}',"
        " PRIMARY KEY (テーブル名, 行ID))")
    # 既存DBに列が無ければ足す(古い記録は「送信ID未採番・送信済み」扱い)
    columns = {row[1] for row in conn.execute(
        f"PRAGMA table_info([{SYNC_LOG_TABLE}])")}
    if "状態" not in columns:
        conn.execute(f"ALTER TABLE [{SYNC_LOG_TABLE}] ADD COLUMN"
                     f" 状態 TEXT NOT NULL DEFAULT '{SYNC_DONE}'")
    if "送信ID" not in columns:
        conn.execute(f"ALTER TABLE [{SYNC_LOG_TABLE}] ADD COLUMN 送信ID TEXT")
    if owned:
        conn.commit()


def pending_rows(conn: sqlite3.Connection, spec: WriteBackSpec) -> list[sqlite3.Row]:
    """まだAccessに送れていない行を拾う(予約はしない)。件数確認用。"""
    ensure_sync_table(conn)
    conn.row_factory = sqlite3.Row
    return conn.execute(
        f"SELECT * FROM [{spec.sqlite_table}] WHERE [{spec.key_column}] NOT IN"
        f" (SELECT 行ID FROM [{SYNC_LOG_TABLE}]"
        f"  WHERE テーブル名 = ? AND 状態 = ?)"
        f" ORDER BY [{spec.key_column}]",
        (spec.sqlite_table, SYNC_DONE)).fetchall()


def unsent_tables(conn: sqlite3.Connection,
                   specs: Iterable[WriteBackSpec]) -> dict[str, int]:
    """複数specの中で、まだAccessへ送っていない行が残っているものを返す。

    総入れ替え取り込みの前に「未送信を失わないか」を確認する用途。
    """
    remaining: dict[str, int] = {}
    for spec in specs:
        try:
            rows = pending_rows(conn, spec)
        except sqlite3.Error:
            continue
        if rows:
            remaining[spec.sqlite_table] = len(rows)
    return remaining


def claim_rows(conn: sqlite3.Connection,
               spec: WriteBackSpec) -> tuple[list[sqlite3.Row], dict[int, str]]:
    """送る行を予約してから返す。行IDごとの送信IDも一緒に返す。

    【なぜ予約が要るか】
    書き戻しは複数の操作の直後に裏で走りうる。予約せずに「未送信の行」を
    毎回SELECTすると、同時に走った処理同士が同じ行を拾ってAccessへ二重に
    INSERTしてしまう。ここで先に予約しておけば、スレッドでも別プロセス
    でも同じ行を2度は拾わない(予約はひとつのトランザクションで行う)。

    送信IDは予約時に採番し、期限切れの予約を拾い直すときは同じIDを
    使い回す(=Accessに届いていたかもしれない行だけ、IDを引き継ぐ)。
    確実に失敗したとわかった行は `release_claim` で予約ごと外すので、
    そちらは次回このIDを見ることはなく、新しいIDで送られる。
    """
    ensure_sync_table(conn)
    conn.row_factory = sqlite3.Row
    op_ids: dict[int, str] = {}
    conn.execute("BEGIN IMMEDIATE")
    try:
        # 1) 期限切れの予約を拾い直す。送信IDは引き継ぐ(採番し直さない)
        stale = conn.execute(
            f"SELECT 行ID, 送信ID FROM [{SYNC_LOG_TABLE}]"
            f" WHERE テーブル名 = ? AND 状態 = ?"
            f" AND 同期日時 < datetime('now','localtime',?)",
            (spec.sqlite_table, SYNC_SENDING,
             f"-{STALE_CLAIM_MINUTES} minutes")).fetchall()
        for record in stale:
            row_id = int(record["行ID"])
            op_id = record["送信ID"] or uuid.uuid4().hex
            op_ids[row_id] = op_id
            conn.execute(
                f"UPDATE [{SYNC_LOG_TABLE}]"
                " SET 送信ID = ?, 同期日時 = datetime('now','localtime')"
                " WHERE テーブル名 = ? AND 行ID = ?",
                (op_id, spec.sqlite_table, row_id))
        if stale:
            log.warning("%s: 送信中のまま残っていた予約を%s件拾い直します"
                        "(同じ送信IDで再送するのでAccess側が重複を弾きます)",
                        spec.sqlite_table, len(stale))

        # 2) まだ記録の無い行を新規に予約する(送信IDを新規採番)
        fresh = conn.execute(
            f"SELECT [{spec.key_column}] AS 行ID FROM [{spec.sqlite_table}]"
            f" WHERE [{spec.key_column}] NOT IN"
            f" (SELECT 行ID FROM [{SYNC_LOG_TABLE}] WHERE テーブル名 = ?)",
            (spec.sqlite_table,)).fetchall()
        for record in fresh:
            row_id = int(record["行ID"])
            op_id = uuid.uuid4().hex
            op_ids[row_id] = op_id
            conn.execute(
                f"INSERT INTO [{SYNC_LOG_TABLE}] (テーブル名, 行ID, 送信ID, 状態)"
                " VALUES (?, ?, ?, ?)",
                (spec.sqlite_table, row_id, op_id, SYNC_SENDING))

        # 3) 予約できた行の実データをまとめて取り直す
        rows: list[sqlite3.Row] = []
        if op_ids:
            ids = list(op_ids)
            marks = ", ".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT * FROM [{spec.sqlite_table}]"
                f" WHERE [{spec.key_column}] IN ({marks})"
                f" ORDER BY [{spec.key_column}]", ids).fetchall()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return rows, op_ids


def mark_synced(conn: sqlite3.Connection, spec: WriteBackSpec,
                row_ids: Iterable[int]) -> None:
    """送れた行を「済」にする。予約が無くても記録する(手動送信など)。

    `INSERT OR REPLACE` ではなく `ON CONFLICT ... DO UPDATE`(UPSERT)に
    しているのは、予約時に採番した送信IDを消さないため
    (REPLACEは行を作り直すので既存の送信ID列がNULLに戻ってしまう)。
    要SQLite 3.24以降。
    """
    ensure_sync_table(conn)
    with conn:
        conn.executemany(
            f"INSERT INTO [{SYNC_LOG_TABLE}] (テーブル名, 行ID, 状態)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(テーブル名, 行ID) DO UPDATE SET"
            " 状態 = excluded.状態, 同期日時 = datetime('now','localtime')",
            [(spec.sqlite_table, int(i), SYNC_DONE) for i in row_ids])


def release_claim(conn: sqlite3.Connection, spec: WriteBackSpec,
                  row_ids: Iterable[int]) -> None:
    """送れなかったことが確実な行の予約を外す(次回すぐ新しい送信IDで拾える)。

    ここに渡すのは、Accessへの送信で `AccessError` を捕まえて「届いて
    いない」と判断できた行**だけ**にすること(型不一致・列不足など、
    待っても直らない失敗)。記録ごと削除するので、次回は真新しい予約
    として拾われ、送信IDも採番し直される――届いていないと分かっている
    のでそれで安全。

    逆に「Accessに届いたかどうか分からない行」(想定外の例外で処理を
    抜けた場合など)はここに渡さず、「送信中」のまま残すこと。
    `claim_rows` の期限切れ拾い直しが同じ送信IDで再送し、Access側の
    一意インデックスに二重登録を弾かせるのが、その場合の唯一の安全策
    になる。
    """
    ids = [int(i) for i in row_ids]
    if not ids:
        return
    ensure_sync_table(conn)
    with conn:
        conn.executemany(
            f"DELETE FROM [{SYNC_LOG_TABLE}]"
            " WHERE テーブル名 = ? AND 行ID = ? AND 状態 = ?",
            [(spec.sqlite_table, i, SYNC_SENDING) for i in ids])


# ------------------------------------------------------------------
# Access側テーブルの準備(送信ID列 / 一意インデックス)
# ------------------------------------------------------------------
# ensure_op_id_column の結果(使えた/使えなかった)を端末内で使い回す
# キャッシュ。key: (Accessファイルのパス, access_table, op_id_column)
_op_id_column_cache: dict[tuple[str, str, str], bool] = {}


def _try_ddl(access: "access_odbc.AccessConnection", sql: str,
            label: str) -> Optional[bool]:
    """DDLを1つ実行する。戻り値は3通り。

    True  : 成功した、または「もう存在する」ので実質OK
    False : 権限不足など、待っても直らない失敗(以後は諦めて素通りする)
    None  : ロック競合など一時的な失敗(今回は判定できない、次回また試す)
    """
    try:
        access.execute(sql)
        log.info("%s: 完了しました", label)
        return True
    except access_odbc.AccessError as exc:
        message = str(exc)
        if access_odbc.is_lock_error(message):
            log.debug("%s: ロック競合のため今回は見送ります(次回再試行)", label)
            return None
        if access_odbc.is_already_exists_error(message):
            log.debug("%s: 既にあります", label)
            return True
        log.warning(
            "%s: できませんでした(送信IDによる重複防止なしで送信を続けます): %s",
            label, exc)
        return False


def ensure_op_id_column(access: "access_odbc.AccessConnection",
                        spec: WriteBackSpec) -> bool:
    """Access側テーブルに送信ID列と一意インデックスがあるかを確かめる。

    無ければ作る。列を足し忘れた端末で書き戻すとINSERTが「そんな列は
    無い」で全滅しかねないので、使えるかどうかを毎回(ただしキャッシュ
    しつつ)確認する。作れない環境でも書き戻し自体は止めたくないので、
    戻り値の真偽だけを `write_back` に返す
    (False のときは送信ID列を省いて、旧来どおり予約による二重防止
    だけで送る)。
    """
    cache_key = (str(access.path), spec.access_table, spec.op_id_column)
    cached = _op_id_column_cache.get(cache_key)
    if cached is not None:
        return cached

    column_ok = _try_ddl(
        access,
        f"ALTER TABLE {access_odbc.quote_identifier(spec.access_table)}"
        f" ADD COLUMN {access_odbc.quote_identifier(spec.op_id_column)} TEXT(32)",
        f"{spec.access_table} への {spec.op_id_column} 列の追加")
    if column_ok is None:
        return False

    index_name = f"IX_{spec.access_table}_{spec.op_id_column}"
    index_ok = _try_ddl(
        access,
        f"CREATE UNIQUE INDEX {access_odbc.quote_identifier(index_name)}"
        f" ON {access_odbc.quote_identifier(spec.access_table)}"
        f" ({access_odbc.quote_identifier(spec.op_id_column)})",
        f"{spec.access_table} への一意インデックスの作成")
    if index_ok is None:
        return False

    ready = column_ok and index_ok
    _op_id_column_cache[cache_key] = ready
    return ready


# ------------------------------------------------------------------
# 書き戻し本体
# ------------------------------------------------------------------
def write_back(conn: sqlite3.Connection,
               access: "access_odbc.AccessConnection",
               specs: Iterable[WriteBackSpec]) -> WriteBackResult:
    """specsに列挙された各テーブルについて、SQLite→Accessへ送る。

    呼び出し側(アプリ層)は「Accessに接続できるか」「ファイルが
    見つかるか」を先に確認し、開いた `AccessConnection` をここへ渡す。
    このエンジンは接続の成否や業務固有のファイル探索を一切知らない。
    """
    result = WriteBackResult()
    for spec in specs:
        op_id_ready = spec.use_op_id_guard and ensure_op_id_column(access, spec)
        try:
            rows, op_ids = claim_rows(conn, spec)
        except sqlite3.Error as exc:
            result.errors.append(f"{spec.sqlite_table}: {exc}")
            continue
        if not rows:
            continue

        sent = 0
        # ここに入るのは「確実に失敗した」とわかった行だけ(release_claim参照)
        failed: list[int] = []
        try:
            for row in rows:
                row_id = int(row[spec.key_column])
                values = {k: row[k] for k in row.keys() if k != spec.key_column}
                if op_id_ready:
                    values[spec.op_id_column] = op_ids[row_id]
                try:
                    access.insert(spec.access_table, values)
                except access_odbc.AccessError as exc:
                    if op_id_ready and access_odbc.is_duplicate_error(str(exc)):
                        # 前回の送信が届いていた(送信ID重複)。エラーではなく成功
                        log.info("%s 行%s は既に届いていました(送信ID重複)",
                                 spec.access_table, row_id)
                    else:
                        log.warning("%s の1行を送れませんでした: %s",
                                    spec.access_table, exc)
                        result.errors.append(
                            f"{spec.access_table}(行{row_id}): {exc}")
                        failed.append(row_id)
                        continue
                mark_synced(conn, spec, [row_id])
                sent += 1
        except Exception:
            # 想定外の例外。ここまでに確実に失敗したとわかった分だけ予約を
            # 戻し、残り(未処理の行・Accessに届いたかもしれない行)は
            # 「送信中」のまま残す。期限切れ拾い直しが同じ送信IDで後日
            # 再送し、Access側の一意インデックスが二重登録を防ぐ
            release_claim(conn, spec, failed)
            log.exception("%s: 書き戻し中に想定外の例外", spec.sqlite_table)
            raise
        release_claim(conn, spec, failed)
        if sent:
            result.sent[spec.sqlite_table] = sent
    return result
