"""理由は行ごとに持ち、出すときに1つへまとめる

【紙が1つだったのは、紙の都合だった】
梱包実績日報表の「ヨ：その他（理由を記載）」は欄が1つしかなく、VBA も
それに合わせてページに1つ(`daily_header.reason`)でした。ですが書きたいのは
**その行で何があったか**です ── 停止理由で「その他」を選ぶのは行ごとで、
1つのページに2つ「その他」があれば、理由も2つあります。

DBは**行ごとに持ちます**(`daily_detail.reason`)。紙と共有へ出すときだけ
行番号を付けて1つにまとめます:

    3行目: 棚卸し準備 / 7行目: 点検表の差し替え

**まとめ方をここ1か所に置くのが要点です。** 紙・共有への保存・画面の
3か所で別々に組み立てると、紙には出ているのに共有には無い、が起きます。

【なぜ紙のレイアウトを変えないか】
紙は現場が20年見てきた形で、欄の位置が変わると目が迷います。行番号を
付ければ「どの行の理由か」は読めるので、**欄は1つのまま**にしました。
"""
from __future__ import annotations

from typing import Iterable, Optional

#: まとめるときの区切り。**読点ではなく「 / 」**にするのは、理由そのものに
#: 読点が入るため ── 「点検表を差し替え、その後清掃」を切らない
SEPARATOR = " / "


def label(row: int) -> str:
    """行番号の書き方。**画面も紙も共有も同じ言い方にする。**"""
    return f"{row}行目"


def combine(by_row: dict[int, str]) -> str:
    """行ごとの理由を、紙と共有へ出す1つの文字列に。

    `{3: "棚卸し準備", 7: "点検表の差し替え"}`
        → `"3行目: 棚卸し準備 / 7行目: 点検表の差し替え"`

    **行の順に並べます。** 書いた順や長さの順ではなく、紙を上から
    追う順です ── 読む人は行を目で追って理由を探します。

    空の理由は飛ばします。行だけあって中身が無いものを
    「3行目: 」と出しても読めません。
    """
    parts = []
    for row in sorted(by_row):
        text = (by_row[row] or "").strip()
        if text:
            parts.append(f"{label(row)}: {text}")
    return SEPARATOR.join(parts)


def split(text: str) -> dict[int, str]:
    """`combine` の逆。**古い書き方も読めるように。**

    行ごとに持つ前のデータは、ページに1つの理由が入っています。行番号の
    付いていない文字列はどの行のものか分からないので、`0` に入れて
    返します ── **捨てません。** 呼び手が「ページに書かれていた理由」として
    出せるようにするためです。
    """
    out: dict[int, str] = {}
    plain: list[str] = []
    for chunk in (text or "").split(SEPARATOR):
        piece = chunk.strip()
        if not piece:
            continue
        head, sep, tail = piece.partition(":")
        head = head.strip()
        if sep and head.endswith("行目"):
            try:
                row = int(head[:-2])
            except ValueError:
                plain.append(piece)
                continue
            out[row] = tail.strip()
        else:
            plain.append(piece)
    if plain:
        out[0] = SEPARATOR.join(plain)
    return out


def needs_reason(row_values: dict[str, str], other_codes: Iterable[str]) -> bool:
    """その行が理由を書く行か(停止理由で「その他」を選んでいるか)。

    見るのは**3つの停止記号すべて**です ── 作業停止①②③のどれで
    「その他」を選んでも、書きたいことは同じ1つです。
    """
    codes = {c for c in other_codes if c}
    if not codes:
        return False
    return any((row_values.get(family) or "").strip() in codes
               for family in ("S", "SS", "STH"))


def missing(by_row: dict[int, str], needed: Iterable[int]) -> list[int]:
    """「その他」を選んだのに理由が空の行。

    **断りには使いません。** 理由は後から書き足すこともあるので、
    画面で「まだ書いていない行があります」と言うためだけのものです
    ── 打つ手が止まるほうが困ります。
    """
    return [row for row in sorted(set(needed))
            if not (by_row.get(row) or "").strip()]


def first_missing(by_row: dict[int, str],
                  needed: Iterable[int]) -> Optional[int]:
    """まだ書いていない先頭の行。無ければ None。"""
    found = missing(by_row, needed)
    return found[0] if found else None
