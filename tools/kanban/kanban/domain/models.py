"""看板の状態モデルと状態遷移規則。

VBA では ``cls_MaterialButton.btn_Click`` / ``cls_ShipButton.btn_Click`` の中に
状態遷移と DB 更新と画面描画が混在していた。ここでは「次の状態を決める」
純粋関数として切り出し、DB 更新(:mod:`kanban.db.store`)と画面
(:mod:`kanban.ui`)から独立してテストできるようにする。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from .. import config

#: 状態遷移で書き換わる列(論理名)
MUTABLE_FIELDS = ("want", "unwant", "ordered_at", "shipped", "confirmed_at", "hold", "hold_at")


class DomainError(Exception):
    """業務ルール上できない操作。"""


CANCELLED_WHILE_SHIPPING = "発送処理中に注文が取り消されました"


class CancelledWhileShippingError(DomainError):
    """「赤なし・緑あり」の看板を、現場が確かめないまま動かそうとした。"""


class OrderMissingError(DomainError):
    """発注が無いのに発送しようとした場合。

    VBA のメッセージ「注文がありません。先にサイズボタンで発注してください。」
    に相当する。
    """


@dataclass(frozen=True)
class KanbanItem:
    """看板 1 行(資材 × サイズ)の状態。"""

    line: str
    mgmt_no: str
    material: str = ""
    size: str = ""
    want: str = ""
    """欲(発注中なら ``〇``)。"""

    unwant: str = ""
    """不(発注していないなら ``〇``)。"""

    ordered_at: str = ""
    """更新日(現場が発注した日時)。"""

    shipped: str = ""
    """発送(倉庫が発送済みにしたら ``〇``)。"""

    confirmed_at: str = ""
    """倉庫確認日時。"""

    permanent: str = ""
    """常設品(``×`` なら非常設品)。"""

    hold: str = ""
    """注文中(旧:保留。``〇`` なら注文中)。"""

    hold_at: str = ""
    """注文中(旧:保留)にした日時。自由入力の理由ではなく自動記録の日時。"""

    row_order: int = 0
    rev: int = 1
    updated_at: str = ""
    updated_by: str = ""

    # -- 判定 ------------------------------------------------------------
    @property
    def is_ordered(self) -> bool:
        """発注中(画面では赤)。"""
        return self.want.strip() == config.MARK_ON

    @property
    def is_shipped(self) -> bool:
        """発送済み(画面では緑)。"""
        return self.shipped.strip() == config.MARK_ON

    @property
    def is_non_permanent(self) -> bool:
        """非常設品(画面では青字斜体)。"""
        return self.permanent.strip() == config.MARK_NON_PERMANENT

    @property
    def is_held(self) -> bool:
        """注文中(旧:保留。画面では黄)。"""
        return self.hold.strip() == config.MARK_ON

    @property
    def needs_shipping(self) -> bool:
        """倉庫の一括発送の対象(発注済みかつ未発送)。"""
        return self.is_ordered and not self.is_shipped

    @property
    def is_cancelled_while_shipping(self) -> bool:
        """発送処理中に注文が取り消された(赤なし・緑あり)。

        倉庫が発送したのを現場の端末が取り込む前に、現場が赤を消すと起きる
        (赤を消しても発送の印には触れない。VBA と同じ)。**現場に確かめて
        もらうまで**、この看板は出し直せない(:func:`order_button_changes`)。
        """
        return not self.is_ordered and self.is_shipped

    @property
    def is_delivered_candidate(self) -> bool:
        """現場の一括リセットの対象(赤と緑が両方点灯)。"""
        return self.is_ordered and self.is_shipped

    def key(self) -> tuple[str, str]:
        return (self.line, self.mgmt_no)

    def with_changes(self, changes: dict[str, str]) -> "KanbanItem":
        return replace(self, **changes)


def now_string(now: datetime | None = None) -> str:
    """DB へ書き込む日時文字列。

    VBA には ``Format(Now, "yyyy/m/d h:mm")``(ボタン操作時)と
    ``Format(Now, "yyyy/mm/dd hh:nn:ss")``(``NowDBString``、一括処理時)の
    2 通りが混在していた。これらは TEXT 列に入り文字列比較でソート・抽出
    されるため、混在するとソート順が崩れる。Python 版はゼロ埋めした
    ``yyyy/mm/dd hh:nn:ss`` に統一する(表示側で ``m/d h:mm`` に整形する)。
    """
    return (now or datetime.now()).strftime("%Y/%m/%d %H:%M:%S")


#: 日時として受け付ける書き方。
#:
#: VBA の ``CDate`` は ``2026/1/28 13:48`` のようなゼロ埋めなしも解釈したので、
#: こちらも同じものを受け付ける。書き込む側は ``now_string`` が
#: ``yyyy/mm/dd hh:nn:ss`` に統一しているが、**読む側は昔の書き方も通す** ──
#: VBA 版が書いた行が共有 DB にそのまま残っているため。
_DATETIME_PATTERNS = (
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d",
    "%Y-%m-%d",
)


def parse_datetime(text: str) -> datetime | None:
    """``更新日`` 等の文字列を datetime へ変換する(失敗時 None)。"""
    value = (text or "").strip()
    if not value:
        return None
    for pattern in _DATETIME_PATTERNS:
        try:
            return datetime.strptime(value, pattern)
        except ValueError:
            continue
    return None


def format_display_time(value: str) -> str:
    """看板用の時刻表示 ``7/14 9:05`` を作る(VBA の ``FormatKanbanTime``)。

    日時として解釈できない場合は元の文字列をそのまま返す。
    """
    text = (value or "").strip()
    if not text:
        return ""
    parsed = parse_datetime(text)
    if parsed is None:
        return text
    return f"{parsed.month}/{parsed.day} {parsed.hour}:{parsed.minute:02d}"


# ---------------------------------------------------------------------------
# 状態遷移
# ---------------------------------------------------------------------------
def order_button_changes(
    item: KanbanItem, treat_as_delivered: bool = False, now: datetime | None = None
) -> dict[str, str]:
    """サイズ(資材)ボタンを押したときの変更内容を返す。

    VBA ``cls_MaterialButton.btn_Click`` と同じ規則。

    * 発注中 かつ 注文中(倉庫が対応中) → 倉庫が注文中を解除するまで、
      現場はこのボタンを一切操作できない(VBA の
      ``欲の解除不可: 注文中(保留)のため`` と同じ無言のガード。確認
      ダイアログも出さず、何も変更しない)。
    * 発注中かつ発送済み → ``treat_as_delivered`` が True のときだけ、発注と
      発送の両方を解除する(VBA は「この資材は届いていますか？」の確認を出し、
      [はい] のときだけ解除していた)。False の場合は変更なし。
    * 発注中 → 発注解除。更新日をクリアし、発送状態はそのまま維持する。
    * 未発注 → 発注。更新日を現在時刻にし、発送はクリアする。

    戻り値が空辞書なら「何も変更しない」。
    """
    if item.is_ordered and item.is_held:
        return {}

    if item.is_cancelled_while_shipping:
        # 黙って出し直すと(発送が消える)、倉庫が発送した物の行方が分からなくなる
        raise CancelledWhileShippingError(
            f"{CANCELLED_WHILE_SHIPPING}。倉庫はすでに発送しています。"
            "資材を確かめてから「確認した」を押してください。"
        )

    if item.is_ordered and item.is_shipped:
        if not treat_as_delivered:
            return {}
        return {
            "want": config.MARK_OFF,
            "unwant": config.MARK_ON,
            "ordered_at": "",
            "shipped": config.MARK_OFF,
        }

    if item.is_ordered:
        # 欲 -> 不 (更新日クリア、発送は維持)
        # 「維持」= この操作は発送列に一切触れない、という意味。
        # ここで item.shipped をそのまま書き戻すと、ローカルの発送列が
        # 古い(例えば倉庫が Access へ発送済みにした直後で、この端末の
        # ローカル SQLite がまだそれを取り込んでいない)場合に、その新しい
        # 発送済みの状態を空文字で上書きしてしまう。戻り値に "shipped" を
        # 含めないことで、Store 側は発送列を SET 句にも書き戻し対象にも
        # 含めない(kanban/db/store.py の dirty_columns の仕組みを参照)。
        return {
            "want": config.MARK_OFF,
            "unwant": config.MARK_ON,
            "ordered_at": "",
        }

    # 不 -> 欲 (更新日を記録、発送クリア)
    return {
        "want": config.MARK_ON,
        "unwant": config.MARK_OFF,
        "ordered_at": now_string(now),
        "shipped": config.MARK_OFF,
    }


def ship_button_changes(item: KanbanItem, now: datetime | None = None) -> dict[str, str]:
    """発送ボタンを押したときの変更内容を返す。

    VBA ``cls_ShipButton.btn_Click`` と同じ規則。発送マークを付けるには
    発注中である必要があり、そうでなければ :class:`OrderMissingError`。

    発送する場合、注文中(旧:保留)であれば同時に解除する(VBA の
    ``needClearHold`` と同じ。以前のようにエラーで発送を拒むのではなく、
    発送処理の中で注文中を自動的に解除する)。発送を解除する方向
    (発送済み -> 未発送)は注文中の状態を変えない。
    """
    if not item.is_shipped and not item.is_ordered:
        raise OrderMissingError(
            "注文がありません。先にサイズボタンで発注してください。"
        )

    if item.is_shipped:
        return {"shipped": config.MARK_OFF, "confirmed_at": ""}

    changes = {"shipped": config.MARK_ON, "confirmed_at": now_string(now)}
    if item.is_held:
        changes["hold"] = config.MARK_OFF
        changes["hold_at"] = ""
    return changes


def hold_button_changes(item: KanbanItem, now: datetime | None = None) -> dict[str, str]:
    """「注文中」ボタン(旧:保留ボタン)を押したときの変更内容を返す。

    VBA ``cls_HoldButton.btn_Click`` と同じ規則。倉庫モードでのみ操作
    できる(現場では表示のみ)。理由入力・確認ダイアログはどちらの方向も
    無く、即座に切り替わる。

    * 注文中 -> 解除。注文中・注文中日時をクリアする(発送列には一切
      触れない。VBA の ``ExecuteSQLUnhold`` も保留・注文中日時の 2 列
      しか SET しない)。
    * 未注文中 -> 注文中にする。日時を自動記録し、発送を強制解除する
      (発送済みなら解除される)。欲は一切変更しない(現場の赤はそのまま)。

    **発注が無い行は注文中にできません**(:class:`OrderMissingError`)。
    「注文中」は *現場から来た発注を倉庫が仕入れ先へ注文した* という意味
    なので、発注が無い行に付けても指すものがありません。発送ボタンが
    同じ理由で断るのと揃えてあります。

    解除する方向は発注の有無を見ません ── 付いてしまった印を外す道は、
    どんな状態でも残しておく必要があります(発注が先に取り消されても、
    倉庫は注文中を下ろせなければならない)。
    """
    if item.is_held:
        return {"hold": config.MARK_OFF, "hold_at": ""}
    if not item.is_ordered:
        raise OrderMissingError(
            "注文がありません。現場が発注してから注文中にしてください。"
        )
    return {
        "hold": config.MARK_ON,
        "hold_at": now_string(now),
        "shipped": config.MARK_OFF,
    }


def acknowledge_cancelled_shipping_changes(item: KanbanItem) -> dict[str, str]:
    """「発送処理中に注文が取り消されました」を現場が確かめた。発送の印を消す。

    もう「赤なし・緑あり」でなければ何もしない(別の端末が先に片付けた)。
    """
    if not item.is_cancelled_while_shipping:
        return {}
    return {"shipped": config.MARK_OFF, "confirmed_at": ""}


def batch_reset_changes() -> dict[str, str]:
    """現場の一括リセット(届いた資材の確認)で設定する値。

    VBA ``ExecuteSQLBatchReset`` と同じ。倉庫確認日時もクリアする。
    """
    return {
        "want": config.MARK_OFF,
        "unwant": config.MARK_ON,
        "shipped": config.MARK_OFF,
        "ordered_at": "",
        "confirmed_at": "",
    }


def batch_ship_changes(
    item: KanbanItem | None = None, now: datetime | None = None
) -> dict[str, str]:
    """倉庫の一括発送で設定する値(VBA ``ExecuteSQLBatchShip``)。

    **注文中なら一緒に解除します**(発送ボタン 1 つぶんと同じ)。VBA の
    ``ExecuteSQLBatchShip`` は保留列を触らなかったので、一括発送を通すと
    **緑が付いているのに黄も点いたまま**の行ができていました ── 倉庫から
    見れば「発送したのにまだ注文中」で、どちらが本当か読めません。

    「注文中」は発送すれば終わる状態なので、1 行ずつ押しても一括で押しても
    同じ結果になるべきです。``item`` を省略した場合(古い呼び出し)は
    発送列だけを返します。
    """
    changes = {"shipped": config.MARK_ON, "confirmed_at": now_string(now)}
    if item is not None and item.is_held:
        changes["hold"] = config.MARK_OFF
        changes["hold_at"] = ""
    return changes
