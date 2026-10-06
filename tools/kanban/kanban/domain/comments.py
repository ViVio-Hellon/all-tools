"""看板コメントをいつ書けるか・いつ片付くか(倉庫 ⇔ 現場のやり取り)

コメントは**段階に結び付ける**。いつでも書けると、どの段階の話なのか分からず、
古い一言がいつまでも残る。

============ ===================================================================
書ける側     書けるとき
============ ===================================================================
現場         赤を付けたあと、倉庫がまだ黄(注文中)・緑(発送)を付けていないあいだ
倉庫         黄(注文中)か緑(発送)を付けたあと、現場が赤を消すまで
             赤だけのあいだは、**現場のコメントへの返事だけ**書ける(押す前に問い返せる)
============ ===================================================================

* 赤が付いていない看板には書けない(書く理由が無い)
* 緑が付いたあと(受け取る段階)、現場は書けない ── 受け取るだけなので
* **どちらの側でも、ボタンで状態が変わったら、それまでのコメントは確認済み**になる
  (「── 倉庫が注文中(黄)にした(ここまで確認済み)」の区切りが入る)。相手のコメントは
  読んでからでないと押せない(押す前に画面が中身を見せる)
* **やり取りは、届く(現場が赤と緑を消す)・赤を取り消すまで開いたまま**見せる(赤 → 返事 →
  黄 → 緑 と続けて読める)。届いたら「前の回のやり取り」へ畳む(消さない。後から追える)
"""
from __future__ import annotations

from typing import Iterable, Sequence

from . import events
from .models import KanbanItem

SITE = "現場"
WAREHOUSE = "倉庫"

#: 出来事 → 片付けの印に残す一言。倉庫に表示(押す操作ではない)は片付けない
_CLOSE_REASON = {
    events.ORDERED: "現場が赤を付けた",
    events.CANCELLED: "現場が赤を取り消した",
    events.DELIVERED: "現場が受け取った(赤を消した)",
    events.HOLD: "倉庫が注文中(黄)にした",
    events.UNHOLD: "倉庫が注文中(黄)を外した",
    events.SHIPPED: "倉庫が発送(緑)にした",
    events.UNSHIPPED: "倉庫が発送(緑)を取り消した",
}

#: その出来事を起こすのはどちらの側か(ボタンを押せる側が決まっている)
_ACTOR = {
    events.ORDERED: SITE, events.CANCELLED: SITE, events.DELIVERED: SITE,
    events.HOLD: WAREHOUSE, events.UNHOLD: WAREHOUSE,
    events.SHIPPED: WAREHOUSE, events.UNSHIPPED: WAREHOUSE,
}


#: 前の回へ畳む出来事(その回のやり取りが終わる)。ほかの出来事は確認の区切りだけ
_CLOSING = frozenset({events.DELIVERED, events.CANCELLED})


def closes_round(kinds: Iterable[str]) -> bool:
    """その操作で、やり取りを前の回へ畳むか(届いた・取り消した)。"""
    return any(k in _CLOSING for k in kinds)


def close_reason(kinds: Iterable[str]) -> str:
    """その操作でコメントを片付けるなら、何をして片付いたか。片付けないなら空。"""
    reasons = [_CLOSE_REASON[k] for k in kinds if k in _CLOSE_REASON]
    # 発送すると注文中も外れる(発送・注文中解除)。いちばん大きいほうだけ言う
    if len(reasons) > 1 and _CLOSE_REASON[events.SHIPPED] in reasons:
        return _CLOSE_REASON[events.SHIPPED]
    return "・".join(dict.fromkeys(reasons))


def actor_of(kinds: Iterable[str]) -> str:
    """その操作をしたのはどちらの側か(現場 / 倉庫)。分からなければ空。"""
    for kind in kinds:
        if kind in _ACTOR:
            return _ACTOR[kind]
    return ""


def can_write(item: KanbanItem | None, side: str, open_comments: Sequence[dict]) -> tuple[bool, str]:
    """``(書けるか, 書けない理由)``。``open_comments`` はいまの回のコメント(``side`` を持つ)。"""
    if item is None:
        return False, "その看板がありません。"
    if side not in (SITE, WAREHOUSE):
        return False, "倉庫参照は読むだけです。"
    if not item.is_ordered:
        return False, ("赤が付いていない看板には書けません。"
                       "現場が赤を付けたあと(倉庫は黄・緑を付けたあと)に書けます。")
    answered = item.is_held or item.is_shipped
    if side == SITE:
        if answered:
            return False, ("倉庫が注文中(黄)・発送(緑)にしたあとは、現場からは書けません"
                           "(受け取るだけなので)。倉庫のコメントは読めます。")
        return True, ""
    if answered:
        return True, ""
    if any(c.get("side") == SITE and c.get("kind", "コメント") == "コメント" for c in open_comments):
        return True, ""   # 現場のコメントへの返事(押す前に問い返せる)
    return False, ("倉庫が書けるのは、注文中(黄)か発送(緑)を付けたあとです"
                   "(現場からコメントがあれば、その返事は書けます)。")


def write_hint(item: KanbanItem | None, side: str, open_comments: Sequence[dict]) -> str:
    """書けるときに添える一言(何についての一言か)。"""
    if item is None or not item.is_ordered:
        return ""
    if side == SITE:
        return "赤を付けた理由・数量・急ぎかどうかなど(倉庫が黄・緑を付けると確認済みになります)"
    if item.is_shipped:
        return "発送についての一言(現場が受け取ると確認済みになります)"
    if item.is_held:
        return "注文中の理由・入る見込みなど(緑を付けるか、現場が受け取ると確認済みになります)"
    return "現場のコメントへの返事(黄・緑を付けると確認済みになります)"
