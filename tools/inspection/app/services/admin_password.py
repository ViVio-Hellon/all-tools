"""管理者パスワード ── 現場で変えられるようにする(python-web-tools と同じ作り)

【何のためのものか】
配布設定(`distribution.py`)を書き出す・読み込み直す・消すときの確認です。
1台の操作で全ラインの設定が変わるので、**誤って押されない**ためのガードです。
誰がその端末を使えるかを決める、本来の意味でのアクセス制御ではありません。

【既定の値】
一度も変えていない端末では `INSPECTION_ADMIN_PASSWORD`(環境変数)、無ければ
python-web-tools(梱包資材総合ツール)と同じ既定値が通ります ── 同じ現場で
並べて使う道具なので、覚える値を増やさない。設定画面の「管理者パスワード」で
変えられ、変えた値は配布設定で各ラインへ配れます。

【平文で持たない】
配布はフォルダごとコピーなので、設定ファイルは**そのまま持ち出せます**。
UIガードとはいえ平文で置く理由が無いので、PBKDF2 で撹拌して持ちます。
これは「盗まれても困らない」ためではなく、**ついでに漏れない**ため。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass
from typing import Any, Optional

# 設定に入れる鍵。値は撹拌済みの文字列で、平文は入らない
KEY = "admin_password"

# 一度も変えていない端末で通る値(python-web-tools の config.ADMIN_PASSWORD と同じ)
DEFAULT = os.environ.get("INSPECTION_ADMIN_PASSWORD", "nisk")

# 撹拌の仕様。`pbkdf2$<繰り返し>$<塩>$<結果>` の形で1つの文字列に畳む
SCHEME = "pbkdf2"
ITERATIONS = 200_000
SALT_BYTES = 16

# 短すぎるものは断る。**UIガードなので厳しくはしない** ── 長さの規則を
# 増やすほど、現場は紙に書いて画面に貼る
MIN_LENGTH = 4

# 断りの種類。**文言から推し量らない**
REFUSE_WRONG = "wrong_password"     # いまのパスワードが違う
REFUSE_TOO_SHORT = "too_short"      # 新しいパスワードが短い
REFUSE_MISMATCH = "mismatch"        # 確認用と一致しない
REFUSE_SAME = "same"                # 変わっていない


@dataclass
class Result:
    ok: bool = True
    message: str = ""
    reason: str = ""


def _encode(password: str, salt: bytes, iterations: int = ITERATIONS) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "$".join([SCHEME, str(iterations),
                     base64.b64encode(salt).decode("ascii"),
                     base64.b64encode(digest).decode("ascii")])


def _matches(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt, _digest = stored.split("$")
        if scheme != SCHEME:
            return False
        expected = _encode(password, base64.b64decode(salt), int(iterations))
    except (ValueError, TypeError):               # 壊れた値
        return False
    return hmac.compare_digest(expected, stored)


def looks_stored(value: Any) -> bool:
    """撹拌済みの値の形か(配布設定から読み込むときに確かめる)。"""
    return isinstance(value, str) and value.startswith(SCHEME + "$") and value.count("$") == 3


class AdminPassword:
    """`SettingsService`(利用者ごとの user_settings.json)に撹拌して持つ。"""

    def __init__(self, settings: Any, logger: Any, default: Optional[str] = None):
        self.settings = settings
        self.log = logger
        self.default = DEFAULT if default is None else default

    def _stored(self) -> Optional[str]:
        value = self.settings.get(KEY)
        return value if isinstance(value, str) and value.strip() else None

    def is_custom(self) -> bool:
        """この端末で変えてあるか。設定画面に出す(値そのものは出さない)。"""
        return self._stored() is not None

    def verify(self, password: str) -> bool:
        """合っているか。**照合はここでしかしない。**"""
        stored = self._stored()
        if stored is not None:
            return _matches(str(password), stored)
        return hmac.compare_digest(str(password).encode("utf-8"), self.default.encode("utf-8"))

    def change(self, current: str, new: str, confirm: str) -> Result:
        """変える。**いまのパスワードを知っている人だけ。**"""
        if not self.verify(current):
            # **何が違うのかは言わない。** 総当たりの手がかりにしない
            self.log.warning("管理者パスワードの変更に失敗しました(いまの値が違う)")
            return Result(False, "いまのパスワードが違います。", REFUSE_WRONG)
        new = str(new)
        if len(new) < MIN_LENGTH:
            return Result(False, f"新しいパスワードは{MIN_LENGTH}文字以上にしてください。", REFUSE_TOO_SHORT)
        if new != str(confirm):
            return Result(False, "確認用と一致しません。", REFUSE_MISMATCH)
        if self.verify(new):
            return Result(False, "いまと同じパスワードです。", REFUSE_SAME)
        self.settings.put(KEY, _encode(new, os.urandom(SALT_BYTES)))
        self.log.info("管理者パスワードを変更しました")
        return Result(True, "管理者パスワードを変えました。")

    def reset(self, current: str) -> Result:
        """既定に戻す。忘れたときの逃げ道は**設定ファイルの `admin_password` の行を消すこと**
        (画面から「忘れた」で戻せると、確認そのものが意味を失う)。"""
        if not self.verify(current):
            return Result(False, "いまのパスワードが違います。", REFUSE_WRONG)
        self.settings.put(KEY, None)
        self.log.info("管理者パスワードを既定に戻しました")
        return Result(True, "既定のパスワードに戻しました。")
