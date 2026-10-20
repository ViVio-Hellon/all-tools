"""管理番号(看板のキー)のそろえ方。**ここ 1 か所で決める。**

同じ看板の管理番号が、場所によって ``1``(数値)・``1.0``(Access の Double)・``'1.0'``
(TEXT の列へ数値を書いたもの)・``'１'``(全角)・``' 1 '`` と違う形で届く。比べ方が
場所ごとに違うと、同じ看板を別物とみなして**行が倍になる・重なりを見逃す・未送信を
見落とす**(VER3.2.1 で直した「中身の入れ替えで表が倍になる」はこれだった)。

比べるときは必ずこの関数を通す。**DB へ書く値の形は変えない**(書き方は今のまま)。
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable

_INTEGRAL = re.compile(r"[+-]?\d+(?:\.0*)?")


def normalize(value: Any) -> str:
    """比べるための形。``1`` / ``1.0`` / ``'1.0'`` / ``'１'`` / ``' 1 '`` はどれも ``'1'``。

    全角は半角に(NFKC)、前後の空白は落とし、整数として書ける数は整数の書き方にそろえる。
    それ以外(英字を含む番号など)は空白を落とした文字のまま。``None`` は空。
    """
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = unicodedata.normalize("NFKC", "" if value is None else str(value)).strip()
    if _INTEGRAL.fullmatch(text):
        text = str(int(float(text)))
    return text


def same(a: Any, b: Any) -> bool:
    """同じ管理番号か。:func:`normalize` が同じか、数として等しければ同じ(``'1.50'`` と ``1.5``)。"""
    left, right = normalize(a), normalize(b)
    if left == right:
        return True
    try:
        return float(left) == float(right)
    except ValueError:
        return False


def pair(incoming: "list[str]", local: "Iterable[str]") -> "tuple[dict[str, str], set[str]]":
    """共有DBから読んだ管理番号(``incoming``)を、手元の管理番号(``local``)に結び付ける(VER3.5.7)。

    取り込みは手元の行を**手元の管理番号のまま**持ち続けます(共有DBの書き方が ``1`` → ``1.0`` →
    ``'１'`` と変わっても、別の看板とみなして消さない)。結び方:

    1. 文字がそのまま同じものはそのまま
    2. 残ったものは :func:`normalize` が同じもの同士で、**1 対 1 のときだけ**結ぶ
    3. 1 対 1 でない(``'1.0'`` と ``'１'`` が両方届いた、など)ものは結ばず、その番号を
       ``ambiguous`` に入れる(推し量って混ぜない。呼び手は未送信の行を消さずに残す)

    返すのは(届いた番号 → 手元の番号。文字が違うものだけ, 結べなかった番号の :func:`normalize`)。
    """
    local_keys = list(local)
    local_set = set(local_keys)
    incoming_set = set(incoming)
    left_in: dict[str, list[str]] = {}
    for raw in dict.fromkeys(incoming):
        if raw not in local_set:
            left_in.setdefault(normalize(raw), []).append(raw)
    left_local: dict[str, list[str]] = {}
    for key in local_keys:
        if key not in incoming_set:
            left_local.setdefault(normalize(key), []).append(key)
    pairs: dict[str, str] = {}
    ambiguous: set[str] = set()
    for canon, raws in left_in.items():
        mine = left_local.get(canon, [])
        if len(raws) == 1 and len(mine) == 1:
            pairs[raws[0]] = mine[0]
        elif raws and mine:
            ambiguous.add(canon)
    return pairs, ambiguous
