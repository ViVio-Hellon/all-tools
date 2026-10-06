"""VBA の書式・数値変換の再現

VC 計算の**結果そのもの**が VBA の `Format` の丸め方で決まるので、
ここを Python の `round()` や f 文字列で済ませると、境目の値で
0.1 ずれます。

    VBA  Format(12.35, "0.0") = "12.4"     (0.5 は 0 から遠い側へ)
    Py   f"{12.35:.1f}"        = "12.3"     (2進の近似値をそのまま丸める)
    Py   round(0.25, 1)        = 0.2        (銀行丸め)

VBA の `Format` は倍精度の値を**有効15桁の10進**に直してから、
0.5 を 0 から遠い側へ丸めます(`CStr` が有効15桁で出すのと同じ作り)。
ここではそれを `Decimal` でなぞります。
"""
from __future__ import annotations

import math
import re
import unicodedata
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Optional

# VBA が倍精度を10進に直すときの有効桁
VBA_DIGITS = 15


def vba_format_fixed(value: float, decimals: int) -> str:
    """`Format(value, "0.0…")`(小数 `decimals` 桁)。

    >>> vba_format_fixed(12.35, 1)
    '12.4'
    >>> vba_format_fixed(0.125, 2)
    '0.13'
    """
    if value is None or math.isnan(value) or math.isinf(value):
        raise ValueError(f"数値ではありません: {value!r}")
    exact = Decimal(format(float(value), f".{VBA_DIGITS}g"))
    step = Decimal(1).scaleb(-decimals)
    rounded = exact.quantize(step, rounding=ROUND_HALF_UP)
    if rounded == 0:
        rounded = abs(rounded)        # "-0.0" を出さない
    return f"{rounded:f}"


def excel_to_int(value: float, mode: str) -> int:
    """Excel の ROUNDDOWN(x, 0) / ROUND(x, 0) と同じく、有効15桁の10進で整数にする。

    2進の誤差で 67.0 が 66.99999… になっても 66 に落とさない。
    `mode` は "切り捨て" か "四捨五入"。
    """
    exact = Decimal(format(float(value), f".{VBA_DIGITS}g"))
    rounding = ROUND_HALF_UP if mode == "四捨五入" else ROUND_DOWN
    return int(exact.quantize(Decimal(1), rounding=rounding))


def vba_round_value(value: float, decimals: int) -> float:
    """`Format(val(x), "0.00")` を Double に入れ直した値。

    VBA は `y = Format(val(.COatu), "0.00")` のように、書式を整えた
    **文字列を Double の変数へ代入**して計算に使う。
    """
    return float(vba_format_fixed(value, decimals))


# ------------------------------------------------------------------
# 入力欄の文字を数として読む
# ------------------------------------------------------------------
# 受ける形: 123 / 12.3 / .5 / 12. / 1e3(符号つきも読むが、負は呼び手が断る)
_NUMBER = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


def normalize_text(text: Optional[str]) -> str:
    """全角を半角に直し、前後の空白を落とす。

    VBA の日本語版 `IsNumeric` / `Format` は全角数字を数として読むので、
    Web 版も同じ入力を受ける。
    """
    if text is None:
        return ""
    return unicodedata.normalize("NFKC", str(text)).strip()


def parse_number(text: Optional[str]) -> Optional[float]:
    """入力欄の文字を数にする。数でなければ None。空も None。

    VBA の `IsNumeric` は `1,000` や `&H10` まで通すが、`Val` と組み合わさると
    `1,000` を 1 と読む(経寸丈で起きる)。Web 版はこうした形を**断る**
    (docs/vc/VBA解析.md §14 D6)。
    """
    s = normalize_text(text)
    if not s or not _NUMBER.match(s):
        return None
    value = float(s)
    if math.isnan(value) or math.isinf(value):
        return None
    return value
