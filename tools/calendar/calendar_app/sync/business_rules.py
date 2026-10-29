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

そのため休みの登録 (区分='休み') だけ、送信の直前に取り込み元へ
``日付 + 識別コード`` の重複確認を挟む (VBA の ``ExistsRecord`` と
同じ考え方)。

**確かめと INSERT は、共有の書き込みの錠1つの中で行う**
(``SourceConnection.insert_unless``。``BEGIN IMMEDIATE``)。以前は確かめと
INSERT が別々で、2台がほぼ同時に送ると両方が「まだ無い」と見てから両方が
書き、同じ人・同じ日の休みが2行できた(「既知の限界」としていたが、
Windows の遅い環境で実際に起きた)。いまは後から来た端末は錠が空くのを待ち、
先の行が見えてから確かめるので、取りやめ+知らせの道へ入る。待ちきれなければ
送信待ちに残し、次の周期で送り直す。

後から足した列 (VER4.2.4)
-------------------------
休み管理には「記入班」(連絡を残した班)と「登録日時」(保存した時刻)を
足した。共有の表がまだ前の形なら、送る前に**後ろへ**足す
(``missing_added_columns``)。前からの列は変えない。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .. import config
from ..dbkit import outbox_sync, source_db
from ..logging_utils import debug_log

__all__ = ["GuardedSendResult", "send_data_with_duplicate_guard", "duplicate_guard",
           "missing_added_columns"]


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


def _waiting_for_delete(conn: sqlite3.Connection, source_table: str,
                        values: dict[str, object]) -> bool:
    """同じ人・同じ日・同じ区分の削除が、まだ取り込み元へ届いていないか。"""
    from .deletes import unsent_delete_matches

    return unsent_delete_matches(conn, source_table, {
        "日付": values.get("日付"), "区分": values.get("区分"),
        "識別コード": values.get("識別コード")})


#: 後から足した列を**足せなかった**取り込み元 (パス, 表) → 足せなかった列。
#: 権限が無い・読み取り専用など、待っても直らない相手に毎回 ALTER を投げて
#: 失敗のログを積まないため(``outbox_sync._op_id_failed`` と同じ考え方)。
#: 「ある」ほうは覚えない ── 取り込み元のファイルは差し替えられるので、毎回見る
_added_failed: dict[tuple[str, str], frozenset[str]] = {}


def missing_added_columns(source: "source_db.SourceConnection",
                           table: str) -> frozenset[str]:
    """後から足した列のうち、**今回は省いて送る**ものを返す (VER4.2.4)。

    共有の表に列が無ければ、**前からの列は変えず、後ろに**足す
    (``ALTER TABLE ... ADD COLUMN``。python-web-tools など外からも読まれる
    ので、列の順も意味も動かさない)。見るのは ``PRAGMA`` 1回で、
    足すのは最初に送る1度だけ。

    * 足せた・もうある   … 省かない(届く)
    * 待っても足せない   … 省いて送る(連絡そのものは届く。時刻と班は手元にだけ残る)
    * ロック競合・共有の瞬断で今は不明 … 省かない。列が無ければその行は送れずに
      送信待ちに残り、次の周期で足し直してから届く(省いて送ると、
      時刻と班が共有から永久に欠ける。前は瞬断(``disk I/O error`` など)を
      「待っても足せない」と覚え、再起動するまで省いて送っていた)

    :raises source_db.SourceError: 共有の表の列を**読めなかった**(無い表は空として扱う)。
        呼ぶ側は今回は送らない
    """
    present = set(source.columns(table))
    if not present:
        return frozenset()                  # 表が無い。送ろうとして失敗する(前と同じ)
    missing = [name for name in config.DATA_ADDED_COLUMNS if name not in present]
    if not missing:
        _added_failed.pop((str(source.path), table), None)
        return frozenset()
    known = _added_failed.get((str(source.path), table))
    if known is not None:
        return known & frozenset(missing)

    failed: set[str] = set()
    for name in missing:
        try:
            source.execute(
                f"ALTER TABLE {source_db.quote_identifier(table)} "
                f"ADD COLUMN {source_db.quote_identifier(name)} TEXT NOT NULL DEFAULT ''")
            debug_log(f"business_rules: {table} に「{name}」列を足しました")
        except source_db.SourceError as exc:
            text = str(exc).lower()
            if "duplicate column" in text:
                continue                    # ほかの端末が先に足した
            if source_db.is_transient_error(exc):
                debug_log(f"business_rules: {table} の「{name}」列を今は足せません(使用中・瞬断) {exc}")
                continue
            debug_log(f"business_rules: {table} に「{name}」列を足せません。省いて送ります {exc}")
            failed.add(name)
    if failed:
        _added_failed[(str(source.path), table)] = frozenset(failed)
    return frozenset(failed)


def reset_added_column_cache() -> None:
    """試験用。取り込み元を作り直したときに呼ぶ。"""
    _added_failed.clear()


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

    try:
        # **共有の表の形を確かめられなければ、今回は送らない。** 読めなかったことを
        # 「送信ID列が無い」と読んで送信IDなしで送ると、前の送信が届いていた行が
        # 共有にもう1行できる(``outbox_sync.ensure_op_id_column``)。予約は送信IDを
        # 残したまま戻し、次の回に送る
        op_id_ready = (spec.use_op_id_guard
                       and outbox_sync.ensure_op_id_column(source, spec))
        # 後から足した列(記入班・登録日時)。共有の表に無ければ後ろに足す。
        # 足せない相手には、その列を省いて送る(行そのものは止めない)
        unsendable = missing_added_columns(source, spec.source_table)
    except source_db.SourceError as exc:
        outbox_sync.release_claim(conn, spec, list(op_ids), op_ids)
        debug_log(f"business_rules: {spec.source_table} の形を確かめられず今回は送らない {exc}")
        result.errors.append(f"{spec.source_table}: {exc}")
        return result
    failed: list[int] = []
    try:
        for row in rows:
            row_id = int(row[spec.key_column])
            values = {k: row[k] for k in row.keys()
                      if k != spec.key_column and k not in unsendable}

            guard = duplicate_guard(spec.source_table, values)
            if guard and _waiting_for_delete(conn, spec.source_table, values):
                # **同じ人・同じ日を消して登録し直したところ。** 消す前の行が
                # まだ取り込み元にあるうちに確かめると「先に登録済み」に
                # 見えて取りやめになり、あとで削除が届いて両方とも消える。
                # 削除が届くまで送らずに待つ(送信待ちに残す。次の周期で送る)
                debug_log(
                    f"business_rules: {spec.source_table} 行{row_id} は "
                    "同じ人・同じ日の削除がまだ届いていないため次回に回す")
                failed.append(row_id)
                continue

            sending = dict(values)
            if op_id_ready:
                sending[spec.op_id_column] = op_ids[row_id]
            try:
                if guard is None:
                    source.insert(spec.source_table, sending)
                elif not source.insert_unless(spec.source_table, sending, *guard):
                    # **確かめから書き込みまで共有の錠の中**(``insert_unless``)。
                    # 2台がほぼ同時に送っても、後のほうはここへ来る
                    outbox_sync.mark_synced(conn, spec, [row_id])
                    result.duplicates += 1
                    result.skipped_rows.append(values)
                    debug_log(
                        f"business_rules: {spec.source_table} 行{row_id} は "
                        "他ラインが既に登録済みのため送信を省略"
                    )
                    continue
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
        outbox_sync.release_claim(conn, spec, failed, op_ids)
        raise
    outbox_sync.release_claim(conn, spec, failed, op_ids)
    return result
