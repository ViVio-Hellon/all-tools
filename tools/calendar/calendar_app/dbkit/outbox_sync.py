"""手元のSQLite -> 共有の取り込み元 への書き戻し汎用エンジン

特定のテーブル名や業務に一切依存しない。「送信したい行が増えるSQLite
テーブル」と「送り先の取り込み元テーブル」の対応を `WriteBackSpec` で
渡すだけで、以下をまとめて面倒みる。

    - 二重取得の防止(claim/予約): 背景スレッドが同時に走っても、
      同じ行を2度は拾わない
    - 中断からの再開: 送信の途中で落ちて「送信中」のまま残った予約は、
      一定時間後に拾い直す。そのとき送信IDは新規採番せず引き継ぐ
    - 二重登録の防止: 送信IDを取り込み元の一意インデックスに賭ける。
      再送で同じ行がもう一度INSERTされても、取り込み元が弾いてくれる
      (弾かれたら「失敗」ではなく「既に届いていた」として扱う)
    - 取り込み元に送信ID列を用意できない環境でも、書き戻し自体は
      止めない(その場合は「予約による二重防止」だけで動く)
    - 1行の失敗で全体を止めない

他のVBA移行ツールでも、`WriteBackSpec` のリストを用意して
`write_back(conn, source, specs)` を呼ぶだけで使い回せる。
このファイル自体は業務固有のテーブル名を一切知らない。

【取り込み元の送信ID列について】
`write_back` は呼ぶたびに `ensure_op_id_column` で列とインデックスの
有無を確かめるが、結果(使えた/使えなかった)は端末内でキャッシュする。
ALTER TABLE / CREATE INDEX は失敗しうる操作なので、毎回リトライして
無駄な失敗ログを積み上げたり、ロック競合を増やしたりしないためである。
ロック競合など「今回はたまたま判定できなかった」場合だけキャッシュ
せず、次回また試す。

【このエンジンが対応していない操作】
`write_back` は取り込み元への **INSERT のみ** を面倒みる。UPDATE/DELETEの
反映は業務ごとに「何をキーに対象行を特定するか」の判断が
必要になるため、意図的にこのエンジンの範囲外にしている
(呼び出し側アプリが `source_db` を直接使って実装すること)。
"""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Iterable, Optional

from . import source_db
from .logging_utils import get_logger

log = get_logger("outbox_sync")

# 同期記録テーブル(このエンジンが自分のSQLite内に持つ管理テーブル)。
# 業務テーブルの名前は一切ここに出てこない。
# **名前は変えない** ── 既に端末で使われているので、変えると
# 「送信済みの記録」が丸ごと失われ、全行が再送される
SYNC_LOG_TABLE = "Access同期記録"

# 同期記録の状態。送信前に「送信中」で予約し、送れたら「済」にする
SYNC_SENDING = "送信中"
SYNC_DONE = "済"

# 「送信中」のまま放置された予約を、何分たったら拾い直すか。
# アプリが落ちる・端末が落ちる等で予約だけ残ることがある
STALE_CLAIM_MINUTES = 10

# 取り込み元側に足す送信ID列の既定名。テーブルごとに変えたい理由がなければ
# このままでよい
DEFAULT_OP_ID_COLUMN = "送信ID"


@dataclass(frozen=True)
class WriteBackSpec:
    """書き戻し対象1テーブル分の定義。呼び出し側(アプリ層)が組み立てる。

    sqlite_table   : ローカルSQLite側のテーブル名(行が増えていく方)
    source_table   : 送り先の取り込み元テーブル名
    key_column     : 両テーブルで共通の主キー列名(整数、オートナンバー等)
    op_id_column   : 取り込み元側に足す送信ID列の名前
    use_op_id_guard: 取り込み元側テーブルに送信ID列/一意インデックスを
                     作成してよいか。他チーム管理のテーブル等、
                     スキーマを触りたくない送り先では False にする
                     (その場合は予約による二重防止だけで動く)
    """
    sqlite_table: str
    source_table: str
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
            return "取り込み元に送る新しいデータはありませんでした。"
        lines = [f"取り込み元へ{self.total}件を反映しました。"]
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
    """まだ取り込み元に送れていない行を拾う(予約はしない)。件数確認用。"""
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
    """複数specの中で、まだ取り込み元へ送っていない行が残っているものを返す。

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
    毎回SELECTすると、同時に走った処理同士が同じ行を拾って取り込み元へ二重に
    INSERTしてしまう。ここで先に予約しておけば、スレッドでも別プロセス
    でも同じ行を2度は拾わない(予約はひとつのトランザクションで行う)。

    送信IDは予約時に採番し、期限切れの予約を拾い直すときは同じIDを
    使い回す(=取り込み元に届いていたかもしれない行だけ、IDを引き継ぐ)。
    確実に失敗したとわかった行は `release_claim` で予約ごと外すので、
    そちらは次回このIDを見ることはなく、新しいIDで送られる。
    """
    ensure_sync_table(conn)
    conn.row_factory = sqlite3.Row
    op_ids: dict[int, str] = {}
    # 呼んだ側の書きかけは先に確定させる(``ensure_sync_table`` が以前は
    # ここで commit していた。それを当てにしている呼び手のため)
    if conn.in_transaction:
        conn.commit()
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
                        "(同じ送信IDで再送するので取り込み元側が重複を弾きます)",
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

    ここに渡すのは、取り込み元への送信で `SourceError` を捕まえて「届いて
    いない」と判断できた行**だけ**にすること(型不一致・列不足など、
    待っても直らない失敗)。記録ごと削除するので、次回は真新しい予約
    として拾われ、送信IDも採番し直される――届いていないと分かっている
    のでそれで安全。

    逆に「取り込み元に届いたかどうか分からない行」(想定外の例外で処理を
    抜けた場合など)はここに渡さず、「送信中」のまま残すこと。
    `claim_rows` の期限切れ拾い直しが同じ送信IDで再送し、取り込み元側の
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
# 取り込み元テーブルの準備(送信ID列 / 一意インデックス)
# ------------------------------------------------------------------
# 「作れなかった」という結果だけを端末内で覚えておく。
# key: (取り込み元ファイルのパス, source_table, op_id_column)
#
# **「ある」ほうは覚えない。** 取り込み元のファイルは差し替えられる
# (控えから戻す・年度で入れ替える)ので、「列はある」と覚えたまま長時間
# 動き続けると、列の無いファイルへ送り続けて毎行 no such column で落ちる。
# プロセスを開けっぱなしにする運用ではこれが**再起動するまで直らない**。
# 実在するかどうかは PRAGMA と sqlite_master を見れば足りる(1周期に
# 数回の軽い読みで、ALTER TABLE を投げるより安い)。
_op_id_failed: dict[tuple[str, str, str], bool] = {}


def _try_ddl(source: "source_db.SourceConnection", sql: str,
             label: str) -> Optional[bool]:
    """DDLを1つ実行する。戻り値は3通り。

    True  : 成功した、または「もう存在する」ので実質OK
    False : 権限不足など、待っても直らない失敗(以後は諦めて素通りする)
    None  : ロック競合など一時的な失敗(今回は判定できない、次回また試す)
    """
    try:
        source.execute(sql)
        log.info("%s: 完了しました", label)
        return True
    except source_db.SourceError as exc:
        if source_db.is_lock_error(exc):
            log.debug("%s: ロック競合のため今回は見送ります(次回再試行)", label)
            return None
        message = str(exc).lower()
        if "duplicate column" in message or "already exists" in message:
            log.debug("%s: 既にあります", label)
            return True
        log.warning(
            "%s: できませんでした(送信IDによる重複防止なしで送信を続けます): %s",
            label, exc)
        return False


def ensure_op_id_column(source: "source_db.SourceConnection",
                        spec: WriteBackSpec) -> bool:
    """取り込み元テーブルに送信ID列と一意インデックスがあるかを確かめる。

    無ければ作る。列を足し忘れた端末で書き戻すとINSERTが「そんな列は
    無い」で全滅しかねないので、使えるかどうかを毎回(ただしキャッシュ
    しつつ)確認する。作れない環境でも書き戻し自体は止めたくないので、
    戻り値の真偽だけを `write_back` に返す
    (False のときは送信ID列を省いて、予約による二重防止だけで送る)。

    **毎回、実際にあるかどうかを見る。** 見るのは PRAGMA と sqlite_master の
    2回で、ALTER TABLE を投げるより安い。覚えで済ませると、取り込み元が
    差し替わった端末が「列はある」と思い込んだまま送り続ける
    (``_op_id_failed`` の説明)。
    """
    cache_key = (str(source.path), spec.source_table, spec.op_id_column)
    index_name = f"IX_{spec.source_table}_{spec.op_id_column}"

    has_column = spec.op_id_column in source.columns(spec.source_table)
    if has_column and source.has_index(index_name):
        # 既に整っている。**ここが普段通る道**なので、何も書かない
        _op_id_failed.pop(cache_key, None)
        return True

    # 作れないと分かっている相手には、もう挑まない(無駄な失敗ログと
    # ロック競合を積み上げないため)
    if _op_id_failed.get(cache_key):
        return False

    if has_column:
        column_ok: Optional[bool] = True
    else:
        column_ok = _try_ddl(
            source,
            f"ALTER TABLE {source_db.quote_identifier(spec.source_table)}"
            f" ADD COLUMN {source_db.quote_identifier(spec.op_id_column)} TEXT",
            f"{spec.source_table} への {spec.op_id_column} 列の追加")
    if column_ok is None:
        return False

    index_ok = _try_ddl(
        source,
        f"CREATE UNIQUE INDEX IF NOT EXISTS "
        f"{source_db.quote_identifier(index_name)}"
        f" ON {source_db.quote_identifier(spec.source_table)}"
        f" ({source_db.quote_identifier(spec.op_id_column)})",
        f"{spec.source_table} への一意インデックスの作成")
    if index_ok is None:
        return False

    ready = bool(column_ok and index_ok)
    if not ready:
        _op_id_failed[cache_key] = True
    return ready


def reset_op_id_cache() -> None:
    """テスト用。取り込み元を作り直したときに呼ぶ。"""
    _op_id_failed.clear()


# ------------------------------------------------------------------
# 書き戻し本体
# ------------------------------------------------------------------
def write_back(conn: sqlite3.Connection,
               source: "source_db.SourceConnection",
               specs: Iterable[WriteBackSpec]) -> WriteBackResult:
    """specsに列挙された各テーブルについて、手元→取り込み元へ送る。

    呼び出し側(アプリ層)は「取り込み元に届くか」「ファイルが見つかるか」
    を先に確認し、開いた `SourceConnection` をここへ渡す。
    このエンジンは接続の成否や業務固有のファイル探索を一切知らない。
    """
    result = WriteBackResult()
    for spec in specs:
        op_id_ready = spec.use_op_id_guard and ensure_op_id_column(source, spec)
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
                    source.insert(spec.source_table, values)
                except source_db.SourceError as exc:
                    if op_id_ready and source_db.is_duplicate_error(exc):
                        # 前回の送信が届いていた(送信ID重複)。エラーではなく成功
                        log.info("%s 行%s は既に届いていました(送信ID重複)",
                                 spec.source_table, row_id)
                    else:
                        log.warning("%s の1行を送れませんでした: %s",
                                    spec.source_table, exc)
                        result.errors.append(
                            f"{spec.source_table}(行{row_id}): {exc}")
                        failed.append(row_id)
                        continue
                mark_synced(conn, spec, [row_id])
                sent += 1
        except Exception:
            # 想定外の例外。ここまでに確実に失敗したとわかった分だけ予約を
            # 戻し、残り(未処理の行・取り込み元に届いたかもしれない行)は
            # 「送信中」のまま残す。期限切れ拾い直しが同じ送信IDで後日
            # 再送し、取り込み元の一意インデックスが二重登録を防ぐ
            release_claim(conn, spec, failed)
            log.exception("%s: 書き戻し中に想定外の例外", spec.sqlite_table)
            raise
        release_claim(conn, spec, failed)
        if sent:
            result.sent[spec.sqlite_table] = sent
    return result
