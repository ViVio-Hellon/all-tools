"""負荷係数(係数処理ﾛｯﾄ数) ── コイルのラインだけの数え方

【なぜロット数をそのまま数えないのか】
コイル(丈=0)は1本の重さも手間もばらばらで、「3ロットやった」だけでは
仕事の量になりません。そこで**用途ごとの重み**を掛けて数えます:

    係数処理ﾛｯﾄ数 = その行の用途コードに決められた換算係数
    係数Lot数     = それを直ぶん足したもの
    ﾛｯﾄ数         = 係数の付いた行を、ロット番号で重複を除いて数えたもの

重みの出どころは伝送用ファイルの `用途名負荷係数算出`
(`access_bridge/factor_master.py`)。登録の無い用途コードは **1**
(VBA `負荷LotC = 1`)。

【どのラインで何を数えるか】(v4.18.0 / v4.20.0)
VBA は `If UFdaily.LS Or UFdaily.NS1 Or UFdaily.AIM Then` で囲って
いました(LS = 機側)。現場の指示で、係数だけ AIM を外しました:

    係数Lot数：計算は機側とNS1だけでいいです
    AIM の ﾛｯﾄ数は自動のまま
    機側、NS1はコイル形状担当です　その他は板形状です

    ライン        ﾛｯﾄ数   係数処理ﾛｯﾄ数・係数Lot数
    機側・NS1     計算    計算(コイル ── 用途コードごとの重み)
    AIM           計算    手入力(板。27列目にも触れない)
    ほか          手入力  手入力

AIM の ﾛｯﾄ数は**機側・NS1 と同じ数え方**(同じ直の中で1回・前の直から続く
ロットは数えない)です。係数の欄を書かないので、数えるときだけ写しの上で
同じ手順を通します(:func:`recalculate`)。

【同じロットを2度数えない】
1直でロットAが終わらず、2直で続けて終えると、**どちらの直にもロットAの
行がある**ので、放っておくと係数が2回足されます。VBA はそれを2段構えで
防いでいました:

    不要係数処理ロットクリア … **前の直**にも同じロットがあれば、
                               今の直の係数を消す(3シート分だけ遡る)
    Lot数計算 の重複チェック … **同じ直の中**で2度目に出たロットの
                               係数を消す(ページをまたいでも1回だけ)

この層は**渡された行だけ**で判断します。前の直を探してくるのは
`services/load_factor.py` の仕事です。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Iterable, Sequence

from ..db.models import DetailRecord
from .numeric import is_numeric, to_float

#: 係数を計算するライン。**機側と NS1 だけ**(コイル形状担当 ── v4.18.0。VBA は
#: AIM も入れていたが、「計算は機側とNS1だけでいいです」)
COEFFICIENT_LINES: frozenset[str] = frozenset({"機側", "NS1"})

#: ﾛｯﾄ数を数えるライン。VBA `UFdaily.LS Or NS1 Or AIM` の3つのまま
#: (v4.20.0 ──「AIM の ﾛｯﾄ数は自動のまま」)
LOT_COUNT_LINES: frozenset[str] = COEFFICIENT_LINES | {"AIM"}

#: 用途コードがマスタに無いときの係数。VBA `負荷LotC = 1`
DEFAULT_FACTOR = 1.0

#: 遡る枚数。VBA `不要係数処理ロットクリア` のコメント「3シート分だけ遡る」
LOOK_BACK_SHEETS = 3

#: 用途コードが入っている欄。紙の AN 列 = `others1`
USAGE_CODE_FIELD = "others1"


def applies(line: str) -> bool:
    """そのラインで何かを数えるか(ﾛｯﾄ数。機側・NS1・AIM)。"""
    return line in LOT_COUNT_LINES


def counts_coefficient(line: str) -> bool:
    """そのラインで係数(係数処理ﾛｯﾄ数・係数Lot数)まで計算するか(機側・NS1)。"""
    return line in COEFFICIENT_LINES


def format_factor(value: float) -> str:
    """書き込む形。VBA `Format(負荷LotC, "0.0")` と同じ小数1桁。"""
    return f"{value:.1f}"


def factor_for(usage_code: str, factors: dict[str, float]) -> float:
    """その用途コードの係数。登録が無ければ 1。"""
    return factors.get((usage_code or "").strip(), DEFAULT_FACTOR)


def _lot(row: DetailRecord) -> str:
    return (row.lot or "").strip()


def _ordered(rows: Iterable[DetailRecord]) -> list[DetailRecord]:
    return sorted(rows, key=lambda d: (d.page, d.row_no))


# ======================================================================
# 1. 係数を書き込む (VBA `負荷係数反映(27)`)
# ======================================================================
def apply_factors(rows: Sequence[DetailRecord],
                  factors: dict[str, float]) -> int:
    """ロット番号のある行に係数を入れ、無い行は空にする。

    **ロット番号が入っている行だけ**です(VBA `If IWS1.Cells(i, 1) <> ""`)。
    打ちかけの行に係数だけ残ると、そのぶん多く数えます。
    """
    touched = 0
    for row in rows:
        wanted = (format_factor(factor_for(getattr(row, USAGE_CODE_FIELD, ""),
                                           factors))
                  if _lot(row) else "")
        if row.keisu != wanted:
            row.keisu = wanted
            touched += 1
    return touched


# ======================================================================
# 2. 前の直と重なるぶんを消す (VBA `不要係数処理ロットクリア`)
# ======================================================================
def clear_carried_over(rows: Sequence[DetailRecord],
                       previous: Sequence[Sequence[DetailRecord]]) -> list[str]:
    """前の直にもあるロットの係数を消す。消したロット番号を返す。

    `previous` は**新しい順**に並んだ、前の直のページ(`LOOK_BACK_SHEETS` 枚
    まで)。呼び手が用意します。

    【VBAと変えたところ】
    VBA は2つの範囲を狭く取っていました:

        ・見る先は**今の直の1ページ目だけ**(`ClearIfLotFound(IWS1, ...)`)
        ・1枚目で1件でも消せたら、**残りの2枚は見ない**

    どちらも、引き継いだロットが2ページ目に来た日・2枚前の直から続いていた
    日に**取りこぼします**(そのぶん係数が2回足される)。この関数が
    そもそも二重計上を防ぐためのものなので、ここでは**全ページ × 3枚とも**
    見ます。数える回数が増えるだけで、正しく1回に収まります。
    """
    seen: set[str] = set()
    for sheet in previous[:LOOK_BACK_SHEETS]:
        for row in sheet:
            if _lot(row):
                seen.add(_lot(row))
    if not seen:
        return []

    cleared: list[str] = []
    for row in _ordered(rows):
        lot = _lot(row)
        if lot and lot in seen and row.keisu:
            row.keisu = ""
            cleared.append(lot)
    return cleared


# ======================================================================
# 3. 同じ直の中の重複を消して数える (VBA `Lot数計算`)
# ======================================================================
@dataclass(frozen=True)
class Totals:
    """直ぶんの数え上げ。**そのまま合計欄に入る2つ。**"""

    lot_count: int = 0            # ﾛｯﾄ数 (AlLotC)
    coefficient: float = 0.0      # 係数Lot数 (Allcoefficient)

    @property
    def lot_count_text(self) -> str:
        return str(self.lot_count)

    @property
    def coefficient_text(self) -> str:
        """VBA `Format(係数後, "0.0")`。"""
        return f"{self.coefficient:.1f}"


def count(rows: Sequence[DetailRecord]) -> Totals:
    """port of ``Lot数計算``。**2度目に出たロットの係数は消します。**

    ページをまたいでも、同じロット番号は1回だけ数えます ── 1つのロットを
    12行に分けて打つことがあり、行の数をそのまま足すと何倍にもなります。

    ﾛｯﾄ数に数えるのは**係数の付いている行だけ**です
    (VBA `If IWS1.Cells(i, 27) <> "" Then count = count + 1`)。前の直から
    続いているロットは係数を消してあるので、ここでも数えません。
    """
    seen: set[str] = set()
    lots = 0
    total = 0.0
    for row in _ordered(rows):
        lot = _lot(row)
        if not lot:
            continue
        if lot in seen:
            # 2度目以降。**消してから次へ**(VBA も同じ場所で消す)
            row.keisu = ""
            continue
        seen.add(lot)
        if not (row.keisu or "").strip():
            continue
        lots += 1
        if is_numeric(row.keisu):
            total += to_float(row.keisu)
    return Totals(lot_count=lots, coefficient=total)


# ======================================================================
# まとめて
# ======================================================================
@dataclass
class Result:
    """1直ぶんの計算結果。**やったことも持つ。**"""

    applied: bool = False              # そのラインで計算したか
    #: 係数まで計算したか(機側・NS1)。False なら ﾛｯﾄ数だけ(AIM)
    coefficient: bool = True
    totals: Totals = Totals()
    #: 前の直と重なって消したロット
    cleared_lots: list[str] = field(default_factory=list)
    factors_found: int = 0             # マスタから引けた用途コードの数

    @property
    def message(self) -> str:
        if not self.applied:
            return ""
        text = f"ﾛｯﾄ数 {self.totals.lot_count_text}"
        if self.coefficient:
            text += f" / 係数Lot数 {self.totals.coefficient_text}"
        if self.cleared_lots:
            text += f"(前の直から続き {len(self.cleared_lots)}件は数えません)"
        return text

    def as_dict(self) -> dict:
        return {"applied": self.applied,
                "lot_count": self.totals.lot_count_text,
                # 係数を計算しないライン(AIM)では空(打った値を触らない)
                "coefficient": self.totals.coefficient_text if self.coefficient else "",
                "cleared_lots": list(self.cleared_lots),
                "message": self.message}


def recalculate(rows: Sequence[DetailRecord], *, line: str,
                factors: dict[str, float],
                previous: Sequence[Sequence[DetailRecord]] = ()) -> Result:
    """係数を入れ直して数える。**順番は VBA のまま。**

        負荷係数反映 → 不要係数処理ロットクリア → Lot数計算

    順を変えると数が変わります ── 消す前に数えれば二重に足しますし、
    係数を入れる前に消しても意味がありません。

    **AIM は ﾛｯﾄ数だけ**(v4.20.0)。数え方は同じですが、係数の欄(27列目)は
    書かないので、**写しの上で**同じ手順を通して数だけ持ち帰ります。
    """
    if not applies(line):
        return Result(applied=False)
    coefficient = counts_coefficient(line)
    target = rows if coefficient else [replace(r) for r in rows]
    apply_factors(target, factors)
    cleared = clear_carried_over(target, previous)
    totals = count(target)
    if not coefficient:
        totals = Totals(lot_count=totals.lot_count)
    return Result(applied=True, coefficient=coefficient, totals=totals,
                  cleared_lots=cleared, factors_found=len(factors))
