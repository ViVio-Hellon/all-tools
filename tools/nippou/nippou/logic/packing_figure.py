"""梱包図 ── 寸法から梱包の外形を組み立てる (VBA ``図形展開``)

【VBAは何をしていたか】
`UFGW` の梱包図ボタン(`CommandButton5_Click`)が「梱包図形」シートを開き、
標準モジュール `GW計算` の3つの Sub がオートシェイプで断面図を描いて、
`CreatePictureFromClipboard`(OLE API)で画像にしてフォームへ貼っていました。

    図形展開   約460行   梱包の外形(板の山・パレット・アングル・バンド)
                         ※ アングル・合板はいまの梱包に合わせて描き直した(`build`)
    分析矢印   約410行   寸法の矢印と寸法線
    中身       約200行   合紙・ポリシート・ハードボードなどの中身

【ここが持つのは外形だけ】
使い方を聞いたところ「**輸出梱包の完成確認程度**」「印刷不要」でした。
完成した梱包を見て合っているか確かめるものなので、要るのは

    どんな形か(幅・高さ・奥行と積み形態) / バンドは何本どこを通るか /
    縦バンドアングルは付くか / 蓋 / パレットの脚は何本か

です。**中身は梱包すると見えません**から、完成確認には要りません
(`中身` は移植していません)。寸法の矢印(`分析矢印`)も、数字は左の欄に
出ているので外しました。

【座標は持たない。cm の箱だけ持つ】
ここが返すのは **cm の直方体の並び**です。画面の大きさに合わせる倍率も、
斜めに倒す投影も、絵にするのは `views/gw.js` の仕事 ── グラフ
(`presenters/dashboard.py` → `chart.js`)と同じ分け方にします。

**並び順は描く順**です。VBA がシェイプを作った順に重ね描きしていたのと
同じで、後のものが前に出ます(奥から手前へ)。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .gw_autoselect import BAND_PET
from .gw_calculation import ANGLES_PER_VERTICAL_BAND, MAX_VERTICAL_BANDS

#: 奥行きの倒し方。VBA が座標に掛けていた ``0.354`` そのもの
#: (45度に倒して奥行きを半分に見せる、いわゆるキャビネット図)
DEPTH_RATIO = 0.354

#: 箱の種類。**色を決めるのはここではありません** ── 画面が種類ごとの
#: 色を持ちます(`tokens.css`)。ここは「何の部材か」だけを言います
KIND_PRODUCT = "product"        # 製品(板の山)
KIND_HARDBOARD = "hardboard"    # 上下のハードボード
KIND_PALLET = "pallet"          # パレットの角材・脚
KIND_LID = "lid"                # 蓋の板(製品の上に載せる)
KIND_LID_CLEAT = "lid-cleat"    # 蓋の桟(縦桟・横桟)。蓋板の上に組む角材
KIND_ANGLE = "angle"            # 縦バンドアングル(縦バンドの角の保護)
KIND_BAND = "band"              # バンド

#: 蓋板の厚み(cm)。**いまの梱包は蓋を必ず使う**
LID_CM = 2.0
#: 蓋の桟(cm)。**蓋は板1枚ではなく、板の上に桟を組んだもの**(v3.99.0)
#:
#:     あれー蓋描写しないね(VBA の梱包図には板と桟の蓋が描かれている)
#:
#: 縦桟 … 丈方向(手前→奥)に走る角材。本数はパレットの角材と同じ
#:         (蓋はパレットを伏せたような形)
#: 横桟 … 幅方向に渡す角材。**横バンドの下に1本ずつ**(バンドは横桟の上を通る)。
#:         左右に少しはみ出す
CLEAT_W = 8.0                   # 縦桟の幅(パレットの角材と同じ)
CLEAT_H = 2.5                   # 桟の厚み
CROSS_D = 5.0                   # 横桟の奥行き(幅)
CROSS_OVERHANG = 2.0            # 横桟の左右のはみ出し
#: 縦バンドアングルの大きさ(cm)。角に当てる短い部材
ANGLE_CM = 3.0

#: バンドの**材質**。色はここでは決めません(`tokens.css`) ── 画面が
#: `fig-box--band-pet` / `fig-box--band-steel` として塗り分けます。
#:
#:     PETバンド  … 緑
#:     それ以外    … 青(帯鉄 シール無・シール有)
#:
#: 輸出梱包の完成確認で「どのバンドで締めたか」を、字を読まずに
#: 見分けるためです。VBA も PETバンドだけ縦バンドの色を変えていました
#: (`SchemeColor = 7`)。
TONE_PET = "pet"
TONE_STEEL = "steel"


def band_tone(band_kind: str) -> str:
    """バンドの種別から材質を決める。**PET 以外はみな帯鉄。**"""
    return TONE_PET if str(band_kind).strip() == BAND_PET else TONE_STEEL


#: 材質を字で言うときの呼び名(図の脇の「バンド種別」に添える)
TONE_WORD = {TONE_PET: "緑", TONE_STEEL: "青"}

#: パレット角材の間隔を決める割り算。VBA の `(HABA + 4 - 8) / y`
_PALLET_MARGIN = 4.0
_PALLET_TIMBER = 8.0

#: 図を描けない理由。
#
# **入力の良し悪しはここで言いません。** VBA は梱包図が別のボタンだった
# ので、押したときに「横バンドの入力が不正です」と自前で確かめていました。
# Web版は計算ボタン1つで、図はその結果から作ります ── 入力の関門は
# `gw_calculation.validate_inputs` の1か所にあり(板厚・巾・丈・枚数・
# 横バンド2本以上・梱包高さを**すべて**見ています)、そこを通らなければ
# 図まで来ません。
#
# ここで言うのは「渡された数では形にならない」だけです。文言を2か所に
# 持つと、片方を直した日にもう片方が古いことを言い始めます。
@dataclass(frozen=True)
class FigureProblem:
    field: str
    message: str


@dataclass(frozen=True)
class Box:
    """直方体1つ。**cm で持つ**(画面の大きさは画面が決める)。

    `x` `y` は製品の左上を原点とした位置で、`y` は**下が正**です
    (画面と同じ向き。VBA も 300 を基準に下へ足していました)。
    """

    kind: str
    x: float
    y: float
    w: float
    h: float
    depth: float
    #: 何の部材か。画面が図の脇に出す
    label: str = ""
    #: 材質(いまはバンドだけ ── `TONE_PET` / `TONE_STEEL`)。**色ではない**
    tone: str = ""

    def as_dict(self) -> dict:
        return {"kind": self.kind, "x": round(self.x, 3),
                "y": round(self.y, 3), "w": round(self.w, 3),
                "h": round(self.h, 3), "depth": round(self.depth, 3),
                "label": self.label, "tone": self.tone}


@dataclass
class Figure:
    """梱包図ひとそろい。**画面はこれを写すだけ。**"""

    boxes: list[Box] = field(default_factory=list)
    #: 図全体の大きさ(cm)。画面が倍率を決めるのに使う
    width_cm: float = 0.0
    height_cm: float = 0.0
    depth_cm: float = 0.0
    #: 図の脇に字で出すもの。**色や形だけに頼らない**
    facts: list[dict[str, str]] = field(default_factory=list)
    #: 描けなかった理由。空なら描けている
    problems: list[FigureProblem] = field(default_factory=list)

    @property
    def drawable(self) -> bool:
        return not self.problems and bool(self.boxes)

    def as_dict(self) -> dict:
        return {
            "boxes": [b.as_dict() for b in self.boxes],
            "width_cm": round(self.width_cm, 2),
            "height_cm": round(self.height_cm, 2),
            "depth_cm": round(self.depth_cm, 2),
            "depth_ratio": DEPTH_RATIO,
            "facts": self.facts,
            "problems": [{"field": p.field, "message": p.message}
                         for p in self.problems],
            "drawable": self.drawable,
        }


#: 形にするのに 0 より大きくないと困る寸法。**入力の良し悪しではなく、
#: 「その数では絵にならない」だけ**を見ます
_NEEDED = (("thickness_mm", "板厚"), ("width_mm", "製品巾"),
           ("length_mm", "製品丈"), ("count", "梱包枚数"))


def check(**values: float) -> list[FigureProblem]:
    """その数で形になるか。**入力の関門ではありません。**

    通る道では `gw_calculation.validate_inputs` を通ったあとの値しか
    来ないので、ここが何か言うことはまずありません。`build()` を単体で
    呼んだときに、黙って潰れた絵を返さないための歯止めです。
    """
    out = [FigureProblem(key, f"{label}が0のため、図にできません。")
           for key, label in _NEEDED if values.get(key, 0) <= 0]
    # 横バンドが1本だと、図の上を走る帯が引けない(VBA も `YOBA <= 1`
    # で梱包図ボタンを断っていた。いまは計算側が同じ条件で断る)
    if values.get("horizontal_bands", 0) <= 1:
        out.append(FigureProblem(
            "horizontal_bands", "横バンドが2本未満のため、図にできません。"))
    # 縦バンドの置き方は「1本は中央・2本は左右対称」の2つだけ。
    # 3本目の置き場所は決まっていない(計算側も同じ条件で断る)
    if values.get("vertical_bands", 0) > MAX_VERTICAL_BANDS:
        out.append(FigureProblem(
            "vertical_bands",
            f"縦バンドが{MAX_VERTICAL_BANDS}本を超えるため、図にできません。"))
    return out


def pallet_timbers(width_mm: float, columns: int, *, four_way: bool) -> int:
    """パレット角材の本数 (VBA の `y`)。

    4方向パレット(`four_way`)のときだけ幅で変わります ── 600〜1300mm は
    3本、それ以外は 330mm ごとに1本(切り上げ)。2方向パレットは3本に固定。
    どちらも積み形態(列)の数だけ増えます。

    VBA はこれを「アングルを使うか」で分けていました。アングルは縦バンドの
    角当て(個数で数える部材)に改めたので、パレットの形は**縦バンド2本か**
    で決めます ── VBA でアングルが付くのは縦2本のときだったので、描かれる
    パレットはこれまでと変わりません。
    """
    if four_way and 600 < width_mm < 1300:
        per_column = 3
    elif four_way:
        per_column = math.ceil(width_mm / 330) if width_mm > 0 else 1
    else:
        per_column = 3                      # 2方向パレットは固定
    return max(1, per_column * max(1, columns))


#: 縦バンドの幅(cm)。VBA の縦バンドのシェイプと同じ 2
VERTICAL_BAND_W = 2.0


def vertical_band_positions(haba: float, span: float, timbers: int,
                            bands: int) -> list[float]:
    """縦バンドの左端の x(cm)。**1本は中央、2本は左右対称。**

    2本のときの右は、これまでどおり「最後から2本目の角材の右」
    (VBA の縦バンド右の置き方)。左はそれを**中央で折り返した位置**です。
    折り返しで決めるので、角材の本数や積み形態が変わっても必ず左右対称に
    なります。
    """
    if bands <= 0:
        return []
    if bands == 1:
        return [haba / 2 - VERTICAL_BAND_W / 2]
    right = -2 + 8 + 4 + span * (timbers - 1)
    left = haba - right - VERTICAL_BAND_W
    return [left, right]


def vertical_band_fact(bands: int) -> str:
    """縦バンドの本数を、**どこを通るか**まで字で言う。"""
    where = {1: "(中央)", 2: "(左右)"}.get(bands, "")
    return f"{bands} 本{where}"


def build(*, thickness_mm: float, width_mm: float, length_mm: float,
          count: float, horizontal_bands: float, vertical_bands: float,
          pack_height_mm: float, columns: int = 1,
          use_angle: bool = True, band_kind: str = "") -> Figure:
    """寸法から外形を組み立てる (VBA `図形展開` を、いまの梱包に合わせて)。

    VBA と同じく **cm** で組みます(mm を10で割る)。原点は製品の左上で、
    そこから上下へ部材を積みます。

    【VBA から変えたところ】(「VBAの梱包図の仕様が古かった」)

        全丈アングル   製品の左右の上角に**丈いっぱい**のアングルを描いて
                       いた ── 蓋を使わない頃の梱包。**描きません**
        合板           縦2本のとき上下に合板を挟んでいた。**描きません**
        蓋             **いつも上に載せる**(いまの梱包は蓋しか使わない)。
                       蓋板の上に縦桟(パレットの角材と同じ本数)と横桟
                       (横バンドの下に1本ずつ)を組む ── 板1枚だけだと
                       パレットと同じ色の平らな箱になり、蓋に見えなかった
        縦バンドアングル 縦バンドが角を回る4か所(天面・底面の手前と奥)に
                       1つずつ。縦バンド1本なら4個、2本なら8個

    `use_angle` は縦バンドアングルを描くか(資材の「縦バンドアングル」の
    チェック)。縦バンドが無ければ、チェックがあっても描きません。
    """
    problems = check(thickness_mm=thickness_mm, width_mm=width_mm,
                     length_mm=length_mm, count=count,
                     horizontal_bands=horizontal_bands,
                     vertical_bands=vertical_bands)
    if problems:
        return Figure(problems=problems)

    retu = max(1, int(columns))
    t = thickness_mm / 10.0
    take = length_mm / 10.0                 # 奥行(板丈)
    haba = width_mm / 10.0 * retu           # 全幅(列を横に並べる)
    stack = math.ceil(count / retu)         # 積み枚数(VBA `tumisuu/retu` 切上)
    takasa = stack * t                      # 板の山の高さ

    z = max(2, int(horizontal_bands))       # 横バンド
    # 縦バンド。**0本なら描きません**
    taba = max(0, int(vertical_bands))
    tone = band_tone(band_kind)
    y = pallet_timbers(width_mm, retu, four_way=taba == 2)
    angles = use_angle and taba > 0

    lid_top = -0.3 - LID_CM                 # 蓋板の上面
    cleat_top = lid_top - CLEAT_H           # 縦桟の上面
    cross_top = cleat_top - CLEAT_H         # 横桟の上面(バンドはこの上を通る)
    bottom = takasa + 0.3                   # 下のハードボードの下面

    boxes: list[Box] = []
    add = boxes.append

    # ---- 奥から手前へ。**この順が描く順**(後のものが前に出る) --------

    # パレットの脚(丈方向)。横バンドの本数ぶん、奥へずらして並ぶ
    for i in range(z * 2 - 1, 0, -2):
        slide = ((take + 4 - 12) * DEPTH_RATIO) / (z * 2) * i
        add(Box(KIND_PALLET, -2 - 2 * DEPTH_RATIO + slide,
                takasa + 8 - 2 * DEPTH_RATIO - slide + 0.3,
                haba + 4, 16, 12, "パレット脚"))

    # パレットの丈木材。幅いっぱいに y+1 本
    span = (haba + _PALLET_MARGIN - _PALLET_TIMBER) / y
    for i in range(y + 1):
        add(Box(KIND_PALLET, -2 - 2 * DEPTH_RATIO + span * i,
                takasa + 2 * DEPTH_RATIO + 0.3,
                _PALLET_TIMBER, _PALLET_TIMBER, take + 4, "パレット角材"))

    add(Box(KIND_HARDBOARD, 0, takasa, haba, 0.3, take, "ハードボード(下)"))

    positions = vertical_band_positions(haba, span, y, taba)
    back = (take - ANGLE_CM) * DEPTH_RATIO  # 奥の端までずらす量

    def angle(x: float, top: float, where: str, *, far: bool) -> None:
        shift = back if far else 0.0
        add(Box(KIND_ANGLE, x - 0.5 + shift, top - shift,
                VERTICAL_BAND_W + 1, ANGLE_CM, ANGLE_CM,
                f"縦バンドアングル({where})"))

    # 底の奥の角は製品の陰になるので、**製品より先に**描く
    if angles:
        for x in positions:
            angle(x, bottom - ANGLE_CM + 0.5, "底・奥", far=True)

    # 製品(板の山)。**図の主役**
    add(Box(KIND_PRODUCT, 0, 0, haba, takasa, take, "製品"))

    add(Box(KIND_HARDBOARD, 0, -0.3, haba, 0.3, take, "ハードボード(上)"))
    add(Box(KIND_LID, 0, lid_top, haba, LID_CM, take, "蓋"))

    # 蓋の縦桟。蓋板の上を手前から奥まで。**パレットの角材と同じ本数**を
    # 蓋の幅に等間隔で並べる(左右の端は蓋板の端にそろえる)
    cleats = y + 1
    for i in range(cleats):
        add(Box(KIND_LID_CLEAT, (haba - CLEAT_W) * i / max(1, cleats - 1), cleat_top,
                CLEAT_W, CLEAT_H, take, "蓋の縦桟"))

    # 天面の角当ては、縦バンドが曲がるところ(蓋の桟の上の角)に当てる
    if angles:
        for x in positions:
            angle(x, cross_top - 0.5, "天・奥", far=True)

    # 横バンドの奥行き位置(手前からの cm)。横バンドが通るところに横桟を渡す
    def cross_depth(i: int) -> float:
        return 3 + (take + 4 - 12) / (z * 2) * i

    # 蓋の横桟。**奥から手前へ**(手前のものが前に出る)。横バンドの真下に1本ずつ、
    # 左右へ少しはみ出す
    for i in range(z * 2 - 1, 0, -2):
        d = cross_depth(i) + 1 - CROSS_D / 2          # 横バンド(奥行き2)の中心に合わせる
        add(Box(KIND_LID_CLEAT, -CROSS_OVERHANG + d * DEPTH_RATIO, cross_top - d * DEPTH_RATIO,
                haba + CROSS_OVERHANG * 2, CLEAT_H, CROSS_D, "蓋の横桟"))

    # 縦バンド。**置き方は本数で決まります**(1本は中央、2本は左右対称)。
    # 天面を手前から奥へ走るぶん(奥行きいっぱい)と、端面を降りるぶん。
    # 蓋の桟の上を通る
    band_top = cross_top - 1.0
    band_h = bottom + 1.0 - band_top

    def vertical_band(x: float) -> None:
        add(Box(KIND_BAND, x, band_top, VERTICAL_BAND_W, 1, take,
                "縦バンド(天面)", tone))
        add(Box(KIND_BAND, x, band_top, VERTICAL_BAND_W, band_h, 1,
                "縦バンド(端面)", tone))

    for x in positions:
        vertical_band(x)

    # 手前の角のアングルは、バンドの**上から**当てる
    if angles:
        for x in positions:
            angle(x, cross_top - 0.5, "天・手前", far=False)
            angle(x, bottom - ANGLE_CM + 0.5, "底・手前", far=False)

    # 横バンド。天面(蓋の横桟の上)を走るぶんと、右の側面を降りるぶん。
    # 奥のものから描くので、`z*2-1` から2つ飛ばしで戻ります(VBAと同じ)
    for i in range(z * 2 - 1, 0, -2):
        d = cross_depth(i)
        left = -2 + d * DEPTH_RATIO
        top = band_top - d * DEPTH_RATIO        # 横桟の上面にちょうど載る
        add(Box(KIND_BAND, left, top, haba + 4, 1, 2, "横バンド(上)", tone))
        # 側面は上面の右端から、パレットの脚の下まで降りる
        add(Box(KIND_BAND, left + haba + 4, top, 1,
                takasa + 24.0 - band_top, 2, "横バンド(横)", tone))

    facts = [
        {"label": "積み形態", "value": f"{retu}山積み"},
        {"label": "積み枚数", "value": f"{stack} 枚"},
        {"label": "外寸(幅×高さ×奥行)",
         "value": f"{haba:.1f} × {takasa:.1f} × {take:.1f} cm"},
        {"label": "横バンド", "value": f"{z} 本"},
        {"label": "縦バンド", "value": vertical_band_fact(taba)},
        {"label": "縦バンドアングル",
         "value": (f"{taba * ANGLES_PER_VERTICAL_BAND} 個(縦バンド {taba} 本 × "
                   f"{ANGLES_PER_VERTICAL_BAND})") if angles else "なし"},
        {"label": "蓋", "value": f"あり(縦桟 {cleats} 本・横桟 {z} 本)"},
        {"label": "パレット角材", "value": f"{y + 1} 本"},
    ]
    if band_kind:
        # **色の意味を字でも言う。** 図の色だけでは、何色が何か分かりません
        facts.append({"label": "バンド種別",
                      "value": f"{band_kind}({TONE_WORD[tone]})"})

    return Figure(boxes=boxes, width_cm=haba, height_cm=takasa,
                  depth_cm=take, facts=facts)
