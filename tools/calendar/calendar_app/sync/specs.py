"""取り込み元へ書き戻す対象テーブルの定義 (dbkit.outbox_sync.WriteBackSpec)。

``repository.py`` (件数の確認) と ``sync/autosync.py`` (実際の送信) の
両方から参照するため、共通の置き場所にしている。

班員名簿 (``TABLE_MEMBER``) は Access → SQLite への読み取り専用テーブルなので
ここには含めない。
"""

from __future__ import annotations

from .. import config
from ..dbkit.outbox_sync import WriteBackSpec

#: sqlite_table は "_送信用" ビュー (db.py 参照) を指す。
#: outbox_sync は SELECT * した列をそのまま Access へ渡すため、
#: SQLite だけが持つ access_id 列を含めないようにするため。
DATA_SPEC = WriteBackSpec(
    sqlite_table=f"{config.TABLE_DATA}_送信用",
    source_table=config.TABLE_DATA,
    key_column="ID",
)
DEL_HISTORY_SPEC = WriteBackSpec(
    sqlite_table=f"{config.TABLE_DEL_HISTORY}_送信用",
    source_table=config.TABLE_DEL_HISTORY,
    key_column="ID",
)

#: SQLite → Access へ追記する対象。どちらも「増える一方」の append 専用テーブル
WRITE_BACK_SPECS: tuple[WriteBackSpec, ...] = (DATA_SPEC, DEL_HISTORY_SPEC)
