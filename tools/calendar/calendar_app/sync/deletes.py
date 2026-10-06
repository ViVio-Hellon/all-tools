"""削除の Access への転送 (dbkit.outbox_sync ではカバーしない部分)。

``dbkit.outbox_sync`` は「SQLite の業務テーブルに新しく増えた行を
Access へ追記する」ことだけを面倒みる汎用エンジンで、UPDATE/DELETE の
転送は意図的に対象外にしている (対象行の特定方法が業務ごとに違うため)。

このカレンダーでは、休みの登録を削除すると SQLite 側の行そのものが
消えるため、outbox_sync の「今あるテーブルを見る」方式では拾えない。
そこで削除だけは ``db.py`` の ``_pending_deletes`` に予約しておき、
ここで Access へ ``DELETE`` を発行する。対象の特定は

* Access 側の ID が分かっていれば ``[ID] = <id>``
* 分からなければ自然キー (日付・区分・登録内容・識別コード・班・ライン)
  で絞り込む

という 2 段構え (VBA からの移行前の設計をそのまま踏襲)。

【消しすぎない】
自然キーは**一意とはかぎりません。** 連絡は識別コードが ``-`` 固定なので、
別のラインが同じ日に同じ文面(「残業なし」など)を出していると条件に
当たります。そのため

* 自然キーに**班とラインまで入れる**
* それでも当たった行のうち**1行だけ消す**(``build_delete_one``)

の2段で守ります。消し足りないほうは次の転送でまた当たりますが、
消しすぎたぶんは戻せません。
"""

from __future__ import annotations

import json
import sqlite3

from .. import db
from ..dbkit import outbox_sync, source_db
from ..logging_utils import debug_log

__all__ = [
    "was_sent_to_access",
    "queue_delete",
    "pending_delete_count",
    "forward_pending_deletes",
    "natural_key_where",
]


def was_sent_to_access(conn: sqlite3.Connection, sqlite_table: str, row_id: int) -> bool:
    """指定した SQLite の行が、outbox_sync によって既に送信済みか。

    削除しようとしている行の access_id が NULL でも、直前の同期サイクルで
    Access への送信自体は完了している場合がある
    (再取り込みでまだ access_id が更新されていないだけ)。
    その場合も Access 側には実在するはずなので、削除を転送する必要がある。
    """
    row = conn.execute(
        f'SELECT 1 FROM "{outbox_sync.SYNC_LOG_TABLE}" '
        'WHERE "テーブル名"=? AND "行ID"=? AND "状態"=?',
        (sqlite_table, row_id, outbox_sync.SYNC_DONE),
    ).fetchone()
    return row is not None


def queue_delete(
    conn: sqlite3.Connection,
    spec: "outbox_sync.WriteBackSpec",
    row: sqlite3.Row,
    *,
    row_id: int,
) -> bool:
    """削除された行を Access へ転送すべきか判定し、必要なら予約する。

    ``spec.sqlite_table`` (outbox_sync が送信記録を残す名前 = "_送信用" ビュー)
    で送信済みかどうかを確認し、``spec.source_table`` (実際の Access
    テーブル名) を転送予約に使う――この 2 つは別の名前になりうるため、
    どちらか片方の文字列だけでは両方を賄えない。

    Access にまだ届いていない (=access_id が無く、送信もされていない)
    行であれば、Access 側には最初から存在しないため転送は不要。
    その場合はローカルの削除だけで完結する
    (登録して即削除した場合が典型例)。

    戻り値: 転送予約を行ったら True。
    """
    access_id = row["access_id"] if "access_id" in row.keys() else None
    if access_id is None and not was_sent_to_access(conn, spec.sqlite_table, row_id):
        debug_log(
            f"sync.deletes.queue_delete: {spec.source_table} 行{row_id} は "
            "Access未送信のため転送不要 (ローカル削除のみ)"
        )
        return False

    # **班とラインまで入れる。** これが無いと、別のラインが同じ日に
    # 同じ文面の連絡を出していたときに、そちらまで当たってしまう
    # (連絡は識別コードが "-" 固定なので、日付と文面しか手がかりが無い)。
    # 実際に「残業なし」のような普通の文面で他ラインの行が消えた
    natural_key = {
        name: (row[name] or "") if name in row.keys() else ""
        for name in ("日付", "区分", "登録内容", "識別コード", "班", "ライン")
    }
    db.queue_pending_delete(
        conn, spec.source_table, access_id=access_id, natural_key=natural_key
    )
    return True


def pending_delete_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        'SELECT COUNT(*) AS cnt FROM "_pending_deletes" WHERE "sent_at" IS NULL'
    ).fetchone()
    return int(row["cnt"])


def natural_key_where(natural_key: dict[str, str]) -> dict[str, object]:
    """自然キーの JSON を、WHERE に使う値に整える。

    **日付は文字列のまま。** 手元も取り込み元も ``yyyy/mm/dd`` の TEXT で
    持っているので、変換すると逆に一致しなくなる
    (Access のときは ``#...#`` リテラルにするため date 型へ直していた)。
    """
    return {k: v for k, v in natural_key.items() if v not in (None, "")}


def forward_pending_deletes(
    conn: sqlite3.Connection,
    source: "source_db.SourceConnection",
    table_name: str,
) -> tuple[int, list[str]]:
    """未転送の削除予約を取り込み元へ送る。

    戻り値: (転送できた件数, エラーメッセージの一覧)
    """
    rows = [
        r for r in db.pending_deletes(conn) if r["table_name"] == table_name
    ]
    if not rows:
        return 0, []

    sent_seqs: list[int] = []
    errors: list[str] = []
    for row in rows:
        source_id = row["access_id"]
        if source_id:
            where: dict[str, object] = {"ID": source_id}
        else:
            natural_key = json.loads(row["natural_key"] or "{}")
            where = natural_key_where(natural_key)

        if not where:
            # 対象を特定する材料が無い (データ不備)。滞留させても解決しないため
            # 転送済み扱いにして進める
            debug_log(
                f"sync.deletes.forward_pending_deletes: seq={row['seq']} は "
                "削除対象を特定できず転送を諦めました"
            )
            sent_seqs.append(int(row["seq"]))
            continue

        # **消すのは1行だけ。** 空の WHERE を断るだけでは足りない ──
        # 自然キーは一意とはかぎらないので、条件に2行当たれば2行消える。
        # 1件のつもりで2件消すのは取り返しがつかず、しかも消した人には
        # 見えない(``build_delete_one`` の説明)
        sql, params = source_db.build_delete_one(table_name, where)
        try:
            source.execute(sql, params)
            sent_seqs.append(int(row["seq"]))
            debug_log(f"sync.deletes.forward_pending_deletes: 転送成功 {sql} {params}")
        except source_db.SourceError as exc:
            debug_log(f"sync.deletes.forward_pending_deletes: 転送失敗 {sql} / {exc}")
            errors.append(f"{table_name}(seq={row['seq']}): {exc}")

    if sent_seqs:
        db.mark_deletes_sent(conn, sent_seqs)
    return len(sent_seqs), errors
