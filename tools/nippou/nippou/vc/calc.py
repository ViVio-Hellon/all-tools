"""VC 計算 ── `UFVC計算.CommandButton1_Click` の移植

VBA の計算ボタンは次の3つを順に呼ぶ(docs/vc/VBA解析.md §8)。

    VC長さ … 肉厚 → VC長さ(VC長さが空のときだけ書く)
    肉厚   … VC長さ → 肉厚(肉厚が空のときだけ書く。既定では到達しない)
    VC枚数 … VC長さ → 枚数(定尺ごと + 経寸丈)

**丸め・判定の順序は1つも変えていない。** 式は VC長さ の `− y`(転記ミス)だけ
取り除き、逆算と同じ正しい式に揃えた(VER1.2.0)。 VBA の画面は入力欄の
文字列そのものが状態なので、ここも欄の文字列を受け取って、書き換えた
欄の文字列を返す純関数にしてある(画面もDBも知らない)。

Web 版で足したのは、VBA では実行時エラーや「黙って消える」になる
入力を、理由つきで断ることだけ(docs/vc/VBA解析.md §14)。
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Iterable, Optional

from .vba_compat import normalize_text, parse_number, vba_format_fixed, vba_round_value

# VBA `pi = 4 * Atn(1)`。倍精度では math.pi と同じ値になる
PI = 4 * math.atan(1)

# 欄の名前(VBA のコントロール名)と画面での呼び名
FIELD_LABELS = {
    "coatu": "肉厚",     # COatu
    "vcatu": "VC厚",     # VCatu
    "inside": "内径",    # Inside
    "vclen": "VC長さ",   # VClen
    "prolen": "経寸丈",  # Prolen
}

# 欄を抜けたときの書式(`*_AfterUpdate`)。経寸丈には無い
AFTER_UPDATE_DECIMALS = {"coatu": 1, "vcatu": 2, "inside": 1, "vclen": 1}

# 断りの種類。**文言から推し量らない**
REFUSE_BAD_VALUE = "bad_value"      # 数でない・負・VC厚が0
REFUSE_MISSING = "missing"          # 肉厚・VC厚・内径のどれかが空(VBA は黙って Exit Sub)


@dataclass
class Fields:
    """計算画面の入力欄。VBA と同じく**文字列**で持つ。"""

    coatu: str = ""    # 肉厚 (mm)
    vcatu: str = ""    # VC厚 (mm)
    inside: str = ""   # 内径 (mm)
    vclen: str = ""    # VC長さ (m)
    prolen: str = ""   # 経寸丈 (mm)

    @classmethod
    def from_dict(cls, data: dict) -> "Fields":
        return cls(**{k: _text(data.get(k)) for k in FIELD_LABELS})

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SheetSize:
    """枚数を出す定尺1つ(VBA の M1 / M4 / M5)。"""

    key: str
    label: str
    length_mm: float


@dataclass
class Count:
    key: str
    label: str
    length_mm: float
    value: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CalcResult:
    ran: bool
    fields: Fields
    computed: list[str] = field(default_factory=list)
    counts: list[Count] = field(default_factory=list)
    mk: str = ""
    errors: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    trace: list[str] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)      # 計算の経過(画面に順に出す)
    geometry: dict = field(default_factory=dict)         # 図に使う寸法(mm)
    message: str = ""
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "ran": self.ran,
            "fields": self.fields.to_dict(),
            "computed": list(self.computed),
            "counts": [c.to_dict() for c in self.counts],
            "mk": self.mk,
            "errors": dict(self.errors),
            "notes": list(self.notes),
            "trace": list(self.trace),
            "steps": list(self.steps),
            "geometry": dict(self.geometry),
            "message": self.message,
            "reason": self.reason,
        }


def _text(value) -> str:
    return "" if value is None else str(value)


# ------------------------------------------------------------------
# VBA の3つの手続き
# ------------------------------------------------------------------
def vc_length(coatu: float, vcatu: float, inside: float) -> float:
    """VC長さ(m) = 巻きの断面積 ÷ フィルム厚。引数は `Format(val(x), "0.00")` 済みの値。

        π × ((2y + b)² − b²) ÷ (4a) ÷ 1000      y 肉厚 / a VC厚 / b 内径

    VBA(`VC長さ`)とブックの式には `− y`(面積から長さを引く、意味の無い項)が
    入っていたが、VBA の旧式・逆算式(`wall_thickness`)には無い。利用者の判断で
    **転記ミスとして取り除き、逆算と同じ正しい式に揃えた**(VER1.2.0、
    docs/vc/VBA解析.md §6.1)。演算の順序は VBA の書き方どおり左から。
    """
    y, a, b = coatu, vcatu, inside
    return PI * ((y * 2 + b) * (y * 2 + b) - b * b) / (4 * a) / 1000


def wall_thickness(vclen: float, vcatu: float, inside: float) -> float:
    """`肉厚` の逆算式(`vc_length` を肉厚について解いた形)。引数は整形済みの値。"""
    y, a, b = vclen, vcatu, inside
    return (-b + math.sqrt(abs(b ** 2 + (4 * a * y * 1000 / PI)))) / 2


def sheet_count(vclen: float, length_mm: float) -> float:
    """`VC枚数` の式。`x * 1000 / 2010#`"""
    return vclen * 1000 / length_mm


def _f2(text: str) -> float:
    """`Format(val(.X), "0.00")` を Double に入れた値。空は `Val("") = 0`。"""
    value = parse_number(text)
    return vba_round_value(value if value is not None else 0.0, 2)


# ------------------------------------------------------------------
# 計算ボタン
# ------------------------------------------------------------------
def calculate(fields: Fields, sheets: Iterable[SheetSize], *,
              reverse: bool = False) -> CalcResult:
    """`CommandButton1_Click` と同じ順で計算する。

    `reverse` … 肉厚の逆算を画面から使えるようにする(既定 False = VBA と同じ)。
    VBA は入口で肉厚が空だと抜けるので、`肉厚` の逆算は到達しない
    (docs/vc/VBA解析.md §8 / Q3)。
    """
    f = Fields(**{k: normalize_text(v) for k, v in fields.to_dict().items()})
    result = CalcResult(ran=False, fields=f)

    # --- 入力の形(Web 版で足した関門。VBA では打った瞬間に消える) ---
    for name in AFTER_UPDATE_DECIMALS:
        text = getattr(f, name)
        if text == "":
            continue
        value = parse_number(text)
        if value is None:
            result.errors[name] = f"{FIELD_LABELS[name]}は数値で入れてください。"
        elif value < 0:
            result.errors[name] = f"{FIELD_LABELS[name]}に負の数は使えません。"
    if result.errors:
        result.reason = REFUSE_BAD_VALUE
        result.message = "入力を確かめてください。"
        return result

    # --- 欄を抜けたときの書式(*_AfterUpdate)。計算の前に済んでいる ---
    for name, decimals in AFTER_UPDATE_DECIMALS.items():
        text = getattr(f, name)
        if text != "":
            setattr(f, name, vba_format_fixed(parse_number(text), decimals))

    # --- If Me.COatu = "" Or Me.VCatu = "" Or Me.Inside = "" Then Exit Sub ---
    need = []
    if f.coatu == "" and not (reverse and f.vclen != ""):
        need.append("肉厚かVC長さ" if reverse else "肉厚")
    if f.vcatu == "":
        need.append("VC厚")
    if f.inside == "":
        need.append("内径")
    if need:
        result.reason = REFUSE_MISSING
        result.message = f"{'・'.join(need)}を入れてから計算してください。"
        return result

    a = _f2(f.vcatu)
    b = _f2(f.inside)
    if a == 0:
        # VBA は 0 除算の実行時エラーで止まる(VC長さが入っていても落ちる)
        result.errors["vcatu"] = ("VC厚が 0(小数2桁に丸めて 0.00)では計算できません。")
        result.reason = REFUSE_BAD_VALUE
        result.message = "入力を確かめてください。"
        return result

    result.ran = True

    # --- Call VC長さ ---
    y = _f2(f.coatu)
    x = vc_length(y, a, b)
    if f.vclen == "" and f.coatu != "" and f.vcatu != "" and f.inside != "":
        f.vclen = vba_format_fixed(x, 1)
        result.computed.append("vclen")
        result.trace.append(
            f"VC長さ = π×((2×{_s(y)}+{_s(b)})² − {_s(b)}²) ÷ (4×{_s(a)}) ÷ 1000"
            f" = {f.vclen} m")

    # --- Call 肉厚 ---
    y = _f2(f.vclen)
    x = wall_thickness(y, a, b)
    if f.coatu == "" and f.vcatu != "" and f.inside != "" and f.vclen != "":
        f.coatu = vba_format_fixed(x, 1)
        result.computed.append("coatu")
        result.trace.append(
            f"肉厚 = (−{_s(b)} + √({_s(b)}² + 4×{_s(a)}×{_s(y)}×1000÷π)) ÷ 2"
            f" = {f.coatu} mm")

    # --- Call VC枚数 ---
    x = _f2(f.vclen)
    if f.vclen != "" and parse_number(f.vclen) is not None:
        for sheet in sheets:
            result.counts.append(Count(
                key=sheet.key, label=sheet.label, length_mm=sheet.length_mm,
                value=vba_format_fixed(sheet_count(x, sheet.length_mm), 1)))
        if f.prolen != "":
            prolen = parse_number(f.prolen)
            if prolen is None:
                result.notes.append("経寸丈が数値ではないので、経寸丈の枚数は出していません。")
            elif prolen <= 0:
                result.notes.append("経寸丈は 0 より大きい数で入れてください"
                                    "(経寸丈の枚数は出していません)。")
            else:
                # 経寸丈は整形されない(`val(.Prolen)` のまま割る)
                result.mk = vba_format_fixed(sheet_count(x, prolen), 1)
    explain(result, a=a, b=b, sheets=list(sheets))
    return result


# ------------------------------------------------------------------
# 計算の経過(画面の「計算の経過」と図のため。計算の結果は変えない)
# ------------------------------------------------------------------
def _n(value: float, digits: int = 2) -> str:
    """経過に出す数。桁区切りつき、末尾の 0 は落とす。"""
    text = f"{value:,.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _step(title: str, formula: str, work: str, value: str, unit: str,
          part: str, note: str = "") -> dict:
    return {"title": title, "formula": formula, "work": work, "value": value,
            "unit": unit, "part": part, "note": note}


def explain(result: CalcResult, *, a: float, b: float, sheets: list[SheetSize]) -> None:
    """計算の経過を1段ずつ作る。

    式は `vc_length` / `wall_thickness` を段に分けて書き直したもの。最後の段の値は
    必ず計算の結果(`result.fields`)と同じものを出す(経過と結果が食い違わない)。
    `part` は図のどこを光らせるか(core 内径 / film 巻き / outer 外径 / layer 1枚)。
    """
    f = result.fields
    y = _f2(f.coatu)
    length_m = _f2(f.vclen)
    if y <= 0 or length_m <= 0:
        return
    outer = b + 2 * y
    area = PI * (outer * outer - b * b) / 4
    turns = y / a
    mean_round = PI * (b + y)
    steps = result.steps
    if "coatu" in result.computed:
        # 逆算: 長さ → 断面積 → 外径 → 肉厚
        area_back = length_m * 1000 * a
        outer_back = math.sqrt(b * b + 4 * area_back / PI)
        steps.append(_step("巻きの断面積", "断面積 = 長さ × VC厚",
                           f"{_n(length_m, 1)} m × 1000 × {_s(a)} mm", _n(area_back, 1), "mm²",
                           "film", "長さを mm にして、フィルム1枚の厚さを掛ける"))
        steps.append(_step("外径", "外径 = √(内径² + 4 × 断面積 ÷ π)",
                           f"√({_s(b)}² + 4 × {_n(area_back, 1)} ÷ π)", _n(outer_back, 2), "mm",
                           "outer"))
        steps.append(_step("肉厚", "肉厚 = (外径 − 内径) ÷ 2",
                           f"({_n(outer_back, 2)} − {_s(b)}) ÷ 2",
                           f.coatu, "mm", "film", "小数1桁に四捨五入"))
    else:
        steps.append(_step("外径", "外径 = 内径 + 2 × 肉厚", f"{_s(b)} + 2 × {_s(y)}",
                           _n(outer, 1), "mm", "outer"))
        steps.append(_step("巻きの断面積", "断面積 = π × (外径² − 内径²) ÷ 4",
                           f"π × ({_n(outer, 1)}² − {_s(b)}²) ÷ 4", _n(area, 1), "mm²", "film",
                           "外径の円から内径(紙管)の円を引いたドーナツの面積"))
        steps.append(_step("VC長さ", "長さ = 断面積 ÷ VC厚 ÷ 1000",
                           f"{_n(area, 1)} ÷ {_s(a)} ÷ 1000", _n(area / a / 1000, 2), "m",
                           "layer",
                           f"画面の VC長さは小数1桁に四捨五入して {f.vclen} m"
                           if "vclen" in result.computed else
                           f"VC長さは入力の {f.vclen} m をそのまま使う"))
    steps.append(_step("検算(巻き数)", "長さ = 巻き数 × 平均の1周",
                       f"({_s(y)} ÷ {_s(a)}) 巻 × π × ({_s(b)} + {_s(y)}) ÷ 1000",
                       _n(turns * mean_round / 1000, 2), "m", "layer",
                       f"巻き数 {_n(turns, 1)} 巻、平均の1周 {_n(mean_round, 1)} mm"))
    for c in result.counts:
        steps.append(_step(f"枚数 {c.label or c.key}", "枚数 = VC長さ × 1000 ÷ 製品の丈",
                           f"{f.vclen} × 1000 ÷ {_n(c.length_mm, 0)}", c.value, "枚", "",
                           "小数1桁に四捨五入"))
    if result.mk:
        steps.append(_step("枚数 経寸丈", "枚数 = VC長さ × 1000 ÷ 経寸丈",
                           f"{f.vclen} × 1000 ÷ {f.prolen}", result.mk, "枚", ""))
    result.geometry = {"inside": b, "wall": y, "outer": outer, "film": a,
                       "turns": turns, "length_m": length_m,
                       "mean_round": mean_round, "area": area}


def _s(value: float) -> str:
    """式の説明に出す数。余計な 0 を付けない。"""
    return format(value, "g")


def select_product(fields: Fields, *, vcatu: float,
                   inside: Optional[float]) -> Fields:
    """`List選択` ── 品種を選んだときの欄。

    VC厚・内径を入れ、肉厚と VC長さを空にする。内径を選ぶ品種(ニットウ)は
    内径も空にする(大/小を押すまで)。経寸丈は触らない。
    VBA は VC厚を `"0.10"` のような文字で入れるので、表示は `0.00` に揃える。
    """
    return Fields(
        coatu="",
        vcatu=vba_format_fixed(vcatu, 2),
        inside="" if inside is None else vba_format_fixed(inside, 1),
        vclen="",
        prolen=fields.prolen,
    )
