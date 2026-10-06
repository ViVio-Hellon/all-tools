"""dbkit: 複数のVBA→Python移行プロジェクトで使い回す汎用DB接続レイヤー。

プロジェクト固有のテーブル名・スキーマには一切依存しない、以下3つの
部品で構成される:

- :mod:`dbkit.sqlite_toolkit` -- SQLite側のロック競合リトライ・簡易CRUD
- :mod:`dbkit.access_odbc`    -- Access(.accdb)への直接接続(Windows専用、
  ctypes + odbc32.dll。pyodbc等の追加インストールは不要)
- :mod:`dbkit.outbox_sync`    -- SQLite→Access書き戻し(送信ID+一意
  インデックスによる二重送信防止つき)

呼び出し側アプリケーションは、テーブル名やスキーマ適用など業務固有の
部分だけを自分のプロジェクトに残し、DB接続まわりの土台はここに委ねる。
"""
from __future__ import annotations

from .access_odbc import (
    AccessConnection,
    AccessError,
    connect as access_connect,
    diagnose as access_diagnose,
    find_access_driver,
    installed_drivers,
    is_available as is_access_available,
    is_already_exists_error,
    is_duplicate_error,
    is_lock_error as is_access_lock_error,
    quote as access_quote,
    quote_identifier as access_quote_identifier,
)
from .outbox_sync import (
    WriteBackResult,
    WriteBackSpec,
    claim_rows,
    ensure_op_id_column,
    ensure_sync_table,
    mark_synced,
    pending_rows,
    release_claim,
    unsent_tables,
    write_back,
)
from .sqlite_toolkit import (
    ExecResult,
    UpdateResult,
    enable_wal,
    execute_with_retry,
    fetch_all,
    fetch_one,
    insert_record,
    is_lock_error as is_sqlite_lock_error,
    now_db_string,
    sanitize_for_db,
    update_record,
)

__all__ = [
    # access_odbc
    "AccessConnection",
    "AccessError",
    "access_connect",
    "access_diagnose",
    "find_access_driver",
    "installed_drivers",
    "is_access_available",
    "is_already_exists_error",
    "is_duplicate_error",
    "is_access_lock_error",
    "access_quote",
    "access_quote_identifier",
    # outbox_sync
    "WriteBackResult",
    "WriteBackSpec",
    "claim_rows",
    "ensure_op_id_column",
    "ensure_sync_table",
    "mark_synced",
    "pending_rows",
    "release_claim",
    "unsent_tables",
    "write_back",
    # sqlite_toolkit
    "ExecResult",
    "UpdateResult",
    "enable_wal",
    "execute_with_retry",
    "fetch_all",
    "fetch_one",
    "insert_record",
    "is_sqlite_lock_error",
    "now_db_string",
    "sanitize_for_db",
    "update_record",
]
