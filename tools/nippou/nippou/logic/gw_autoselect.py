"""オーダーから梱包仕様を自動で選ぶ (VBA ``VC選択`` / ``合紙選択`` / ``バンド選択``)

【何をするものか】
LotNo を入れてオーダーが決まると、VBA はその場で**チェックを勝手に付け直して
いました**。VC を使うか、合紙を挟むか、帯鉄かPETバンドか、縦バンドは何本か
── どれも取引先・納入先・包装仕様で決まっているので、毎回人が選ぶ必要が
ありません。

    VC選択    … VC_表 / VC_裏 が入っていればVCを使う。品名もそのまま入れる
    合紙選択  … 「合紙」列が "1" なら合紙を使う
    バンド選択… 取引先と納入先の組み合わせ、または包装仕様NOで
                PETバンド・合紙・縦バンド1本を決める

【なぜ表にするのか】
VBA では取引先ごとの `Select Case` が入れ子で並んでいて、条件が1つ増えるたびに
分岐が伸びていました。ここでは :data:`EXPORT_RULES` の1つの表にしてあります
── 新しい輸出形態が増えたとき、足すのは行1つです。

**決めるだけで、押しません。** 返すのは「こうなります」という選択で、
それを画面に出して人が変えられるようにするのは呼び出し側の仕事です
(VBAも赤色で「自動で決まった」ことを示していただけで、変更はできました)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# バンドの種別。資材重量マスタを引くときの名前は
# `app/routes/gw.py:_band_material_name` が付ける
BAND_NO_SEAL = "シール無"
BAND_WITH_SEAL = "シール有"
BAND_PET = "PETバンド"


@dataclass(frozen=True)
class ExportRule:
    """輸出形態1つぶんの決めごと。

    ``customer`` と ``delivery`` の**両方**が一致したときに効きます
    (VBA も取引先の `Select Case` の中で納入先を見ていました)。
    ``delivery`` が空なら取引先だけで決まります。
    """

    customer: str
    delivery: str = ""
    band_kind: str = BAND_PET
    interleaf: Optional[bool] = None      # None = 触らない
    vertical_bands: Optional[int] = None
    note: str = ""


# 取引先・納入先で決まる輸出形態 (VBA ``バンド選択`` の `Select Case` 群)。
#
# 名前は Access に入っている**半角カナのまま**です。見た目で書き換えると
# 一致しなくなります(ﾅﾒｶﾜｱﾙﾐ(ｶ の閉じ括弧が無いのも原文どおり)。
EXPORT_RULES: tuple[ExportRule, ...] = (
    ExportRule("ﾅﾒｶﾜｱﾙﾐ(ｶ", "KIZAN CHUANFU", BAND_PET, True, 1,
               "2017.11.29 新輸出形態"),
    ExportRule("ﾆﾂｹｲｻﾝｷﾞﾖｳ(ｶ :ｷﾕｳｼﾖｳｼﾞｸﾞﾁ", "ﾁﾕｳｺﾞｸ", BAND_PET, True, 1),
    ExportRule("ﾆﾂｹｲｻﾝｷﾞﾖｳ(ｶ :ｷﾕｳｼﾖｳｼﾞｸﾞﾁ", "ﾀｲﾜﾝ", BAND_PET, True, 1),
    ExportRule("ﾆﾂﾃﾂｽﾐｷﾝﾌﾞﾂｻﾝ(ｶ)ｷﾉｳﾏﾃﾘｱﾙﾌﾞ", "ﾀｲﾜﾝ", BAND_PET, True, 1),
    # **ここだけ合紙を「使わない」に倒す。** VBA のコメントも「無し指定」
    ExportRule("ｲｲﾀﾞｹｲｷﾝ(ｶ", "ﾀｲﾜﾝ", BAND_PET, False, 1),
)

# 取引先がこれなら、納入先を問わずPETバンド (VBA の最初の `If`)
PET_CUSTOMERS: tuple[str, ...] = ("ﾅﾒｶﾜｱﾙﾐ(ｶ",)

# この包装仕様NOなら PETバンド + 縦バンド1本
# (VBA ``If SH.Cells(a, R_PackN) = "7P0106"``)
PET_PACK_SPECS: tuple[str, ...] = ("7P0106",)

# 「合紙を使う」を表す値。Access には文字の "1" で入っている
INTERLEAF_ON = "1"

# 縦バンドアングルは**縦バンドを掛けるときだけ**使う(縦バンドの角の保護)。
#
#     縦バンド使用時のコーナー保護で使用するものです
#     縦バンドの時だけチェックが入り 計算的には個数計算です
#
# VBA ``TABA_Change`` は「縦2本のときだけ」でした ── 蓋を使わない頃の、
# 全丈方向に掛けるアングルの決め方が残っていたもので、縦1本でも角は要ります
ANGLE_MIN_VERTICAL_BANDS = 1


@dataclass
class AutoSelection:
    """オーダーから決まった梱包仕様。

    ``decided`` は「自動で決まった項目」の名前。画面はこれを見て
    「ここは自動で入りました」と示せます ── VBA が赤色でしていたこと。
    """

    use_vc: bool = False
    vc_side_a: bool = False
    vc_side_b: bool = False
    vc_name_a: str = ""
    vc_name_b: str = ""
    interleaf: bool = False
    band_kind: str = BAND_NO_SEAL
    vertical_bands: Optional[int] = None
    angle: bool = False
    decided: list[str] = field(default_factory=list)
    note: str = ""


def select_vc(vc_front: str, vc_back: str) -> AutoSelection:
    """port of ``VC選択``: VC_表 / VC_裏 の入っているほうを使う。

    両方空なら VC を使いません(VBA ``UFGW.CheckBox8 = False``)。
    """
    front = (vc_front or "").strip()
    back = (vc_back or "").strip()
    out = AutoSelection()
    if not front and not back:
        return out
    out.use_vc = True
    out.vc_side_a = bool(front)
    out.vc_side_b = bool(back)
    out.vc_name_a = front
    out.vc_name_b = back
    out.decided.append("vc")
    return out


def select_interleaf(interleaf_flag: str) -> bool:
    """port of ``合紙選択``: 「合紙」列が "1" のときだけ使う。"""
    return str(interleaf_flag).strip() == INTERLEAF_ON


def find_export_rule(customer: str, delivery: str) -> Optional[ExportRule]:
    """取引先と納入先に当たる輸出形態。無ければ ``None``。"""
    customer = (customer or "").strip()
    delivery = (delivery or "").strip()
    for rule in EXPORT_RULES:
        if rule.customer != customer:
            continue
        if rule.delivery and rule.delivery != delivery:
            continue
        return rule
    return None


def select_band(customer: str, delivery: str,
                pack_spec_no: str = "") -> AutoSelection:
    """port of ``バンド選択``。

    決まり方は3段で、**あとの段が前の段を上書きします**(VBA の書き順どおり)。

        1. 既定 … 帯鉄シール無・縦バンドアングル無し
        2. 取引先がPET指定の相手なら PETバンド
        3. 取引先+納入先の輸出形態、または包装仕様NO で PET+縦1本
    """
    out = AutoSelection(band_kind=BAND_NO_SEAL)
    customer = (customer or "").strip()
    spec = str(pack_spec_no).strip()

    if customer in PET_CUSTOMERS:
        out.band_kind = BAND_PET
        out.decided.append("band_kind")

    rule = find_export_rule(customer, delivery)
    if rule is not None:
        out.band_kind = rule.band_kind
        if "band_kind" not in out.decided:
            out.decided.append("band_kind")
        if rule.interleaf is not None:
            out.interleaf = rule.interleaf
            out.decided.append("interleaf")
        if rule.vertical_bands is not None:
            out.vertical_bands = rule.vertical_bands
            out.decided.append("vertical_bands")
        out.note = rule.note

    if spec in PET_PACK_SPECS:
        out.band_kind = BAND_PET
        out.vertical_bands = 1
        for name in ("band_kind", "vertical_bands"):
            if name not in out.decided:
                out.decided.append(name)
    return out


def use_angle(vertical_bands: float) -> bool:
    """縦バンドを1本でも掛けるなら縦バンドアングルを使う。"""
    return (vertical_bands or 0) >= ANGLE_MIN_VERTICAL_BANDS


def select_all(*, vc_front: str = "", vc_back: str = "",
               interleaf_flag: str = "", customer: str = "",
               delivery: str = "", pack_spec_no: str = "",
               vertical_bands: Optional[float] = None) -> AutoSelection:
    """オーダー1件から、決まるものを全部決める。

    VBA は ``Hiki展開`` の末尾で ``VC選択`` → ``合紙選択`` → ``バンド選択``
    をこの順に呼んでいました。**順番に意味があります** ── 合紙は
    「合紙」列で一度決まったあと、輸出形態が上書きすることがあります。
    """
    out = select_vc(vc_front, vc_back)
    out.interleaf = select_interleaf(interleaf_flag)
    if out.interleaf:
        out.decided.append("interleaf")

    band = select_band(customer, delivery, pack_spec_no)
    out.band_kind = band.band_kind
    out.vertical_bands = band.vertical_bands
    out.note = band.note
    if "interleaf" in band.decided:
        # 輸出形態が合紙を決めていれば、そちらが勝つ ── ｲｲﾀﾞｹｲｷﾝ(ｶ/ﾀｲﾜﾝ は
        # オーダーの合紙列が "1" でも「無し指定」になる(VBA のコメント)
        out.interleaf = band.interleaf
    for name in band.decided:
        if name not in out.decided:
            out.decided.append(name)

    # 縦バンドアングルは縦バンドの本数から決まる。自動で決まった本数があれば
    # それを、無ければ画面に入っている本数を見る
    bands = out.vertical_bands if out.vertical_bands is not None else vertical_bands
    if bands is not None:
        out.angle = use_angle(bands)
        if out.angle:
            out.decided.append("angle")
    return out
