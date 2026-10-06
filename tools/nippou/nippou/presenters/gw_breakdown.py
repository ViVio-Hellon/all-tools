"""GW計算の内訳 ── **何を、どう使って、その数になったのか**

【なぜ要るのか】
VBA の `NewGW` は、押すと欄に数字が出るだけでした。数字を疑われたとき
(「この梱包で 12.3kg は多くないか」)に、確かめる手が**ソースを読む**
しかありません。現場でそれはできないので、事実上「そういうものだ」と
受け入れるしかありませんでした。

ここでは1つずつ、次の4段で見せます:

    ① 式          板厚 × 積み枚数 × (板幅 × 列 + 板丈) × 2 ÷ 1,000,000
    ② 数字を入れた式  1.99 × 40 × (81 × 1 + 0) × 2 ÷ 1,000,000
    ③ 基準量       0.0129 m²      ← ここまでが寸法の話
    ④ × 単位質量 × 係数 = 重量      ← ここからがマスタの話

**②と③があることが要点です。** 式だけでは「どの数字を入れたか」が
分からず、答えだけでは「どう出したか」が分かりません。間違いはたいてい
**入れた数字のほう**(板丈が0のまま、枚数が前のロットのまま)なので、
入れた数字が見えれば現場が自分で気づけます。

【式はここで組み立てない】
計算そのものは `logic/gw_calculation.py` の `*_base()` を呼びます ──
内訳のために式をもう一度書くと、片方だけ直したときに「画面の説明と
実際の計算が違う」という、いちばん質の悪いずれ方をします。ここが持つ
のは**文字にする仕事だけ**です。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..logic import gw_calculation as gw

#: 面積で効く資材(単位質量は kg/m²)と、長さで効く資材(kg/m)
AREA = ("m²", "kg/m²")
LENGTH = ("m", "kg/m")
#: 個数で数える資材(縦バンドアングル)。単位質量は1個あたり
COUNT = ("個", "kg/個")


def num(value: float, digits: int = 2) -> str:
    """数字を読める形に。**桁区切りを入れる** ── 1000000 は目で数えない。"""
    if value == int(value) and abs(value) < 10**15:
        return f"{int(value):,}"
    return f"{value:,.{digits}f}".rstrip("0").rstrip(".")


@dataclass
class Line:
    """資材1つぶんの内訳。"""

    key: str
    label: str
    #: 使ったか。False なら重量0で、`skipped` に理由が入る
    used: bool = True
    skipped: str = ""
    #: ① 記号の式
    formula: str = ""
    #: ② 数字を入れた式
    substituted: str = ""
    #: ③ 基準量とその単位
    base: float = 0.0
    base_unit: str = ""
    #: ④ マスタの値
    unit_mass: float = 0.0
    unit_mass_unit: str = ""
    coefficient: float = 1.0
    #: 係数を掛けるか(VCだけ掛けない)
    uses_coefficient: bool = True
    source: str = ""
    weight: float = 0.0
    note: str = ""

    @property
    def last_step(self) -> str:
        """④の行。`0.0129 m² × 0.45 kg/m² × 1.10 = 0.01 kg`"""
        if not self.used:
            return ""
        parts = [f"{num(self.base, 4)} {self.base_unit}",
                 f"{num(self.unit_mass, 4)} {self.unit_mass_unit}"]
        if self.uses_coefficient:
            parts.append(f"係数 {num(self.coefficient, 3)}")
        return " × ".join(parts) + f" = {num(self.weight)} kg"

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "used": self.used,
                "skipped": self.skipped, "formula": self.formula,
                "substituted": self.substituted,
                "base": round(self.base, 6), "base_unit": self.base_unit,
                "unit_mass": self.unit_mass,
                "unit_mass_unit": self.unit_mass_unit,
                "coefficient": self.coefficient,
                "uses_coefficient": self.uses_coefficient,
                "source": self.source, "weight": round(self.weight, 2),
                "last_step": self.last_step, "note": self.note}


@dataclass
class Total:
    """合計の積み上げ1段。"""

    label: str
    formula: str = ""
    substituted: str = ""
    value: Optional[float] = None
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"label": self.label, "formula": self.formula,
                "substituted": self.substituted,
                "value": None if self.value is None else round(self.value, 2),
                "note": self.note}


@dataclass
class Breakdown:
    """内訳ぜんぶ。**画面はこれを写すだけ。**"""

    #: 先に押さえる値(積み形態・積み枚数など)
    common: list[dict[str, str]] = field(default_factory=list)
    #: 打った値・引いてきた値
    inputs: list[dict[str, str]] = field(default_factory=list)
    lines: list[Line] = field(default_factory=list)
    totals: list[Total] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"common": self.common, "inputs": self.inputs,
                "lines": [line.as_dict() for line in self.lines],
                "totals": [total.as_dict() for total in self.totals],
                "notes": self.notes}

    def as_text(self) -> str:
        """貼り付けられる形。**数字を疑われたときに、そのまま送れる。**"""
        out: list[str] = ["■ 入力"]
        out += [f"  {row['label']}: {row['value']}"
                + (f"  ({row['note']})" if row.get("note") else "")
                for row in self.inputs]
        out.append("")
        out.append("■ 先に決まる値")
        out += [f"  {row['label']}: {row['value']}"
                + (f"  ({row['note']})" if row.get("note") else "")
                for row in self.common]
        out.append("")
        out.append("■ 資材ごと")
        for line in self.lines:
            if not line.used:
                out.append(f"  {line.label}: 0 kg ({line.skipped})")
                continue
            out.append(f"  {line.label}: {num(line.weight)} kg")
            out.append(f"    式   {line.formula}")
            out.append(f"    数字 {line.substituted}")
            out.append(f"    {line.last_step}")
            out.append(f"    出どころ {line.source}")
        out.append("")
        out.append("■ 合計")
        for total in self.totals:
            if total.value is None:
                continue
            out.append(f"  {total.label}: {num(total.value)} kg"
                       f"   ({total.substituted})")
        if self.notes:
            out.append("")
            out.append("■ 注記")
            out += [f"  ・{note}" for note in self.notes]
        return "\n".join(out)


# ------------------------------------------------------------------
# 組み立て
# ------------------------------------------------------------------
def _line(key: str, label: str, *, used: bool, skipped: str,
          formula: str, substituted: str, base: float, kind: tuple[str, str],
          rate: gw.MaterialRate, weight: float, source: str,
          uses_coefficient: bool = True, note: str = "") -> Line:
    return Line(
        key=key, label=label, used=used, skipped=skipped,
        formula=formula if used else "", substituted=substituted if used else "",
        base=base if used else 0.0, base_unit=kind[0],
        unit_mass=rate.unit_mass, unit_mass_unit=kind[1],
        coefficient=rate.coefficient, uses_coefficient=uses_coefficient,
        source=source, weight=weight, note=note)


def build(dims: gw.PackingDimensions, flags: gw.MaterialFlags,
          rates: gw.MaterialRates, weights: gw.MaterialWeights, *,
          vc_selection: gw.VcFilmSelection,
          vc_a_rate: gw.MaterialRate = gw.ZERO_RATE,
          vc_b_rate: gw.MaterialRate = gw.ZERO_RATE,
          vc_name_a: str = "", vc_name_b: str = "",
          band_kind: str = "", band_material: str = "",
          material_master: str = "資材重量マスタ",
          vc_master: str = "VC重量マスタ",
          combined_load_kg: float = 0.0, use_combined_load: bool = False,
          input_weight_kg: Optional[float] = None,
          tare_weight_kg: float = 0.0,
          gross_weight_kg: Optional[float] = None,
          product: Optional[dict[str, Any]] = None) -> Breakdown:
    """計算に使った値そのものから内訳を組み立てる。

    **計算し直しません。** 渡された `weights` はすでに出ている答えで、
    ここはその答えに至る道を文字にするだけです ── 組み立て直すと、
    画面の内訳と実際の答えが食い違う余地ができます。
    """
    itaretu = dims.stack_pattern
    tumisuu = gw.stack_count(dims)
    t, w, length = dims.thickness_mm, dims.width_mm, dims.length_mm

    common = [
        {"label": "積み形態", "value": f"{itaretu} 山",
         "note": "板を横に何列並べるか。式の「列」"},
        {"label": "積み枚数", "value": f"{num(tumisuu)} 枚",
         "note": ("1山なので梱包締枚数そのまま" if itaretu == 1 else
                  f"梱包締枚数 {num(dims.count)} ÷ {itaretu} を切り上げ")},
        {"label": "板の高さ", "value": f"{num(t * tumisuu)} mm",
         "note": f"板厚 {num(t)} × 積み枚数 {num(tumisuu)}"},
    ]

    inputs = [
        {"label": "板厚", "value": f"{num(t)} mm"},
        {"label": "板幅", "value": f"{num(w)} mm"},
        {"label": "板丈", "value": f"{num(length)} mm"},
        {"label": "梱包締枚数", "value": f"{num(dims.count)} 枚"},
        {"label": "縦バンド本数", "value": f"{num(dims.vertical_bands)} 本"},
        {"label": "横バンド本数", "value": f"{num(dims.horizontal_bands)} 本"},
        {"label": "梱包高さ", "value": f"{num(dims.pack_height_mm)} mm",
         "note": "パレット含む。バンド(横)の長さに効く"},
        {"label": "パレット+蓋の重量",
         "value": f"{num(dims.pallet_weight_kg)} kg",
         "note": "風袋総重量に足す。資材計には入らない"},
        {"label": "バンドの種別", "value": band_kind or "(未選択)",
         "note": f"マスタでは「{band_material}」で引く" if band_material else ""},
    ]
    for key, label in (("lot_no", "LotNo"), ("order_no", "オーダーNo"),
                       ("pack_spec_no", "包装仕様")):
        value = str((product or {}).get(key, "")).strip()
        if value:
            inputs.append({"label": label, "value": value,
                           "note": "ロット・受注から引いた値"})

    def source_of(name: str) -> str:
        return f"{material_master}「{name}」"

    pw, pl, ph = gw.poly_sheet_sizes(dims)
    lines = [
        _line("dunplate", "ダンプレート", used=flags.dunplate,
              skipped="使用しない(チェックOFF)",
              formula="板厚 × 積み枚数 × (板幅 × 列 + 板丈) × 2 ÷ 1,000,000",
              substituted=(f"{num(t)} × {num(tumisuu)} × "
                           f"({num(w)} × {itaretu} + {num(length)}) × 2 "
                           f"÷ 1,000,000"),
              base=gw.dunplate_base(dims), kind=AREA, rate=rates.dunplate,
              weight=weights.dunplate, source=source_of("ダンプレート")),
        _line("outer_paper", "外装紙", used=flags.outer_paper,
              skipped="使用しない(チェックOFF)",
              formula=("((板幅 × 列 + 板丈) × 2 × 板厚 × 積み枚数"
                       " + 板幅 × 列 × 板丈 × 2) ÷ 1,000,000"),
              substituted=(f"(({num(w)} × {itaretu} + {num(length)}) × 2 × "
                           f"{num(t)} × {num(tumisuu)} + "
                           f"{num(w)} × {itaretu} × {num(length)} × 2) "
                           f"÷ 1,000,000"),
              base=gw.outer_paper_base(dims), kind=AREA, rate=rates.outer_paper,
              weight=weights.outer_paper, source=source_of("外装紙"),
              note="側面ぐるり + 上下面"),
        _line("interleaf", "合紙", used=flags.interleaf,
              skipped="使用しない(チェックOFF)",
              formula="板幅 × 板丈 × (梱包締枚数 + 1) ÷ 1,000,000",
              substituted=(f"{num(w)} × {num(length)} × "
                           f"({num(dims.count)} + 1) ÷ 1,000,000"),
              base=gw.interleaf_base(dims), kind=AREA, rate=rates.interleaf,
              weight=weights.interleaf, source=source_of("合紙"),
              note="板と板のあいだ + 上下で、枚数より1枚多い"),
        _line("band", f"バンド({band_kind})" if band_kind else "バンド",
              used=flags.band, skipped="使用しない(チェックOFF)",
              formula=("(縦: (板丈 + 板厚 × 積み枚数) × 2 × 縦本数"
                       " + 横: (板幅 × 列 + 梱包高さ) × 2 × 横本数) ÷ 1,000"),
              substituted=(
                  f"(({num(length)} + {num(t)} × {num(tumisuu)}) × 2 × "
                  f"{num(dims.vertical_bands)} + "
                  f"({num(w)} × {itaretu} + {num(dims.pack_height_mm)}) × 2 × "
                  f"{num(dims.horizontal_bands)}) ÷ 1,000"),
              base=gw.band_base(dims), kind=LENGTH, rate=rates.band,
              weight=weights.band,
              source=source_of(band_material or "バンド"),
              note=("縦 {} m / 横 {} m".format(
                  num(gw.band_lengths(dims)[0] / 1000, 3),
                  num(gw.band_lengths(dims)[1] / 1000, 3)))),
        _line("poly_sheet", "ポリシート", used=flags.poly_sheet,
              skipped="使用しない(チェックOFF)",
              formula=("((幅 + 丈) × 2 × 高さ + 幅 × 丈) ÷ 1,000,000"
                       "  ※ 幅 = 板幅 × 列 + 200 / 丈 = 板丈 + 200 /"
                       " 高さ = 板厚 × 積み枚数 + 200"),
              substituted=(f"(({num(pw)} + {num(pl)}) × 2 × {num(ph)} + "
                           f"{num(pw)} × {num(pl)}) ÷ 1,000,000"),
              base=gw.poly_sheet_base(dims), kind=AREA, rate=rates.poly_sheet,
              weight=weights.poly_sheet, source=source_of("ポリシート"),
              note="側面ぐるり + 天面。大きめに包むので各寸法 +200mm"),
        _line("angle", "縦バンドアングル", used=flags.angle,
              skipped="使用しない(縦バンドが無い・チェックOFF)",
              formula=f"縦バンド本数 × {gw.ANGLES_PER_VERTICAL_BAND}",
              substituted=(f"{num(dims.vertical_bands, 0)} × "
                           f"{gw.ANGLES_PER_VERTICAL_BAND}"),
              base=gw.angle_base(dims), kind=COUNT, rate=rates.angle,
              weight=weights.angle, source=source_of("縦バンドアングル"),
              note="縦バンドが角を回る4か所(天面・底面の手前と奥)のコーナー保護"),
        _line("hardboard", "ハードボード", used=flags.hardboard,
              skipped="使用しない(チェックOFF)",
              formula="板幅 × 列 × 板丈 × 2 ÷ 1,000,000",
              substituted=(f"{num(w)} × {itaretu} × {num(length)} × 2 "
                           f"÷ 1,000,000"),
              base=gw.hardboard_base(dims), kind=AREA, rate=rates.hardboard,
              weight=weights.hardboard, source=source_of("ハードボード"),
              note="上下2枚"),
    ]
    lines.append(_vc_line(dims, vc_selection, vc_a_rate, vc_b_rate,
                          vc_name_a, vc_name_b, weights.vc_film, vc_master))

    used_labels = [line.label for line in lines if line.used and line.weight]
    totals = [
        Total("梱包資材重量(資材計)",
              " + ".join(used_labels) if used_labels else "(使った資材なし)",
              " + ".join(num(line.weight) for line in lines
                         if line.used and line.weight) or "0",
              weights.total),
        Total("風袋総重量",
              "パレット+蓋 + 資材計" + (" + 積合せ" if use_combined_load else ""),
              f"{num(dims.pallet_weight_kg)} + {num(weights.total)}"
              + (f" + {num(combined_load_kg)}" if use_combined_load else ""),
              tare_weight_kg,
              note="" if use_combined_load else "積合せは無し"),
        Total("GW", "風袋総重量 + Aインプット重量",
              f"{num(tare_weight_kg)} + {num(input_weight_kg or 0)}"
              if gross_weight_kg is not None else "",
              gross_weight_kg,
              note=("Aインプット重量が空(または0)なので出しません"
                    if gross_weight_kg is None else "")),
    ]

    notes = [
        "重量(kg) = 基準量 × 単位質量 × 係数。単位質量と係数は"
        f"{material_master}から引いた値です。",
        "÷1,000,000 は mm² を m² に、÷1,000 は mm を m に直す換算です"
        "(だから単位質量が kg/m² か kg/m かで意味が変わります)。",
        "VCフィルムだけ係数を掛けません ── VBA の `NewGW` が"
        "単位質量だけで計算していたためで、意図して合わせています。",
        "梱包高さはバンド(横)にだけ効きます。ほかの資材の式には出てきません。",
        "パレット+蓋の重量は資材計に入らず、風袋総重量で足します。",
    ]
    if any(line.used and line.unit_mass == 0 for line in lines):
        notes.append("単位質量が 0 の資材があります ── マスタに"
                     "その名前の行が無いか、マスタを読めていません。")

    return Breakdown(common=common, inputs=inputs, lines=lines,
                     totals=totals, notes=notes)


def _vc_line(dims, selection, a_rate, b_rate, name_a, name_b,
             weight: float, vc_master: str) -> Line:
    """VCフィルムの内訳。**A面・B面それぞれに面積を掛ける。**"""
    if not selection.use_vc:
        return Line(key="vc_film", label="VCフィルム", used=False,
                    skipped="使用しない(VC使用がOFF)", base_unit=AREA[0],
                    unit_mass_unit=AREA[1], uses_coefficient=False)

    base = gw.vc_film_base(dims)
    sides = []
    if selection.use_side_a:
        sides.append(("A面", name_a, a_rate))
    if selection.use_side_b:
        sides.append(("B面", name_b, b_rate))
    if not sides:
        return Line(key="vc_film", label="VCフィルム", used=False,
                    skipped="A面・B面のどちらも選ばれていません",
                    base_unit=AREA[0], unit_mass_unit=AREA[1],
                    uses_coefficient=False)

    detail = " + ".join(
        f"{side}: {num(base, 4)} m² × {num(rate.unit_mass, 4)} kg/m²"
        f"({name or '(未選択)'})" for side, name, rate in sides)
    return Line(
        key="vc_film", label="VCフィルム", used=True,
        formula="板幅 × 板丈 × 梱包締枚数 ÷ 1,000,000 × 単位質量(面ごと)",
        substituted=(f"{num(dims.width_mm)} × {num(dims.length_mm)} × "
                     f"{num(dims.count)} ÷ 1,000,000"),
        base=base, base_unit=AREA[0],
        unit_mass=sum(rate.unit_mass for _s, _n, rate in sides),
        unit_mass_unit=AREA[1],
        coefficient=1.0, uses_coefficient=False,
        source=f"{vc_master}(" + " / ".join(
            f"{side} {name or '(未選択)'}" for side, name, _r in sides) + ")",
        weight=weight,
        note=detail + " ── 係数は掛けません(VBAに合わせています)")
