"""この端末・この利用者の設定(`<手元の領域>\\data\\user_settings.json`)

入っているもの: 共有の DB の置き場所(フォルダ・ファイル名)・管理者パスワードの撹拌値。
書くときは一時ファイルに書いてから入れ替える(書きかけで壊さない)。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Optional

from . import app_config

_lock = threading.Lock()

KEY_SHARED_DIR = "shared_db_dir"
KEY_SHARED_NAME = "shared_db_name"
KEY_ADMIN_PASSWORD = "admin_password"
KEY_DISTRIBUTION_APPLIED = "distribution_applied"


def path() -> Path:
    custom = os.environ.get("ALLTOOLS_SETTINGS_PATH", "").strip()
    if custom:
        return Path(custom)
    return app_config.local_root() / "data" / "user_settings.json"


def load() -> dict[str, Any]:
    try:
        data = json.loads(path().read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def get(key: str, default: Optional[Any] = None) -> Any:
    return load().get(key, default)


def save(key: str, value: Any) -> None:
    update({key: value})


def update(values: dict[str, Any]) -> None:
    with _lock:
        data = load()
        for key, value in values.items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
        target = path()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, target)
