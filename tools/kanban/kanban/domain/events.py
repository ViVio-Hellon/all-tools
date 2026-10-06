"""看板の出来事(集計のための記録)

看板の表(``看板_<ライン>``)は**いまの状態**しか持ちません。「いつ出して、いつ
届いて、次にいつ出したか」は、状態が変わった瞬間にしか分からないので、ボタンを
押して状態が変わるたびに出来事として記録します(:meth:`kanban.db.store.Store.apply_transition`)。
記録は端末の手元に溜め、書き戻しと一緒に共有DBの ``看板履歴`` へ送ります
(:func:`kanban.db.sync.export_events`)。集計は :mod:`kanban.presenters.stats`。

出来事は状態の**変わり方**から決めます(どのボタンを押したかではなく)。一括
操作も 1 枚ずつのボタンも、同じ変わり方なら同じ出来事になります。

======== ================================================================
出来事   変わり方
======== ================================================================
出した   発注していない → 発注(赤が点いた)
取消     発注(未発送) → 発注していない(届く前に赤を消した)
発送     未発送 → 発送済み(緑が点いた)
発送取消 発送済み → 未発送、発注はそのまま(緑だけ消した・注文中にした)
届いた   発注かつ発送済み → 発注していない(届いたので赤を消した・一括リセット)
注文中   注文中でない → 注文中(黄が点いた。倉庫の在庫切れで仕入れ先へ注文した)
注文中解除 注文中 → 注文中でない(黄を消した・発送して自動で消えた)
倉庫に表示 倉庫の端末に、出した看板が初めて取り込まれた(押す操作ではない。
         倉庫の端末が取り込みのときに書く。:meth:`kanban.db.store.Store.import_line`)
======== ================================================================

**押し間違いは、時間ではなく状態の変わり方で見分ける**のが基本(発送前に赤を消した
= 取消)。時間で見分けるのは、状態だけでは区別できないもの ── すぐ外した注文中と、
発送の無いラインですぐ消した赤 ── だけで、集計の側で行う
(:data:`kanban.presenters.stats.MISTAKE_MINUTES`)。記録はそのまま残す。
"""

from __future__ import annotations

from typing import Mapping

from .. import config
from .models import KanbanItem

ORDERED = "出した"
CANCELLED = "取消"
SHIPPED = "発送"
UNSHIPPED = "発送取消"
DELIVERED = "届いた"
HOLD = "注文中"
UNHOLD = "注文中解除"
SEEN = "倉庫に表示"

ALL_KINDS = (ORDERED, CANCELLED, SHIPPED, UNSHIPPED, DELIVERED, HOLD, UNHOLD, SEEN)


def classify(before: KanbanItem, changes: Mapping[str, str]) -> list[str]:
    """状態の変わり方から出来事を決める。何も起きていなければ空。"""
    want = changes.get("want", before.want)
    shipped = changes.get("shipped", before.shipped)
    was_ordered = before.is_ordered
    was_shipped = before.is_shipped
    is_ordered = want == config.MARK_ON
    is_shipped = shipped == config.MARK_ON

    kinds: list[str] = []
    if not was_ordered and is_ordered:
        kinds.append(ORDERED)
    if was_ordered and not is_ordered:
        kinds.append(DELIVERED if was_shipped else CANCELLED)
    if is_ordered and not was_shipped and is_shipped:
        kinds.append(SHIPPED)
    if is_ordered and was_shipped and not is_shipped:
        kinds.append(UNSHIPPED)
    was_held = before.is_held
    is_held = changes.get("hold", before.hold) == config.MARK_ON
    if not was_held and is_held:
        kinds.append(HOLD)
    if was_held and not is_held:
        kinds.append(UNHOLD)
    return kinds
