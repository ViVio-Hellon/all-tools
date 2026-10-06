"""管理者パスワード ── 現場で変えられるようにする

【何のためのものか】
過去データの呼び出しや参照パスの変更を**誤って押されない**ための関門
です(VBA ``AuthenticateAdmin`` / ``chkAdminMode`` から引き継いだ役目)。
本来の意味でのアクセス制御ではありません ── その端末に触れる人は
ファイルを直接開けます。

【なぜ変えられるようにするのか】
これまでは `config.py` の値だけで、**運用では変えられませんでした。**
変えられないパスワードは、実質「変えない」と同じです。人が入れ替わっても
直せず、結局みんなが同じ値を知っている状態が続きます。

【平文で持たない】
配布はフォルダごとコピーなので、設定ファイルは**そのまま持ち出せます**。
関門とはいえ平文で置く理由が無いので、PBKDF2 で撹拌して持ちます。
「盗まれても困らない」ためではなく、**ついでに漏れない**ため。

【変えていないときは、これまでどおり】
一度も変えていない端末では `config.SETTINGS.admin_password` がそのまま
通ります。入れ替えただけで認証が通らなくなる、を作りません。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass
from typing import Optional

from . import user_settings
from .config import KEY_ADMIN_PASSWORD, SETTINGS
from .logging_setup import get_logger

log = get_logger("admin_password")

# 撹拌の仕様。`pbkdf2$<繰り返し>$<塩>$<結果>` の形で1つの文字列に畳む
SCHEME = "pbkdf2"
ITERATIONS = 200_000
SALT_BYTES = 16

# 短すぎるものは断る。**関門なので厳しくはしない** ── 長さの規則を
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


# ==================================================================
# 撹拌
# ==================================================================
def _encode(password: str, salt: bytes, iterations: int = ITERATIONS) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 salt, iterations)
    return "$".join([SCHEME, str(iterations),
                     base64.b64encode(salt).decode("ascii"),
                     base64.b64encode(digest).decode("ascii")])


def _matches(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt, _digest = stored.split("$")
        if scheme != SCHEME:
            return False
        expected = _encode(password, base64.b64decode(salt), int(iterations))
    except (ValueError, TypeError):                  # 壊れた値
        return False
    return hmac.compare_digest(expected, stored)


# ==================================================================
# 使う
# ==================================================================
def _stored() -> Optional[str]:
    value = user_settings.get(KEY_ADMIN_PASSWORD)
    return value if isinstance(value, str) and value.strip() else None


def is_custom() -> bool:
    """この端末で変えてあるか。設定画面に出す(値そのものは出さない)。"""
    return _stored() is not None


def verify(password: str) -> bool:
    """合っているか。**照合はここでしかしない。**"""
    stored = _stored()
    if stored is not None:
        return _matches(str(password), stored)
    # 一度も変えていない端末。これまでどおり既定の値で通す。
    # `compare_digest` にはバイト列を渡す(非ASCIIで TypeError になる)
    return hmac.compare_digest(str(password).encode("utf-8"),
                               SETTINGS.admin_password.encode("utf-8"))


def new_password_problem(new: str, confirm: str) -> str:
    """新しいパスワードとして使えるか。**使えなければ理由**、使えれば空。

    変えるときの決まりを1か所にするためのものです。
    """
    new = str(new)
    if len(new) < MIN_LENGTH:
        return f"新しいパスワードは{MIN_LENGTH}文字以上にしてください。"
    if new != str(confirm):
        return "確認用と一致しません。"
    return ""


def hashed(new: str) -> str:
    """撹拌した値。**保存するのはこれだけ**(値そのものは書かない)。"""
    return _encode(str(new), os.urandom(SALT_BYTES))


def change(current: str, new: str, confirm: str) -> Result:
    """変える。**いまのパスワードを知っている人だけ。**

    肩越しに見ていた人が勝手に変えられる、を作らないための確認。
    """
    if not verify(current):
        # **何が違うのかは言わない。** 「そのパスワードは存在しない」等を
        # 返すと、総当たりの手がかりになる
        log.warning("管理者パスワードの変更に失敗しました(いまの値が違う)")
        return Result(False, "いまのパスワードが違います。", REFUSE_WRONG)
    new = str(new)
    if len(new) < MIN_LENGTH:
        return Result(False,
                      f"新しいパスワードは{MIN_LENGTH}文字以上にしてください。",
                      REFUSE_TOO_SHORT)
    if new != str(confirm):
        return Result(False, "確認用と一致しません。", REFUSE_MISMATCH)
    if verify(new):
        return Result(False, "いまと同じパスワードです。", REFUSE_SAME)

    user_settings.save(KEY_ADMIN_PASSWORD, hashed(new))
    log.info("管理者パスワードを変更しました")
    return Result(True, "管理者パスワードを変えました。")


def reset(current: str) -> Result:
    """既定に戻す。

    忘れたときの逃げ道は**設定ファイルを直すこと** ── `user_config.json`
    の `admin_password` の行を消せば既定に戻ります。画面から「忘れた」で
    戻せるようにすると、確認そのものが意味を失います。
    """
    if not verify(current):
        return Result(False, "いまのパスワードが違います。", REFUSE_WRONG)
    user_settings.save(KEY_ADMIN_PASSWORD, "")
    log.info("管理者パスワードを既定に戻しました")
    # 配布設定(`distribution`)に入れてあれば、戻る先はそちら
    if user_settings.origin(KEY_ADMIN_PASSWORD) == user_settings.ORIGIN_SITE:
        return Result(True, "配布設定のパスワードに戻しました。")
    return Result(True, "既定のパスワードに戻しました。")
