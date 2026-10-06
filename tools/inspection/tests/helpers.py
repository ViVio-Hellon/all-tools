"""テスト共通の準備(設定・ローカル領域・ログ)。

**本番のローカル領域を汚さない。** このモジュールを読み込んだ時点で
`INSPECTION_LOCAL_DIR` を一時フォルダへ向ける(`core.app_config.local_root`)。
"""
from __future__ import annotations

import atexit
import logging
import os
import shutil
import sys
import tempfile
from typing import Any

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)

if not os.environ.get("INSPECTION_LOCAL_DIR"):
    _LOCAL = tempfile.mkdtemp(prefix="isp_local_")
    os.environ["INSPECTION_LOCAL_DIR"] = _LOCAL
    atexit.register(shutil.rmtree, _LOCAL, True)
if not os.environ.get("INSPECTION_DISTRIBUTION_DIR"):
    # 配布設定もアプリのフォルダ(このリポジトリ)に書かせない
    _DIST_BASE = tempfile.mkdtemp(prefix="isp_dist_")
    os.environ["INSPECTION_DISTRIBUTION_DIR"] = os.path.join(_DIST_BASE, "配布設定")
    atexit.register(shutil.rmtree, _DIST_BASE, True)

from app.services.settings_service import ExcelConfig, InspectionConfig  # noqa: E402
from core import logging_utils  # noqa: E402

# 試験の出力にログを混ぜない(ファイルへは一時フォルダに書かれる)
logging_utils.silence_console()


def quiet_logger(name: str = "inspection.test") -> logging.Logger:
    logger = logging.getLogger(name)
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    return logger


def make_cfg(root_folder: str = "", **excel: Any) -> InspectionConfig:
    """業務設定を1つ作る。`excel` は ExcelConfig の項目を上書きする。"""
    return InspectionConfig(root_folder=root_folder, excel=ExcelConfig(**excel))


def make_tree(root: str, files) -> None:
    """files: [相対パス, ...] の空ファイルを作る。"""
    for rel in files:
        full = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb"):
            pass


def temp_dir() -> tempfile.TemporaryDirectory:
    return tempfile.TemporaryDirectory(prefix="isp_test_")
