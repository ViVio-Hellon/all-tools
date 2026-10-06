"""画面から呼ばれる業務ロジック層。

VBA では UserForm と クラスモジュールが「状態遷移」「DB 更新」「画面描画」を
すべて抱えていた。ここでは画面が使う操作をサービスとしてまとめ、
:mod:`kanban.ui` からは DB の詳細を見えないようにする。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .. import applog, config
from ..db.store import ConflictError, LockTimeout, Store
from . import models
from .models import MUTABLE_FIELDS, DomainError, KanbanItem, OrderMissingError

__all__ = [
    "ConflictError",
    "DomainError",
    "LockTimeout",
    "MaterialGroup",
    "OrderMissingError",
    "KanbanService",
]


@dataclass
class MaterialGroup:
    """1 つの資材(フレーム 1 枚)にまとめられたサイズの一覧。"""

    material: str
    items: list[KanbanItem] = field(default_factory=list)

    @property
    def size_count(self) -> int:
        return len(self.items)

    @property
    def is_non_permanent(self) -> bool:
        """先頭行で判定する非常設品フラグ(VBA と同じ判定)。"""
        return bool(self.items) and self.items[0].is_non_permanent


@dataclass
class BatchResult:
    """一括処理の結果。"""

    count: int
    items: list[KanbanItem] = field(default_factory=list)


class KanbanService:
    """看板の参照・更新をまとめたサービス。"""

    def __init__(self, store: Store, on_changed: Callable[[str], None] | None = None):
        self.store = store
        self.on_changed = on_changed
        """更新が確定したときに呼ばれる(書き戻しの即時要求などに使う)。"""

    # -- 参照 ------------------------------------------------------------
    def items(self, line: str) -> list[KanbanItem]:
        return self.store.items(line)

    def change_token(self, line: str | None = None) -> str:
        return self.store.change_token(line)

    def has_non_permanent(self, line: str) -> bool:
        """非常設品(常設品 = ``×``)が含まれるか。注意書きラベルの表示判定。"""
        return any(item.is_non_permanent for item in self.store.items(line))

    def groups(self, line: str) -> list[MaterialGroup]:
        """資材ごとにまとめ、VBA と同じ並び順で返す。

        1. サイズが 2 つ以上の資材を「サイズ数の多い順」に並べる
           (同数なら資材名の降順。VBA のソートキー ``"000|資材名"`` を
           昇順ソートしてから反転した結果と一致する)
        2. その後にサイズが 1 つだけの資材を Access の行順で並べる
        """
        grouped: dict[str, MaterialGroup] = {}
        for item in self.store.items(line):
            material = item.material.strip()
            size = item.size.strip()
            if not material or not size:
                continue
            group = grouped.get(material)
            if group is None:
                group = MaterialGroup(material=material)
                grouped[material] = group
            if any(existing.size.strip() == size for existing in group.items):
                # VBA も同一サイズの重複を無視していた
                continue
            group.items.append(item)

        multi = [g for g in grouped.values() if g.size_count > 1]
        single = [g for g in grouped.values() if g.size_count == 1]
        multi.sort(key=lambda g: f"{g.size_count:03d}|{g.material}", reverse=True)
        return multi + single

    def storable_columns(self, line: str) -> set[str]:
        """そのラインで**共有DBへ書ける**状態列(論理名)。

        共有DBの看板テーブルに実際にあった列だけが入ります。``保留`` と
        ``注文中日時`` は任意列なので(:data:`kanban.config.REQUIRED_COLUMNS`)、
        持たないテーブルが実際にありえます。

        **無い列のボタンは出しません。** 出すと、押せて・色が変わって・
        手元にも残るのに、**共有DBには一生届きません** ── 倉庫が「注文中に
        した」と思っている一方で、現場には何も見えない、という形になります。
        書き戻しも送り先が無いので失敗し続け、その行は「要確認」に落ちます。

        取り込めていないラインでは空ではなく**全部**を返します ── まだ何も
        分かっていない段階で、分かっている風にボタンを消すほうが害が大きい。
        """
        if self.table_missing(line):
            return set()            # 共有DBに表が無い。押しても届かないので、どのボタンも出さない
        source = self.store.line_source(line)
        if source is None:
            return set(MUTABLE_FIELDS)
        return {name for name in MUTABLE_FIELDS if source["column_map"].get(name)}

    def table_missing(self, line: str) -> bool:
        """前の取り込みで、共有DBにそのラインの看板の表が無かったか。"""
        return line in self.store.get_meta("missing_line_tables", "").split(",")

    def pending_count(self) -> int:
        """共有DBへ未反映の件数。"""
        return self.store.pending_count()

    def sync_failures(self) -> list[dict[str, object]]:
        return self.store.sync_failures()

    # -- 単票操作 --------------------------------------------------------
    def needs_delivery_confirmation(self, item: KanbanItem) -> bool:
        """サイズボタンを押したときに確認ダイアログが必要か。

        発注中(赤)かつ発送済み(緑)の状態では、VBA と同じく
        「この資材は届いていますか？」の確認を出す必要がある。
        """
        return item.is_ordered and item.is_shipped

    def toggle_order(
        self,
        line: str,
        mgmt_no: str,
        expected_rev: int | None = None,
        treat_as_delivered: bool = False,
    ) -> KanbanItem:
        """サイズ(資材)ボタンの操作を適用する。"""
        applog.debug(
            "toggle_order: line=%s no=%s rev=%s delivered=%s",
            line,
            mgmt_no,
            expected_rev,
            treat_as_delivered,
        )
        item = self.store.apply_transition(
            line,
            mgmt_no,
            lambda current: models.order_button_changes(
                current, treat_as_delivered=treat_as_delivered
            ),
            operation="order",
            expected_rev=expected_rev,
        )
        self._notify(line)
        return item

    def toggle_ship(
        self, line: str, mgmt_no: str, expected_rev: int | None = None
    ) -> KanbanItem:
        """発送ボタンの操作を適用する。

        発注が無い状態で発送しようとした場合は :class:`OrderMissingError`。
        注文中(旧:保留)の場合は、発送と同時に注文中を自動解除する。
        """
        applog.debug("toggle_ship: line=%s no=%s rev=%s", line, mgmt_no, expected_rev)
        item = self.store.apply_transition(
            line,
            mgmt_no,
            models.ship_button_changes,
            operation="ship",
            expected_rev=expected_rev,
        )
        self._notify(line)
        return item

    def toggle_hold(
        self, line: str, mgmt_no: str, expected_rev: int | None = None
    ) -> KanbanItem:
        """「注文中」ボタン(旧:保留ボタン)の操作を適用する(倉庫モードのみ操作可能)。

        理由入力・確認ダイアログはどちらの方向も無く、即座に切り替わる
        (:func:`kanban.domain.models.hold_button_changes` 参照)。
        """
        applog.debug("toggle_hold: line=%s no=%s rev=%s", line, mgmt_no, expected_rev)
        item = self.store.apply_transition(
            line,
            mgmt_no,
            models.hold_button_changes,
            operation="hold",
            expected_rev=expected_rev,
        )
        self._notify(line)
        return item

    # -- 一括操作 --------------------------------------------------------
    def acknowledge_cancelled_shipping(
        self, line: str, mgmt_no: str, expected_rev: int | None = None
    ) -> KanbanItem:
        """「発送処理中に注文が取り消されました」を現場が確かめた(発送の印を消す)。"""
        applog.info("発送処理中の取り消しを確認: line=%s no=%s", line, mgmt_no)
        item = self.store.apply_transition(
            line,
            mgmt_no,
            models.acknowledge_cancelled_shipping_changes,
            operation="ack_cancelled_shipping",
            expected_rev=expected_rev,
        )
        self._notify(line)
        return item

    def count_delivered_candidates(self, line: str) -> int:
        """現場の一括リセット対象(赤と緑が両方点灯)の件数。"""
        return sum(1 for item in self.store.items(line) if item.is_delivered_candidate)

    def batch_reset(self, line: str) -> BatchResult:
        """届いた資材を一括確認してリセットする(VBA の一括解除)。"""
        applog.info("batch_reset: 開始 line=%s", line)
        updated = self.store.apply_batch(
            line,
            select=lambda item: item.is_delivered_candidate,
            changes_for=lambda item: models.batch_reset_changes(),
            operation="batch_reset",
        )
        applog.info("batch_reset: 完了 line=%s 件数=%d", line, len(updated))
        if updated:
            self._notify(line)
        return BatchResult(count=len(updated), items=updated)

    def count_shipping_targets(self, line: str) -> int:
        """倉庫の一括発送対象(発注済み・未発送)の件数。"""
        return sum(1 for item in self.store.items(line) if item.needs_shipping)

    def batch_ship(self, line: str) -> BatchResult:
        """表示中のラインの未発送品を一括で発送済みにする。"""
        applog.info("batch_ship: 開始 line=%s", line)
        # **行ごとに決める。** 注文中の行は発送と同時に解除する ── 1 行ずつ
        # 押したときと同じ結果にする(まとめたほうだけ黄が残ると、倉庫から
        # 見て「発送したのにまだ注文中」になり、どちらが本当か読めない)
        updated = self.store.apply_batch(
            line,
            select=lambda item: item.needs_shipping,
            changes_for=models.batch_ship_changes,
            operation="batch_ship",
        )
        applog.info("batch_ship: 完了 line=%s 件数=%d", line, len(updated))
        if updated:
            self._notify(line)
        return BatchResult(count=len(updated), items=updated)

    # フォーム状態(VBA の ``UpdateLineStatus`` / ``DrawStatusButtons``)は
    # :mod:`kanban.presence` へ移しました。tkinter 版は「開いた・閉じた」を
    # 画面が知っていましたが、Web 版で画面(ブラウザ)とプロセスが別になった
    # ため、**画面の都合ではなくプロセスの生死**で名乗る必要があります。

    # -- 内部 ------------------------------------------------------------
    def _notify(self, line: str) -> None:
        if self.on_changed is None:
            return
        try:
            self.on_changed(line)
        except Exception:
            applog.exception("KanbanService: 変更通知でエラー")
