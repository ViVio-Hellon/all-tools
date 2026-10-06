"""起動の記録(launcher.log / guard.log)を、試験のあいだは一時フォルダへ寄せる。

起動基盤のロガーは**読み込んだ時点で**この端末の logs
(`~/.local/share/NippouTool/logs`)へつながります(`logging_setup.get_launch_logger`
── 起動できないときに読むものなので、設定したログの置き場所にも従いません)。
試験は `launch_guard` / `start_app` を読み込むだけで本物のフォルダにつながり、
多重起動の試験の記録(「印に載っていないインスタンスを止めます」など)を
**本物の guard.log に書き足していました。** 試験は自分の一時フォルダの外へ
書かない、が約束です。

使い方: 起動基盤を読み込む試験のモジュールで、読み込んだ**あと**に1度呼ぶ。

    import launch_guard
    import start_app
    from tests._launch_logs import keep_in_temp
    keep_in_temp()
"""
from __future__ import annotations

import atexit
import logging
import shutil
import sys
import tempfile
from pathlib import Path

_DIR: Path | None = None


def keep_in_temp() -> Path:
    """起動の記録の書き先を、この試験の実行かぎりの一時フォルダにする。"""
    global _DIR
    from nippou import logging_setup

    if _DIR is None:
        _DIR = Path(tempfile.mkdtemp(prefix="nippou-launch-logs-"))
        atexit.register(shutil.rmtree, _DIR, True)
    # これから作られるぶん(読み込み済みのモジュールが持つ logging_setup に効く)
    logging_setup._launch_log_path = lambda filename: _DIR / filename
    # もう作られたぶん: 本物のフォルダへのハンドラを外す
    for name, logger in list(logging.Logger.manager.loggerDict.items()):
        if not name.startswith("nippou.launch") or not isinstance(logger, logging.Logger):
            continue
        for handler in list(logger.handlers):
            path = getattr(handler, "_nippou_log_path", None)
            if path is not None and Path(path).parent != _DIR:
                logger.removeHandler(handler)
                handler.close()
    # `start_app.log()` は初めて呼ばれたときに作る ── いまの書き先で作り直しておく
    start_app = sys.modules.get("start_app")
    if start_app is not None and hasattr(start_app, "_log"):
        start_app._log = None
        start_app.log()
    return _DIR
