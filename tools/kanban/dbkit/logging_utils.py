"""dbkit 内のモジュールが使う、依存を増やさないためのロガー取得。

各プロジェクトはアプリ本体側で独自のログ基盤(ログファイルの場所、
ライン名ごとのフォルダ分け等)を持っていることが多い。dbkit はどの
プロジェクトにもそのまま持ち込めることを優先し、標準の ``logging`` だけを
使う。プロジェクト側は ``logging.getLogger("dbkit")`` にハンドラを追加
すれば、dbkit 配下のすべてのモジュールのログを自分の出力先へ合流できる
(例: ``kanban.applog`` は初期化時にこれを行っている)。
"""

from __future__ import annotations

import logging

#: dbkit 配下のロガーの共通ルート名前空間
ROOT_LOGGER_NAME = "dbkit"


def get_logger(name: str) -> logging.Logger:
    """``dbkit.<name>`` という名前のロガーを返す。"""
    return logging.getLogger(f"{ROOT_LOGGER_NAME}.{name}")
