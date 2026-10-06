"""dbkit共通のロガー取得ヘルパー。

このパッケージは複数のVBA移行プロジェクトで使い回すことを前提にしている
ため、自分自身ではハンドラの追加やファイル出力先の決定を一切行わない。
呼び出し側アプリケーション(例: nippou)がルートロガーにハンドラを
設定していれば、標準の`logging`伝播(propagation)によって
`dbkit.*`名前空間のログも自動的に同じ出力先へ流れる。

アプリ側が何もロギング設定をしていない場合は、Pythonの`logging`の
既定動作(ハンドラ未設定時はWARNING以上がstderrに出る)に委ねる。
"""
from __future__ import annotations

import logging

PACKAGE_LOGGER_NAME = "dbkit"


def get_logger(component: str | None = None) -> logging.Logger:
    name = PACKAGE_LOGGER_NAME if component is None else f"{PACKAGE_LOGGER_NAME}.{component}"
    return logging.getLogger(name)
