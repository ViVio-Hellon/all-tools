"""梱包実績の集計 (VBA ``Agg_OutPut`` / ``Aggre_Calcul`` の**中身だけ**)

【座標は捨てる。内容と計算は残す】
VBA の `Agg_OutPut` は、印刷シートの各セルを読んで「梱包実績日報2原紙」の
決まった座標(`Cells(n, 4)` `Cells(n, 25)` …)へ書き出していました。どの
列が何かはシートの位置でしか分からず、列を1つ足すと以降の座標が全部
ずれます。**その仕組みは持ち込みません。**

持ち込むのは、そこで**何を出していたか**と**どう計算していたか**です。
出していた項目は1つも減らしません ── 紙に載らない用途コード・用途名・
納入先・包装仕様書No・コイル縦割/横縦割まで含めて、ここが唯一の出口
だったので。

    VBA                        ここ
    Agg_OutPut   の中身   →   `build_rows`  (1ロット1行 + 停止は縦持ち)
    Aggre_Calcul の計算   →   `totals`      (直1つぶんの合計)
    (VBAに無い)            →   `by_key`      (ロット・用途・納入先別)

【`aggregation.py` と何が違うのか】
`aggregation.py` は**直の合計**を明細から直接出します(`aggregate_shift`)。
こちらは**1ロット1行の並び**を先に作り、その並びから合計を出します ──
VBA が「集計シートへ並べてから、その表を読んで合計した」のと同じ順序
です。`totals(build_rows(...))` と `aggregate_shift(...)` は**同じ値**に
なります(`tests/test_packing_agg.py` がそれを見張ります)。順序を合わせて
あるのは、ロット別の集計と直の合計が食い違わないようにするためです。

【空の行は並べない】
12行のうち打ったのが3行なら、残り9行は紙の余白です。VBA も
`Tim_BlankCount` で開始時刻のある行だけを置いていました。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Optional

from ..db.models import DetailRecord, HeaderRecord
from .aggregation import (
    MINUTES_PER_DAY,
    SHIFT_ORDER,
    STOP_KINDS,
    ShiftAggregate,
    classify_stop_code,
)
from .numeric import is_numeric, to_float
from .shift import parse_business_date

#: 停止3組の (記号の列, 時間の列)。紙の作業停止①②③
STOP_PAIRS: tuple[tuple[str, str], ...] = (("s", "th"), ("ss", "ths"),
                                           ("sth", "tht"))

#: ロット別集計の鍵。**この5つが揃って1つの単位**
KEY_FIELDS: tuple[str, ...] = ("lot_no", "purpose_code", "purpose_name",
                               "delivery_destination", "packing_spec_no")

#: 鍵の見出し(画面・CSVで使う)
KEY_LABELS: tuple[tuple[str, str], ...] = (
    ("lot_no", "ロット№"), ("purpose_code", "用途コード"),
    ("purpose_name", "用途名"), ("delivery_destination", "納入先"),
    ("packing_spec_no", "包装仕様NO"),
)


def num(text: object) -> float:
    """文字を数にする。数でなければ 0。**空欄は0として足す。**"""
    s = "" if text is None else str(text)
    return to_float(s) if is_numeric(s) else 0.0


def _int(text: object) -> Optional[int]:
    """時・分。**打っていなければ None**(0時0分と区別する)。"""
    s = ("" if text is None else str(text)).strip()
    if not is_numeric(s):
        return None
    return int(to_float(s))


# ======================================================================
# 停止1つぶん ── **横持ちから縦持ちへ**
# ======================================================================
@dataclass
class StopRow:
    """作業停止①②③のうちの1つ。

    シートでは3組が横に並んでいました(記号・時間 ×3)。縦に持つと、
    停止が4つ必要になっても列を増やさずに済み、「記号ごとに足す」が
    素直な集計になります。
    """

    stop_no: int                 # 1〜3(紙の①②③)
    stop_code: str = ""
    stop_reason: str = ""        # 内訳名(マスタが読めたとき)
    stop_minutes: float = 0.0
    #: 管理ロス停止 / 突発停止 / ハンドリング停止 / その他。
    #: **そのときの分類で固定して残します** ── VBA `Stop_Distr` は
    #: 記号の文字種で振り分けており、記号の付け替えで過去の分類が
    #: 変わってしまうと、去年のグラフが今年書き換わります
    stop_kind: str = ""

    @property
    def text(self) -> str:
        return (f"{self.stop_code} {self.stop_reason}".strip()
                if self.stop_reason else self.stop_code)


# ======================================================================
# ロット1行ぶん (`Agg_OutPut` が集計シートの1行に書いていたもの)
# ======================================================================
@dataclass
class LotRow:
    """集計の明細1行。**紙に載らない欄もここに入ります。**"""

    work_date: str
    line_name: str
    shift: str
    page: int
    row_no: int

    lot_no: str = ""
    material_condition: str = ""      # 材・調質
    dimension: str = ""               # 厚×幅×丈
    incoming_quantity: float = 0.0    # 検入枚数
    start_hour: Optional[int] = None
    start_minute: Optional[int] = None
    end_hour: Optional[int] = None
    end_minute: Optional[int] = None
    worker_count: float = 0.0         # 作業人数
    #: 合紙。**紙は「有 / 無」の2択**なので数にしません
    interleaf: str = ""
    packing_quantity: float = 0.0        # 個装単位 枚数
    packing_package_count: float = 0.0   # 梱包単位 包数
    vc_type: str = ""
    etc: str = ""
    actual_quantity: float = 0.0      # 実績合計 枚数
    actual_weight: float = 0.0        # 実績合計 重量(Kg)
    work_time: float = 0.0            # 作業時間(分)
    unit_weight: float = 0.0          # 単重
    coefficient_lot_count: float = 0.0   # 係数処理ﾛｯﾄ数(負荷係数)

    purpose_code: str = ""            # 用途コード
    purpose_name: str = ""            # 用途名
    delivery_destination: str = ""    # 納入先
    packing_spec_no: str = ""         # 包装仕様書No
    coil_vertical_split: str = ""     # コイル縦割
    coil_horizontal_split: str = ""   # コイル横縦割
    hiki_no: str = ""                 # 引当番号(この端末だけの控え)

    stops: list[StopRow] = field(default_factory=list)

    @property
    def stop_minutes(self) -> float:
        return sum(s.stop_minutes for s in self.stops)

    def minutes_of(self, kind: str) -> float:
        return sum(s.stop_minutes for s in self.stops if s.stop_kind == kind)

    @property
    def key(self) -> tuple[str, ...]:
        return tuple(getattr(self, name) for name in KEY_FIELDS)

    @property
    def sort_key(self) -> tuple:
        """並べ替えの鍵。**日付→直→ページ→行**(紙をめくる順)。"""
        return (parse_business_date(self.work_date) or date.min,
                SHIFT_ORDER.get(self.shift, 99), self.shift,
                self.page, self.row_no)


def has_content(d: DetailRecord) -> bool:
    """その行に何か打ってあるか。**空の行は集計に並べません。**"""
    watched = ("lot", "zai", "siz", "ken", "kz", "kh", "sz", "sh", "hit", "ai",
               "mai", "tut", "vc", "et", "s", "th", "ss", "ths", "sth", "tht",
               "con", "wei", "tim", "uni",
               "others1", "others2", "others3", "others4", "others5",
               "others6", "keisu")
    return any((getattr(d, name, "") or "").strip() for name in watched)


def build_stops(d: DetailRecord,
                labels: Optional[dict[str, str]] = None) -> list[StopRow]:
    """停止3組を縦にする。**記号が空の組は作りません。**

    時間だけ入っていて記号が無い行は、どの分類にも入れようがないので
    置きません(VBA も文字種で振り分けられないものは集計から外して
    いました)。記号があって時間が空なら、0分の1回として残します ──
    「書いたのに回数に出ない」を避けるためです。
    """
    names = labels or {}
    out: list[StopRow] = []
    for index, (code_field, minutes_field) in enumerate(STOP_PAIRS, start=1):
        code = (getattr(d, code_field, "") or "").strip()
        if not code:
            continue
        out.append(StopRow(
            stop_no=index, stop_code=code,
            stop_reason=names.get(code, ""),
            stop_minutes=num(getattr(d, minutes_field, "")),
            stop_kind=STOP_KINDS.get(classify_stop_code(code), "その他")))
    return out


def build_row(header: HeaderRecord, d: DetailRecord,
              labels: Optional[dict[str, str]] = None) -> LotRow:
    """明細1行 → 集計の1行。**座標ではなく名前で移します。**"""
    return LotRow(
        work_date=header.report_date, line_name=header.line,
        shift=header.shift, page=header.page, row_no=d.row_no,
        lot_no=(d.lot or "").strip(),
        material_condition=(d.zai or "").strip(),
        dimension=(d.siz or "").strip(),
        incoming_quantity=num(d.ken),
        start_hour=_int(d.kz), start_minute=_int(d.kh),
        end_hour=_int(d.sz), end_minute=_int(d.sh),
        worker_count=num(d.hit),
        interleaf=(d.ai or "").strip(),
        packing_quantity=num(d.mai),
        packing_package_count=num(d.tut),
        vc_type=(d.vc or "").strip(),
        etc=(d.et or "").strip(),
        actual_quantity=num(d.con),
        actual_weight=num(d.wei),
        work_time=num(d.tim),
        unit_weight=num(d.uni),
        coefficient_lot_count=num(d.keisu),
        purpose_code=(d.others1 or "").strip(),
        purpose_name=(d.others2 or "").strip(),
        delivery_destination=(d.others3 or "").strip(),
        packing_spec_no=(d.others4 or "").strip(),
        coil_vertical_split=(d.others5 or "").strip(),
        coil_horizontal_split=(d.others6 or "").strip(),
        hiki_no=(d.hiki_no or "").strip(),
        stops=build_stops(d, labels))


def build_rows(header: HeaderRecord, details: Iterable[DetailRecord],
               labels: Optional[dict[str, str]] = None) -> list[LotRow]:
    """1ページぶん(`Agg_OutPut` の1回ぶん)。行番号の順に並べます。"""
    return [build_row(header, d, labels)
            for d in sorted(details, key=lambda r: r.row_no)
            if has_content(d)]


def build_shift_rows(pages: Iterable[tuple[HeaderRecord, list[DetailRecord]]],
                     labels: Optional[dict[str, str]] = None) -> list[LotRow]:
    """直1つぶん(ページをまたいで並べる)。"""
    out: list[LotRow] = []
    for header, details in pages:
        out.extend(build_rows(header, details, labels))
    out.sort(key=lambda r: r.sort_key)
    return out


# ======================================================================
# 直の合計 (`Aggre_Calcul`)
# ======================================================================
def totals(rows: Iterable[LotRow], *, work_date: str = "", line: str = "",
           shift: str = "") -> ShiftAggregate:
    """ロット行の並び → 直1つぶんの合計。

    **`aggregation.aggregate_shift` と同じ値になります。** あちらは
    明細から直接、こちらは並べたロット行から。VBA も「シートへ並べて
    から、その表を読んで合計する」順序だったので、そちらに合わせて
    あります ── 画面に出ている表と合計が食い違わないように。

    稼働時間・操業時間・稼働率・生産性は `ShiftAggregate` が停止時間
    から出します(式を2か所に持たない)。
    """
    rows = list(rows)
    first = rows[0] if rows else None
    agg = ShiftAggregate(
        report_date=work_date or (first.work_date if first else ""),
        line=line or (first.line_name if first else ""),
        shift=shift or (first.shift if first else ""))
    for row in rows:
        agg.sheet_count += row.actual_quantity
        agg.weight_kg += row.actual_weight
        agg.work_minutes += row.work_time
        agg.management_loss_minutes += row.minutes_of("管理ロス停止")
        agg.unplanned_stop_minutes += row.minutes_of("突発停止")
        agg.handling_stop_minutes += row.minutes_of("ハンドリング停止")
    return agg


def lot_count(rows: Iterable[LotRow]) -> int:
    """ﾛｯﾄ数。**同じロットが何行に分かれていても1つ**(重複を除く)。

    空のロット番号は数えません ── 番号を打たずに時間だけ入れた行
    (全停など)を1ロットと数えると、ロット数が実態より多く出ます。
    """
    return len({r.lot_no for r in rows if r.lot_no})


def coefficient_lot_count(rows: Iterable[LotRow]) -> float:
    """係数処理ﾛｯﾄ数。負荷係数の合計(`logic/load_factor.py` が入れた値)。"""
    return sum(r.coefficient_lot_count for r in rows)


def stop_rollup(rows: Iterable[LotRow]) -> list[tuple[StopRow, int]]:
    """記号ごとの (代表の停止, 回数)。時間は代表に足し込んであります。

    並びは**時間の長い順** ── 長いものから手を打つための表です。
    """
    found: dict[str, StopRow] = {}
    times: dict[str, int] = {}
    for row in rows:
        for stop in row.stops:
            kept = found.get(stop.stop_code)
            if kept is None:
                kept = StopRow(stop_no=0, stop_code=stop.stop_code,
                               stop_reason=stop.stop_reason,
                               stop_kind=stop.stop_kind)
                found[stop.stop_code] = kept
                times[stop.stop_code] = 0
            kept.stop_minutes += stop.stop_minutes
            times[stop.stop_code] += 1
    return sorted(((s, times[s.stop_code]) for s in found.values()),
                  key=lambda pair: (-pair[0].stop_minutes, pair[0].stop_code))


# ======================================================================
# ロット・用途・納入先ごと (**VBAには無い。今回の追加**)
# ======================================================================
@dataclass
class KeyTotals:
    """1つの鍵(ロット№・用途コード・用途名・納入先・包装仕様NO)ぶん。

    【直をまたいだぶんは、終わった直に付けます】
    1直で始めて2直で終わったロットは、**2直の行になります。** 開始側の
    作業時間・停止時間も、そこへ足します。

    そうしないと、同じ1つの荷が2つの行に割れて、どちらを見ても本当に
    かかった時間が出てきません。「このロットに何分かかったか」は1つの
    数であるべきで、直の区切りは作業の区切りではないからです。

    `shifts` に、またいだ直を並べてあります ── 数が合わないと言われた
    ときに、**どの直を足した結果なのかが行から読める**ようにするため。
    """

    lot_no: str = ""
    purpose_code: str = ""
    purpose_name: str = ""
    delivery_destination: str = ""
    packing_spec_no: str = ""

    #: 終わった側(この行が属する直)
    work_date: str = ""
    line_name: str = ""
    shift: str = ""

    package_count: float = 0.0    # 梱包数(包)
    quantity: float = 0.0         # 実績枚数
    weight_kg: float = 0.0        # 実績重量
    work_time: float = 0.0        # 作業時間(分)
    rows: int = 0                 # もとになった行の数

    #: またいだ直。1つなら直をまたいでいません
    shifts: list[str] = field(default_factory=list)
    #: 記号ごとの停止(長い順)。`(停止, 回数)`
    stops: list[tuple[StopRow, int]] = field(default_factory=list)

    @property
    def carried(self) -> bool:
        """直をまたいだか。**画面で印を付けるため。**"""
        return len(self.shifts) > 1

    @property
    def stop_minutes(self) -> float:
        return sum(s.stop_minutes for s, _ in self.stops)

    @property
    def stop_text(self) -> str:
        """停止を1行で。`ｲ.突発(機械) 30分×1 / 0 休憩食事 60分×2`"""
        return " / ".join(f"{s.text} {s.stop_minutes:g}分×{n}"
                          for s, n in self.stops)

    @property
    def key(self) -> tuple[str, ...]:
        return tuple(getattr(self, name) for name in KEY_FIELDS)


def _shift_label(row: LotRow) -> str:
    return f"{row.work_date} {row.shift}"


def by_key(rows: Iterable[LotRow]) -> list[KeyTotals]:
    """ロット・用途・納入先ごとにまとめる。**直またぎを1つにします。**

    並びは、終わった直の**新しい順 → ロット№**。直の終わりに「今日
    終わったぶん」から見るための並びです。

    期間を指定して呼ぶので、**またいだ片方が期間の外にあると、その
    ぶんは入りません。** `shifts` を見れば何直ぶんを足したかは分かる
    ので、月初の行に1直しか出ていなければ前月から続いた荷だと読めます。
    """
    groups: dict[tuple[str, ...], list[LotRow]] = {}
    for row in rows:
        groups.setdefault(row.key, []).append(row)

    out: list[KeyTotals] = []
    for key, members in groups.items():
        members.sort(key=lambda r: r.sort_key)
        last = members[-1]                     # **終わった側の直**
        total = KeyTotals(
            **dict(zip(KEY_FIELDS, key)),
            work_date=last.work_date, line_name=last.line_name,
            shift=last.shift, rows=len(members))
        seen: list[str] = []
        for row in members:
            total.package_count += row.packing_package_count
            total.quantity += row.actual_quantity
            total.weight_kg += row.actual_weight
            total.work_time += row.work_time
            label = _shift_label(row)
            if label not in seen:
                seen.append(label)
        total.shifts = seen
        total.stops = stop_rollup(members)
        out.append(total)

    out.sort(key=lambda t: (-(parse_business_date(t.work_date) or date.min).toordinal(),
                            -SHIFT_ORDER.get(t.shift, 99), t.lot_no))
    return out


# ======================================================================
# 日ごとの指標 (集計シートの上の帯が描いていたもの)
# ======================================================================
@dataclass
class DayPoint:
    """1日ぶんの指標。**画面の数字とグラフはここだけを読みます。**"""

    work_date: str
    line_name: str = ""
    quantity: float = 0.0          # 直枚数(枚) ── その日の全直ぶん
    weight_kg: float = 0.0
    work_minutes: float = 0.0
    stop_minutes: float = 0.0      # 停止(分)
    operating_minutes: float = 0.0  # 稼働時間(分)
    shifts: int = 0
    cumulative_quantity: float = 0.0   # 累積枚数(枚)
    #: その日の枚数の**直ごとの内訳**(直名 → 枚数)。棒を直ごとに
    #: 積み上げるために持ちます ── `quantity` はこの合計です。
    #:
    #: **合計を捨てて内訳だけにはしません。** 累積も稼働率も合計から
    #: 出しているので、読む側が毎回足し直すことになります。
    quantity_by_shift: dict[str, float] = field(default_factory=dict)

    @property
    def weight_ton(self) -> float:
        return self.weight_kg / 1000.0

    @property
    def operating_rate_pct(self) -> float:
        """稼働率(%)。**直の平均ではなく、合計から出し直します。**

        1直だけ動いた日と3直とも動いた日が同じ数字にならないよう、
        止まっていた分を足してから、直の数だけ伸ばした1440分で割ります。
        """
        whole = float(MINUTES_PER_DAY * self.shifts)
        if whole <= 0:
            return 0.0
        return max(0.0, (whole - self.stop_minutes) / whole * 100)

    @property
    def productivity_t_per_h(self) -> float:
        hours = self.operating_minutes / 60
        return self.weight_ton / hours if hours > 0 else 0.0


def day_points(shifts: Iterable[ShiftAggregate]) -> list[DayPoint]:
    """直ごとの合計 → 日ごとの指標(**累積枚数つき**)。

    累積は**渡された期間の中**で積みます。VBA の集計シートが常に
    「月初〜出力時まで」を描いていたので、既定の期間も月初〜当日です
    ── 月の途中で「いま何枚まで来たか」を見るための数字だからです。
    """
    order: list[str] = []
    found: dict[str, DayPoint] = {}
    for agg in shifts:
        point = found.get(agg.report_date)
        if point is None:
            point = DayPoint(work_date=agg.report_date, line_name=agg.line)
            found[agg.report_date] = point
            order.append(agg.report_date)
        point.quantity += agg.sheet_count
        # 直ごとの内訳。**同じ直が2度来たら足します** ── ふつうは
        # 1日1回ですが、ラインをまたいで読んだときに起こりえます
        point.quantity_by_shift[agg.shift] = (
            point.quantity_by_shift.get(agg.shift, 0.0) + agg.sheet_count)
        point.weight_kg += agg.weight_kg
        point.work_minutes += agg.work_minutes
        point.stop_minutes += agg.total_stop_minutes
        point.operating_minutes += agg.operating_minutes
        point.shifts += 1

    order.sort(key=lambda d: parse_business_date(d) or date.min)
    running = 0.0
    out: list[DayPoint] = []
    for work_date in order:
        point = found[work_date]
        running += point.quantity
        point.cumulative_quantity = running
        out.append(point)
    return out
