"""ロット番号から行を埋めるまでの段取り (VBA `SQLiteLot検索` の外側)

**式は `logic/lot_fill.py`、ファイルを読むのは `access_bridge/gw_master.py`。**
ここがやるのは、その2つを VBA と同じ順で呼ぶことだけです:

    1. SIKALOT  を ﾛｯﾄ番号 で引く   … 無ければそこで終わり
    2. SIKAHIKI を ﾛｯﾄ番号 で引く   … 無ければそこで終わり
       └ 引当が複数あれば**選ばせる**(ここだけ VBA と違う)
    3. SIKAODR  を 受注番号 で引く  … 無くても続ける(VC/単重は空のまま)
    4. LS4LOT   を ﾛｯﾄ番号 で引く   … **機側・NS1 のときだけ**(コイル形状)。
       コイル縦割・横縦割(others5/6)。無くても続ける
    5. 注意_包装仕様 を 包装仕様NO で引く … 注意があれば etc 欄へ。
       **コイルは画面の前に出す**(VBA の `MsgBox`)
    6. 引けた値から書く値を組み立てる

【断るときも画面は止めない】
どの段で見つからなくても、**打ったロット番号は消しません。** VBA は
`MsgBox` を出して `Exit Sub` していましたが、そのあと手で埋められるのは
同じです。ここは理由を返すだけで、入力そのものは受け付けます。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..logic import g_course, lot_fill, pack_note
from ..logging_setup import get_logger

log = get_logger("services.lot_lookup")


@dataclass
class LotLookup:
    """引いた結果。**どれか1つだけが入る。**"""

    #: 引けなかった理由(引けたときは None)
    refusal: Optional[lot_fill.Refusal] = None
    #: 引当が複数あって選ばせるとき、その一覧(選ばせないときは空)
    choices: list[dict[str, str]] = field(default_factory=list)
    #: 書く値(選ばせるときと断るときは空)
    fill: lot_fill.RowFill = field(default_factory=lot_fill.RowFill)
    #: 画面に出す一言
    message: str = ""

    @property
    def note(self):
        """包装仕様の注意。**画面はこれを見て出し方を決めます。**"""
        return self.fill.note


def fetch(lot_no: str, *, line: str = "", hiki_no: str = "") -> LotLookup:
    """ロット番号1本で3ファイルを辿る。

    `hiki_no` を渡すと、その引当で決め打ちします(選ばせたあとの呼び直し)。
    渡さないときは、引当が1件ならそれを使い、複数なら選ばせます。
    """
    from ..access_bridge import gw_master

    short = lot_fill.check_length(lot_no)
    if short is not None:
        return LotLookup(refusal=short)

    try:
        lot = gw_master.search_lot(lot_no)
    except Exception as exc:                      # noqa: BLE001 - 画面を落とさない
        log.exception("SIKALOT を読めませんでした lot_no=%s", lot_no)
        return LotLookup(refusal=lot_fill.Refusal(
            lot_fill.REFUSE_UNREADABLE,
            f"仕掛ロット(SIKALOT)を読めませんでした: {exc}"))

    if lot is None:
        return LotLookup(refusal=lot_fill.Refusal(
            lot_fill.REFUSE_NO_LOT,
            f"SIKALOT に ロット {lot_no} がありません。手で入れてください。"))

    try:
        allocations = gw_master.search_allocations(lot_no)
    except Exception as exc:                      # noqa: BLE001 - 画面を落とさない
        log.exception("SIKAHIKI を読めませんでした lot_no=%s", lot_no)
        return LotLookup(refusal=lot_fill.Refusal(
            lot_fill.REFUSE_UNREADABLE,
            f"仕掛引当(SIKAHIKI)を読めませんでした: {exc}"))

    if not allocations:
        # **ここで止まると VC も単重も出ません。** ロット番号から受注番号を
        # 知る道はここしか無いので、その旨まで書きます
        return LotLookup(refusal=lot_fill.Refusal(
            lot_fill.REFUSE_NO_HIKI,
            f"SIKAHIKI に ロット {lot_no} の引当がありません。"
            "受注が分からないので、ＶＣ・単重・合紙は手で入れてください。"))

    picked = _pick(allocations, hiki_no)
    if picked is None:
        # 複数あって、まだ選ばれていない
        return LotLookup(choices=[
            {"hiki_no": a.hiki_no, "order_no": a.order_no,
             "quantity": a.quantity, "adjust_no": a.adjust_no,
             "label": a.label}
            for a in allocations])

    order = None
    try:
        order = gw_master.search_order(picked.order_no)
    except Exception:                             # noqa: BLE001 - 続ける
        # **止めない。** VBA も受注が引けないときは
        # 「VC/単重/包装仕様は空のまま続行」していた
        log.exception("SIKAODR を読めませんでした order_no=%s", picked.order_no)

    coil = _coil_split(lot_no, line)
    note = _pack_note(lot, order)

    fill = lot_fill.build(line=line, lot=lot, allocation=picked, order=order,
                          coil=coil, note=note)
    missing = "" if order is not None else "(受注が引けませんでした。ＶＣ・単重は空のままです)"
    return LotLookup(
        fill=fill,
        message=f"ロット {lot_no} / 受注 {picked.order_no}"
                f"{' / 引当 ' + picked.hiki_no if picked.hiki_no else ''}{missing}")


def _coil_split(lot_no: str, line: str):
    """コイルの割り数。**機側・NS1 のときだけ引きます。**

    ほかのライン(AIM を含む ── 板形状)はコイルを扱わないので、共有の
    ファイルを開く意味がありません。VBA は AIM も引いていましたが、
    「AIMはコイル割り数不要です」(v4.21.0)。
    引けなくても**止めません** ── 縦横が空になるだけです。
    """
    from ..access_bridge import gw_master

    if not lot_fill.needs_coil_split(line):
        return None
    try:
        return gw_master.search_coil_split(lot_no)
    except Exception:                             # noqa: BLE001 - 続ける
        log.exception("LS4LOT を読めませんでした lot_no=%s", lot_no)
        return None


def _pack_note(lot, order) -> pack_note.PackNote:
    """包装仕様の注意。**受注が引けていなければ番号も無いので何もしません。**

    VBA は注意が空のときそこで `Exit Sub` していましたが、ここは続けます
    ── 注意が無いことと、行を埋められないことは別です。あちらは同じ
    `Sub` の中で etc の組み立てまでやっていたので、抜けるしかなかった
    だけでした。
    """
    from ..access_bridge import pack_note_master

    number = getattr(order, "pack_spec_no", "") if order else ""
    if not (number or "").strip():
        return pack_note.PackNote()
    rows = pack_note_master.load_notes(number)
    # 寸法の条件は**製品の寸法**で見る ── Gコースなら BOX最終実績(v4.17.0)。
    # 日報の製品寸法・GW と同じ寸法(`logic/g_course.product_size`)
    thickness, width, length = g_course.product_size(lot)
    return pack_note.build(
        number, rows,
        usage_code=lot.usage_code, thickness=thickness,
        width=width, length=length,
        # 受注(SIKAODR)のほうを先に見る。**引当で決まった相手**なので
        # ── ロット側(SIKALOT)は引当が決まる前の見込みが入ることがある
        delivery=getattr(order, "delivery", "") or lot.delivery,
        customer=getattr(order, "customer", "") or lot.customer)


def _pick(allocations: list[Any], hiki_no: str):
    """使う引当を1つ決める。決められなければ `None`(=選ばせる)。"""
    if hiki_no:
        # 選ばれたもの。**無ければ選ばせ直す**(一覧が入れ替わったとき)
        return next((a for a in allocations if a.hiki_no == hiki_no), None)
    if len(allocations) == 1:
        return allocations[0]
    return None
