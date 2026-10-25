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
    """取り込み元へ未反映の件数を、**操作の数で**数える (VER4.2.10)。

        未送信の件数: 削除1回が「未送信 2件」と数えられます → 操作の数で数える

    削除1件は、取り込み元で「その行を消す」(``_pending_deletes``)と「削除履歴を足す」
    (削除履歴の送信待ち)の2つを送る。前は両方を足していたので、休み・連絡・削除の
    3操作で「未送信 4 件」と出ていた。いまは削除をまとめて1件と数える:

        休み・連絡の未送信 + max(消す予約, 削除履歴の未送信)

    (片方だけ送れた削除も1件。まだ送っていない登録を消した場合は、消す予約は無く
    削除履歴だけが残るので、やはり1件)

    ``repository.Repository.pending_sync_count`` / ``autosync`` /
    ``importer`` の 3 箇所から同じ定義を使うための共通実装。
    """
    unsent = outbox_sync.unsent_tables(conn, _specs.WRITE_BACK_SPECS)
    adds = unsent.get(_specs.DATA_SPEC.sqlite_table, 0)
    history = unsent.get(_specs.DEL_HISTORY_SPEC.sqlite_table, 0)
    return adds + max(pending_delete_count(conn), history)


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
