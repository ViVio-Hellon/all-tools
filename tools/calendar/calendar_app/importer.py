"""共有の取り込み元 (sqlite3) から、手元の SQLite への取り込み

取り込みは「**取り込み元が正**」として行うため、対象テーブルを一度空に
してから入れ直す。ただし手元で追加されて まだ取り込み元へ送っていない行は
失われると困るので、既定では未送信の変更が残っている場合に中断する
(``force=True`` で上書き可能)。

【以前は .accdb でした】
Jet4 のバイナリを自前で解析していたので、811行の読み取り器と、その
不具合(実データ照合で5件見つかった)を抱えていました。取り込み元が
sqlite3 になったので、``SELECT * FROM 表`` を投げるだけになっています。

**日付は文字列のまま持つ。** 手元のスキーマも取り込み元も TEXT で
``yyyy/mm/dd`` を持つ(VBA からの引き継ぎ)。ただし別の手段で作られた
取り込み元が ISO 形式を持っている場合に備えて、整える関数は残してある。
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import config, db, sources
from .dbkit import outbox_sync, source_db
from .logging_utils import debug_log
from .sync import reconcile_after_reimport, total_pending_count
from .sync import specs as sync_specs

__all__ = [
    "ImportResult", "PendingChangesError",
    "import_source", "import_all", "import_master_table", "import_members_csv",
    "is_importable",
]

#: 取り込み対象テーブル名 -> 対応する書き戻し定義。
#: 班員名簿 (TABLE_MEMBER) は書き戻しが無いテーブルなので含まれない。
_SPEC_BY_SOURCE_TABLE = {spec.source_table: spec
                         for spec in sync_specs.WRITE_BACK_SPECS}


class PendingChangesError(RuntimeError):
    """取り込み元へ未送信の変更があるまま取り込もうとした場合に送出する。"""


@dataclass
class ImportResult:
    """取り込み結果のサマリ。"""

    source: str
    tables: dict[str, int] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    #: どの開き方で読めたか。**「手元への写し」なら中身が少し古いことがある**
    opened_by: str = ""

    @property
    def total(self) -> int:
        return sum(self.tables.values())

    def describe(self) -> str:
        parts = [f"{name}: {count} 件" for name, count in self.tables.items()]
        text = f"{Path(self.source).name} から取り込み -> " + ", ".join(parts)
        if self.skipped:
            text += f" / 見つからなかったテーブル: {', '.join(self.skipped)}"
        if self.opened_by == source_db.WAY_COPY:
            text += ("\n注意: 共有の上で直接開けなかったため、手元へ写して"
                     "読みました。直前の更新が含まれていない可能性があります。")
        return text


# ---------------------------------------------------------------------------
# 値の整え
# ---------------------------------------------------------------------------
def _to_date_key(value: Any) -> str:
    """日付値を ``yyyy/mm/dd`` に整える。"""
    if isinstance(value, _dt.datetime):
        return value.strftime(config.DATE_KEY_FORMAT)
    if isinstance(value, _dt.date):
        return value.strftime(config.DATE_KEY_FORMAT)
    text = db.sanitize(value)
    # 別の手段で作られた取り込み元が ISO 形式を持っていることがある
    if text[:4].isdigit() and "-" in text[:10]:
        try:
            return _dt.datetime.strptime(text[:10], "%Y-%m-%d").strftime(
                config.DATE_KEY_FORMAT)
        except ValueError:
            pass
    return text


def _to_datetime_string(value: Any) -> str:
    """日時値を ``yyyy/mm/dd hh:mm:ss`` に整える。"""
    if isinstance(value, _dt.datetime):
        return value.strftime(config.DATETIME_FORMAT)
    if isinstance(value, _dt.date):
        return _dt.datetime(value.year, value.month, value.day).strftime(
            config.DATETIME_FORMAT)
    text = db.sanitize(value)
    if text[:4].isdigit() and "-" in text[:10]:
        for form in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return _dt.datetime.strptime(text, form).strftime(
                    config.DATETIME_FORMAT)
            except ValueError:
                continue
    return text


def _rows(source: sources.SourceConnection, table: str) -> list[dict[str, Any]]:
    return source.query(f"SELECT * FROM {source_db.quote_identifier(table)}")


# ---------------------------------------------------------------------------
# 表ごとの取り込み
# ---------------------------------------------------------------------------
def _import_data_table(conn: sqlite3.Connection,
                       rows: list[dict[str, Any]]) -> int:
    """休み管理テーブルを取り込む。"""
    conn.execute(f'DELETE FROM "{config.TABLE_DATA}"')
    for row in rows:
        # 取り込み元の ID をそのまま主キーに使うことで、画面で選んだ行と
        # 取り込み元の行が 1 対 1 で対応する
        source_id = row.get("ID") if isinstance(row.get("ID"), int) else None
        conn.execute(
            f'INSERT INTO "{config.TABLE_DATA}" '
            '("ID","access_id","日付","区分","登録内容","識別コード",'
            '"直","残業者","早出者","班","ライン") '
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                source_id,
                source_id,
                _to_date_key(row.get("日付")),
                db.sanitize(row.get("区分")),
                db.sanitize(row.get("登録内容")),
                db.sanitize(row.get("識別コード")),
                db.sanitize(row.get("直")),
                db.sanitize(row.get("残業者")),
                db.sanitize(row.get("早出者")),
                db.sanitize(row.get("班")),
                db.sanitize(row.get("ライン")),
            ),
        )
    _align_autoincrement(conn, config.TABLE_DATA, rows)
    return len(rows)


def _import_history_table(conn: sqlite3.Connection,
                          rows: list[dict[str, Any]]) -> int:
    """削除履歴テーブルを取り込む。"""
    conn.execute(f'DELETE FROM "{config.TABLE_DEL_HISTORY}"')
    for row in rows:
        source_id = row.get("ID") if isinstance(row.get("ID"), int) else None
        conn.execute(
            f'INSERT INTO "{config.TABLE_DEL_HISTORY}" '
            '("ID","access_id","削除日時","削除実行者","対象日付","区分",'
            '"登録内容","識別コード","班","ライン") '
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                source_id,
                source_id,
                _to_datetime_string(row.get("削除日時")),
                db.sanitize(row.get("削除実行者")),
                _to_date_key(row.get("対象日付")),
                db.sanitize(row.get("区分")),
                db.sanitize(row.get("登録内容")),
                db.sanitize(row.get("識別コード")),
                db.sanitize(row.get("班")),
                db.sanitize(row.get("ライン")),
            ),
        )
    _align_autoincrement(conn, config.TABLE_DEL_HISTORY, rows)
    return len(rows)


def _import_member_table(conn: sqlite3.Connection,
                         rows: list[dict[str, Any]]) -> int:
    """班員名簿 (マスタ DB 側) を取り込む。

    VBA では列位置(0始まり)で参照していたが、取り込み元が sqlite3 に
    なって列名が確実に引けるようになったので、**名前だけで読む**。
    名前が違えばその列は空になり、マスタ管理の画面に
    「扱える列がありません」と出る ── 黙って位置で拾うより、
    形が違うことを見せるほうがよい。
    """
    conn.execute(f'DELETE FROM "{config.TABLE_MEMBER}"')
    imported = 0
    for row in rows:
        code = db.sanitize(row.get("管理番号"))
        if not code:
            continue  # 管理番号が無い行は選択対象にできないため取り込まない
        conn.execute(
            f'INSERT OR REPLACE INTO "{config.TABLE_MEMBER}" '
            '("管理番号","苗字","班","名前","読み","担当ライン") VALUES (?,?,?,?,?,?)',
            (
                code,
                db.sanitize(row.get("苗字")),
                db.sanitize(row.get("班")),
                db.sanitize(row.get("名前")),
                db.sanitize(row.get("読み")),
                db.sanitize(row.get("担当ライン")),
            ),
        )
        imported += 1
    return imported


def _align_autoincrement(conn: sqlite3.Connection, table: str,
                         rows: list[dict[str, Any]]) -> None:
    """SQLite の採番開始値を取り込み元の最大 ID に合わせる。

    こうしておくと手元で新規追加した行の ID が、取り込み元の既存 ID と
    衝突しない。``sqlite_sequence`` には一意制約が無いため
    UPSERT ではなく更新→挿入の順で行う。
    """
    ids = [r.get("ID") for r in rows if isinstance(r.get("ID"), int)]
    if not ids:
        return
    max_id = max(ids)
    cur = conn.execute(
        'UPDATE "sqlite_sequence" SET "seq"=? WHERE "name"=? AND "seq"<?',
        (max_id, table, max_id),
    )
    if cur.rowcount == 0:
        exists = conn.execute(
            'SELECT 1 FROM "sqlite_sequence" WHERE "name"=?', (table,)
        ).fetchone()
        if not exists:
            conn.execute(
                'INSERT INTO "sqlite_sequence" ("name","seq") VALUES (?,?)',
                (table, max_id),
            )


#: テーブル名 -> 取り込み関数
def _import_access_table(conn: sqlite3.Connection,
                         rows: list[dict[str, Any]]) -> int:
    """アクセス権限 (マスタ DB 側。他のツールと共用) を取り込む。"""
    from . import access_control

    return access_control.import_row_list(conn, rows)


_IMPORTERS: dict[str, Callable[[sqlite3.Connection, Any], int]] = {
    config.TABLE_DATA: _import_data_table,
    config.TABLE_DEL_HISTORY: _import_history_table,
    config.TABLE_MEMBER: _import_member_table,
    "アクセス権限": _import_access_table,
}


def is_importable(table: str) -> bool:
    """手元へ写しを取る表か。

    **写しを持たない表もあります**(端末一覧)。それらは画面が取り込み元を
    直に読むので、書いたあとに取り込み直す必要がありません。
    """
    return table in _IMPORTERS


# ---------------------------------------------------------------------------
# 取り込み
# ---------------------------------------------------------------------------
def import_source(conn: sqlite3.Connection, path: str | Path, *,
                  force: bool = False) -> ImportResult:
    """1 つの取り込み元から、見つかった対象テーブルを手元へ取り込む。

    :param force: 未送信の変更があっても取り込みを強行する
    :raises PendingChangesError: 未送信の変更が残っている (force=False のとき)
    :raises SourceError: 取り込み元を開けない
    :raises sqlite3.OperationalError: 手元の錠が取れない (登録の最中など)

    【確かめるのは2回 ── 入れ替える直前に、錠を取ってからもう一度】
    以前は入口で1回だけ「送信待ちが0件か」を見て、そのあと共有を読み、
    それから手元を入れ替えていた。**共有を読んでいるあいだ(数秒かかる
    こともある)に登録された行は、確かめたあとに増えたので見えず、
    入れ替えでそのまま消えた。** 送信待ちにも残らないので、取り込み元へも
    届かない(「打った行が消えることがありました」── とんでもない話である)。

    いまは、共有を**読み終えてから** ``BEGIN IMMEDIATE`` で手元の書き込みを
    止め、その中で数え直す。0件でなければ入れ替えずにやめる
    (``PendingChangesError``。自動同期は今回の取り込みを見送るだけ)。
    錠を取ってから入れ替え終わるまでは登録が待たされるが、読み終えた行を
    書くだけなので一瞬で済む。待ちきれなかった登録は画面に「もう一度」を
    出す(``app/routes/calendar.py``)── 黙って消えるよりずっとよい。
    """
    # 入口の数は記録のためだけ。**断るかどうかは入れ替える直前に決める**
    # (下の BEGIN IMMEDIATE の中)。名簿だけの取り込み元なら断らない
    pending = total_pending_count(conn)

    target = Path(path)
    result = ImportResult(source=str(target))
    debug_log(f"importer.import_source: 開始 {target}")

    # **取り込み元は1つの時点で読む**(``reading``)。表ごとに読むと、
    # その合間に他の端末が書けてしまい、表どうしが少しずれた組み合わせを
    # 取り込むことがある。**読むのは手元の錠を取る前**(取ってから読むと、
    # 共有が遅い日にそのあいだずっと登録を待たせる)
    snapshot: dict[str, list[dict[str, Any]]] = {}
    with source_db.connect(target, read_only=True) as source, source.reading():
        result.opened_by = source.way
        available = set(source.table_names())
        for table_name in _IMPORTERS:
            if table_name not in available:
                result.skipped.append(table_name)
                continue
            snapshot[table_name] = _rows(source, table_name)

    # 送信待ちと関わる表(書き戻しのある表)を入れ替えるか。名簿だけなら
    # 送信待ちは消えないので、数え直す必要は無い
    replaces_outbox = any(name in _SPEC_BY_SOURCE_TABLE for name in snapshot)

    # **手元は1トランザクション**で入れ替えるので、途中の状態が画面に
    # 出ることも無い。``IMMEDIATE`` にするのは、数え直してから入れ替え
    # 終わるまで、ほかの接続(登録の API)に書かせないため
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if replaces_outbox and not force:
            pending_now = total_pending_count(conn)
            if pending_now:
                debug_log("importer.import_source: 読んでいるあいだに未送信が"
                          f"{pending_now}件増えたため入れ替えをやめる")
                raise PendingChangesError(_pending_message(pending_now))

        for table_name, rows in snapshot.items():
            # このテーブルは総入れ替えされるため、書き戻し対象なら
            # 送信記録・削除予約を先に片付けておく
            spec = _SPEC_BY_SOURCE_TABLE.get(table_name)
            if spec is not None:
                conn.execute(
                    f'DELETE FROM "{outbox_sync.SYNC_LOG_TABLE}" '
                    'WHERE "テーブル名"=?',
                    (spec.sqlite_table,),
                )
            db.discard_pending_deletes(conn, table_name)

            count = _IMPORTERS[table_name](conn, rows)
            result.tables[table_name] = count
            debug_log(f"importer.import_source: {table_name} {count} 件")

        # 取り込み直した内容は取り込み元に存在することが保証されている
        # ため、送信済みとして記録し、次回送信での二重 INSERT を防ぐ。
        # **入れ替えた表だけ**(名簿だけの取り込みで、未送信の登録を
        # 「送信済み」にしない)
        replaced = [_SPEC_BY_SOURCE_TABLE[name] for name in snapshot
                    if name in _SPEC_BY_SOURCE_TABLE]
        if replaced:
            reconcile_after_reimport(conn, replaced)

        if force and pending:
            debug_log(f"importer.import_source: 未送信の変更 {pending} 件を破棄")

        db.set_meta(conn, "last_import_at", db.now_string())
        db.set_meta(conn, "last_import_source", str(target))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise

    debug_log(f"importer.import_source: 終了 {result.describe()}")
    return result


def _pending_message(pending: int) -> str:
    return (f"取り込み元へ未送信の変更が {pending} 件あります。\n"
            "先に同期してから取り込み直すか、"
            "変更を破棄する場合は force=True を指定してください。")


def import_master_table(conn: sqlite3.Connection, path: str | Path,
                        table_name: str) -> int:
    """マスタ表を **1 つだけ** 取り込み直す。取り込んだ件数を返す。

    マスタ管理 (``master_admin``) が、取り込み元へ書いた直後に
    「手元を追いつかせる」ために使う。元は直っているのに画面の動きが
    変わらない状態(**直したのに効かない**)を作らないため。

    ``import_source`` と分けてあるのは、あちらが**すべての対象テーブルを
    総入れ替えする**ため:

    * 休み管理・削除履歴まで入れ替わる → まだ送れていない入力が消える
    * 未送信の変更があると ``PendingChangesError`` で止まる

    班員名簿は書き戻しが無い読み取り専用のマスタなので、送信待ちを
    気にせず単独で入れ直せる。書き戻しのある表を渡された場合は、
    その安全確認を飛ばすことになるので断る。
    """
    if table_name in _SPEC_BY_SOURCE_TABLE:
        raise ValueError(
            f"{table_name} は取り込み元へ書き戻す表なので、"
            "単独での取り込み直しはできません (import_source を使ってください)")
    func = _IMPORTERS.get(table_name)
    if func is None:
        raise ValueError(f"取り込みに対応していない表です: {table_name}")

    target = Path(path)
    with source_db.connect(target, read_only=True) as source:
        if not source.has_table(table_name):
            raise ValueError(f"{target.name} に {table_name} 表がありません")
        rows = _rows(source, table_name)
    # 読み終えてから手元を書く(共有を読んでいるあいだ手元を塞がない)
    with conn:
        count = func(conn, rows)
    debug_log(f"importer.import_master_table: {table_name} {count} 件 ({target})")
    return count


def import_all(conn: sqlite3.Connection, paths: list[str | Path], *,
               force: bool = False) -> list[ImportResult]:
    """複数の取り込み元 (保存用DB とマスタDB) をまとめて取り込む。"""
    # 未送信の確かめは**どの取り込み元でも**行う。1つ目を取り込んだあと、
    # 2つ目を読んでいるあいだに登録が入ることがある。名簿だけの取り込み元
    # なら送信待ちは消えないので、数え直しは書き戻しのある表のときだけ
    # (``import_source``)
    return [import_source(conn, path, force=force) for path in paths]


# ---------------------------------------------------------------------------
# CSV からの取り込み (マスタDB が手元に無いとき用)
# ---------------------------------------------------------------------------
def import_members_csv(conn: sqlite3.Connection, csv_path: str | Path) -> int:
    """班員名簿を CSV から取り込む。

    マスタ DB が手元に無い場合の代替手段。ヘッダ行に
    ``管理番号,苗字,班,名前,読み,担当ライン`` を含むこと。
    列順が VBA と同じであればヘッダ名が違っても位置で取り込む。
    """
    import csv as _csv

    path = Path(csv_path)
    expected = ["管理番号", "苗字", "班", "名前", "読み", "担当ライン"]

    # Excel が書き出す CSV は BOM 付き UTF-8 か CP932 のことが多い
    text: Optional[str] = None
    for encoding in ("utf-8-sig", "cp932", "utf-8"):
        try:
            text = path.read_text(encoding=encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError(f"文字コードを判別できませんでした: {path}")

    rows = list(_csv.reader(text.splitlines()))
    if not rows:
        return 0

    header = [c.strip() for c in rows[0]]
    has_header = any(col in header for col in expected)
    body = rows[1:] if has_header else rows
    # ヘッダがあれば名前で、無ければ VBA と同じ列位置で読む
    index = {name: header.index(name) for name in expected if name in header}

    imported = 0
    with conn:
        conn.execute(f'DELETE FROM "{config.TABLE_MEMBER}"')
        for row in body:
            if not any(cell.strip() for cell in row):
                continue

            def cell(name: str, position: int, _row=row) -> str:
                pos = index.get(name, position)
                return db.sanitize(_row[pos]) if pos < len(_row) else ""

            code = cell("管理番号", 0)
            if not code:
                continue
            conn.execute(
                f'INSERT OR REPLACE INTO "{config.TABLE_MEMBER}" '
                '("管理番号","苗字","班","名前","読み","担当ライン") VALUES (?,?,?,?,?,?)',
                (
                    code,
                    cell("苗字", 1),
                    cell("班", 2),
                    cell("名前", 3),
                    cell("読み", 4),
                    cell("担当ライン", 5),
                ),
            )
            imported += 1

    debug_log(f"importer.import_members_csv: {imported} 件を取り込み ({path})")
    return imported
