"""看板(1 ライン分)のビューモデル。

tkinter 版の ``material_form`` / ``warehouse_form`` / ``warehouse_view`` は、
同じ「資材フレーム × サイズ行」を**モードごとに3回**組み立てていた。
違うのは次の3点だけなので、Web 版はこの1つに統合し、差は
:class:`BoardPolicy` が持つ。

======== ============ ============ ============
モード    サイズ       発送         注文中
======== ============ ============ ============
現場      操作可 (※)  操作可 (※)   表示のみ
倉庫      表示のみ     操作可       操作可
倉庫参照  表示のみ     表示のみ     表示のみ
======== ============ ============ ============

(※) 現場のサイズ/発送はラインマスタの ``発送無効`` で入れ替わる。VBA が
``sizeEnabled = disableShipping`` / ``shipEnabled = Not disableShipping`` と
していたのと同じ。

**色の名前は状態の名前で渡す**(``ordered`` / ``shipped`` / ``held`` /
``neutral``)。16 進の色そのものを API で渡すと、色の出どころが
``tokens.css`` とサーバの2か所になる(設計 §1 の規則7)。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .. import config
from ..domain.models import CANCELLED_WHILE_SHIPPING, KanbanItem, format_display_time
from ..domain.service import KanbanService, MaterialGroup

#: ボタンの状態(= ``tokens.css`` の色トークン名)
STATE_ORDERED = "ordered"
STATE_SHIPPED = "shipped"
STATE_HELD = "held"
STATE_NEUTRAL = "neutral"


@dataclass(frozen=True)
class BoardPolicy:
    """そのモードで何を操作できるか。"""

    size: bool
    ship: bool
    hold: bool

    @property
    def read_only(self) -> bool:
        return not (self.size or self.ship or self.hold)


def policy_for(
    mode: str, line: str, storable: set[str] | None = None
) -> BoardPolicy:
    """モードとラインから操作可否を決める。

    現場モードだけラインマスタを見る。VBA の ``IsShippingDisabledLine`` に
    従い、発送を無効にしているラインでは代わりにサイズボタンを操作できる
    (発注は現場が、発送は倉庫が行う、という運用の切り分け)。

    ``storable`` は**共有DBへ書ける状態列**(:meth:`~kanban.domain.service.
    KanbanService.storable_columns`)。渡すと、書けない列のボタンを落とします
    ── ``保留`` と ``注文中日時`` は任意列なので、持たない看板テーブルが
    実際にありえます。押せて・色が変わって・手元にも残るのに**共有DBには
    一生届かない**ボタンを出すより、最初から出さないほうがよい。
    """
    if mode == config.MODE_SITE:
        shipping_disabled = config.is_shipping_disabled_line(line)
        policy = BoardPolicy(
            size=shipping_disabled, ship=not shipping_disabled, hold=False
        )
    elif mode == config.MODE_WAREHOUSE:
        policy = BoardPolicy(size=False, ship=True, hold=True)
    else:
        # 倉庫参照モードは一切操作させない
        policy = BoardPolicy(size=False, ship=False, hold=False)

    if storable is None:
        return policy
    return BoardPolicy(
        # サイズ(発注)は ``欲`` を、発送は ``発送`` を書く。どちらも必須列
        # なので普通は落ちないが、判断の出どころは 1 つにしておく
        size=policy.size and "want" in storable,
        ship=policy.ship and "shipped" in storable,
        hold=policy.hold and "hold" in storable,
    )


@dataclass
class ButtonView:
    """1 つのボタン(またはラベル)の見え方。"""

    kind: str
    """``size`` / ``ship`` / ``hold``。"""

    label: str
    state: str
    enabled: bool
    tip: str = ""
    """マウスを乗せたときに出す一行。空なら出さない。"""


@dataclass
class RowView:
    """サイズ 1 行。"""

    mgmt_no: str
    rev: int
    non_permanent: bool
    size: ButtonView
    ship: ButtonView
    hold: ButtonView
    locked: bool = False
    """現場で操作できない状態か(発注中 かつ 注文中)。理由は ``locked_note``。"""

    locked_note: str = ""
    alert: str = ""
    """「発送処理中に注文が取り消されました」など、確かめてほしいこと。空なら無い。"""

    comments: int = 0
    """いまのやり取りのコメント数(届いたら片付く)。"""
    unread: int = 0
    """そのうち相手側が書いて、この端末でまだ開いていないもの。"""
    batch_reset: bool = False
    """「届いた資材を一括確認」の対象か(押す前にこの看板のコメントを見せるため)。"""
    batch_ship: bool = False
    """「一括発送」の対象か(同上)。"""


@dataclass
class AlertView:
    """盤面の上に出す「確かめてください」。"""

    line: str
    line_label: str
    mgmt_no: str
    rev: int
    material: str
    size: str
    message: str
    shipped_at: str = ""
    can_acknowledge: bool = False
    """この画面で「確認した」を押せるか(現場だけ)。"""


@dataclass
class CommentAlertView:
    """盤面の上の「新しいコメント」。"""

    line: str
    line_label: str
    mgmt_no: str
    material: str
    size: str
    unread: int
    last_at: str
    last_side: str
    last_body: str


@dataclass
class GroupView:
    """資材 1 枚(フレーム 1 つ)。"""

    material: str
    rows: list[RowView] = field(default_factory=list)


@dataclass
class BoardView:
    """1 ラインの看板ぜんぶ。"""

    line: str
    line_label: str
    groups: list[GroupView] = field(default_factory=list)
    note: str = ""
    """「※文字色青は非常設品」。出す必要が無ければ空。"""

    read_only: bool = False
    ordered_count: int = 0
    shipped_count: int = 0
    held_count: int = 0
    #: 一括操作の対象件数。ボタンの脇に出して、押す前に何件動くか分かるようにする
    batch_reset_count: int = 0
    batch_ship_count: int = 0
    alerts: list[AlertView] = field(default_factory=list)
    comment_alerts: list[CommentAlertView] = field(default_factory=list)
    can_comment: bool = False
    """この画面でコメントを書けるか(現場・倉庫。倉庫参照は読むだけ)。"""
    comment_side: str = ""
    """この画面が書くときの名乗り(現場 / 倉庫)。"""
    missing_table: str = ""
    """共有DBにこのラインの看板の表が無いときの説明。空なら問題なし。"""


# ------------------------------------------------------------------
# 組み立て
# ------------------------------------------------------------------
def build(service: KanbanService, line: str, mode: str) -> BoardView:
    """1 ラインのビューモデルを作る。"""
    policy = policy_for(mode, line, service.storable_columns(line))
    groups = service.groups(line)
    side = comment_side(mode)
    counts = service.store.comment_counts(line, side)

    view = BoardView(
        line=line,
        line_label=config.display_name(line),
        read_only=policy.read_only,
    )
    for group in groups:
        view.groups.append(_group(group, policy))
    for g in view.groups:
        for r in g.rows:
            r.comments, r.unread = counts.get(r.mgmt_no, (0, 0))
    view.can_comment = mode in (config.MODE_SITE, config.MODE_WAREHOUSE)
    if service.table_missing(line):
        view.missing_table = (
            f"共有DBに {config.table_name_for(line)} がありません(消されたか、名前が変わりました)。"
            "いま出ているのは最後に取り込んだ内容です。押しても共有DBへ届かないので、ボタンは止めています。"
            "表が戻れば、次の取り込みで元どおりになります。"
        )
    view.comment_side = side

    items = [item for group in groups for item in group.items]
    view.ordered_count = sum(1 for i in items if i.is_ordered)
    view.shipped_count = sum(1 for i in items if i.is_shipped)
    view.held_count = sum(1 for i in items if i.is_held)
    view.batch_reset_count = sum(1 for i in items if i.is_delivered_candidate)
    view.batch_ship_count = sum(1 for i in items if i.needs_shipping)
    view.alerts = _alerts(service, line, mode, policy, items)
    view.comment_alerts = _comment_alerts(service, line, mode, side, counts, items)
    if any(g.is_non_permanent for g in groups):
        view.note = "※文字色青は非常設品"
    return view


def _alerts(
    service: KanbanService, line: str, mode: str, policy: BoardPolicy, items: list[KanbanItem]
) -> list[AlertView]:
    """赤なし・緑あり(発送処理中に注文が取り消された)の一覧。

    **現場**は自分のラインだけ(確かめるのは現場)。**倉庫・倉庫参照**は
    倉庫の全ライン ── いま開いているタブの外で起きても見落とさないように。
    """
    if mode == config.MODE_SITE:
        found = [(line, i) for i in items if i.is_cancelled_while_shipping]
    else:
        found = [
            (code, i)
            for code in config.warehouse_line_codes()
            for i in (items if code == line else service.items(code))
            if i.is_cancelled_while_shipping
        ]
    return [
        AlertView(
            line=code, line_label=config.display_name(code),
            mgmt_no=i.mgmt_no, rev=i.rev, material=i.material, size=i.size,
            message=CANCELLED_WHILE_SHIPPING,
            shipped_at=format_display_time(i.confirmed_at),
            can_acknowledge=policy.size and code == line,
        )
        for code, i in found
    ]


def comment_side(mode: str) -> str:
    """コメントを書く・読むときの名乗り。倉庫参照は倉庫の側として読む。"""
    return "現場" if mode == config.MODE_SITE else "倉庫"


def _comment_alerts(
    service: KanbanService, line: str, mode: str, side: str,
    counts: dict[str, tuple[int, int]], items: list[KanbanItem],
) -> list[CommentAlertView]:
    """未読のコメントがある看板。現場は自分のライン、倉庫は倉庫の全ライン(帯と同じ)。"""
    lines = [line] if mode == config.MODE_SITE else config.warehouse_line_codes()
    out: list[CommentAlertView] = []
    for code in lines:
        mine = counts if code == line else service.store.comment_counts(code, side)
        unread = {no: u for no, (_n, u) in mine.items() if u}
        if not unread:
            continue
        by_no = {i.mgmt_no: i for i in (items if code == line else service.items(code))}
        for no, u in unread.items():
            item = by_no.get(no)
            opened = service.store.comment_thread(code, no, history=0)["open"]
            last = [c for c in opened if c["kind"] == "コメント"][-1:]   # 確認の区切りは除く
            out.append(CommentAlertView(
                line=code, line_label=config.display_name(code), mgmt_no=no,
                material=item.material if item else "", size=item.size if item else "",
                unread=u,
                last_at=format_display_time(last[0]["at"]) if last else "",
                last_side=last[0]["side"] if last else "",
                last_body=last[0]["body"] if last else "",
            ))
    return out


def _group(group: MaterialGroup, policy: BoardPolicy) -> GroupView:
    return GroupView(
        material=group.material,
        rows=[_row(item, policy) for item in group.items],
    )


def _row(item: KanbanItem, policy: BoardPolicy) -> RowView:
    # 現場は「発注中 かつ 注文中」の間だけ、倉庫が解除するまでサイズを
    # 動かせない(VBA cls_MaterialButton の無言のガード)。押しても何も
    # 起きないと壊れて見えるので、**押せないことを見せて理由も添える**
    locked = item.is_ordered and item.is_held
    return RowView(
        batch_reset=item.is_delivered_candidate,
        batch_ship=item.needs_shipping,
        mgmt_no=item.mgmt_no,
        rev=item.rev,
        non_permanent=item.is_non_permanent,
        locked=locked and policy.size,
        locked_note="倉庫が対応中です(注文中)。解除されるまで変更できません"
        if locked and policy.size
        else "",
        alert=CANCELLED_WHILE_SHIPPING if item.is_cancelled_while_shipping else "",
        size=ButtonView(
            kind="size",
            label=item.size,
            state=STATE_ORDERED if item.is_ordered else STATE_NEUTRAL,
            enabled=policy.size and not locked,
            tip=(f"{CANCELLED_WHILE_SHIPPING}。押すと確認します"
                 if item.is_cancelled_while_shipping and policy.size
                 else _time_tip("発注", item.ordered_at, item.is_ordered)),
        ),
        ship=ButtonView(
            kind="ship",
            label="発送",
            state=STATE_SHIPPED if item.is_shipped else STATE_NEUTRAL,
            enabled=policy.ship,
            tip=_time_tip("発送", item.confirmed_at, item.is_shipped),
        ),
        hold=ButtonView(
            kind="hold",
            label="注文中",
            # **発注の無い行は注文中にできない。** 「注文中」は現場から来た
            # 発注を倉庫が仕入れ先へ注文したという意味なので、発注が無ければ
            # 指すものがない。押してから断るのではなく、押せないことを見せる。
            # 解除する方向はいつでも押せる ── 付いた印を外す道は残す
            state=STATE_HELD if item.is_held else STATE_NEUTRAL,
            enabled=policy.hold and (item.is_ordered or item.is_held),
            tip=_hold_tip(item),
        ),
    )


def _time_tip(prefix: str, value: str, active: bool) -> str:
    if not active:
        return ""
    text = format_display_time(value)
    return f"{prefix}: {text}" if text else ""


def _hold_tip(item: KanbanItem) -> str:
    """注文中の時刻。VBA ``cls_HoldButton.RefreshDisplay`` と同じ文言。

    押せないときは**なぜ押せないか**を出す。押しても何も起きないだけだと
    壊れているようにしか見えない。
    """
    if not item.is_held:
        if not item.is_ordered:
            return "発注がありません。現場が発注してから注文中にできます"
        return ""
    text = format_display_time(item.hold_at)
    return f"注文中: {text}" if text else "注文中"


def to_dict(view: BoardView) -> dict[str, Any]:
    """JSON で返せる形にする。"""
    return asdict(view)
