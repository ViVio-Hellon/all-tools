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

【梱包数に合わせる(v4.34.0)】

    時間を割り振る:梱包数に数値がある場合はそこを加味した時間振りにしてください
    梱包10包み1
    梱包5包み1 であれば梱包10包み1のほうが時間振りが多い
    梱包数の入力がなければ均等でいいです

梱包数は、保存前の確かめ「梱包数が検入枚数を超えている」(`save_checks.check_pack_count`)と
同じ **個装単位 枚数(MAI)× 梱包単位 包数(TUT)**。包数が空なら 1 と数えます(入力画面でも、
包数が空のまま離れると 1 が入る ── `input_shortcuts.default_packs`)。

停止の分を先に渡した**残りを、梱包数の比で分けます**(梱包 10 の行は梱包 5 の行の 2 倍)。

    ・**梱包数がどの行にも無ければ、いままでどおり均等**(重みが全部同じ、として同じ道を通す)
    ・一部の行だけ梱包数が無いときは、その行は**梱包数がある行の平均**で数える。0 と数えると
      その行が極端に短く、いちばん大きい行で数えると極端に長くなるため(停止だけの行もこれ)
    ・5 分刻み・積み上げで寄せる・最後の行は直の終わりちょうど・休憩を飛ばす、は同じ。
      1 分刻みに落とすのも今までどおり、均等でも 1 行に 5 分取れないときだけ。比が極端(1 と
      1000)でも 5 分刻みのまま、**どの行にも最低 1 刻み(5 分)は渡す**(後ろの行のぶんを残して
      区切る。停止がある行は、停止を刻みに切り上げた分が下限)。例: 梱包 1 と 1000 で 480 分なら
      0〜5 分・5〜480 分

ここは計算だけ。どの行を数えるか・ページをまたいで書くのは `app/routes/entry.distribute_times`。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
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


def row_packs(values: dict[str, str]) -> Decimal:
    """その行の梱包数(個装単位 枚数 × 梱包単位 包数)。無ければ 0。

    読み方は `save_checks.check_pack_count` と同じ(数字でない欄は 0、小数は切り捨て)。
    **包数が空なら 1**(入力画面で包数が空のまま離れると 1 が入るのと同じ)。
    """
    from .numeric import is_numeric, to_float

    def whole(text: str) -> int:
        return int(to_float(text)) if is_numeric(text) else 0

    mai = str(values.get("MAI", "") or "").strip()
    tut = str(values.get("TUT", "") or "").strip()
    packs = whole(mai) * (whole(tut) if tut else 1)
    return Decimal(packs) if packs > 0 else Decimal(0)


def pack_weights(packs: Sequence[Decimal]) -> tuple[list[Decimal], bool]:
    """行ごとの梱包数を、割り振りの重みに。返すのは(重み, 梱包数に合わせたか)。

    梱包数がどの行にも無ければ全部 1(均等)。一部の行だけ無ければ、その行は**梱包数がある行の
    平均**で数えます(0 だと極端に短く、最大だと極端に長くなるため)。
    """
    given = [p for p in packs if p > 0]
    if not given:
        return [Decimal(1)] * len(packs), False
    average = sum(given, Decimal(0)) / len(given)
    return [p if p > 0 else average for p in packs], True


#: 確かめの窓に出す、割り振り方の一言(`pack_weights` の 2 つめで選ぶ)
WEIGHT_NOTES: dict[bool, str] = {
    True: "梱包数に合わせて割り振ります(梱包数が無い行は平均で数えます)",
    False: "梱包数が無いので均等に割り振ります",
}


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
          rest: Optional[tuple[int, int]] = None,
          weights: Optional[Sequence[Decimal]] = None) -> Plan:
    """`stops`(行ごとの停止の分)の行へ、`total` 分を割り振る。

    `rest` は休憩(直の始めから何分目, 長さ)。その長さを除いて割り振り、休憩にかかる行の
    終わりをのばし、後ろの行をずらす(`break_window`)。

    `weights` は行ごとの重み(梱包数。`pack_weights`)。停止の分を先に渡した残りを、この比で
    分けます。省けば全部 1 ── 均等も同じ道です。
    """
    if rest is not None:
        at, length = rest
        plan = split(stops, total - length, step=step, weights=weights)
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
    if weights is None or len(weights) != n or any(w <= 0 for w in weights):
        weights = [Decimal(1)] * n
    weight_total = sum(weights, Decimal(0))
    shares = [work * w / weight_total for w in weights]
    # 1 分刻みに落とすのは、均等でも 1 行に 5 分取れないとき(打った行の数 × 5 分が収まらない)だけ。
    # 梱包数の比が極端で短い行が出ても 5 分刻みのまま、その行にも 1 刻み(5 分)は渡す(`least`)
    if work / n < step:
        step = 1
    unit = Decimal(step)

    def snap(value: Decimal) -> int:
        return int((value / unit).quantize(Decimal(1), rounding=ROUND_HALF_UP) * unit)

    def ceil_step(value: Decimal) -> int:
        return int((value / unit).quantize(Decimal(1), rounding=ROUND_CEILING) * unit)

    def floor_step(value: Decimal) -> int:
        return int((value / unit).quantize(Decimal(1), rounding=ROUND_FLOOR) * unit)

    # 行ごとに最低これだけは渡す(停止の分を刻みに切り上げたもの。停止が無くても 1 刻み)。
    # 後ろの行のぶんを残して区切るので、比が極端でも 0 分の行は出ない
    least = [max(ceil_step(stop), step) for stop in stops]
    reserve = [sum(least[index + 1:]) for index in range(n)]

    slots: list[tuple[int, int]] = []
    start = 0
    ideal = Decimal(0)
    for index, (stop, share) in enumerate(zip(stops, shares)):
        ideal += stop + share
        end = total if index == n - 1 else min(snap(ideal), floor_step(Decimal(total - reserve[index])))
        # 停止の分より短くしない(作業時間をマイナスにしない)。最後の行は直の終わりちょうど
        floor = start + ceil_step(stop) if stop > 0 else start + (1 if step == 1 else 0)
        if index < n - 1 and end < floor:
            end = floor
        if index < n - 1 and end <= start:
            end = start + step
        if end - start < stop or end <= start or end > total:
            return Plan(False, message="刻みに寄せると直の中に収まりません(停止の分が大きい行が続いています)。"
                                       "停止の分を確かめてください")
        slots.append((start, end))
        start = end
    return Plan(True, slots=slots, step=step)
