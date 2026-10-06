"""dbkit の汎用エンジンだけではカバーしない、カレンダー固有の業務ルール。

休みの二重登録防止
------------------
``dbkit.outbox_sync`` の送信ID(送信ID列/一意インデックス)は
「自分が送った内容が本当に Access へ届いたか」(クラッシュ後の再送で
同じ行を 2 回 INSERT してしまわないか) だけを保証する。

これは「別のラインが同じ実世界の事実 (同じ作業者の同じ日の休み) を
それぞれ独立に入力した」ケースは防げない。それぞれ別の SQLite 行・
別の送信ID として正当に生成されるため、outbox_sync からは単に
「2 件の別々の新規行」にしか見えない。

そのため休みの登録 (区分='休み') だけ、送信の直前に Access へ
``日付 + 識別コード`` の重複確認を挟む (VBA の ``ExistsRecord`` と
同じ考え方)。確認と実際の INSERT の間にはごく短い競合の余地が残るが
(同時に 2 ラインが送信した場合など)、元の設計からの既知の限界であり、
実運用では確認〜挿入までの時間が短いため実害は小さい。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .. import config
from ..dbkit import outbox_sync, source_db
from ..logging_utils import debug_log

__all__ = ["GuardedSendResult", "send_data_with_duplicate_guard", "duplicate_guard"]


@dataclass
class GuardedSendResult:
    """業務レベルの重複確認を挟んだ送信の結果。"""

    sent: int = 0
    duplicates: int = 0
    errors: list[str] = field(default_factory=list)
    #: 送らずに取りやめた行の中身。**その端末の人に知らせる**(``notices``)
    skipped_rows: list[dict[str, object]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.sent + self.duplicates


def duplicate_guard(source_table: str,
                    values: dict[str, object]) -> tuple[str, list[object]] | None:
    """休みの登録なら、重複確認用の COUNT(*) を (SQL, 値) で返す。

    それ以外 (コメント・連絡) は ``None`` ── 同じ日に何件でも登録できる。

    **値はプレースホルダで渡す。** Access のときは SQL リテラルを自前で
    組み立てるしかなく、日付を ``#...#`` に変換する必要があったが、
    sqlite3 では日付も TEXT のまま比べられる(手元も取り込み元も
    ``yyyy/mm/dd`` の TEXT で持っている)。
    """
    if values.get("区分") != config.KUBUN_REST:
        return None
    code = str(values.get("識別コード") or "").strip()
    if not code or code == "-":
        return None
    date_value = values.get("日付")
    if not isinstance(date_value, str) or not date_value:
        return None
    sql = (
        f"SELECT COUNT(*) AS cnt FROM {source_db.quote_identifier(source_table)} "
        'WHERE "日付" = ? AND "識別コード" = ? AND "区分" = ?'
    )
    return sql, [date_value, code, config.KUBUN_REST]


def _already_registered(source: "source_db.SourceConnection",
                        guard: tuple[str, list[object]]) -> bool:
    sql, params = guard
    try:
        rows = source.query(sql, params)
    except source_db.SourceError as exc:
        # 確認できなかった場合は、確認不能を理由に入力を止めたくないため
        # 安全側 (=重複防止を優先せず送信する) に倒す
        debug_log(f"business_rules._already_registered: 確認に失敗したため送信を継続 {exc}")
        return False
    if not rows:
        return False
    value = next(iter(rows[0].values()), 0)
    try:
        return int(value) > 0
    except (TypeError, ValueError):
        return False


def send_data_with_duplicate_guard(
    conn: sqlite3.Connection,
    source: "source_db.SourceConnection",
    spec: outbox_sync.WriteBackSpec,
) -> GuardedSendResult:
    """休み管理 spec を送る。休みの登録だけ重複確認を挟む。

    ``dbkit.outbox_sync.write_back`` とほぼ同じ処理を、claim_rows などの
    公開部品を直接呼ぶ形で組み立てている
    (write_back には業務チェックを差し込む余地が無いため)。
    """
    result = GuardedSendResult()
    rows, op_ids = outbox_sync.claim_rows(conn, spec)
    if not rows:
        return result

    op_id_ready = spec.use_op_id_guard and outbox_sync.ensure_op_id_column(source, spec)
    failed: list[int] = []
    try:
        for row in rows:
            row_id = int(row[spec.key_column])
            values = {k: row[k] for k in row.keys() if k != spec.key_column}

            guard = duplicate_guard(spec.source_table, values)
            if guard and _already_registered(source, guard):
                outbox_sync.mark_synced(conn, spec, [row_id])
                result.duplicates += 1
                result.skipped_rows.append(values)
                debug_log(
                    f"business_rules: {spec.source_table} 行{row_id} は "
                    "他ラインが既に登録済みのため送信を省略"
                )
                continue

            if op_id_ready:
                values[spec.op_id_column] = op_ids[row_id]
            try:
                source.insert(spec.source_table, values)
            except source_db.SourceError as exc:
                if op_id_ready and source_db.is_duplicate_error(exc):
                    pass  # 前回の送信が既に届いていた (送信ID重複 = 成功扱い)
                else:
                    debug_log(f"business_rules: 送信失敗 行{row_id} {exc}")
                    result.errors.append(f"{spec.source_table}(行{row_id}): {exc}")
                    failed.append(row_id)
                    continue
            outbox_sync.mark_synced(conn, spec, [row_id])
            result.sent += 1
    except Exception:
        outbox_sync.release_claim(conn, spec, failed)
        raise
    outbox_sync.release_claim(conn, spec, failed)
    return result
