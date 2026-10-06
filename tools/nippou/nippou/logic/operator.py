"""オペレーター区分 ── 班員名簿の「オペレーター」を、標準作業時間と梱包力に使う(v4.15.0)

    テーブル:班員名簿に 列:オペレーター を追加した
    AOP:検査できる人  ABOP:どっちもできる人  BOP:検査できない人 で分けてある
    標準時間と梱包力に使用してください

【何に使うか ── 作業の「オペレーター構成」】
作業者欄は直ごとにその場に居た全員の名前です。名前を名簿で引いて区分を数えると、
**その作業をどういう顔ぶれでやったか**が出ます:

    作業者「青木 木村 佐藤」 → AOP 1人・BOP 2人 → 「AOP1・BOP2」

検査できる人が居るかどうかで、同じ条件でも手の進み方は変わりえます。班で分けた
標準・梱包力と同じように、**構成で分けて**見られるようにします
(標準の面のまとめ方・同条件の作業の列・梱包力のまとめ方)。

【読み方 ── 決めた3つだけ】
`AOP` `ABOP` `BOP` だけを読みます(全角半角・大文字小文字・前後の空白は揃える)。
それ以外の値(`OP` など)は**読まずに「区分なし」**として数え、どの行かを画面に
出します(`inspect`)── 黙って別の区分に寄せると、書き間違いに気づけません。
名簿に無い名前・オペレーターが空の人も「区分なし」です。

**ここは純粋な計算だけ**で、名簿はもらうだけです(`services/standard_time`)。
"""
from __future__ import annotations

import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

#: 区分(この順に並べる)と、その意味
KINDS: tuple[str, ...] = ("AOP", "ABOP", "BOP")
MEANINGS: dict[str, str] = {
    "AOP": "検査できる人",
    "ABOP": "どっちもできる人",
    "BOP": "検査できない人",
}
#: 区分が分からない人(名簿に無い・空・読めない値)
NO_KIND = "区分なし"
#: 名前が1人も無い作業
NO_WORKER = "作業者なし"
#: 構成の区切り
SEPARATOR = "・"


def fold(text: object) -> str:
    """比べる形(全角半角・大文字小文字・前後の空白を揃える)。"""
    return unicodedata.normalize("NFKC", "" if text is None else str(text)).strip().upper()


def read(value: object) -> str:
    """名簿の値 → 区分。決めた3つでなければ空。"""
    found = fold(value)
    return found if found in KINDS else ""


def problem(value: object) -> str:
    """読めない値なら理由。空・読める値なら空。"""
    raw = "" if value is None else str(value).strip()
    if not raw or read(raw):
        return ""
    return f"「{raw}」は読みません({' / '.join(KINDS)} のどれかで入れてください)"


def composition(names: Iterable[str], kinds: Mapping[str, str]) -> str:
    """名前の並び → 構成「AOP1・BOP2」。区分の分からない人は「区分なし1」。"""
    counts = Counter(kinds.get(name) or NO_KIND for name in names)
    if not counts:
        return NO_WORKER
    order = [*KINDS, NO_KIND]
    return SEPARATOR.join(f"{kind}{counts[kind]}" for kind in order if counts[kind])


def sort_key(label: str) -> tuple:
    """構成の並び。人数の少ない順 → AOP の多い順 …(読み比べやすいように)。"""
    counts = {kind: 0 for kind in (*KINDS, NO_KIND)}
    for part in label.split(SEPARATOR):
        for kind in sorted(counts, key=len, reverse=True):     # ABOP を AOP より先に
            if part.startswith(kind) and part[len(kind):].isdigit():
                counts[kind] = int(part[len(kind):])
                break
    return (sum(counts.values()), *(-counts[k] for k in KINDS), counts[NO_KIND], label)


@dataclass(frozen=True)
class Finding:
    """名簿で読めなかった行。"""

    name: str
    value: str
    reason: str

    def describe(self) -> str:
        return f"{self.name}: {self.reason}"


def inspect(rows: Iterable[Mapping[str, object]], *, name_column: str = "名前",
            operator_column: str = "オペレーター") -> list[Finding]:
    """名簿の全部の行を見て、**読めない値**を並べる(空は言わない ── 作業長など)。"""
    out = []
    for row in rows:
        reason = problem(row.get(operator_column))
        if reason:
            out.append(Finding(name=str(row.get(name_column) or "").strip() or "(名前なし)",
                               value=str(row.get(operator_column)).strip(), reason=reason))
    return out


def legend() -> str:
    """「AOP 検査できる人 / ABOP どっちもできる人 / BOP 検査できない人」。"""
    return " / ".join(f"{kind} {MEANINGS[kind]}" for kind in KINDS)


def kind_of(name: str, kinds: Mapping[str, str]) -> Optional[str]:
    """その人の区分(分からなければ None)。"""
    return kinds.get(name) or None
