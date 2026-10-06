"""dbkit 内で使う最小限のロガー取得口。

dbkit は特定プロジェクトから独立していることが前提のため、ここでは
標準の ``logging`` モジュールをそのまま使うだけにとどめる。
ライブラリ作法どおり既定では ``NullHandler`` を付けて何も出力しない
状態にしておき、実際にどこへ出すか (ファイル/画面) は呼び出し側の
アプリケーション (このプロジェクトでは ``calendar_app.logger``) が
``logging.getLogger("dbkit")`` にハンドラを足すことで決める。
"""

from __future__ import annotations

import logging

_ROOT_NAME = "dbkit"
logging.getLogger(_ROOT_NAME).addHandler(logging.NullHandler())


def get_logger(name: str) -> logging.Logger:
    """``dbkit.<name>`` という名前のロガーを返す。"""
    return logging.getLogger(f"{_ROOT_NAME}.{name}")
