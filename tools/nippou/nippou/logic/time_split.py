"""時間の自動割り振り (v4.30.0) ── 直の開始〜終了を、打った行へ順に割り振る

    日報入力: 開始行と終了行の時間は一切入力せず 作業者の任意のタイミングで発火させると
    開始行と終了行までの時間を割り当てる機能
    例: 1ページ 1〜12、2ページ 1〜10 で梱包数・停止・LOTNO を入れたあとに発火させたとして
    1直として、その直の開始時間からスタート 1ページ 1行目 07:00
    2ページ 10行目に直の終了時間 15:00
    22行を 480分で割り振る(21分あたりずつ振っていく感じ)
    07:00 07:20 / 07:20 07:45 / 07:45 08:05 と自動で振り分けていく
    この機能のオンオフは設定に置く パスワード要

【割り振り方】
直の長さ(分)を、打った行の数で**等しく**分けます。ただし

    ・**停止の分はその行に先に渡す**(停止 30 分の行に 20 分しか渡さないと、作業時間が
      マイナスになって保存できない)。残りを行の数で等しく分ける
    ・区切りの時刻は**5 分刻みに寄せる**(四捨五入。紙に書く時刻と同じ刻み)。寄せるのは
      「始めから何分目か」の積み上げなので、ずれは貯まらない
    ・最初の行の開始は直の開始、最後の行の終了は直の終了ちょうど

例の 22 行・480 分なら 1 行あたり 21.8 分で、区切りは 21.8 → 20、43.6 → 45、65.5 → 65 …
(07:20・07:45・08:05)── 依頼の例と同じになります。

1 行に 5 分も渡らないときは 1 分刻み、1 分も渡らないとき・停止の合計が直より長いときは
割り振りません(理由を返す)。

ここは計算だけ。どの行を数えるか・ページをまたいで書くのは `app/routes/entry.distribute_times`。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Optional, Sequence

#: 区切りの刻み(分)。1 行に渡る分がこれより短いときは 1 分刻み
STEP = 5

#: 打った行とみなす欄(どれか1つでも入っていれば数える)。時刻の欄は見ない ── 割り振る前は空
FILLED_FAMILIES: tuple[str, ...] = (
    "LOT", "ZAI", "SIZ", "KEN", "HIT", "MAI", "TUT", "CON", "WEI",
    "S", "TH", "SS", "THS", "STH", "THT", "S4", "TH4", "S5", "TH5",
)

#: 停止の分の欄(①〜⑤)
STOP_MINUTE_FAMILIES: tuple[str, ...] = ("TH", "THS", "THT", "TH4", "TH5")


def is_filled(values: dict[str, str]) -> bool:
    """割り振る行か(打った欄が1つでもある)。"""
    return any(str(values.get(f, "") or "").strip() for f in FILLED_FAMILIES)


def row_stop_minutes(values: dict[str, str]) -> Decimal:
    """その行の停止の分の合計(①〜⑤)。"""
    from .work_time import stop_minutes

    return sum((stop_minutes(str(values.get(f, "") or "")) for f in STOP_MINUTE_FAMILIES),
               Decimal(0))


def parse_hhmm(text: str) -> Optional[int]:
    """"07:00" → 420(0:00 からの分)。読めなければ None。"""
    try:
        hour, minute = str(text).strip().split(":")
        h, m = int(hour), int(minute)
    except (ValueError, AttributeError):
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h * 60 + m


def clock(start: int, offset: int) -> tuple[str, str]:
    """直の開始(0:00 からの分)から offset 分あとの (時, 分)。日をまたげば 0 時へ戻る。"""
    total = (start + offset) % (24 * 60)
    return f"{total // 60:02d}", f"{total % 60:02d}"


@dataclass
class Plan:
    """割り振った結果。`slots` は行ごとの(始めから何分目, 終わりは何分目)。"""

    ok: bool
    slots: list[tuple[int, int]] = field(default_factory=list)
    message: str = ""
    step: int = STEP


def split(stops: Sequence[Decimal], total: int, *, step: int = STEP) -> Plan:
    """`stops`(行ごとの停止の分)の行へ、`total` 分を割り振る。"""
    n = len(stops)
    if n == 0:
        return Plan(False, message="割り振る行がありません(ロット№・枚数・停止などを打った行が1つもありません)")
    if total <= 0:
        return Plan(False, message="直の長さが分かりません(時間マスタの開始・終了を確かめてください)")
    stop_total = sum(stops, Decimal(0))
    work = Decimal(total) - stop_total
    if work < n:
        if work < 0:
            return Plan(False, message=f"停止の合計 {stop_total} 分が、直の長さ {total} 分より長いので割り振れません")
        return Plan(False, message=f"{n}行に {work} 分しか残らず、1行に1分も渡らないので割り振れません"
                                   f"(直の長さ {total} 分・停止の合計 {stop_total} 分)")
    each = work / n
    if each < step:
        step = 1
    unit = Decimal(step)

    def snap(value: Decimal) -> int:
        return int((value / unit).quantize(Decimal(1), rounding=ROUND_HALF_UP) * unit)

    def ceil_step(value: Decimal) -> int:
        return int((value / unit).quantize(Decimal(1), rounding=ROUND_CEILING) * unit)

    slots: list[tuple[int, int]] = []
    start = 0
    ideal = Decimal(0)
    for index, stop in enumerate(stops):
        ideal += stop + each
        end = total if index == n - 1 else snap(ideal)
        # 停止の分より短くしない(作業時間をマイナスにしない)。最後の行は直の終わりちょうど
        floor = start + ceil_step(stop) if stop > 0 else start + (1 if step == 1 else 0)
        if index < n - 1 and end < floor:
            end = floor
        if index < n - 1 and end <= start:
            end = start + step
        if end - start < stop or end > total:
            return Plan(False, message="刻みに寄せると直の中に収まりません(停止の分が大きい行が続いています)。"
                                       "停止の分を確かめてください")
        slots.append((start, end))
        start = end
    return Plan(True, slots=slots, step=step)
