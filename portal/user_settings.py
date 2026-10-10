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
#: 画面の色の既定(ライト / ダーク)。大きなタブの画面と、4ツールの「選んでいないとき」の色
KEY_THEME_DEFAULT = "theme_default"
THEMES = ("light", "dark")
#: ブラウザ版: 起動してから最初の心拍(画面が開いた合図)を待つ分。来なければ終わる
#: (ブラウザが開かなかったときに残り続けない)。デスクトップ版には効かない
KEY_FIRST_CONTACT_MIN = "first_contact_min"
FIRST_CONTACT_MIN_DEFAULT = 5
FIRST_CONTACT_MIN_RANGE = (1, 60)


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


def theme_default() -> str:
    """画面の色の既定。決めていない・知らない値ならライト(4ツールの既定と同じ)。"""
    value = get(KEY_THEME_DEFAULT)
    return value if value in THEMES else "light"


def first_contact_min() -> int:
    """最初の心拍を待つ分。決めていない・範囲の外・数でなければ既定(5分)。"""
    value = get(KEY_FIRST_CONTACT_MIN)
    low, high = FIRST_CONTACT_MIN_RANGE
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        return FIRST_CONTACT_MIN_DEFAULT
    return value


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
