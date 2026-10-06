"""計算の途中式 ── **その数がどこから来たかを見せる**

【なぜ要るのか】
稼働率 90.0%、生産性 0.22t/h と出ていても、**その数を作った式は
どこにも出ていませんでした。** VBA も同じで、式はマクロの中に埋まって
いたので、「なぜこの数になるのか」を訊かれると中を開くしかありません。

現場が数字を疑うのは当たり前のことで、疑ったときに確かめられないと、
**数字そのものが使われなくなります。** 移行を機に、途中式を出します。

【3行で出す】
式は3段に分けます。読む人が見たいのは、たいてい2行目です:

    稼働率 = 稼働時間 ÷ (1440分 × 直数) × 100      ← 何を割っているか
           = (4320 − 432) ÷ 4320 × 100             ← **実際の数**
           = 90.0 %                                 ← 答え

【ここは絵も画面も知りません】
この層が持つのは「式の形」と「入った数」だけです。どこに出すか(画面・
CSV・紙)は呼び手が決めます ── そうしておくと、**同じ式が画面とCSVで
食い違うことがありません。**

【元の式と1か所にする、ができない理由】
理想は `ShiftAggregate.operating_rate_pct` が式と答えを同時に返すこと
ですが、あちらは数だけを返す前提で広く使われています。**代わりに
テストで縛ります**(`tests/test_formula.py`)── 途中式の答えと、元の
計算の答えが一致しない限り通りません。ずれたら必ず落ちます。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .aggregation import MINUTES_PER_DAY, ShiftAggregate
from .numeric import format_fixed

__all__ = ["Term", "Step", "shift_steps", "day_steps", "as_rows"]


def _num(value: float, digits: int = 0) -> str:
    """式の中の数。**桁を揃える** ── 1440 と 432 が並ぶので。"""
    return format_fixed(float(value), digits)


@dataclass(frozen=True)
class Term:
    """式に入った値1つ。**どこから来たかまで持つ。**

    「1440分」が定数なのか、打った値の合計なのかで、疑うべき先が
    変わります。`source` にそれを書きます。
    """

    label: str
    value: float
    unit: str = ""
    digits: int = 0
    #: その値の出どころ。"入力" / "合計" / "定数" / "計算"
    source: str = ""

    @property
    def text(self) -> str:
        return f"{_num(self.value, self.digits)}{self.unit}"

    def as_dict(self) -> dict[str, Any]:
        return {"label": self.label, "value": round(self.value, 4),
                "unit": self.unit, "text": self.text, "source": self.source}


@dataclass(frozen=True)
class Step:
    """途中式1本。"""

    key: str
    name: str
    #: 式の形(言葉)。「稼働時間 ÷ (1440分 × 直数) × 100」
    formula: str
    #: 数を入れた式。「(4320 − 432) ÷ 4320 × 100」
    substituted: str
    #: 答え(単位つき)
    result: str
    #: 使った値。**疑ったときに辿る先**
    terms: list[Term] = field(default_factory=list)
    #: VBAとの違い・気をつけること。無ければ空
    note: str = ""

    @property
    def lines(self) -> list[str]:
        """紙とCSVに出す3行。**画面もこれと同じものを出します。**"""
        return [f"{self.name} = {self.formula}",
                f"= {self.substituted}",
                f"= {self.result}"]

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "name": self.name, "formula": self.formula,
                "substituted": self.substituted, "result": self.result,
                "terms": [t.as_dict() for t in self.terms],
                "note": self.note, "lines": self.lines}


# ======================================================================
# 直1つぶん
# ======================================================================
def _weight_ton(agg: ShiftAggregate) -> Step:
    return Step(
        key="weight_ton", name="重量(t)",
        formula="重量(kg) ÷ 1000",
        substituted=f"{_num(agg.weight_kg, 1)} ÷ 1000",
        result=f"{_num(agg.weight_ton, 3)} t",
        terms=[Term("重量(kg)", agg.weight_kg, "kg", 1, "明細の合計"),
               Term("1000", 1000, "", 0, "定数")])


def _stop_total(agg: ShiftAggregate) -> Step:
    return Step(
        key="stop_total", name="停止合計(分)",
        formula="管理ロス + 突発 + ハンドリング",
        substituted=(f"{_num(agg.management_loss_minutes)}"
                     f" + {_num(agg.unplanned_stop_minutes)}"
                     f" + {_num(agg.handling_stop_minutes)}"),
        result=f"{_num(agg.total_stop_minutes)} 分",
        terms=[
            Term("管理ロス停止", agg.management_loss_minutes, "分", 0, "明細の合計"),
            Term("突発停止", agg.unplanned_stop_minutes, "分", 0, "明細の合計"),
            Term("ハンドリング停止", agg.handling_stop_minutes, "分", 0, "明細の合計"),
        ],
        note="分類は停止理由の記号の「文字種」で決まります"
             "(数字=管理ロス / カタカナ=突発 / 英字=ハンドリング)")


def _operating(agg: ShiftAggregate) -> Step:
    return Step(
        key="operating_minutes", name="稼働時間(分)",
        formula="1440分 − 停止合計",
        substituted=f"{_num(MINUTES_PER_DAY)} − {_num(agg.total_stop_minutes)}",
        result=f"{_num(agg.operating_minutes)} 分",
        terms=[Term("1440", MINUTES_PER_DAY, "分", 0, "定数(1日)"),
               Term("停止合計", agg.total_stop_minutes, "分", 0, "計算")],
        note="VBA Aggre_Calcul の OpeTime と同じ式です")


def _operational(agg: ShiftAggregate) -> Step:
    return Step(
        key="operational_minutes", name="操業時間(分)",
        formula="1440分 − 管理ロス停止",
        substituted=(f"{_num(MINUTES_PER_DAY)}"
                     f" − {_num(agg.management_loss_minutes)}"),
        result=f"{_num(agg.operational_minutes)} 分",
        terms=[Term("1440", MINUTES_PER_DAY, "分", 0, "定数(1日)"),
               Term("管理ロス停止", agg.management_loss_minutes, "分", 0,
                    "明細の合計")],
        note="引くのは管理ロスだけです(VBA Opetional)。"
             "突発とハンドリングは操業していた時間の中の出来事なので引きません")


def _rate(agg: ShiftAggregate) -> Step:
    return Step(
        key="operating_rate", name="稼働率(%)",
        formula="稼働時間 ÷ 1440分 × 100",
        substituted=(f"{_num(agg.operating_minutes)}"
                     f" ÷ {_num(MINUTES_PER_DAY)} × 100"),
        result=f"{_num(agg.operating_rate_pct, 1)} %",
        terms=[Term("稼働時間", agg.operating_minutes, "分", 0, "計算"),
               Term("1440", MINUTES_PER_DAY, "分", 0, "定数(1日)")],
        note="VBA OpeRate と同じ式です")


def _productivity(agg: ShiftAggregate) -> Step:
    hours = agg.operating_minutes / 60
    if hours <= 0:
        return Step(
            key="productivity", name="生産性(t/h)",
            formula="重量(t) ÷ 稼働時間(h)",
            substituted=f"{_num(agg.weight_ton, 3)} ÷ 0",
            result="0.00 t/h",
            terms=[Term("重量(t)", agg.weight_ton, "t", 3, "計算"),
                   Term("稼働時間(h)", 0.0, "h", 2, "計算")],
            note="稼働時間が0なので割れません。0として出しています")
    return Step(
        key="productivity", name="生産性(t/h)",
        formula="重量(t) ÷ 稼働時間(h)   ※稼働時間(h) = 稼働時間(分) ÷ 60",
        substituted=(f"{_num(agg.weight_ton, 3)} ÷ ("
                     f"{_num(agg.operating_minutes)} ÷ 60)"
                     f" = {_num(agg.weight_ton, 3)} ÷ {_num(hours, 2)}"),
        result=f"{_num(agg.productivity_t_per_h, 2)} t/h",
        terms=[Term("重量(t)", agg.weight_ton, "t", 3, "計算"),
               Term("稼働時間(分)", agg.operating_minutes, "分", 0, "計算"),
               Term("稼働時間(h)", hours, "h", 2, "計算")],
        note="VBAとは分母が違います。VBA DayShift_Agg は "
             "(1直+2直の重量) ÷ ((515分 − 管理ロス) ÷ 60) という、"
             "特定のセル位置と直の組み合わせに合わせた式でした。直によって"
             "前提が変わって一般化できないので、こちらは「その直の稼働時間」"
             "を分母にしています")


#: 直1つぶんに出す途中式。**並びは読む順**(素の合計 → 引き算 → 割り算)
SHIFT_BUILDERS = (_weight_ton, _stop_total, _operating, _operational,
                  _rate, _productivity)


def shift_steps(agg: ShiftAggregate) -> list[Step]:
    """直1つぶんの途中式。"""
    return [build(agg) for build in SHIFT_BUILDERS]


# ======================================================================
# その日ぶん(直をまたいで足したもの)
# ======================================================================
def day_steps(rows: list[ShiftAggregate], cumulative: float = 0.0,
              period_note: str = "") -> list[Step]:
    """その日の数字の途中式。**直の平均ではないことを、式で見せます。**

    直ごとの稼働率を平均すると、1直だけ動いた日と3直とも動いた日が
    同じ数字になります。止まった分を足してから、直の数だけ伸ばした
    1440分で割り直す ── そこが**式を見ないと分からないところ**なので、
    まさにここを出します。
    """
    shifts = len(rows)
    stopped = sum(r.total_stop_minutes for r in rows)
    whole = float(MINUTES_PER_DAY * shifts)
    weight_kg = sum(r.weight_kg for r in rows)
    weight_ton = weight_kg / 1000.0
    operating = whole - stopped
    hours = operating / 60

    rate = max(0.0, operating / whole * 100) if whole > 0 else 0.0
    productivity = weight_ton / hours if hours > 0 else 0.0

    steps = [
        Step(key="day_shifts", name="直数",
             formula="保存のあった直を数える",
             substituted=" + ".join(r.shift for r in rows) or "(なし)",
             result=f"{shifts} 直",
             terms=[Term(r.shift, 1, "直", 0, "保存済み") for r in rows]),
        Step(key="day_whole", name="のべ時間(分)",
             formula="1440分 × 直数",
             substituted=f"{_num(MINUTES_PER_DAY)} × {shifts}",
             result=f"{_num(whole)} 分",
             terms=[Term("1440", MINUTES_PER_DAY, "分", 0, "定数(1日)"),
                    Term("直数", shifts, "直", 0, "計算")],
             note="直の平均にしないために、直の数だけ伸ばします"),
        Step(key="day_stop", name="停止合計(分)",
             formula="直ごとの停止を足す",
             substituted=" + ".join(
                 f"{r.shift} {_num(r.total_stop_minutes)}" for r in rows)
             or "(なし)",
             result=f"{_num(stopped)} 分",
             terms=[Term(r.shift, r.total_stop_minutes, "分", 0, "計算")
                    for r in rows]),
        Step(key="day_operating", name="稼働時間(分)",
             formula="のべ時間 − 停止合計",
             substituted=f"{_num(whole)} − {_num(stopped)}",
             result=f"{_num(operating)} 分",
             terms=[Term("のべ時間", whole, "分", 0, "計算"),
                    Term("停止合計", stopped, "分", 0, "計算")]),
        Step(key="day_rate", name="稼働率(%)",
             formula="(のべ時間 − 停止合計) ÷ のべ時間 × 100",
             substituted=(f"({_num(whole)} − {_num(stopped)})"
                          f" ÷ {_num(whole)} × 100") if whole > 0
             else "直がありません",
             result=f"{_num(rate, 1)} %",
             terms=[Term("のべ時間", whole, "分", 0, "計算"),
                    Term("停止合計", stopped, "分", 0, "計算")],
             note="直ごとの稼働率の平均ではありません。平均にすると、"
                  "1直だけ動いた日と3直とも動いた日が同じ数字になります"),
        Step(key="day_productivity", name="生産性(t/h)",
             formula="重量(t) ÷ 稼働時間(h)",
             substituted=(f"{_num(weight_ton, 2)} ÷ ({_num(operating)} ÷ 60)"
                          f" = {_num(weight_ton, 2)} ÷ {_num(hours, 2)}")
             if hours > 0 else "稼働時間が0なので割れません",
             result=f"{_num(productivity, 2)} t/h",
             terms=[Term("重量(t)", weight_ton, "t", 2, "計算"),
                    Term("稼働時間(h)", hours, "h", 2, "計算")]),
    ]
    if cumulative:
        steps.append(Step(
            key="day_cumulative", name="累積枚数(枚)",
            formula="期間の初日から、その日までの枚数を足す",
            substituted=period_note or "期間内の日別枚数の積み上げ",
            result=f"{_num(cumulative)} 枚",
            terms=[Term("累積", cumulative, "枚", 0, "計算")],
            note="1日の数ではありません。期間は画面の開始日〜終了日"
                 "(既定は月初〜当日)で、VBAの集計シートも同じ範囲でした"))
    return steps


# ======================================================================
# 期間ぶん(日も直もまたいで足したもの)
# ======================================================================
def period_steps(rows: list[ShiftAggregate], note: str = "") -> list[Step]:
    """期間の数字の途中式。**その日ぶんと同じ足し方**です。

        期間表示は本日系の表示を切り替えてでも
        期間でしか見れないデータを見せるべき

    数え方は `day_steps` と1つも変えません ── 変えると、同じ「稼働率」
    が日と期間で別のものになります。違うのは**数える範囲だけ**:

        その日  … その日に保存のあった直ぜんぶ
        期間    … 開始日〜終了日に保存のあった直ぜんぶ

    予定総時間(のべ時間)は **1440分 × 直数**。日数ではありません ──
    2直しか動かなかった日を3直ぶんで割ると、稼働率が実際より低く出ます。
    """
    shifts = len(rows)
    days = len({r.report_date for r in rows if r.report_date})
    stopped = sum(r.total_stop_minutes for r in rows)
    whole = float(MINUTES_PER_DAY * shifts)
    weight_ton = sum(r.weight_kg for r in rows) / 1000.0
    operating = whole - stopped
    hours = operating / 60
    rate = max(0.0, operating / whole * 100) if whole > 0 else 0.0
    productivity = weight_ton / hours if hours > 0 else 0.0
    where = note or "期間"

    return [
        Step(key="period_shifts", name="直数(期間)",
             formula="期間に保存のあった直を数える",
             substituted=f"{where} に {days}日ぶん",
             result=f"{shifts} 直",
             terms=[Term("日数", days, "日", 0, "保存済み"),
                    Term("直数", shifts, "直", 0, "保存済み")],
             note="日数ではなく直数で数えます ── 2直しか動かなかった日を"
                  "3直ぶんで割ると、稼働率が実際より低く出ます"),
        Step(key="period_whole", name="予定総時間(分)",
             formula="1440分 × 直数",
             substituted=f"{_num(MINUTES_PER_DAY)} × {shifts}",
             result=f"{_num(whole)} 分",
             terms=[Term("1440", MINUTES_PER_DAY, "分", 0, "定数(1日)"),
                    Term("直数", shifts, "直", 0, "計算")]),
        Step(key="period_stop", name="停止合計(分)",
             formula="期間の停止をぜんぶ足す",
             substituted=f"{where} の {shifts}直ぶん",
             result=f"{_num(stopped)} 分",
             terms=[Term("停止合計", stopped, "分", 0, "計算")]),
        Step(key="period_operating", name="稼働時間(分)",
             formula="予定総時間 − 停止合計",
             substituted=f"{_num(whole)} − {_num(stopped)}",
             result=f"{_num(operating)} 分",
             terms=[Term("予定総時間", whole, "分", 0, "計算"),
                    Term("停止合計", stopped, "分", 0, "計算")]),
        Step(key="period_rate", name="稼働率(%)",
             formula="稼働時間 ÷ 予定総時間 × 100",
             substituted=(f"{_num(operating)} ÷ {_num(whole)} × 100")
             if whole > 0 else "直がありません",
             result=f"{_num(rate, 1)} %",
             terms=[Term("稼働時間", operating, "分", 0, "計算"),
                    Term("予定総時間", whole, "分", 0, "計算")],
             note="日ごとの稼働率の平均ではありません。平均にすると、"
                  "1直だけ動いた日と3直とも動いた日が同じ重さになります"),
        Step(key="period_productivity", name="生産性(t/h)",
             formula="期間合計重量(t) ÷ 稼働時間(h)",
             substituted=(f"{_num(weight_ton, 2)} ÷ ({_num(operating)} ÷ 60)"
                          f" = {_num(weight_ton, 2)} ÷ {_num(hours, 2)}")
             if hours > 0 else "稼働時間が0なので割れません",
             result=f"{_num(productivity, 2)} t/h",
             terms=[Term("重量(t)", weight_ton, "t", 2, "計算"),
                    Term("稼働時間(h)", hours, "h", 2, "計算")]),
    ]


# ======================================================================
# 出力用
# ======================================================================
#: CSVの見出し。**画面と同じ言葉**にする(照らし合わせるため)
CSV_FIELDNAMES = ["報告日", "ライン", "直", "項目", "式",
                  "数を入れた式", "答え", "注記"]


def as_rows(report_date: str, line: str, shift: str,
            steps: list[Step]) -> list[dict[str, str]]:
    """CSVの行にする。**1本の式が1行。**"""
    return [{
        "報告日": report_date, "ライン": line, "直": shift,
        "項目": s.name, "式": s.formula,
        "数を入れた式": s.substituted, "答え": s.result,
        "注記": s.note,
    } for s in steps]


# ======================================================================
# 計算内容.csv ── **年月のフォルダに1つだけ置く「式の説明」**
#
#     日フォルダ内に毎回入れる CSVの読み方、計算内容は年月フォルダに
#     1回だけ入れてください 無ければ入れる
#
# 前は日のフォルダに `計算内容_<日付>_<ライン>.csv` を毎日出していました。
# 中身の「式」と「注記」は毎日同じで、変わるのは数を入れた式だけです ──
# 同じ説明が日の数だけ並びます。そこで**式の形と注記だけ**を年月のフォルダに
# 1つ置きます。その日の数は `集計_` の列(稼働率・生産性など)にあり、
# 数を入れた式は画面(集計・グラフの数字を押す)で見られます。
# ======================================================================
GUIDE_FIELDNAMES = ["対象", "項目", "式", "注記"]


def _guide_sample() -> ShiftAggregate:
    """式の形を引き出すための見本。**どの枝にも0で落ちない数**にする
    (稼働時間が0のときだけ、生産性の式が「割れません」に変わるため)。"""
    return ShiftAggregate(report_date="", line="", shift="",
                          sheet_count=1, weight_kg=1000.0, work_minutes=60.0,
                          management_loss_minutes=60.0,
                          unplanned_stop_minutes=10.0,
                          handling_stop_minutes=10.0)


def guide_rows() -> list[dict[str, str]]:
    """計算内容.csv の行。**数は入れません**(どの日にも当てはまる説明なので)。"""
    sample = _guide_sample()
    rows = [{"対象": "直ごと", "項目": s.name, "式": s.formula, "注記": s.note}
            for s in shift_steps(sample)]
    rows += [{"対象": "その日ぜんぶ", "項目": s.name, "式": s.formula,
              "注記": s.note}
             for s in day_steps([sample, sample])]
    return rows
