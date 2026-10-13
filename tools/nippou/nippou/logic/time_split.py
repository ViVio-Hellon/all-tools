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

【休憩の時間を飛ばす(v4.31.0)】

    1直 12:00〜13:00 / 2直 18:00〜19:00 / 3直 02:00〜03:00 この時間を加味した割り振りに
    例: 11:40 12:20 となっていた場合は 60分追加として 11:40 13:20 になる

割り振るのは**休憩を除いた長さ**(1直なら 480 − 60 = 420 分)。行の時刻を並べたあとで、
休憩の始まりにかかる行(始まり < 12:00 ≦ 終わり)の終わりを 60 分のばし、それより後の行は
まるごと 60 分うしろへずらします(行はつながったまま。最後の行は直の終わりちょうど)。
のばした行には、休憩を停止の記号「0」(休憩食事)60 分として入れます ── 入れないと、その行の
作業時間が休憩ぶん多くなり、保存前の確かめも「休憩が足りません」と言います(入れるのは
`app/routes/entry.distribute_times`。直の中にもう「0」の停止があれば入れない)。
昼稼働(有)の直は、休憩を飛ばしません(休憩の確かめと同じ)。日勤は 1直と同じ 12:00〜13:00。

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

#: 停止の(記号, 分)の欄の組(①〜⑤)。休憩を入れる空きを探す
STOP_SLOTS: tuple[tuple[str, str], ...] = (
    ("S", "TH"), ("SS", "THS"), ("STH", "THT"), ("S4", "TH4"), ("S5", "TH5"))

#: 休憩を入れる停止の記号(`作業停止時間内訳_1` の「休憩食事」。`save_checks.REST_STOP_CODE` と同じ)
REST_CODE = "0"

#: 直ごとの休憩(始まり, 終わり)。割り振りではこの時間を飛ばす
BREAKS: dict[str, tuple[str, str]] = {
    "1直": ("12:00", "13:00"),
    "2直": ("18:00", "19:00"),
    "3直": ("02:00", "03:00"),
    "日勤": ("12:00", "13:00"),
}


def break_window(shift: str, start: int, total: int) -> Optional[tuple[int, int]]:
    """その直の休憩を(直の始めから何分目, 長さ)で。直の中に収まらなければ None。"""
    found = BREAKS.get(shift)
    if not found:
        return None
    b_start, b_end = parse_hhmm(found[0]), parse_hhmm(found[1])
    if b_start is None or b_end is None:
        return None
    offset = (b_start - start) % (24 * 60)
    length = (b_end - b_start) % (24 * 60)
    if length <= 0 or offset <= 0 or offset + length >= total:
        return None
    return offset, length


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
    #: 休憩にかかって終わりをのばした行(`slots` の何番目か)。休憩を飛ばさなければ None
    break_index: Optional[int] = None


def split(stops: Sequence[Decimal], total: int, *, step: int = STEP,
          rest: Optional[tuple[int, int]] = None) -> Plan:
    """`stops`(行ごとの停止の分)の行へ、`total` 分を割り振る。

    `rest` は休憩(直の始めから何分目, 長さ)。その長さを除いて割り振り、休憩にかかる行の
    終わりをのばし、後ろの行をずらす(`break_window`)。
    """
    if rest is not None:
        at, length = rest
        plan = split(stops, total - length, step=step)
        if not plan.ok:
            return plan
        moved: list[tuple[int, int]] = []
        for index, (a, b) in enumerate(plan.slots):
            if a >= at:
                moved.append((a + length, b + length))
            elif b >= at:
                moved.append((a, b + length))
                plan.break_index = index
            else:
                moved.append((a, b))
        plan.slots = moved
        return plan
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
