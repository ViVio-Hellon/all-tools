"""この端末・この利用者の設定(`<手元の領域>\\data\\user_settings.json`)

入っているもの: 共有の DB の置き場所(フォルダ・ファイル名)・管理者パスワードの撹拌値。
書くときは一時ファイルに書いてから入れ替える(書きかけで壊さない)。

【読めないのと、無いのは違う】
ファイルが無い(一度も設定していない)ときは空。**ファイルはあるのに読めない**(ほかが入れ替えて
いる最中・ロック・権限・中身が壊れている)ときは `Unreadable`。以前はどちらも空として扱い、
書くとき(`update`)に**空に足した1項目だけで上書き**していた ── 共有の DB の置き場所・管理者
パスワード・配布設定の覚えが消え、起動時の配布設定の読み込みも「この端末に無い」と見て上書きしていた。
書くとき・決めて書くときは `load_checked`(読めなければ書かない)。画面に出すだけなら `load`(空)。
"""
from __future__ import annotations

import json
import os
import threading
import time
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


#: 読めなかったとき、読み直す回数と間(秒)。Windows では入れ替えの最中に一瞬読めないことがある
READ_TRIES = 3
READ_RETRY_SEC = 0.1


class Unreadable(RuntimeError):
    """設定ファイルはあるのに読めない(**無いのとは違う**)。読めないあいだは書かない。"""

    def __init__(self, target: Path, cause: Any) -> None:
        self.path = target
        self.cause = cause
        super().__init__(f"この端末の設定ファイルを読めません({target}: {cause})。"
                         "設定を書き換えずに止めました。少し待ってもう一度試してください"
                         "(続くときはファイルの中身・権限を確かめてください)")


def load_checked() -> dict[str, Any]:
    """設定の中身。**無ければ空、あるのに読めなければ `Unreadable`**(書く前・決めて書く前に使う)。"""
    target = path()
    last: Any = None
    for attempt in range(READ_TRIES):
        try:
            text = target.read_text(encoding="utf-8-sig")
        except FileNotFoundError as exc:
            # 「無い」は、親フォルダを一覧できて本当に無いときだけ(届かないのは読めないのと同じ)
            if app_config.confirmed_missing(target):
                return {}
            last = exc
            if attempt + 1 < READ_TRIES:
                time.sleep(READ_RETRY_SEC)
            continue
        except OSError as exc:                    # ロック中・権限など(一瞬のことがある)
            last = exc
            if attempt + 1 < READ_TRIES:
                time.sleep(READ_RETRY_SEC)
            continue
        except ValueError as exc:                 # 文字の符号が壊れている
            raise Unreadable(target, f"中身が壊れています: {exc}") from exc
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise Unreadable(target, f"中身が壊れています: {exc}") from exc
        if not isinstance(data, dict):
            raise Unreadable(target, "中身の形が違います")
        return data
    raise Unreadable(target, last)


def load() -> dict[str, Any]:
    """画面に出す・既定を決めるための読み。読めなければ空(**書くときは `load_checked`**)。"""
    try:
        return load_checked()
    except Unreadable:
        return {}


def problem() -> str:
    """設定ファイルを読めない理由。読める(か、無い)なら空。"""
    try:
        load_checked()
    except Unreadable as exc:
        return str(exc)
    return ""


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
    """書き換える。**ファイルを読めなければ書かない**(`Unreadable`)── 空に足して上書きしない。"""
    with _lock:
        data = load_checked()
        for key, value in values.items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
        target = path()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        replace(tmp, target)


#: 置き換えを断られたとき、やり直す回数と間(秒)。Windows では読んでいる最中のファイルを
#: 置き換えると一瞬 PermissionError になる
REPLACE_TRIES = 5
REPLACE_WAIT_SEC = 0.1


def replace(tmp: Path, target: Path) -> None:
    """一時ファイルで置き換える。**Windows で一瞬断られたら、少し待ってやり直す。**"""
    for attempt in range(REPLACE_TRIES):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            if attempt + 1 >= REPLACE_TRIES:
                raise
            time.sleep(REPLACE_WAIT_SEC)
