"""ロット番号を打つと行が埋まる (VBA `SQLiteLot検索`)

【VBAが何をしていたか】
日報入力の LOT 欄は `LOT1_Change`〜`LOT12_Change` で `SQLiteLot検索` を
呼んでいました。**7桁そろった時点で**3つのファイルを辿り、その行の
ほとんどを埋めます:

    SIKALOT  ﾛｯﾄ番号で1行   → 材・調質 / 寸法 / 検入枚数 / 合紙 / 用途
          ↓ (ﾛｯﾄ番号)
    SIKAHIKI ﾛｯﾄ番号で1行   → 受注番号            ← ここが唯一の橋
          ↓ (受注番号)
    SIKAODR  受注番号で1行  → VC / 製品単重 / 合紙 / 包装仕様NO / EX

**この道順しかありません。** ロット番号から受注番号を知る手立ては
SIKAHIKI だけなので、そこで見つからなければ VC も単重も出ません
(VBA も `MsgBox "SIKAHIKIヒットなし"` で打ち切っていました)。

【VBAと変えたところ】
VBA は SIKAHIKI で**最初に見つかった1件**を黙って使っていました。
1つのロットが複数の受注に引き当てられていると、そこで拾った受注の
VC・単重・合紙が実際に梱包する相手と食い違いますが、押した人には
見分けが付きません。ここは引当を全部返し、**複数あるときは選ばせます**
(`access_bridge/gw_master.search_allocations`)。

【ここは計算だけ】
ファイルを読むのは `access_bridge/gw_master`、画面へ返すのは
`app/routes/entry.py`。ここは受け取った値から**書く値を決めるだけ**で、
テストがファイル無しで書けます。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from . import etc_marks, g_course
from .numeric import format_fixed, is_numeric, to_float
from .pack_note import PackNote

# ロット番号は7桁そろってから引く(VBA `If Len(...) <> 7 Then Exit Sub`)。
# 打っている途中で毎回引きに行くと、共有ファイルを7回読むことになる
LOT_NO_LENGTH = 7

# 丈が 0 のとき「ｺｲﾙ」と書くライン(VBA の `Case UFdaily.LS` / `Case NS1`。LS = 機側)。
# **AIM は入りません** ── VBA の `Select Case` に無いので、丈0でも数字を書く
COIL_LINES: frozenset[str] = frozenset({"機側", "NS1"})

# 単重(UNI)を入れないライン。コイルは1枚の重さという考え方をしない
NO_UNIT_WEIGHT_LINES: frozenset[str] = COIL_LINES

# コイルの割り数(`LS4LOT`)を引くライン。**コイル形状の機側・NS1 だけ**(v4.21.0)。
# VBA は `If UFdaily.LS Or UFdaily.NS1 Or UFdaily.AIM` で AIM も引いていたが、
#     機側、NS1はコイル形状担当です その他は板形状です / AIMはコイル割り数不要です
# 包み数の自動計算(`PACKAGE_CALC_LINES`)は AIM を含むまま ── **同じ集まりではない**
COIL_SPLIT_LINES: frozenset[str] = COIL_LINES

# 新しいロット番号を打ったときに空にする欄(VBA の先頭の一括クリア)。
# **LOT 自身は消しません。** 打ったばかりの番号を消すことになります
CLEARED_FAMILIES: tuple[str, ...] = (
    "ZAI", "SIZ", "KEN", "SZ", "SH", "HIT", "MAI", "TUT", "VC",
    "S", "TH", "SS", "THS", "STH", "THT", "S4", "TH4", "S5", "TH5",
    "CON", "WEI", "ET", "TIM", "UNI",
)

# 断りの種類。**文言から推し量らない**
REFUSE_SHORT = "short"           # 7桁に足りない
REFUSE_NO_LOT = "no_lot"         # SIKALOT に無い
REFUSE_NO_HIKI = "no_hiki"       # SIKAHIKI に無い
REFUSE_UNREADABLE = "unreadable"  # 参照ファイルが読めない


@dataclass
class Refusal:
    """引けなかった理由。**画面に出す文言もここが持つ。**"""

    reason: str
    message: str


@dataclass
class RowFill:
    """1行ぶんの、埋める値。

    `values` に入っているものだけを書き換えます ── 入っていない欄は
    画面の値をそのまま残します。
    """

    values: dict[str, str] = field(default_factory=dict)
    #: 自動で押した etc の印(押した覚えの無い人へ理由を出すのに使う)
    auto_marks: list[str] = field(default_factory=list)
    #: 使った引当(選ばせたときはその1件)
    order_no: str = ""
    hiki_no: str = ""
    #: 包装仕様の注意(`logic/pack_note.py`)。**コイルは画面の前に出す**
    note: PackNote = field(default_factory=PackNote)
    #: Gコースのロットなら、その中身(`logic/g_course`)。画面に「Gコースだった」と出す
    g_course: Optional[g_course.GCourse] = None


def format_zai(material: str, temper: str) -> str:
    """材・調質(VBA `製造材質 & "-" & " " & 製造調質`)。

    紙の実物は「52S- R」「F52S- R」。**ハイフンの後ろに空白**が入るのが
    VBA の書き方そのままで、紙もそう刷られています。
    """
    return f"{(material or '').strip()}- {(temper or '').strip()}".rstrip()


def format_siz(thickness: float, width: float, length: float,
               *, line: str = "") -> str:
    """製品寸法 厚×幅×丈(VBA の `Format` そのまま)。

        厚 … 0.000  /  幅・丈 … 0.0

    LS・NS1 で丈が 0 のときだけ「ｺｲﾙ」と書きます。紙の実物は
    「20.000×1528.0×3053.0」。端数は VBA と同じく 0.5 を切り上げます
    (Python の書式指定だと 1528.25 が 1528.2 になってしまう)。
    """
    size = f"{format_fixed(thickness, 3)}×{format_fixed(width, 1)}"
    if line in COIL_LINES and not length:
        return f"{size}×ｺｲﾙ"
    return f"{size}×{format_fixed(length, 1)}"


def sheet_count(value) -> str:
    """検入枚数。**整数で書く**(v4.18.0)。

        検入枚数は整数です

    SIKALOT の写しでは枚本数が実数の列のことがあり、`2874.0` のまま入って
    いました。数として読めれば整数に(小数は四捨五入)、読めなければそのまま。
    """
    text = str(value if value is not None else "").strip()
    if not is_numeric(text):
        return text
    number = to_float(text)
    # **四捨五入**(`round` は偶数へ寄せるので 12.5 が 12 になる)
    half_up = math.floor(abs(number) + 0.5)
    return str(-half_up if number < 0 else half_up)


def format_vc(front: str, back: str) -> str:
    """ＶＣ種別(VBA の4分岐)。

        両方あり → "A:表_B:裏"    表だけ → "A:表"
        裏だけ   → "B:裏"          どちらも無し → ""

    紙の実物は「A:VE-20NF_B:VE-20NF」「A:V325NW」。
    """
    a = (front or "").strip()
    b = (back or "").strip()
    if a and b:
        return f"A:{a}_B:{b}"
    if a:
        return f"A:{a}"
    if b:
        return f"B:{b}"
    return ""


def interleaf(flag: str) -> str:
    """合紙。"1" なら有、"0" なら無、それ以外は決めない(空)。

    **出どころは2つあります。** VBA の日報入力(`SQLiteLot検索`)は
    SIKALOT の `梱包_合紙` を、GW計算(`合紙選択`)は SIKAODR の
    `合紙` を見ていました。現場は「SIKAODR の合紙を参照」と言うので、
    受注側を先に見て、無ければロット側に落とします(`interleaf_of`)。
    """
    text = (flag or "").strip()
    if text == "1":
        return "有"
    if text == "0":
        return "無"
    return ""


def interleaf_of(order_flag: str, lot_flag: str) -> str:
    """合紙を決める。**受注(SIKAODR)が先、ロット(SIKALOT)が控え。**

    受注が引けなかったとき(引当が無い・受注番号が SIKAODR に無い)でも
    ロット側に値があれば決まります ── どちらも無ければ空のままで、
    作業者が選びます。
    """
    return interleaf(order_flag) or interleaf(lot_flag)


def unit_weight(value: str, *, line: str = "") -> str:
    """単重(製品単重)。コイルのラインには入れない(VBA の `Case LS/NS1`)。"""
    if line in NO_UNIT_WEIGHT_LINES:
        return ""
    text = (value or "").strip()
    return text if is_numeric(text) else ""


def others(usage_code: str, usage_name: str, delivery: str,
           pack_spec_no: str, coil_v: str = "", coil_h: str = "") -> dict[str, str]:
    """紙の印刷範囲外(AN〜AS列)に出る6つ。

    VBA は `OthersT` に1本のカンマ区切りで持たせていましたが、DBには
    `others1`〜`others6` の6列があり、紙も AN:用途コード / AO:用途名 /
    AP:納入先 / AQ:包装仕様書No / AR:コイル縦割 / AS:コイル横縦割 と
    6つに分かれています。**分かれている先に合わせて**入れます。
    """
    return {
        "others1": (usage_code or "").strip(),
        "others2": (usage_name or "").strip(),
        "others3": (delivery or "").strip(),
        "others4": (pack_spec_no or "").strip(),
        "others5": (coil_v or "").strip(),
        "others6": (coil_h or "").strip(),
    }


def cleared() -> dict[str, str]:
    """新しいロット番号を打ったときの、まっさらな行。"""
    return {family: "" for family in CLEARED_FAMILIES}


def check_length(lot_no: str) -> Optional[Refusal]:
    """7桁そろっているか。足りなければ**黙って何もしない**ための判定。

    VBA も 7 桁でなければ `Exit Sub` するだけで、何も言いませんでした
    ── 打っている途中に毎回「短い」と出ると、打ち終われません。
    """
    if len((lot_no or "").strip()) != LOT_NO_LENGTH:
        return Refusal(REFUSE_SHORT, "")
    return None


def build(*, line: str, lot, allocation, order, coil=None,
          note: Optional[PackNote] = None) -> RowFill:
    """引けた値から、書く値を組み立てる。**VBA の順そのまま。**

    `lot` は `access_bridge.gw_master.LotInfo`、`allocation` は
    `Allocation`、`order` は `OrderInfo`(引けなければ `None`)、
    `coil` は `CoilSplit`(機側・NS1 以外と、ヒットしないときは `None`)、
    `note` は包装仕様の注意(`logic/pack_note.PackNote`)。
    型で縛らずに属性だけ見るのは、テストが軽い作り物を渡せるようにする
    ため ── ファイルを読まずに式だけ確かめられます。
    """
    fill = RowFill(values=cleared())
    values = fill.values

    values["ZAI"] = format_zai(lot.material, lot.temper)
    # 寸法は Gコース(設計_設備ｺｰｽ に GCT/GFS/GSS)なら BOX最終実績の寸法(v4.16.0)。
    # **Gコースだったことは行に控える**(`box_course` ── 寸法の欄に印。v4.17.0)。
    # Gコースでないロットを打ち直したら消す
    fill.g_course = g_course.from_lot(lot)
    values["SIZ"] = format_siz(*g_course.product_size(lot), line=line)
    values[g_course.ROW_FAMILY] = fill.g_course.stored if fill.g_course else ""
    # 検入枚数 = BOX最終実績_枚本数(`gw_master._box_final`)。**整数で**(v4.18.0)
    values["KEN"] = sheet_count(lot.box_sheets)

    order_no = getattr(allocation, "order_no", "") if allocation else ""
    fill.order_no = order_no
    fill.hiki_no = getattr(allocation, "hiki_no", "") if allocation else ""

    # 合紙は**決まったときだけ**書く。VBA も "0"/"1" のときしか触らず、
    # それ以外は前の値を残していた ── 空で上書きすると、作業者が選んだ
    # 「有」がロットを打ち直すたびに消える
    order_interleaf = getattr(order, "interleaf_flag", "") if order else ""
    decided = interleaf_of(order_interleaf, lot.interleaf_flag)
    if decided:
        values["AI"] = decided

    pack_spec_no = ""
    if order is not None:
        values["VC"] = format_vc(order.vc_front, order.vc_back)
        values["UNI"] = unit_weight(_unit_text(order), line=line)
        pack_spec_no = order.pack_spec_no

    values.update(others(lot.usage_code, lot.usage_name, lot.delivery,
                         pack_spec_no,
                         coil_v=getattr(coil, "vertical", "") if coil else "",
                         coil_h=getattr(coil, "horizontal", "") if coil else ""))

    # 包装仕様の注意。**先に入れてから印を押します** ── VBA も
    # `ET` に注意を書いたあとで印を足していたので、紙に出る並びが同じに
    # なります(注意が上、印が下)
    fill.note = note or PackNote()
    if fill.note.has_note:
        values["ET"] = fill.note.text

    # etc の印。**自動で押すのは2つだけ**(残り4つは作業者が押す)
    fill.auto_marks = etc_marks.auto_keys(
        order_no=order_no,
        export_flag=getattr(order, "export_flag", "") if order else "",
        course=lot.course)
    values["ET"] = etc_marks.apply_auto(values.get("ET", ""), fill.auto_marks)
    return fill


def needs_coil_split(line: str) -> bool:
    """コイルの割り数を引くラインか(機側・NS1 ── v4.21.0 で AIM を外した)。"""
    return line in COIL_SPLIT_LINES


def _unit_text(order) -> str:
    """製品単重を文字にする。0 は「入っていない」と同じに扱う。

    **桁を落としません。** `%g` は有効数字6桁までなので、紙の実物にある
    250.04314 が 250.043 になります ── 単重は作業重量のもとになる値
    (重量 = 枚数 × 単重)なので、ここで丸めると重量がずれます。
    `str(float)` は元の値へ戻せる最短の書き方を返すので、桁は落ちません。
    """
    value = getattr(order, "unit_weight_kg", 0.0)
    if not value:
        return ""
    return str(to_float(value))
