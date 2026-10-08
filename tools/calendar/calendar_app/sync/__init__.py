"""Access への同期に関するモジュール群。

* ``specs``          : 書き戻し対象テーブルの定義 (dbkit.outbox_sync.WriteBackSpec)
* ``deletes``         : 削除の転送 (dbkit.outbox_sync ではカバーしない部分)
* ``business_rules``  : 休みの二重登録防止など、カレンダー固有の業務ルール
* ``autosync``        : 送信・受信を短い間隔で回す自動同期の本体
"""

from __future__ import annotations

import sqlite3

from ..dbkit import outbox_sync
from . import specs as _specs
from .deletes import pending_delete_count

__all__ = ["total_pending_count", "reconcile_after_reimport"]


def total_pending_count(conn: sqlite3.Connection) -> int:
    """Access へ未反映の件数 (未送信の追加 + 未転送の削除)。

    ``repository.Repository.pending_sync_count`` / ``autosync`` /
    ``importer`` の 3 箇所から同じ定義を使うための共通実装。
    """
    unsent = outbox_sync.unsent_tables(conn, _specs.WRITE_BACK_SPECS)
    return sum(unsent.values()) + pending_delete_count(conn)


def reconcile_after_reimport(conn: sqlite3.Connection,
                             specs=None) -> None:
    """Access からの総入れ替え取り込みの直後に呼ぶ。

    取り込みは対象テーブルを ``DELETE`` してから Access の内容で作り直すため、
    取り込み後にそのテーブルにある行は「すべて Access に存在する」ことが
    保証されている。dbkit.outbox_sync の送信記録をそれに合わせて更新し
    (=すべて送信済み扱いにする) ことで、次の送信サイクルが
    (Access から取り込んだばかりの行を) 二重送信するのを防ぐ。

    やらないとどうなるか: 例えば送信記録だけを削除して済ませると、
    次の送信サイクルはテーブルの全行を「未送信」と誤認し、
    Access に既にある内容をもう一度 INSERT して重複を作ってしまう。

    **入れ替えた表だけを揃える**(``specs``)。名簿だけの取り込み元
    (マスタDB)を取り込んだときにまで全部の表を「送信済み」にすると、
    保存用DBとマスタDBを続けて取り込むあいだに入った登録が、
    **送っていないのに送信済み**になって二度と送られなかった。
    """
    for spec in (_specs.WRITE_BACK_SPECS if specs is None else specs):
        rows = conn.execute(f'SELECT "{spec.key_column}" FROM "{spec.sqlite_table}"')
        ids = [row[0] for row in rows]
        if ids:
            outbox_sync.mark_synced(conn, spec, ids)
