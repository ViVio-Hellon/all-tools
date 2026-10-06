"""梱包資材重量計算 (GW計算フォーム) のコアロジック。

VBA標準モジュールの ``NewGW`` サブ（~10262行）にあった「資材処理メイン」
（10322-10470行）の計算式を、Access/Excel依存を排した純粋関数として
移植したもの。単位質量・係数はマスタ検索の結果 (:class:`MaterialRate`)
として引数で渡す設計にしてあり、実際のマスタ検索（``GetUnitMassAndCoefficient``
相当）は ``access_bridge`` 経由で別途行う（``gw_master.py``）。

寸法の単位は VBA と同じく mm、重量は kg。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Optional

StackPattern = Literal[1, 2, 3]


class InvalidPackHeightError(ValueError):
    """port of ``If val(.KOT) <= val(.ATU.value * .MAI) Then`` の
    「梱包高さの数値が不正です」バリデーションエラー。"""


@dataclass(frozen=True)
class MaterialRate:
    """資材重量マスタの1行分: 単位質量と係数のペア
    (``GetUnitMassAndCoefficient`` の戻り値 ``result(0)``/``result(1)`` 相当)。"""

    unit_mass: float
    coefficient: float


ZERO_RATE = MaterialRate(unit_mass=0.0, coefficient=0.0)


@dataclass(frozen=True)
class PackingDimensions:
    """製品寸法・梱包仕様の入力値 (UFGW フォームの各テキストボックス相当)。"""

    thickness_mm: float  # 板厚 (ATU)
    width_mm: float  # 板幅 (HABA)
    length_mm: float  # 板丈 (TAKE)
    count: float  # 枚数 (MAI)
    vertical_bands: float  # 縦バンド本数 (TABA)
    horizontal_bands: float  # 横バンド本数 (YOBA)
    pack_height_mm: float  # 梱包高さ (KOT)
    pallet_weight_kg: float  # パレット+蓋の重量 (PAW)
    stack_pattern: StackPattern = 1  # 積み形態 (山_1/山_2/山_3)


def stack_count(dims: PackingDimensions) -> float:
    """積み枚数 (VBAの ``tumisuu``)。1山積みなら全数、2山/3山なら
    ``Application.RoundUp(MA / n, 0)`` と同じ切り上げ等分。"""
    if dims.stack_pattern == 1:
        return dims.count
    return math.ceil(dims.count / dims.stack_pattern)


def product_height_mm(dims: PackingDimensions) -> float:
    """製品の山の高さ(mm)。**板厚 × 積み枚数**。

    2山・3山積みでは、山1つの高さは全枚数ではなく**積み枚数**
    (`stack_count`)で決まります。ダンプレート・外装紙・縦バンドの式が
    すでに `aT * tumisuu` で高さを取っているのと同じ数です。
    """
    return dims.thickness_mm * stack_count(dims)


def pack_height_too_low(dims: PackingDimensions) -> bool:
    """梱包高さが製品の山より低い(か同じ) ── **ありえない梱包。**

    【VBA と違うところ】
    VBA ``NewGW`` 冒頭は ``val(.KOT) <= val(.ATU.value * .MAI)`` で、
    **全枚数**を掛けていました。1山積みなら同じことですが、2山・3山では
    山の高さの2倍・3倍と比べることになり、**正しい梱包高さを断ります。**

        2山・板厚1mm・1000枚 → 山の高さは 500mm
        梱包高さ 700mm(山 + パレット・蓋)
        VBA: 700 <= 1 × 1000 = 1000 → 「梱包高さの数値が不正です」

    断るだけで数字は変えないので、直しても計算結果は1つも動きません。
    """
    return dims.pack_height_mm <= product_height_mm(dims)


def validate_pack_height(dims: PackingDimensions) -> None:
    """port of 最終チェック(梱包高さ不正)。梱包高さが製品の山の高さ以下なら
    :class:`InvalidPackHeightError` を送出する(:func:`pack_height_too_low`)。"""
    if pack_height_too_low(dims):
        raise InvalidPackHeightError("梱包高さの数値が不正です")


def _weight(base: float, rate: MaterialRate) -> float:
    return base * rate.unit_mass * rate.coefficient


# ==================================================================
# 基準量 ── 単位質量を掛ける前の「面積(m²)」「長さ(m)」
# ==================================================================
# **重量の式から切り出してあります。**
#
#     重量(kg) = 基準量 × 単位質量 × 係数
#
# 切り出した理由は、**計算の内訳を見せるため**です
# (`presenters/gw_breakdown.py`)。内訳のために式をもう一度書くと、
# 片方だけ直したときに「画面の説明と実際の計算が違う」が起きます。
# 重量の関数はここを呼ぶだけなので、ずれようがありません。
#
# ÷10^6 は mm² → m²、÷10^3 は mm → m。**どちらなのかで単位質量の
# 意味が変わる**(kg/m² か kg/m か)ので、内訳にも出します。
def dunplate_base(dims: PackingDimensions) -> float:
    """ダンプレートの面積(m²)。"""
    itaretu = dims.stack_pattern
    tumisuu = stack_count(dims)
    return (dims.thickness_mm * tumisuu) * ((dims.width_mm * itaretu) + dims.length_mm) * 2 / 10**6


def outer_paper_base(dims: PackingDimensions) -> float:
    """外装紙の面積(m²)。側面 + 上下面。"""
    itaretu = dims.stack_pattern
    tumisuu = stack_count(dims)
    return (
        ((((dims.width_mm * itaretu) + dims.length_mm) * 2) * (dims.thickness_mm * tumisuu))
        + (((dims.width_mm * itaretu) * dims.length_mm) * 2)
    ) / 10**6


#: ポリシートは**大きめに包む**ので、幅・丈・高さそれぞれにこれだけ足す(mm)
POLY_SHEET_MARGIN_MM = 200.0


def poly_sheet_sizes(dims: PackingDimensions) -> tuple[float, float, float]:
    """ポリシートで包む寸法(mm) ── (幅, 丈, 高さ)。**どれも +200mm。**

        幅   = 板幅 × 列 + 200     (列を並べた全幅に足す)
        丈   = 板丈 + 200
        高さ = 板厚 × 積み枚数 + 200
    """
    itaretu = dims.stack_pattern
    return (dims.width_mm * itaretu + POLY_SHEET_MARGIN_MM,
            dims.length_mm + POLY_SHEET_MARGIN_MM,
            dims.thickness_mm * stack_count(dims) + POLY_SHEET_MARGIN_MM)


def poly_sheet_base(dims: PackingDimensions) -> float:
    """ポリシートの面積(m²)。**側面ぐるり + 天面1面**、各寸法 +200mm。

        ((幅 + 丈) × 2 × 高さ + 幅 × 丈) ÷ 1,000,000

    (現場の指定: 「((板幅 × 列 + 板丈) × 2 × 板厚 × 積み枚数 + 板幅 × 列 ×
    板丈 各寸法+200mm」)。VBA は外装紙と同じ式(上下2面・余りなし)でした。
    """
    w, length, h = poly_sheet_sizes(dims)
    return ((w + length) * 2 * h + w * length) / 10**6


def interleaf_base(dims: PackingDimensions) -> float:
    """合紙の面積(m²)。**枚数+1** ── 上下にも挟むので1枚多い。

    **2山・3山でも +1 です(山の数を足しません)。** 山ごとに天の1枚が
    要るなら「枚数+山数」になりそうに見えますが、現場では山が複数でも
    合紙1枚でまとめることが多く、判断として「枚数+1」を取っています
    (現場に確認済み。VBA ``NewGW`` も ``MA + 1``)。直さないでください。
    """
    return dims.width_mm * dims.length_mm * (dims.count + 1) / 10**6


def band_lengths(dims: PackingDimensions) -> tuple[float, float]:
    """バンドの長さ(mm)。(縦ぶん, 横ぶん)。"""
    itaretu = dims.stack_pattern
    tumisuu = stack_count(dims)
    band_l = (dims.length_mm + dims.thickness_mm * tumisuu) * 2 * dims.vertical_bands
    band_w = ((dims.width_mm * itaretu) + dims.pack_height_mm) * 2 * dims.horizontal_bands
    return band_l, band_w


def band_base(dims: PackingDimensions) -> float:
    """バンドの長さ(m)。**面積ではない** ── 単位質量は kg/m。"""
    band_l, band_w = band_lengths(dims)
    return (band_w + band_l) / 10**3


#: 縦バンド1本に付ける縦バンドアングルの数。バンドが製品の角を回る4か所
#: (天面の手前・奥、底面の手前・奥)に1つずつ
ANGLES_PER_VERTICAL_BAND = 4


def angle_base(dims: PackingDimensions) -> float:
    """縦バンドアングルの**個数**。縦バンド1本につき4個。

    縦バンドが角を回るところに当てるコーナー保護なので、**長さではなく
    個数**で数えます。以前(VBA)は「板丈ぶんを2本」の長さでした ── 蓋を
    使わない頃の、全丈方向に掛けるアングルの計算が残っていたもので、
    いまの梱包(蓋を使う)には合いません。単位質量は **kg/個**。
    """
    return max(0.0, dims.vertical_bands) * ANGLES_PER_VERTICAL_BAND


def hardboard_base(dims: PackingDimensions) -> float:
    """ハードボードの面積(m²)。上下2枚。"""
    itaretu = dims.stack_pattern
    return (dims.width_mm * itaretu) * dims.length_mm * 2 / 10**6


def vc_film_base(dims: PackingDimensions) -> float:
    """VCフィルムの面積(m²)。**片面ぶん**(A面・B面それぞれに掛ける)。"""
    return dims.width_mm * dims.length_mm * dims.count / 10**6


def dunplate_weight(dims: PackingDimensions, rate: MaterialRate) -> float:
    """ダンプレート重量(kg)。"""
    return _weight(dunplate_base(dims), rate)


def outer_paper_weight(dims: PackingDimensions, rate: MaterialRate) -> float:
    """外装紙重量(kg)。"""
    return _weight(outer_paper_base(dims), rate)


def poly_sheet_weight(dims: PackingDimensions, rate: MaterialRate) -> float:
    """ポリシート重量(kg)。側面 + 天面、各寸法 +200mm(:func:`poly_sheet_base`)。"""
    return _weight(poly_sheet_base(dims), rate)


def interleaf_weight(dims: PackingDimensions, rate: MaterialRate) -> float:
    """合紙重量(kg)。"""
    return _weight(interleaf_base(dims), rate)


def band_weight(dims: PackingDimensions, rate: MaterialRate) -> float:
    """バンド重量(kg)。``rate`` はシール無/シール有/PETバンドいずれか
    選択された種別の単位質量・係数。"""
    return _weight(band_base(dims), rate)


def angle_weight(dims: PackingDimensions, rate: MaterialRate) -> float:
    """縦バンドアングル重量(kg) = 個数 × 単位質量(kg/個) × 係数。"""
    return _weight(angle_base(dims), rate)


def hardboard_weight(dims: PackingDimensions, rate: MaterialRate) -> float:
    """ハードボード重量(kg)。"""
    return _weight(hardboard_base(dims), rate)


@dataclass(frozen=True)
class VcFilmSelection:
    """VCフィルムの使用選択 (CheckBox8=VC使用/9=A面/10=B面相当)。"""

    use_vc: bool = False
    use_side_a: bool = False
    use_side_b: bool = False


def vc_film_weight(
    dims: PackingDimensions,
    selection: VcFilmSelection,
    vc_a_rate: MaterialRate = ZERO_RATE,
    vc_b_rate: MaterialRate = ZERO_RATE,
) -> float:
    """VCフィルム重量(kg)。両面/A片面/B片面/未使用の4パターン。

    **他の資材と違い、係数を掛けない。** VBA の ``NewGW`` は VC だけ
    ``検算VC_A.caption`` の値をそのまま使っていて、その caption は
    ``ComboBox1_Change`` が **単位質量だけ**を入れている
    (``UFGW.検算VC_A.caption = VCTempHiki(i, V_単位質量)``)。``NewGW`` の
    VC の列検索も ``管理番号`` と ``品名`` しか拾っておらず、係数の列は
    見ていない。

    ``VC重量`` マスタには係数の列が**ある**ので、掛けるのが本来の意図
    だった可能性はあります。ただ現場が使っている数値はVBAが出したもので、
    掛ければそちらと合わなくなります。**移植では VBA に合わせます。**
    掛けるべきだと分かったら :func:`_weight` に戻すだけです。
    """
    if not selection.use_vc:
        return 0.0
    base = vc_film_base(dims)
    a_value = base * vc_a_rate.unit_mass
    b_value = base * vc_b_rate.unit_mass
    if selection.use_side_a and selection.use_side_b:
        return a_value + b_value
    if selection.use_side_a:
        return a_value
    if selection.use_side_b:
        return b_value
    return 0.0


@dataclass(frozen=True)
class MaterialFlags:
    """資材ごとの使用有無 (CheckBox1〜7相当)。OFFの資材は重量0扱いになる。"""

    dunplate: bool = True
    outer_paper: bool = True
    interleaf: bool = True
    band: bool = True
    poly_sheet: bool = True
    angle: bool = True
    hardboard: bool = True


@dataclass(frozen=True)
class MaterialRates:
    """各資材のマスタ検索結果一式 (資材重量マスタから取得)。"""

    dunplate: MaterialRate = ZERO_RATE
    outer_paper: MaterialRate = ZERO_RATE
    interleaf: MaterialRate = ZERO_RATE
    band: MaterialRate = ZERO_RATE
    poly_sheet: MaterialRate = ZERO_RATE
    angle: MaterialRate = ZERO_RATE
    hardboard: MaterialRate = ZERO_RATE


@dataclass(frozen=True)
class MaterialWeights:
    """算出された各資材重量(kg)。"""

    dunplate: float
    outer_paper: float
    interleaf: float
    band: float
    poly_sheet: float
    angle: float
    hardboard: float
    vc_film: float

    @property
    def total(self) -> float:
        """port of ``資材 = ダンプレート+外装紙+合紙+バンド+ポリシート+アングル+ハードボード+VCフィルム``(アングルは縦バンドアングル)。"""
        return (
            self.dunplate + self.outer_paper + self.interleaf + self.band
            + self.poly_sheet + self.angle + self.hardboard + self.vc_film
        )


def compute_material_weights(
    dims: PackingDimensions,
    flags: MaterialFlags,
    rates: MaterialRates,
    vc_selection: VcFilmSelection,
    vc_a_rate: MaterialRate = ZERO_RATE,
    vc_b_rate: MaterialRate = ZERO_RATE,
) -> MaterialWeights:
    """port of ``NewGW`` の資材処理メイン一式。使用フラグがOFFの資材は
    計算自体スキップして0にする
    (VBAの ``If .CheckBox1 = False Then .ダンプレート = Format$(0, "0.00")`` 相当)。"""
    return MaterialWeights(
        dunplate=dunplate_weight(dims, rates.dunplate) if flags.dunplate else 0.0,
        outer_paper=outer_paper_weight(dims, rates.outer_paper) if flags.outer_paper else 0.0,
        interleaf=interleaf_weight(dims, rates.interleaf) if flags.interleaf else 0.0,
        band=band_weight(dims, rates.band) if flags.band else 0.0,
        poly_sheet=poly_sheet_weight(dims, rates.poly_sheet) if flags.poly_sheet else 0.0,
        angle=angle_weight(dims, rates.angle) if flags.angle else 0.0,
        hardboard=hardboard_weight(dims, rates.hardboard) if flags.hardboard else 0.0,
        vc_film=vc_film_weight(dims, vc_selection, vc_a_rate, vc_b_rate),
    )


def total_packing_weight(
    pallet_weight_kg: float,
    material_total_kg: float,
    combined_load_kg: float = 0.0,
) -> float:
    """風袋総重量 (HUT)。積合せ重量(``combined_load_kg``)は積合せ有り
    (CheckBox11=True)のときのみ加算する。"""
    return pallet_weight_kg + material_total_kg + combined_load_kg


def gross_weight(tare_weight_kg: float, input_weight_kg: Optional[float]) -> Optional[float]:
    """GW = 風袋総重量 + Aインプット重量。

    VBAは Aインプット重量(TextBox38)が空白/0のとき GW欄そのものを
    非表示にしていた(``.TextBox39.visible = False``)。ここでは
    「表示しない」を ``None`` で表す。
    """
    if input_weight_kg is None or input_weight_kg == 0:
        return None
    return tare_weight_kg + input_weight_kg


# ==================================================================
# 入力チェック (VBA ``CommandButton2_Click`` の計算前チェック)
# ==================================================================
# **計算を押した瞬間に、全部まとめて見る。**
#
# VBA は `ElseIf` で1つ見つけたら `MsgBox` して `Exit Sub` していたので、
# 3か所間違っていると3回押し直すことになりました。ここでは全部集めて
# 返します ── 断る回数を減らすためで、判定そのものは変えていません。
@dataclass(frozen=True)
class InputProblem:
    """1つの断り。``field`` は画面の入力欄の名前(印を付けるのに使う)。"""

    field: str
    message: str


# 「EX2方向」の例外にする板幅(mm)の範囲。VBA ``Case 1220# To 1300#``
EX2_EXEMPT_WIDTH_MM = (1220.0, 1300.0)

# 同じく例外にする包装仕様NO。VBA ``If Me.PackNo <> "7P0106"``
# (2024.11.12 ｲｲﾀﾞｹｲｷﾝ(ｶ 向けに追加された)
EX2_EXEMPT_PACK_SPEC = "7P0106"

# EX2方向のときに要る横バンドの本数
EX2_REQUIRED_HORIZONTAL_BANDS = 4

#: 縦バンドの上限。**1本は中央、2本は左右対称**の2つの形しか無い
#: (`logic/packing_figure.py` もこの数を見ます)
MAX_VERTICAL_BANDS = 2


def ex2_band_problem(dims: PackingDimensions,
                     pack_spec_no: str = "") -> Optional[InputProblem]:
    """port of ``CommandButton2_Click`` 冒頭の「EX2方向は横バンド4本」。

    縦バンドが1本のとき(=EX2方向)、横バンドは4本でなければ計算させません。
    ただし次のどちらかなら通します。

        板幅が 1220〜1300mm      … VBA ``Case 1220# To 1300#``
        包装仕様NOが 7P0106      … VBA ``If Me.PackNo <> "7P0106"``

    **縦バンドが1本でなければ何も言いません。** EX2方向でないときの
    横バンド本数はここでは決まりません。
    """
    if dims.vertical_bands != 1:
        return None
    if dims.horizontal_bands == EX2_REQUIRED_HORIZONTAL_BANDS:
        return None
    low, high = EX2_EXEMPT_WIDTH_MM
    if low <= dims.width_mm <= high:
        return None
    if str(pack_spec_no).strip() == EX2_EXEMPT_PACK_SPEC:
        return None
    return InputProblem("horizontal_bands", "EX2方向は横バンド4本")


def validate_inputs(
    dims: PackingDimensions,
    *,
    pack_spec_no: str = "",
    use_combined_load: bool = False,
    combined_load_kg: float = 0.0,
    vc_selection: VcFilmSelection = VcFilmSelection(),
    vc_name_a: str = "",
    vc_name_b: str = "",
) -> list[InputProblem]:
    """計算してよいか。断る理由を**全部**返す(問題なければ空)。

    VBA ``CommandButton2_Click`` の計算前チェックの移植です。
    """
    problems: list[InputProblem] = []

    # --- 数値が入っているか (VBA `Not IsNumeric(...) Or ... = 0`) ---
    # 空欄も0も同じ扱い。「0mm の板」は業務上ありえない
    for field, label, value in (
        ("thickness_mm", "板厚", dims.thickness_mm),
        ("width_mm", "製品巾", dims.width_mm),
        ("length_mm", "製品丈", dims.length_mm),
    ):
        if value <= 0:
            problems.append(InputProblem(field, f"{label}の入力が不正です。"))

    if dims.count <= 0:
        problems.append(InputProblem("count", "梱包総枚数を入力してください。"))

    # **1本では梱包にならない。** VBA `Me.YOBA <= 1`
    if dims.horizontal_bands <= 1:
        problems.append(InputProblem("horizontal_bands", "横バンドの入力が不正です。"))

    if dims.pack_height_mm <= 0:
        problems.append(InputProblem("pack_height_mm", "梱包高さを入力してください。"))
    elif pack_height_too_low(dims):
        # 製品そのものより低い梱包はありえない(VBA `NewGW` 冒頭の最終チェック)
        problems.append(InputProblem("pack_height_mm", "梱包高さの数値が不正です"))

    # **縦バンドは0〜2本。** 図は1本なら中央、2本なら左右対称に通します
    # (`logic/packing_figure.py`)。3本目の置き場所は決まっていないので、
    # 計算させずに断ります ── 計算だけ通して図が描けない、を作らない
    vertical = dims.vertical_bands
    if vertical < 0 or vertical != int(vertical):
        problems.append(InputProblem("vertical_bands", "縦バンドの入力が不正です。"))
    elif vertical > MAX_VERTICAL_BANDS:
        problems.append(InputProblem(
            "vertical_bands", f"縦バンドは{MAX_VERTICAL_BANDS}本までです。"))

    if dims.pallet_weight_kg <= 0:
        problems.append(InputProblem("pallet_weight_kg", "パレット重量を入力してください。"))

    ex2 = ex2_band_problem(dims, pack_spec_no)
    if ex2 is not None:
        problems.append(ex2)

    # --- VCを使うと言ったのに、品名が選ばれていない ---
    if vc_selection.use_vc:
        if vc_selection.use_side_a and not str(vc_name_a).strip():
            problems.append(InputProblem("vc_name_a", "VCを選択してください"))
        if vc_selection.use_side_b and not str(vc_name_b).strip():
            problems.append(InputProblem("vc_name_b", "VCを選択してください"))
        if not (vc_selection.use_side_a or vc_selection.use_side_b):
            problems.append(InputProblem("vc_name_a", "VCを選択してください"))

    # --- 積合せ有りなのに重量が空 ---
    # VBA は「積合せ重量を入れてください」と言いながら**そのまま計算していた**
    # (`MsgBox` のあと `Exit Sub` が無い)。言うだけで通すと、積合せぶんが
    # 抜けたGWが出てしまうので、こちらは断る
    if use_combined_load and combined_load_kg <= 0:
        problems.append(InputProblem("combined_load_kg", "積合せ重量を入れてください"))

    return problems


#: 資材の見出し(断りの文言に出す)。**並びと名前は `MaterialFlags` と同じ**
MATERIAL_LABELS: dict[str, str] = {
    "dunplate": "ダンプレート", "outer_paper": "外装紙", "interleaf": "合紙",
    "band": "バンド", "poly_sheet": "ポリシート", "angle": "縦バンドアングル",
    "hardboard": "ハードボード",
}

#: 資材重量マスタが読めないときの断り(VBA ``MsgBox "資材重量なし"``)
MASTER_UNREADABLE = ("資材重量マスタを読めません(資材重量なし)。"
                     "重量が計算できないので、参照設定の置き場所を確かめてください")


def unregistered_material_problems(
        flags: MaterialFlags, rates: MaterialRates,
        master_names: Optional[dict[str, str]] = None) -> list[InputProblem]:
    """**使うと言った資材の単位質量が0** ── マスタにその名前が無い。

    VCフィルムの「登録されていないVCです」と同じ理由で止めます。そのまま
    通すと、その資材ぶんが抜けた**軽いGW**が出ます(輸出の書類に載る数)。

    【VBA はどうだったか】
    ``GetUnitMassAndCoefficient`` は見つからないと ``Array("", "")`` を
    返し、``"" * 数`` で**型の不一致(実行時エラー13)**になって止まって
    いました。マスタが丸ごと読めないときは ``"資材重量なし"`` で止めて
    いました。どちらも**計算は出ません。**

    移植では一時「引けたぶんだけ出す」にしていて、抜けたことは内訳の
    注記(6つ目)にしか出ていませんでした。

    使わない資材(チェックを外したもの)は見ません ── 0kg で正しいので。
    """
    names = master_names or {}
    out: list[InputProblem] = []
    for key, label in MATERIAL_LABELS.items():
        if not getattr(flags, key):
            continue
        if getattr(rates, key).unit_mass != 0:
            continue
        name = names.get(key, label)
        # バンドは種別の欄に印を付ける(どの種別で引いたかが分かれ目)
        field = "band_kind" if key == "band" else key
        out.append(InputProblem(
            field, f"{label}: 資材重量マスタに「{name}」がありません。"
                   "重量が計算できないので設定登録してください"))
    return out


def unregistered_vc_problem(vc_selection: VcFilmSelection,
                            vc_film_kg: float) -> Optional[InputProblem]:
    """port of 「登録されていないVCです」。

    VCを使うと言い、品名も選んだのに重量が0 ── **マスタに無い品名**です。
    そのまま通すとVCぶんが抜けたGWになるので、計算後にここで止めます。
    (品名が選ばれているかどうかは :func:`validate_inputs` が先に見ます)
    """
    if not vc_selection.use_vc or vc_film_kg != 0:
        return None
    return InputProblem(
        "vc_name_a",
        "登録されていないVCです。VC重量が計算できないので設定登録してください")


# ==================================================================
# 梱包数ごとの重量 (VBA ``RangePaste``)
# ==================================================================
# 計算結果は**1梱包ぶん**。現場は「この梱包を N 個作るなら資材はいくつ要るか」
# を知りたいので、VBA は 1〜100 梱包ぶんの表を作って メイン C25:L124 に貼り、
# `枚数表示` が ListBox に出していました。
#
#   RangePaste(row, col) = RangePaste(1, col) * row
#
# 掛けるだけなので計算式は要りません。**行数だけがここの決めごと**です。
PER_PACK_ROWS = 100

# 表の列。**並びは VBA の RangePaste の列順そのまま**
PER_PACK_COLUMNS: tuple[str, ...] = (
    "ダンプレート", "外装紙", "合紙", "バンド", "ポリシート",
    "縦バンドアングル", "ハードボード", "VCフィルム", "資材計", "風袋総重量",
)


def per_pack_unit(weights: "MaterialWeights",
                  pallet_weight_kg: float) -> list[float]:
    """1梱包ぶん(表の1行目 = 計算結果そのもの)。**掛け算のもとはここ1つ。**

    最後の列は風袋総重量(資材計 + パレット重量)です ── VBA
    ``RangePaste(1, 10) = Format$(資材 + pw, "0")`` に対応します。
    積合せ重量は**入りません**(梱包ごとに変わるものではないため、VBAも
    ここには入れていません)。
    """
    return [
        weights.dunplate, weights.outer_paper, weights.interleaf,
        weights.band, weights.poly_sheet, weights.angle,
        weights.hardboard, weights.vc_film, weights.total,
        weights.total + pallet_weight_kg,
    ]


def per_pack_row(weights: "MaterialWeights", pallet_weight_kg: float,
                 packs: int) -> list[float]:
    """``packs`` 梱包ぶんの1行。**表を作らずに、その行だけ。**

    紙(`reporting/gw_print.py`)が要るのは1行だけです。表を丸ごと作って
    最後の1行を取り出すと、**手間が梱包数に比例します** ── いまは上限
    100梱包なので実害はありませんが、安全が「上限で丸める」の1枚だけに
    乗っている状態でした。ここが O(1) なら、仮に上限が外れても何も
    積み上がりません。

    掛け算は `per_pack_unit` の1か所なので、表と1行は必ず同じ数字です。
    """
    return [value * max(1, packs)
            for value in per_pack_unit(weights, pallet_weight_kg)]


def per_pack_table(weights: "MaterialWeights", pallet_weight_kg: float,
                   rows: int = PER_PACK_ROWS) -> list[list[float]]:
    """1〜``rows`` 梱包ぶんの重量。1行目が1梱包ぶん(=計算結果そのもの)。

    画面の表がこれを使います(`枚数表示` の ListBox 相当)。紙は1行しか
    要らないので `per_pack_row` を使い、**同じ `per_pack_unit` から**
    掛けます。
    """
    unit = per_pack_unit(weights, pallet_weight_kg)
    return [[value * n for value in unit] for n in range(1, max(1, rows) + 1)]
