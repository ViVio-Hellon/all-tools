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

【EX・ロットの変わり目・VC交換(v4.42.0)】

    EX が付いている場合は作業時間が EX でないものより多いです
    (EX:そのLOTすべてが長くなるEX梱包のため)
    LOTが変わるタイミングの1梱包目は通常よりもやや長いです
    (1梱包目の前に段取りが入るためです 1梱包目だけ長めです)
    LOTが変わるタイミングで前のLOTとVCが違う場合も通常より長くなります
    (1梱包目の前にVC交換が入るためです 1梱包目だけ長めです)

梱包数の重みに、さらに倍率を掛けます(`adjust_weights`。倍率は設定で変えられる `Factors`)。

    ・**ロット**は行のロット№。空の行は**上の行と同じロット**(続きの行はロット№を打たない)。
      ページをまたいでも続く
    ・**EX** は etc 欄に「EX」がある行。**そのロットの行すべて**を EX の倍率で(EX の印は
      ロット№を引いた行にしか付かないので、ロットで数える)
    ・**ロットの変わり目**はロット№が上と違う行。その行の**1梱包目だけ**を段取りの倍率で
      長くする(1梱包 = その行の梱包数 ÷ 包数。包数が空なら 1 包)
    ・変わり目で**前のロットと VC(両面・片面)が違えば**、1梱包目にさらに VC交換のぶんを足す
      (段取り 1.5倍・VC交換 1.5倍なら 1梱包目は 1 + 0.5 + 0.5 = 2倍)。VC が空の行は上の行と同じ
    ・直の**最初の行**は、前の直の最後のロット(手元に保存があれば)と比べる。同じロットなら
      変わり目ではない。前の直の保存が無ければ変わり目として数える(段取りから始まる)。
      VC は比べる相手が無ければ足さない
    ・**段取りの停止がある行は段取りのぶんを、VC交換の停止がある行は VC交換のぶんを足さない**(v4.42.2)。
      その時間は停止の分として先に渡してあるので、1梱包目を長くすると2回数える。EX の倍率はそのまま。

          段取り変更 と 段取り(板) は違う(あるLOTの準備をしていたのをやめて違うLOTの準備を始めた
          → 段取り変更)。段取り(板) の停止がある行は長くしないでください。VC交換(板) がVC交換。
          停止内訳の文字列は可変なので 段取り・VC交換 になる可能性もある

      どれが段取り・VC交換の停止かは、停止理由の内訳(マスタ・停止内訳.csv)の**名前**で見る(`stop_kind`):
      段取り = 「段取」を含み「変更」を含まない、VC交換 = 「VC」と「交換」を含む。段取り変更では変えない

ここは計算だけ。どの行を数えるか・ページをまたいで書くのは `app/routes/entry.distribute_times`。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Optional, Sequence

from .. import constants
from . import break_time
from .shift import hhmm_minutes

#: 区切りの刻み(分)。1 行に渡る分がこれより短いときは 1 分刻み
STEP = 5

#: 打った行とみなす欄(どれか1つでも入っていれば数える)。時刻の欄は見ない ── 割り振る前は空
FILLED_FAMILIES: tuple[str, ...] = (
    "LOT", "ZAI", "SIZ", "KEN", "HIT", "MAI", "TUT", "CON", "WEI",
    "S", "TH", "SS", "THS", "STH", "THT", "S4", "TH4", "S5", "TH5",
)

#: 停止の分の欄(①〜⑤)
STOP_MINUTE_FAMILIES: tuple[str, ...] = constants.STOP_TIME_FAMILIES

#: 停止の(記号, 分)の欄の組(①〜⑤)。休憩を入れる空きを探す。**組は `constants.STOP_SLOTS` の1か所**
STOP_SLOTS: tuple[tuple[str, str], ...] = constants.STOP_SLOTS

#: 休憩を入れる停止の記号(`作業停止時間内訳_1` の「休憩食事」。`break_time.REST_STOP_CODE` の1か所)
REST_CODE = break_time.REST_STOP_CODE

#: 直ごとの休憩(始まり, 終わり)。割り振りではこの時間を飛ばす
BREAKS: dict[str, tuple[str, str]] = {
    "1直": ("12:00", "13:00"),
    "2直": ("18:00", "19:00"),
    "3直": ("02:00", "03:00"),
    "日勤": ("12:00", "13:00"),
}


def break_window(shift: str, start: int, total: int, *,
                 from_start: bool = False) -> Optional[tuple[int, int]]:
    """その直の休憩を(直の始めから何分目, 長さ)で。直の中に収まらなければ None。

    ``from_start`` なら、休憩が**ちょうど始まりから**でも数える(v4.43.0。LOTで時間を割り振るは
    前のロットが 12:00 に終わって始まることがある。その最初の行が休憩をまたぐ)。
    """
    found = BREAKS.get(shift)
    if not found:
        return None
    b_start, b_end = parse_hhmm(found[0]), parse_hhmm(found[1])
    if b_start is None or b_end is None:
        return None
    offset = (b_start - start) % (24 * 60)
    length = (b_end - b_start) % (24 * 60)
    if length <= 0 or (offset <= 0 and not from_start) or offset + length >= total:
        return None
    return offset, length


def is_filled(values: dict[str, str]) -> bool:
    """割り振る行か(打った欄が1つでもある)。"""
    return any(str(values.get(f, "") or "").strip() for f in FILLED_FAMILIES)


def is_rest(values: dict[str, str], code_family: str) -> bool:
    """その停止の記号が休憩「0」か(全角の「０」も同じ。決めるのは `break_time.is_rest_code`)。"""
    return break_time.is_rest_code(values.get(code_family, ""))


def row_stop_minutes(values: dict[str, str], *, minus: Decimal = Decimal(0)) -> Decimal:
    """その行の停止の分の合計(①〜⑤)。

    `minus` はそこから引く分(v4.38.1)。休憩の時間を飛ばす直では、決まった休憩を打った分
    (`RestParts.lunch_minutes`)を引く ── 休憩は飛ばした時間そのものなので、打ってある「0」まで
    数えると休憩を2回数えてしまう。
    """
    from .work_time import stop_minutes

    total = sum((stop_minutes(str(values.get(f, "") or "")) for f in STOP_MINUTE_FAMILIES), Decimal(0))
    return max(total - minus, Decimal(0))


@dataclass
class RestParts:
    """その行の休憩「0」の中身(v4.38.2)。"""

    #: 休憩「0」の分の合計(①〜⑤)
    total: Decimal
    #: 決まった休憩とは**別の休憩**の時刻(直の中で、決まった休憩と重ならないもの。9:00〜9:10 など)
    separate: list = field(default_factory=list)
    #: 書いてある時刻が全部、別の休憩か(この行の「0」は決まった休憩を含まない)
    only_separate: bool = False

    @property
    def separate_minutes(self) -> int:
        return sum(r.minutes for r in self.separate)

    @property
    def lunch_minutes(self) -> Decimal:
        """決まった休憩を打ったものとみなす分(割り振りでは数えず、またぐ行へまとめる)。"""
        if self.only_separate:
            return Decimal(0)
        return max(self.total - self.separate_minutes, Decimal(0))


def rest_parts(values: dict[str, str], shift: str,
               shift_window: Optional[tuple[int, int]] = None) -> RestParts:
    """その行の休憩「0」を、決まった休憩(`BREAKS`)と別の休憩に分ける(v4.38.2)。

    時刻が無い・読めない・決まった休憩と重なる・直の外(ありえない時刻)のものは、決まった
    休憩を打ったものとみなします。直の中で決まった休憩と重ならない時刻だけが、別の休憩です。
    ``shift_window`` は直の時間(0 時からの分。`break_time.shift_window`)。
    """
    from . import break_time
    from .work_time import stop_minutes

    total = sum((stop_minutes(str(values.get(minutes, "") or "")) for code, minutes in STOP_SLOTS
                 if is_rest(values, code)), Decimal(0))
    found = BREAKS.get(shift)
    if total <= 0 or not found:
        return RestParts(total)
    ranges = break_time.parse_all(str(values.get(break_time.ROW_FAMILY, "") or ""))
    window = break_time.parse(f"{found[0]}〜{found[1]}")
    if not ranges or window is None:
        return RestParts(total)
    separate = [r for r in ranges if not break_time.overlaps(r, window)
                and (shift_window is None or break_time.inside(r, shift_window))]
    return RestParts(total, separate, only_separate=len(separate) == len(ranges))


def free_slot(values: dict[str, str]) -> Optional[tuple[str, str]]:
    """停止①〜⑤のうち、記号も分も空いている欄。無ければ None。"""
    for code, minutes in STOP_SLOTS:
        if not str(values.get(code, "") or "").strip() and not str(values.get(minutes, "") or "").strip():
            return code, minutes
    return None


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


@dataclass(frozen=True)
class Factors:
    """EX・段取り・VC交換の倍率(v4.42.0)。設定・管理者で変えられる(`KEY_TIME_SPLIT_FACTORS`)。"""

    #: EX のロットの行すべて
    ex: Decimal = Decimal("1.2")
    #: ロットの変わり目の1梱包目(段取り)
    setup: Decimal = Decimal("1.5")
    #: 変わり目で前のロットと VC が違うときの1梱包目(VC交換)。段取りのぶんに足す
    vc: Decimal = Decimal("1.5")

    #: 設定で選べる範囲(1 = 長くしない)
    LOW = Decimal("1")
    HIGH = Decimal("5")

    @classmethod
    def read(cls, raw: object) -> "Factors":
        """設定に書いた値(`{"ex": 1.2, ...}`)から。読めない・範囲の外の値は既定のまま。"""
        base = cls()
        if not isinstance(raw, dict):
            return base
        values = {}
        for name in ("ex", "setup", "vc"):
            got = parse_factor(raw.get(name))
            values[name] = got if got is not None else getattr(base, name)
        return cls(**values)

    def as_dict(self) -> dict[str, float]:
        return {"ex": float(self.ex), "setup": float(self.setup), "vc": float(self.vc)}


def parse_factor(value: object) -> Optional[Decimal]:
    """倍率の1つ(1〜5、小数第2位まで)。読めなければ None。"""
    import unicodedata
    from decimal import InvalidOperation

    text = unicodedata.normalize("NFKC", str(value if value is not None else "")).strip()
    if not text:
        return None
    try:
        got = Decimal(text)
    except InvalidOperation:
        return None
    if not got.is_finite() or got < Factors.LOW or got > Factors.HIGH:
        return None
    return got.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP).normalize()


def _norm(text: object) -> str:
    import unicodedata

    return unicodedata.normalize("NFKC", str(text or "")).strip().upper()


def is_ex(values: dict[str, str]) -> bool:
    """etc 欄に「EX」がある行か(全角の「ＥＸ」も同じ)。"""
    return "EX" in _norm(values.get("ET", ""))


@dataclass
class RowKind:
    """行ごとの、割り振りの重みにかかわる事実(`lot_runs`)。"""

    lot: str
    ex: bool = False
    #: ロットの変わり目(1梱包目に段取りが入る)
    change: bool = False
    #: 変わり目で前のロットと VC が違う(1梱包目に VC交換が入る)
    vc_change: bool = False


def lot_runs(rows: Sequence[dict[str, str]],
             before: Optional[tuple[str, str]] = None) -> list[RowKind]:
    """行の並び(ページ順・行順)を、ロットの続きで読む。

    ``before`` は前の直の最後の(ロット№, VC)。無ければ None(最初の行は変わり目として数える)。
    """
    kinds: list[RowKind] = []
    lot, vc = (_norm(before[0]), _norm(before[1])) if before else ("", "")
    known_before = before is not None and bool(lot)
    for index, values in enumerate(rows):
        mine = _norm(values.get("LOT", ""))
        my_vc = _norm(values.get("VC", ""))
        change = False
        vc_change = False
        if mine and (mine != lot or (index == 0 and not known_before)):
            change = True
            vc_change = bool(my_vc) and bool(vc) and my_vc != vc
            lot = mine
            vc = my_vc
        elif my_vc:
            # 同じロットの続きの行(ロット№が空でも)。最初の行にロット№が無い(停止だけ)ときも
            # 変わり目とは言わない ── 何から何へ変わったのか分からない
            vc = my_vc
        kinds.append(RowKind(lot=lot, change=change, vc_change=vc_change))
    ex_lots = {k.lot for k, values in zip(kinds, rows) if k.lot and is_ex(values)}
    for k, values in zip(kinds, rows):
        k.ex = (k.lot in ex_lots) if k.lot else is_ex(values)
    return kinds


def pack_count(values: dict[str, str]) -> int:
    """その行の包数(梱包単位 包数)。空・読めなければ 1。"""
    from .numeric import is_numeric, to_float

    tut = str(values.get("TUT", "") or "").strip()
    count = int(to_float(tut)) if tut and is_numeric(tut) else 1
    return max(count, 1)


@dataclass(frozen=True)
class StopKinds:
    """段取り・VC交換の停止の記号(v4.42.2。`stop_kinds`)。"""

    #: 段取りの停止(「段取り(板)」など)。その行の1梱包目に段取りのぶんを足さない
    setup: frozenset = frozenset()
    #: VC交換の停止(「VC交換(板)」など)。その行の1梱包目に VC交換のぶんを足さない
    vc: frozenset = frozenset()
    #: 見つけた停止の「記号 内訳」(設定・確かめの窓に出す)
    setup_labels: tuple = ()
    vc_labels: tuple = ()


def stop_kind(label: str) -> Optional[str]:
    """停止理由の内訳の名前から、段取り("setup")か VC交換("vc")か。どちらでもなければ None。

    内訳の名前は変わることがある(「段取り(板)」→「段取り」、「VC交換(板)」→「VC交換」)ので、
    決まった字が含まれるかで見る:

        段取り … 「段取」を含み、**「変更」を含まない**。段取り変更(あるロットの準備をやめて違うロットの
                  準備を始めた)は別の停止で、これでは長さを変えない
        VC交換 … 「VC」(全角の「ＶＣ」・小文字も)と「交換」を両方含む
    """
    import unicodedata

    text = unicodedata.normalize("NFKC", str(label or "")).upper()
    if "VC" in text and "交換" in text:
        return "vc"
    if "段取" in text and "変更" not in text:
        return "setup"
    return None


def stop_kinds(by_category: dict) -> StopKinds:
    """停止理由の一覧(`stop_master.load_all_categories`)から、段取り・VC交換の停止の記号。"""
    found: dict[str, list[tuple[str, str]]] = {"setup": [], "vc": []}
    for reasons in (by_category or {}).values():
        for reason in reasons:
            label = str(getattr(reason, "label", "") or "").strip()
            code = str(getattr(reason, "code", "") or "").strip()
            kind = stop_kind(label)
            if code and kind:
                found[kind].append((code, label))
    return StopKinds(setup=frozenset(_norm(c) for c, _ in found["setup"]),
                     vc=frozenset(_norm(c) for c, _ in found["vc"]),
                     setup_labels=tuple(f"{c} {l}" for c, l in found["setup"]),
                     vc_labels=tuple(f"{c} {l}" for c, l in found["vc"]))


def has_stop(values: dict[str, str], codes: frozenset) -> bool:
    """その行の停止①〜⑤に、その記号の停止があるか。"""
    return bool(codes) and any(_norm(values.get(code, "")) in codes for code, _ in STOP_SLOTS)


def adjust_weights(weights: Sequence[Decimal], rows: Sequence[dict[str, str]],
                   kinds: Sequence[RowKind], factors: Factors,
                   stops: StopKinds = StopKinds()) -> tuple[list[Decimal], list[str]]:
    """梱包数の重みに、EX・段取り・VC交換の倍率を掛ける。返すのは(重み, 確かめの窓に出す一言)。

    ``stops`` は段取り・VC交換の停止の記号(`stop_kinds`)。**段取りの停止がある行は段取りのぶんを、
    VC交換の停止がある行は VC交換のぶんを足さない**(その時間は停止の分として先に渡してある。v4.42.2)。
    """
    out: list[Decimal] = []
    ex_rows = changes = vc_changes = setup_skipped = vc_skipped = 0
    for weight, values, kind in zip(weights, rows, kinds):
        w = weight * (factors.ex if kind.ex else Decimal(1))
        extra = Decimal(0)
        if kind.change:
            if has_stop(values, stops.setup):
                setup_skipped += 1
            else:
                extra += factors.setup - 1
                changes += 1
        if kind.vc_change:
            if has_stop(values, stops.vc):
                vc_skipped += 1
            else:
                extra += factors.vc - 1
                vc_changes += 1
        if extra > 0:
            w += w / pack_count(values) * extra
        ex_rows += 1 if kind.ex and factors.ex != 1 else 0
        out.append(w)
    notes: list[str] = []
    if ex_rows:
        notes.append(f"EX のロットの {ex_rows} 行は {_times(factors.ex)}倍で数えます")
    if changes and factors.setup != 1:
        notes.append(f"ロットの変わり目 {changes} か所の1梱包目は段取りのぶん長め({_times(factors.setup)}倍)")
    if vc_changes and factors.vc != 1:
        notes.append(f"VC が前のロットと違う {vc_changes} か所の1梱包目は、VC交換のぶんさらに長め"
                     f"(+{_times(factors.vc - 1)}倍)")
    if setup_skipped:
        notes.append(f"段取りの停止({'・'.join(stops.setup_labels)})がある {setup_skipped} か所は、"
                     "段取りのぶんを足しません(停止の分として渡します)")
    if vc_skipped:
        notes.append(f"VC交換の停止({'・'.join(stops.vc_labels)})がある {vc_skipped} か所は、"
                     "VC交換のぶんを足しません(停止の分として渡します)")
    return out, notes


def _times(value: Decimal) -> str:
    return format(value.normalize(), "f")


def parse_hhmm(text: str) -> Optional[int]:
    """"07:00" → 420(0:00 からの分)。読めなければ None。読み方は直の境界と同じ(`shift.hhmm_minutes`)。"""
    return hhmm_minutes(text)


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
          weights: Optional[Sequence[Decimal]] = None, origin: int = 0) -> Plan:
    """`stops`(行ごとの停止の分)の行へ、`total` 分を割り振る。

    `rest` は休憩(直の始めから何分目, 長さ)。その長さを除いて割り振り、休憩にかかる行の
    終わりをのばし、後ろの行をずらす(`break_window`)。

    `weights` は行ごとの重み(梱包数。`pack_weights`)。停止の分を先に渡した残りを、この比で
    分けます。省けば全部 1 ── 均等も同じ道です。

    `origin` は始まりの時刻が刻みからどれだけずれているか(分。始まりが 9:37 なら 2)。区切りは
    **時計の5分刻み**(9:40・9:45 …)に寄せる(v4.43.0。LOTで時間を割り振るは前のロットの終わりから
    始まるので、始まりが5分刻みとは限らない)。直の始まりから割り振るときは 0。
    """
    if rest is not None:
        at, length = rest
        plan = split(stops, total - length, step=step, weights=weights, origin=origin)
        if not plan.ok:
            return plan
        moved: list[tuple[int, int]] = []
        for index, (a, b) in enumerate(plan.slots):
            # 休憩がちょうど始まりからなら、最初の行がまたぐ(ずらさずにのばす)
            if a >= at and a > 0:
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
    shift_by = Decimal(origin % step)

    def snap(value: Decimal) -> int:
        """始めからの分 → 時計の刻みに寄せた、始めからの分。"""
        return int(((value + shift_by) / unit).quantize(Decimal(1), rounding=ROUND_HALF_UP) * unit - shift_by)

    def ceil_step(value: Decimal) -> int:
        return int((value / unit).quantize(Decimal(1), rounding=ROUND_CEILING) * unit)

    def floor_step(value: Decimal) -> int:
        """始めからの分 → それ以下で、時計の刻みに当たる始めからの分。"""
        return int(((value + shift_by) / unit).quantize(Decimal(1), rounding=ROUND_FLOOR) * unit - shift_by)

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
