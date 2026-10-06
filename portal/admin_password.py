"""管理者パスワード(大設定の書き換えに使う)

作りは python-web-tools・各ツールの `admin_password` と同じ:

- **平文で持たない。** PBKDF2(SHA-256・20万回・塩16バイト)で撹拌して
  `pbkdf2$<回数>$<塩>$<結果>` の形で `user_settings.json` に入れる
- 一度も変えていない端末は、ほかのツールと同じ既定値(`ALLTOOLS_ADMIN_PASSWORD` で変えられる)
- 変えるには**いまのパスワード**が要る。4文字以上
- 忘れたら `user_settings.json` の `admin_password` を消すと既定に戻る

アクセス制御ではなく、**うっかり書き換えないための UI の守り**(各ツールと同じ位置づけ)。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Optional

from . import user_settings

DEFAULT_PASSWORD = os.environ.get("ALLTOOLS_ADMIN_PASSWORD", "nisk")

SCHEME = "pbkdf2"
ITERATIONS = 200_000
SALT_BYTES = 16
MIN_LENGTH = 4

#: 認証が切れるまでの、何もしない時間(秒)。看板の `admin_lock` と同じ 30 分
IDLE_LOCK_SEC = 30 * 60


@dataclass
class Result:
    ok: bool = True
    message: str = ""
    reason: str = ""


def _encode(password: str, salt: bytes, iterations: int = ITERATIONS) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "$".join([SCHEME, str(iterations), base64.b64encode(salt).decode("ascii"),
                     base64.b64encode(digest).decode("ascii")])


def _matches(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt, _digest = stored.split("$")
        if scheme != SCHEME:
            return False
        expected = _encode(password, base64.b64decode(salt), int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(expected, stored)


def hash_text(password: str) -> str:
    """平文を、端末に持つ形(`pbkdf2$…`)にする(配布設定に手で平文が書かれていたとき)。"""
    return _encode(str(password), secrets.token_bytes(SALT_BYTES))


def _stored() -> Optional[str]:
    value = user_settings.get(user_settings.KEY_ADMIN_PASSWORD)
    return value if isinstance(value, str) and value.strip() else None


def is_custom() -> bool:
    return _stored() is not None


def verify(password: str) -> bool:
    stored = _stored()
    if stored is not None:
        return _matches(str(password), stored)
    return hmac.compare_digest(str(password).encode("utf-8"), DEFAULT_PASSWORD.encode("utf-8"))


def change(current: str, new: str, confirm: str) -> Result:
    if not verify(current):
        return Result(False, "いまのパスワードが違います。", "wrong_password")
    new = str(new)
    if len(new) < MIN_LENGTH:
        return Result(False, f"新しいパスワードは {MIN_LENGTH} 文字以上にしてください。", "too_short")
    if new != str(confirm):
        return Result(False, "確認のために入れたものと一致しません。", "mismatch")
    if verify(new) and new == str(current):
        return Result(False, "いまと同じパスワードです。", "same")
    user_settings.save(user_settings.KEY_ADMIN_PASSWORD, _encode(new, secrets.token_bytes(SALT_BYTES)))
    return Result(True, "管理者パスワードを変えました。")


# ------------------------------------------------------------------
# 認証の状態(このプロセスの中だけ。30分さわらなければ鍵が掛かる)
# ------------------------------------------------------------------
class Session:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._until = 0.0

    def unlock(self, password: str) -> bool:
        ok = verify(password)
        with self._lock:
            self._until = time.monotonic() + IDLE_LOCK_SEC if ok else 0.0
        return ok

    def lock(self) -> None:
        with self._lock:
            self._until = 0.0

    def is_open(self) -> bool:
        with self._lock:
            if time.monotonic() >= self._until:
                return False
            self._until = time.monotonic() + IDLE_LOCK_SEC   # 使えば延びる
            return True

    def peek(self) -> bool:
        """延ばさずに見る(画面の表示用)。"""
        with self._lock:
            return time.monotonic() < self._until


session = Session()
