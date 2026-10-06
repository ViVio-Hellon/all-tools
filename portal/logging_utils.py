"""入口のログ(`<手元の領域>\\logs\\portal_YYYYMMDD.log`)

デスクトップ版では標準エラーにも出す(外枠が末尾を覚えていて、起動できないときに
画面へ出す)。ブラウザ版(pythonw)では標準エラーが無いので、ファイルだけ。
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

from . import app_config

_ready = False
FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
KEEP_DAYS = 30


def _setup() -> None:
    global _ready
    if _ready:
        return
    _ready = True
    root = logging.getLogger("portal")
    root.setLevel(logging.INFO)
    root.propagate = False
    try:
        logs = app_config.local_dir("logs")
        path = logs / f"portal_{time.strftime('%Y%m%d')}.log"
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(handler)
        _prune(logs)
    except OSError:
        pass
    if sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
        root.addHandler(stream)


def _prune(logs: Path) -> None:
    """古いログを消す(30日より前)。"""
    limit = time.time() - KEEP_DAYS * 86400
    for path in logs.glob("portal_*.log"):
        try:
            if path.stat().st_mtime < limit:
                path.unlink()
        except OSError:
            pass


def get_logger(name: str) -> logging.Logger:
    _setup()
    return logging.getLogger(f"portal.{name}")


def log_dir() -> Path:
    return app_config.local_root() / "logs"
