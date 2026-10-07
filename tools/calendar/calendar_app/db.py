"""SQLite の接続とスキーマ定義。

Access のテーブル (休み管理 / 削除履歴 / 班員名簿) をそのままの名前・列名で
SQLite 上に再現する。

同期の考え方
------------
**正式なデータは共有の取り込み元 (sqlite3) だけ**で、手元の SQLite は表示用の写しと
送信待ちの置き場を兼ねる (詳しくは ``sync/autosync.py`` を参照)。

Access への送信 (INSERT) は ``dbkit.outbox_sync`` が汎用的に面倒をみる。
そちらは「SQLite の業務テーブルに新しく増えた行を Access へ追記する」
ことだけを行うため、次の 1 種類だけこのモジュールで別途扱う。

* **削除の転送** — 休みの登録を削除したとき、SQLite 側からは行そのものが
  消えてしまうため、outbox_sync の「今あるテーブルを見る」方式では
  拾えない。そこで削除だけは専用の ``_pending_deletes`` に記録しておく
  (Access 側の ID が分かっていればそれで、無ければ自然キーで特定する)。
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from . import config
from .dbkit import outbox_sync, sqlite_toolkit

_SCHEMA = f"""
PRAGMA foreign_keys = ON;

-- 休み・連絡の本体 (Access: {config.TABLE_DATA})
CREATE TABLE IF NOT EXISTS "{config.TABLE_DATA}" (
    "ID"        INTEGER PRIMARY KEY AUTOINCREMENT,
    "access_id" INTEGER,          -- Access 側の ID (Python で新規作成した行は NULL)
    "日付"       TEXT NOT NULL,     -- yyyy/mm/dd
    "区分"       TEXT NOT NULL,
    "登録内容"    TEXT NOT NULL DEFAULT '',
    "識別コード"  TEXT NOT NULL DEFAULT '',
    "直"        TEXT NOT NULL DEFAULT '',
    "残業者"     TEXT NOT NULL DEFAULT '',
    "早出者"     TEXT NOT NULL DEFAULT '',
    "班"        TEXT NOT NULL DEFAULT '',
    "ライン"     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS "idx_data_date" ON "{config.TABLE_DATA}" ("日付");
CREATE INDEX IF NOT EXISTS "idx_data_access_id" ON "{config.TABLE_DATA}" ("access_id");

-- 削除履歴 (Access: {config.TABLE_DEL_HISTORY})
CREATE TABLE IF NOT EXISTS "{config.TABLE_DEL_HISTORY}" (
    "ID"        INTEGER PRIMARY KEY AUTOINCREMENT,
    "access_id" INTEGER,
    "削除日時"    TEXT NOT NULL,
    "削除実行者"  TEXT NOT NULL DEFAULT '',
    "対象日付"    TEXT NOT NULL,
    "区分"       TEXT NOT NULL DEFAULT '',
    "登録内容"    TEXT NOT NULL DEFAULT '',
    "識別コード"  TEXT NOT NULL DEFAULT '',
    "班"        TEXT NOT NULL DEFAULT '',
    "ライン"     TEXT NOT NULL DEFAULT ''
);

-- 作業者名簿 (Access マスタ: {config.TABLE_MEMBER}) 読み取り専用として取り込む
CREATE TABLE IF NOT EXISTS "{config.TABLE_MEMBER}" (
    "管理番号"    TEXT PRIMARY KEY,
    "苗字"       TEXT NOT NULL DEFAULT '',
    "班"        TEXT NOT NULL DEFAULT '',
    "名前"       TEXT NOT NULL DEFAULT '',
    "読み"       TEXT NOT NULL DEFAULT '',
    "担当ライン"  TEXT NOT NULL DEFAULT ''
);

-- アクセス権限 (梱包資材マスタ。他のツールと共用) 読み取り専用として取り込む。
-- このツールが読むのは「権限」がライン名の行だけ(calendar_app/access_control.py)
CREATE TABLE IF NOT EXISTS "アクセス権限" (
    "管理番号"    INTEGER PRIMARY KEY,
    "ログインID"  TEXT NOT NULL DEFAULT '',
    "PC名"       TEXT NOT NULL DEFAULT '',
    "権限"       TEXT NOT NULL,
    "有効"       INTEGER NOT NULL DEFAULT 1,
    "備考"       TEXT NOT NULL DEFAULT ''
);

-- dbkit.outbox_sync に見せる「送信用の写し」。
-- outbox_sync は SELECT * した列をそのまま Access へ INSERT するため、
-- SQLite だけが持つ "access_id" 列 (Access には存在しない) を
-- 含めてしまうと INSERT が「そんな列は無い」で失敗する。
-- そのため access_id を除いたビューを経由させる
-- (outbox_sync は読み取りしかしないためビューで問題ない)。
CREATE VIEW IF NOT EXISTS "{config.TABLE_DATA}_送信用" AS
SELECT "ID","日付","区分","登録内容","識別コード","直","残業者","早出者","班","ライン"
FROM "{config.TABLE_DATA}";

CREATE VIEW IF NOT EXISTS "{config.TABLE_DEL_HISTORY}_送信用" AS
SELECT "ID","削除日時","削除実行者","対象日付","区分","登録内容","識別コード","班","ライン"
FROM "{config.TABLE_DEL_HISTORY}";

-- Access への削除転送待ち (INSERT は dbkit.outbox_sync が扱うため、
-- ここには「削除」だけを記録する。詳しくはモジュール docstring 参照)
CREATE TABLE IF NOT EXISTS "_pending_deletes" (
    "seq"          INTEGER PRIMARY KEY AUTOINCREMENT,
    "table_name"   TEXT NOT NULL,
    "access_id"    INTEGER,
    "natural_key"  TEXT NOT NULL DEFAULT '{{}}',
    "created_at"   TEXT NOT NULL,
    "sent_at"      TEXT
);
CREATE INDEX IF NOT EXISTS "idx_pending_deletes_unsent"
    ON "_pending_deletes" ("sent_at");

-- 取り込み元やバージョンなどのメタ情報
CREATE TABLE IF NOT EXISTS "_sync_meta" (
    "key"   TEXT PRIMARY KEY,
    "value" TEXT NOT NULL DEFAULT ''
);
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """SQLite へ接続し、スキーマを用意した接続を返す。

    ``path`` に ``:memory:`` を渡すとインメモリ DB になる (テスト用)。
    """
    target = str(path) if path is not None else str(config.sqlite_path())
    conn = sqlite3.connect(target)
    try:
        conn.row_factory = sqlite3.Row
        # 複数 PC から共有フォルダ上の DB を触る運用に備え、待機時間を設ける
        # (dbkit.sqlite_toolkit の Python 側リトライは、これでも失敗した場合の
        #  二段目の備え。詳しくは sqlite_toolkit.execute_with_retry を参照)
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.executescript(_SCHEMA)
        # dbkit.outbox_sync 自身の管理テーブルも、他のスキーマと同じタイミングで
        # 用意しておく (queue_delete 等が先に参照しても失敗しないように)。
        outbox_sync.ensure_sync_table(conn)
        conn.commit()
    except BaseException:
        # **開いたまま投げない。** Windows では開いているファイルをよけられない
        # (壊れた作業用DBを脇へよける ``set_aside_broken`` が続く)
        conn.close()
        raise
    return conn


#: 作業用DBの付き添い(よけるときは一緒に)
SIDECARS = ("-wal", "-shm", "-journal")


def is_broken_db_error(exc: BaseException) -> bool:
    """作業用DBのファイルが壊れている(SQLite のファイルではない・中が傷んでいる)か。

    「使用中(locked)」「開けない」は壊れていない(``OperationalError``)ので数えない。
    """
    return isinstance(exc, sqlite3.DatabaseError) and not isinstance(exc, sqlite3.OperationalError)


def set_aside_broken(path: str | Path | None = None) -> Path:
    """壊れた作業用DBを**消さずに**脇へよける(``<名前>.broken-<日時>``)。よけた先を返す。

    よけたあとは空の作業用DBで動き出せる(起動ごと止まらない。中身は取り込み元から
    取り込み直す)。名前を変えられないとき(Windows で、ほかのプログラムが開いている)は
    OSError のまま投げる。
    """
    import gc
    import os
    import time

    gc.collect()  # 失敗した接続が残っていれば閉じる(開いたままでは名前を変えられない)
    source = Path(path) if path is not None else Path(config.sqlite_path())
    target = source.with_name(f"{source.name}.broken-{time.strftime('%Y%m%d-%H%M%S')}")
    os.replace(source, target)
    for suffix in SIDECARS:
        side = Path(str(source) + suffix)
        if side.exists():
            os.replace(side, Path(str(target) + suffix))
    return target


def now_string() -> str:
    """DB 登録用の日時文字列 (VBA: NowDBString と同じ書式)。

    dbkit.sqlite_toolkit.now_db_string() は ISO 形式 ("YYYY-MM-DD HH:MM:SS")
    を返すが、こちらは意図的に採用していない。Access への書き出し時に
    VBA 版と同じ "yyyy/mm/dd hh:nn:ss" 形式で比較・表示できるようにするため、
    このプロジェクトでは日付書式を業務側 (config.DATETIME_FORMAT) に
    合わせて保つ。
    """
    return _dt.datetime.now().strftime(config.DATETIME_FORMAT)


def sanitize(value: Any) -> str:
    """DB へ書き込むテキストを整える (VBA: SanitizeForDB)。

    実体は dbkit.sqlite_toolkit.sanitize_for_db。文字列以外の値
    (None・数値など) を受けられるよう、ここで str 化してから渡す。
    """
    if value is None:
        return ""
    return sqlite_toolkit.sanitize_for_db(str(value))


# ---------------------------------------------------------------------------
# 削除の転送待ち (Access への INSERT は dbkit.outbox_sync が別途担当する)
# ---------------------------------------------------------------------------
def queue_pending_delete(
    conn: sqlite3.Connection,
    table_name: str,
    *,
    access_id: int | None,
    natural_key: dict[str, Any],
) -> None:
    """削除を Access へ転送するための予約を 1 件記録する。

    呼び出し元と同じトランザクション内 (行の DELETE 実行前後) で
    呼ぶことで、「消したのに転送記録が無い」状態を防ぐ。
    """
    conn.execute(
        'INSERT INTO "_pending_deletes" '
        '("table_name","access_id","natural_key","created_at") VALUES (?,?,?,?)',
        (
            table_name,
            access_id,
            json.dumps(natural_key, ensure_ascii=False),
            now_string(),
        ),
    )


def pending_deletes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """まだ Access へ転送していない削除を古い順に返す。"""
    return list(
        conn.execute(
            'SELECT * FROM "_pending_deletes" WHERE "sent_at" IS NULL ORDER BY "seq"'
        )
    )


def mark_deletes_sent(conn: sqlite3.Connection, seqs: Iterable[int]) -> None:
    """指定した削除予約を「Access へ転送済み」にする。"""
    stamp = now_string()
    conn.executemany(
        'UPDATE "_pending_deletes" SET "sent_at"=? WHERE "seq"=?',
        [(stamp, s) for s in seqs],
    )
    conn.commit()


def discard_pending_deletes(conn: sqlite3.Connection, table_name: str) -> None:
    """指定テーブルの削除予約をすべて捨てる (再取り込みで内容が総入れ替えされた後)。

    Access から総入れ替えで取り込み直した直後は、それ以前に溜まっていた
    削除予約は意味を失う (取り込んだ内容が既に「削除された結果」を
    反映しているか、あるいは削除対象そのものが Access に残っていて、
    次の削除操作で改めて予約されるかのどちらかになるため)。
    """
    conn.execute('DELETE FROM "_pending_deletes" WHERE "table_name"=?', (table_name,))


def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute('SELECT "value" FROM "_sync_meta" WHERE "key"=?', (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        'INSERT INTO "_sync_meta" ("key","value") VALUES (?,?) '
        'ON CONFLICT("key") DO UPDATE SET "value"=excluded."value"',
        (key, value),
    )
