"""管理者パスワード ── 端末の設定を勝手に変えられないようにする

【何のためのものか】
VBA 版は**どの端末からでも、誰でもライン設定を変えられました**。
上部のボタンを押して一覧から選ぶだけです。ライン設定は
「この端末に何を見せるか」を決めるもので、変えられると

* コイルの端末が全ラインを表示する(見えてはいけないものが見える)
* 作業長の端末がコイルだけになる(見えるはずのものが消える)

という形で、**その端末の人には何も起きていないように見えたまま**
表示だけがずれます。気づくのは「あの連絡が来ていない」と言われた
ときなので、押し間違いの1回が後まで残ります。

参照パス(取り込み元の置き場所)も同じです。変えられると、
その端末の入力だけが別の場所へ送られ、他のラインには永遠に届きません。

**どちらも「毎日使うもの」ではなく「最初に1度決めるもの」**なので、
関門を置いても現場の手数は増えません。

【これはアクセス制御ではありません】
誤って押されないための**UIガード**です。パスワードを知っている人なら
誰でも変えられますし、設定ファイルを直接書き換えれば通ります。
「その端末を誰が使えるか」を決めるものではありません。

【平文で持たない】
配布はフォルダごとコピーなので、設定ファイルは**そのまま持ち出せます**。
UIガードとはいえ平文で置く理由が無いので、PBKDF2 で撹拌して持ちます。
これは「盗まれても困らない」ためではなく、**ついでに漏れない**ため。

【変えていないときは、既定の値で通る】
一度も変えていない端末では、既定の値がそのまま通ります
(``default_secret``: 環境変数 → ``config.ADMIN_PASSWORD`` の順)。
更新を入れただけで誰も設定を直せなくなる、を作りません。

組み込みの値は README に書いてあるので、**配るなら1台で変えてから
「配布設定」で書き出すのがふつう**です(撹拌した値のまま配られます。
``calendar_app/distribution.py``)。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass
from typing import Optional

from . import config, settings as user_settings
from .logging_utils import get_logger

log = get_logger("admin_password")

#: 設定ファイルに入れる鍵。値は撹拌済みの文字列で、**平文は入らない**
KEY = "管理者パスワード"

#: 撹拌の仕様。``pbkdf2$<繰り返し>$<塩>$<結果>`` の形で1つの文字列に畳む
SCHEME = "pbkdf2"
ITERATIONS = 200_000
SALT_BYTES = 16

#: 短すぎるものは断る。**UIガードなので厳しくはしない** ── 長さの規則を
#: 増やすほど、現場は紙に書いて画面に貼る
MIN_LENGTH = 4

# 断りの種類。**文言から推し量らない**(設計 §1 の規則4)
REFUSE_NEED_PASSWORD = "need_password"   # 変えるにはパスワードが要る
REFUSE_WRONG = "wrong_password"          # いまのパスワードが違う
REFUSE_TOO_SHORT = "too_short"           # 新しいパスワードが短い
REFUSE_MISMATCH = "mismatch"             # 確認用と一致しない
REFUSE_SAME = "same"                     # 変わっていない

#: 守る設定と、断りに出す名前。**ここに1行足せば守る対象が増える**
PROTECTED_LABELS = {
    "line": "この端末のライン",
    "data_db_dir": "保存用DBのフォルダ",
    "master_db_dir": "マスタDBのフォルダ",
    # 他の端末のラインを前もって決める(端末一覧の「次のライン」)。
    # **目の前に無い端末を変えるので、むしろこちらのほうが危ない**
    "terminal_line": "端末のライン予約",
    # 書き出す・消す・読み込み直す(``distribution.py``)。
    # 配った**全部の端末**に効くので、1台の設定より重い
    "distribution": "配布設定",
    # どの端末(ログインID・PC名)がどのラインを使えるかの表
    # (``access_control.py``)。**行を足す・直す・消すのどれも**
    "access": "アクセス権限",
}


@dataclass
class Result:
    """変えられたか。駄目なら**理由と種別**。"""

    ok: bool = True
    message: str = ""
    reason: str = ""


# ---------------------------------------------------------------------------
# 撹拌
# ---------------------------------------------------------------------------
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
    except (ValueError, TypeError):               # 壊れた値
        return False
    return hmac.compare_digest(expected, stored)


# ---------------------------------------------------------------------------
# 使う
# ---------------------------------------------------------------------------
def _stored() -> Optional[str]:
    value = user_settings.get(KEY, "")
    return value if isinstance(value, str) and value.strip() else None


def is_custom() -> bool:
    """この端末で変えてあるか。設定画面に出す(**値そのものは出さない**)。"""
    return _stored() is not None


#: 既定の出どころ(断りの文言を変えるために見る)
SOURCE_ENV = "env"                    # 環境変数
SOURCE_BUILTIN = "builtin"            # config.ADMIN_PASSWORD(README に書いてある値)


def default_secret() -> tuple[str, str]:
    """端末で変えていないときに通る値と、その出どころ。

    組み込みの値は README に書いてあるので、**配るなら変えておくのが
    ふつう**です(1台で変えて「配布設定」で書き出す)。
    """
    if os.environ.get("CALENDAR_ADMIN_PASSWORD"):
        return config.ADMIN_PASSWORD, SOURCE_ENV
    return config.ADMIN_PASSWORD, SOURCE_BUILTIN


def verify(password: str) -> bool:
    """合っているか。**照合はここでしかしない。**"""
    stored = _stored()
    if stored is not None:
        return _matches(str(password), stored)
    # 一度も変えていない端末。既定の値で通す
    secret, _source = default_secret()
    if secret.startswith(SCHEME + "$"):
        return _matches(str(password), secret)
    return hmac.compare_digest(str(password).encode("utf-8"),
                               secret.encode("utf-8"))


def hash_for_distribution(password: str) -> str:
    """撹拌した値(配布設定に手で平文が書かれていたときに、端末で持つ形)。"""
    return _encode(str(password), os.urandom(SALT_BYTES))


def guard(changing: list[str], password: str) -> Optional[Result]:
    """**書く前に通す関門。** 変えるものがあるなら合っているか確かめる。

    ``changing`` は ``PROTECTED_LABELS`` の鍵(``line`` など)で、
    **本当に値が変わるものだけ**を渡す。同じ値で保存し直したときにまで
    パスワードを聞くと、現場は「押しても何も起きない」と受け取る。

    通ってよければ ``None`` を返す ── 呼ぶ側は ``if result is not None``
    だけ見ればよい。
    """
    if not changing:
        return None
    password = str(password or "")
    if verify(password):
        log.info("設定を変えます(管理者パスワード確認済み): %s",
                 " / ".join(changing))
        return None

    labels = [PROTECTED_LABELS.get(name, name) for name in changing]
    what = " と ".join(labels)

    # **「送っていない」と「違う」は言い分ける。**
    #
    # 総当たりの手がかりを与えないために黙る、という作り方もありますが、
    # これはアクセス制御ではなく押し間違い防止のUIガードです(冒頭)。
    # 値は同じ端末の設定ファイルにあり、隠して得るものがありません。
    # 一方、言い分けないと**同じ問いが何度も出るだけ**になり、
    # 打ち間違えたのか壊れているのかが利用者に分かりません。
    if password:
        log.warning("設定の変更を断りました(管理者パスワードが違う): %s", what)
        return Result(False, "管理者パスワードが違います。" + _hint(),
                      REFUSE_WRONG)

    log.info("管理者パスワードを求めました: %s", what)
    return Result(False, f"{what}を変えるには管理者パスワードが要ります。",
                  REFUSE_NEED_PASSWORD)


def _hint() -> str:
    """**行き止まりを作らない。** どうすれば先へ進めるかを添える。

    最初に設定する端末では、初期値を知らないまま同じ問いを繰り返すことに
    なります ── 実際にそうなったので足しました。この端末で変えてあるなら
    初期値の話は的外れなので、そのときは戻し方のほうを出します。
    """
    if is_custom():
        from . import distribution

        # 配布設定から読み込んだ値かもしれない。**README を指すと
        # 行き止まりに案内する**ので、配った人へ向ける
        applied = user_settings.get(distribution.KEY_APPLIED, None)
        if applied:
            return ("\nこの端末では、配布設定のパスワードか、この端末で変えた"
                    "パスワードです。配布した人(ツールの管理者)に確認してください。")
        return ("\nこの端末では初期値から変更されています。"
                "分からなくなったときは、settings.json の"
                f"「{KEY}」の行を消すと初期値に戻ります。")
    _secret, source = default_secret()
    # **README を指すのは、README に書いてある値が効いているときだけ。**
    if source == SOURCE_BUILTIN:
        return ("\nこの端末では初期値のままです。"
                "初期値は README「3-4-3 管理者パスワード」に書いてあります。")
    return "\nこの端末では、起動するときに決めたパスワードです(環境変数)。"


def change(current: str, new: str, confirm: str) -> Result:
    """変える。**いまのパスワードを知っている人だけ。**

    肩越しに見ていた人が勝手に変えられる、を作らないための確認です。
    """
    if not verify(current):
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

    user_settings.set_value(KEY, _encode(new, os.urandom(SALT_BYTES)))
    log.info("管理者パスワードを変更しました")
    return Result(True, "管理者パスワードを変えました。")


def reset(current: str) -> Result:
    """既定に戻す(``config.ADMIN_PASSWORD`` / 環境変数)。

    忘れたときの逃げ道は**設定ファイルの行を消すこと**です ──
    ``settings.json`` の ``管理者パスワード`` を消せば既定に戻ります。
    画面から「忘れた」で戻せるようにすると、確認そのものが意味を失います。
    """
    if not verify(current):
        return Result(False, "いまのパスワードが違います。", REFUSE_WRONG)
    user_settings.set_value(KEY, "")
    log.info("管理者パスワードを既定に戻しました")
    return Result(True, "既定のパスワードに戻しました。")
