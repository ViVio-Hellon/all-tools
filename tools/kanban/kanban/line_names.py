"""ライン名の定義 ── マスタ(アクセス権限)の正規の呼び名と、このツールのライン(看板_<コード>)

    正規(マスタに書く)  このツール
    L-1                  L1        (看板_L1)
    LVC                  LVC       (看板_LVC)
    HVC                  HVC       (看板_HVC)
    機側                 LS        (看板_LS)
    AIM                  AIM       (看板_AIM)
    コイル               コイル    (看板_コイル)
    トット               大板小板  (看板_大板小板)
    バランサー           大板小板  (看板_大板小板)
    NS1                  NS1       (看板_NS1。倉庫の画面には出さないライン)

**表(アクセス権限)には正規の呼び名で書く。** このツールの表の名前と違って紛らわしいのは
``L1``(正規は L-1)・``LS``(正規は 機側)・``大板小板``(正規は トット か バランサー)の 3 つ。
トットとバランサーは、このツールでは同じ看板(``看板_大板小板``)を使う。

読み方(揃え方)は日報(vba-daily-report-python-migration の ``nippou/logic/line_names.py``)と
同じにしてある。**対応するラインはこのツールのもの**(日報のライン分けとは違う)。

【正規の呼び名だけを読む】
``L1`` ``LS`` ``大板小板``(このツールの表の名前)、``l-1`` ``ｌ―１`` ``L 1``(記号・大文字小文字・
空白の違い)は**読まない**。読まなかったことは設定の画面に出し、正規の書き方を添える
(:func:`hint`。「L1(正規は「L-1」)」「大板小板(正規は「トット」か「バランサー」)」)。
マスタは人が直せるので、直してもらう。

**揃えるのは文字の幅(全角/半角)と前後の空白だけ**(``ＬＶＣ`` = ``LVC``、``ﾄｯﾄ`` = ``トット``)。
同じ字の幅違いは書き間違いではないので。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from . import config


@dataclass(frozen=True)
class LineName:
    official: str
    """正規(マスタ・現場の呼び名)。"""
    code: str
    """このツールのラインコード(``看板_<code>``)。"""


#: **定義の表。**
DEFINITIONS: tuple[LineName, ...] = (
    LineName("L-1", "L1"),
    LineName("LVC", "LVC"),
    LineName("HVC", "HVC"),
    LineName("機側", "LS"),
    LineName("AIM", "AIM"),
    LineName("コイル", "コイル"),
    LineName("トット", "大板小板"),
    LineName("バランサー", "大板小板"),
    LineName("NS1", "NS1"),
)

#: 1 つの欄に並べるときの区切り(**空白では区切らない** ── 「L 1」を割らない)。日報と同じ
_SEPARATORS = re.compile(r"[,、/／・;；]+")


def same(text: object) -> str:
    """比べる形。**文字の幅と前後の空白だけ**を揃える(大文字小文字・記号はそのまま)。"""
    return unicodedata.normalize("NFKC", "" if text is None else str(text)).strip()


_BY_OFFICIAL = {same(d.official): d for d in DEFINITIONS}
#: このツールのコード → 正規の呼び名(大板小板 は トット・バランサー の 2 つ)
_BY_CODE: dict[str, list[LineName]] = {}
for _d in DEFINITIONS:
    _BY_CODE.setdefault(_d.code, []).append(_d)


def tokens(value: object) -> list[str]:
    """1 つの欄に並んだ権限を 1 つずつ(``L-1、mode:field`` → 2 つ)。"""
    return [t.strip() for t in _SEPARATORS.split(str(value or "")) if t.strip()]


def by_official(text: object) -> LineName | None:
    """正規の呼び名 → 定義。正規でなければ None(``L1`` ``LS`` ``l-1`` も None)。"""
    return _BY_OFFICIAL.get(same(text))


def supported(d: LineName) -> bool:
    """このツールに、そのラインの看板があるか。"""
    return config.is_supported_line(d.code)


def officials_of(code: str) -> list[str]:
    """このツールのコード → 正規の呼び名(``大板小板`` → ``["トット", "バランサー"]``)。"""
    return [d.official for d in _BY_CODE.get(code, [])]


def official_of(code: str) -> str:
    """このツールのコード → 正規の呼び名を 1 つの文で(定義に無ければ画面の名前)。"""
    names = officials_of(code)
    return "・".join(names) if names else config.display_name(code)


def _either(names: list[str]) -> str:
    return "」か「".join(names)


def _loose(text: str) -> str:
    return re.sub(r"[\W_]+", "", same(text)).upper()


def hint(text: object) -> str:
    """読まなかった呼び名に添える正規の書き方(分かるときだけ)。``L1`` → ``正規は「L-1」``。"""
    value = same(text)
    found = _BY_CODE.get(value) or _BY_CODE.get(value.upper())
    if not found:
        # l-1 / ｌ―１ / L 1: 記号・大文字小文字・空白を外せば、正規かツールのコードに当たる
        loose = _loose(value)
        hit = next((d for d in DEFINITIONS
                    if loose and loose in (_loose(d.official), _loose(d.code))), None)
        found = ([hit] if _loose(hit.official) == loose else _BY_CODE[hit.code]) if hit else []
    names = [d.official for d in found if d.official != value]
    return f"正規は「{_either(names)}」" if names and len(names) == len(found) else ""


def check(text: object) -> str:
    """**ラインのつもりで書いたのに読めない値**なら、その理由。読める値・ラインでない値は空。"""
    value = same(text)
    if not value or by_official(value):
        return ""
    clue = hint(value)
    return f"「{str(text).strip()}」は読みません({clue})" if clue else ""


def table_rows() -> list[dict[str, str]]:
    """画面に出す形(正規 / このツール / 看板があるか)。"""
    return [{"official": d.official, "code": d.code, "supported": supported(d)} for d in DEFINITIONS]
