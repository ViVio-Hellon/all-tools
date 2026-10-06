"""作業時間の計算 (VBA ``時間計算`` / ``GetShiftLimitMin`` / ``CalcShiftMinutes``)

【なぜフォームを経由していたのか】
VBA はシートへ直接打たせず、わざわざフォームを挟んでいました。理由は
**入力した時点で計算と変換を済ませてから出力するため**です。打った値が
そのままシートへ行くのではなく、

    時は 00〜23 / 分は 00〜59 か  →  開始と終了から作業時間  →
    停止時間があれば引く          →  それから出力

という順で通してから初めて外へ出ます。Web版でも同じ順にします ──
順が崩れると、範囲外の時刻がそのまま保存され、集計だけが合わなく
なります。

【断る場面が4つある】
VBA は次のどれかで **その場で止めて、何も計算しませんでした**
(`Exit Sub`)。ここも同じで、1つでも見つかったら計算結果を返しません。

    ・時が 0〜23 でない / 分が 0〜59 でない / 数字でない
    ・作業時間が直の規定時間を超えている(`GetShiftLimitMin`)
    ・開始と終了が同じ(0分)
    ・停止時間を引いたら**マイナス**になった

最後のマイナスだけは音も鳴ります(`logic/sound.KEY_NEGATIVE_TIME`)。

【日をまたぐ】
3直は夜から翌朝へまたぎます。終了が開始より小さければ +1日
(`If D3 < 0 Then D3 = DateDiff("n", D2, D1 + 1)`)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .numeric import is_numeric, to_float
from .shift import normalize_hhmm

#: 時・分の範囲。VBA の `0 <= val() And 23 >= val()` と同じ
HOUR_MAX = 23
MINUTE_MAX = 59
MINUTES_PER_DAY = 24 * 60

#: 断りの種類。**文言から推し量らない**
REFUSE_RANGE = "time_range"
REFUSE_OVER_SHIFT = "over_shift"
REFUSE_SAME_TIME = "same_time"
REFUSE_NEGATIVE = "negative_time"

#: 欄の見出し。断りの文言に出す(VBA のメッセージと同じ言い回し)
_FIELD_LABELS = {
    "KZ": "開始時間（時）", "KH": "開始時間（分）",
    "SZ": "終了時間（時）", "SH": "終了時間（分）",
}


@dataclass(frozen=True)
class TimeProblem:
    """断る理由。**行番号まで出す** ── 12行のどれが悪いのか分からないと直せない。"""

    row: int
    reason: str
    message: str
    field: str = ""

    #: マイナスのときだけ音を鳴らす(VBA `音楽を流す("作業時間がマイナスです.wav")`)
    @property
    def sounds(self) -> bool:
        return self.reason == REFUSE_NEGATIVE


@dataclass
class TimeResult:
    """計算の結果。**問題があれば `times` は空**(VBA は Exit Sub で何も書かない)。"""

    times: dict[int, str] = field(default_factory=dict)
    problem: Optional[TimeProblem] = None

    @property
    def ok(self) -> bool:
        return self.problem is None


def _range_problem(row: int, family: str, text: str) -> Optional[TimeProblem]:
    """1つの欄の範囲チェック。通れば None。"""
    if text == "":
        return None
    label = _FIELD_LABELS[family]
    limit = HOUR_MAX if family in ("KZ", "SZ") else MINUTE_MAX
    if not is_numeric(text):
        return TimeProblem(row, REFUSE_RANGE,
                           f"{row}行目：{label}の入力が正しくありません。", family)
    value = to_float(text)
    if not 0 <= value <= limit:
        # 分だけは範囲を書き添える(VBA も分のときだけ書いていた)
        tail = "（0～59で入力してください）" if limit == MINUTE_MAX else ""
        return TimeProblem(row, REFUSE_RANGE,
                           f"{row}行目：{label}の入力が正しくありません。{tail}", family)
    return None


def check_ranges(rows: dict[int, dict[str, str]]) -> Optional[TimeProblem]:
    """全行の時・分が範囲に収まっているか(VBA の1つめのループ)。

    **1つでも見つかったらそこで止めます。** 12行ぶんまとめて出すと、
    どれから直せばよいのか分からなくなります。
    """
    for row in sorted(rows):
        values = rows[row]
        for family in ("KZ", "KH", "SZ", "SH"):
            problem = _range_problem(row, family, (values.get(family) or "").strip())
            if problem is not None:
                return problem
    return None


def elapsed_minutes(kz: str, kh: str, sz: str, sh: str) -> int:
    """開始から終了までの分。日をまたぐなら +1日。

    呼ぶ前に `check_ranges` を通してください ── ここは数字であることを
    前提にします。
    """
    start = int(to_float(kz)) * 60 + int(to_float(kh))
    end = int(to_float(sz)) * 60 + int(to_float(sh))
    minutes = end - start
    if minutes < 0:                       # 3直は夜から翌朝へまたぐ
        minutes += MINUTES_PER_DAY
    return minutes


def shift_minutes(start: str, end: str) -> int:
    """直の長さ(分)。VBA ``CalcShiftMinutes``。

    ``"07:00"`` のような文字列を受けます。空なら 0(=上限を見ない)。
    時:分として読めないとき(「7時」「25:00」)も 0 です ── 読み方は
    直の境界と同じ `shift.normalize_hhmm` で決めます。
    """
    start, end = normalize_hhmm(start), normalize_hhmm(end)
    if not start or not end:
        return 0
    s_h, s_m = (int(p) for p in start.split(":"))
    e_h, e_m = (int(p) for p in end.split(":"))
    minutes = (e_h * 60 + e_m) - (s_h * 60 + s_m)
    if minutes <= 0:
        minutes += MINUTES_PER_DAY        # 日付またぎ対応
    return minutes


def shift_limit(shift_times: dict[str, tuple[str, str]], current: str,
                *, admin: bool = False) -> int:
    """作業時間の上限(分)。VBA ``GetShiftLimitMin``。

    **管理者モード中は全直の最大を使います。** 他の直のデータを開いて
    精査することがあるので、いまの直の長さで縛ると直せなくなります。

    上限が取れないとき(時間マスタ未取り込み等)は 0 を返し、
    呼び手は上限チェックを飛ばします ── **上限が分からないことを理由に
    入力を断らない**。
    """
    lengths = {key: shift_minutes(*pair) for key, pair in shift_times.items()}
    numbered = [v for k, v in lengths.items() if k in ("1", "2", "3") and v > 0]
    if admin:
        return max(numbered) if numbered else 0

    # "1直" / "1" / "日勤" のどれで来ても拾えるようにする
    for key, value in lengths.items():
        if key and (key == current or key in current):
            return value
    if "日勤" in current or "昼" in current:
        return lengths.get("昼", 0)
    return 0


def compute(rows: dict[int, dict[str, str]], *,
            limit_minutes: int = 0) -> TimeResult:
    """作業時間を出す (VBA ``時間計算`` そのもの)。

    順は VBA と同じです:

        1. 全行の時・分の範囲を見る(1つでも駄目なら何も計算しない)
        2. 開始・終了が4つとも埋まっている行だけ、差分を出す
        3. 直の規定時間を超えていないか / 同時刻でないかを見る
        4. 停止時間①②③を引く。マイナスなら断る

    `limit_minutes` が 0 のときは上限チェックを飛ばします。
    """
    problem = check_ranges(rows)
    if problem is not None:
        return TimeResult(problem=problem)

    times: dict[int, str] = {}
    for row in sorted(rows):
        values = rows[row]
        kz = (values.get("KZ") or "").strip()
        kh = (values.get("KH") or "").strip()
        sz = (values.get("SZ") or "").strip()
        sh = (values.get("SH") or "").strip()
        # **4つとも埋まっている行だけ。** 途中まで打っている最中に
        # 計算すると、打ち終わる前に断ることになる
        if not (kz and kh and sz and sh):
            continue

        minutes = elapsed_minutes(kz, kh, sz, sh)
        if limit_minutes and minutes > limit_minutes:
            return TimeResult(problem=TimeProblem(
                row, REFUSE_OVER_SHIFT,
                f"{row}行目の時間入力が正しくありません。"
                f"作業時間が直の規定時間（{limit_minutes}分）を超えています。"))
        if minutes == 0:
            return TimeResult(problem=TimeProblem(
                row, REFUSE_SAME_TIME,
                f"{row}行目の時間入力が正しくありません。"
                "開始時間と終了時間が同じになっています。"))
        times[row] = minutes

    # 作業停止分を引く。**停止だけ入っている行も通る**(VBA と同じ)ので、
    # 作業時間が無ければ 0 から引いてマイナスになる ── それも断りの対象
    for row in sorted(rows):
        values = rows[row]
        stops = [(values.get(f) or "").strip() for f in ("TH", "THS", "THT")]
        if not any(stops):
            continue
        minutes = times.get(row, 0)
        for text in stops:
            if is_numeric(text):
                minutes -= int(to_float(text))
        if minutes < 0:
            return TimeResult(problem=TimeProblem(
                row, REFUSE_NEGATIVE,
                f"{row}行目：作業時間がマイナスです。"
                "停止時間が作業時間を超えていないか確かめてください。"))
        times[row] = minutes

    return TimeResult(times={r: str(m) for r, m in times.items()})


def problems(rows: dict[int, dict[str, str]], *,
             limit_minutes: int = 0) -> list[TimeProblem]:
    """**行ごとの断りを、全部**。`compute` と違って途中で止まりません。

    `compute` は VBA `時間計算` の写しなので、1つ見つけたところで
    `Exit Sub` します ── 計算の順を変えないためにそのままにしてあります。
    こちらは**印を付けるため**と**保存を止めるため**のもので、12行の
    どこが直っていないかを1度に出します。

    分けてあるのは、混ぜると片方の都合でもう片方が変わるからです ──
    「全部出す」を `compute` に入れると、2行目で断られたのに3行目の
    作業時間が計算されてしまいます。

    並びは行の順です。**直す人は上から順に見る**ので、見つけた順や
    重さの順には並べ替えません。
    """
    found: list[TimeProblem] = []
    for row in sorted(rows):
        values = rows[row]

        # 1. 時・分の範囲。**その行の最初の1つだけ**(同じ行に4つ出ても
        #    直すのは1か所ずつなので、並べても読めない)
        bad_range = None
        for family in ("KZ", "KH", "SZ", "SH"):
            bad_range = _range_problem(row, family,
                                       (values.get(family) or "").strip())
            if bad_range is not None:
                break
        if bad_range is not None:
            found.append(bad_range)
            continue                      # 範囲が変なら時間は出せない

        kz = (values.get("KZ") or "").strip()
        kh = (values.get("KH") or "").strip()
        sz = (values.get("SZ") or "").strip()
        sh = (values.get("SH") or "").strip()
        stops = [(values.get(f) or "").strip() for f in ("TH", "THS", "THT")]

        minutes = 0
        if kz and kh and sz and sh:
            minutes = elapsed_minutes(kz, kh, sz, sh)
            if limit_minutes and minutes > limit_minutes:
                found.append(TimeProblem(
                    row, REFUSE_OVER_SHIFT,
                    f"{row}行目の時間入力が正しくありません。"
                    f"作業時間が直の規定時間（{limit_minutes}分）を超えています。"))
                continue
            if minutes == 0:
                found.append(TimeProblem(
                    row, REFUSE_SAME_TIME,
                    f"{row}行目の時間入力が正しくありません。"
                    "開始時間と終了時間が同じになっています。"))
                continue

        if any(stops):
            for text in stops:
                if is_numeric(text):
                    minutes -= int(to_float(text))
            if minutes < 0:
                found.append(TimeProblem(
                    row, REFUSE_NEGATIVE,
                    f"{row}行目：作業時間がマイナスです。"
                    "停止時間が作業時間を超えていないか確かめてください。"))
    return found


# ======================================================================
# いまの時刻を入れる(開始・終了の時/分をダブルクリック)
#
#     入力画面の開始終了の時間テキストボックスをダブルクリックすると
#     そのタイミングの時間を入力するようにしてください 時間 分 両方入れて
#     ください またそれに伴い その分の単位の1の位だけを四捨五入
#     1,2,3,4は切り捨て 5はそのまま 6,7,8,9は切り上げ
#
# 「四捨五入」と呼んでいますが、**5はそのまま残す**ので、実際は「分の
# 1の位を 0 か 5 に寄せる」です(13:03 → 13:00 / 13:05 → 13:05 /
# 13:07 → 13:10)。切り上げで60分になれば時を1つ進めます(13:57 → 14:00、
# 23:58 → 00:00 ── 3直は日をまたぐので、0時に戻るのが正しい)。
# ======================================================================
#: ダブルクリックで埋める欄の組。**時と分の2つを一度に**
STAMP_FIELDS: dict[str, tuple[str, str]] = {
    "start": ("KZ", "KH"),
    "end": ("SZ", "SH"),
}


def round_minute(minute: int) -> int:
    """分の1の位だけを丸める。1〜4は切り捨て・5はそのまま・6〜9は切り上げ。

    戻りは 0〜60(60 のときは時を1つ進めるのは呼ぶ側 ── `stamp`)。
    """
    ones = minute % 10
    tens = minute - ones
    if ones in (0, 5):
        return minute
    if ones < 5:
        return tens
    return tens + 10


def stamp(now) -> tuple[str, str]:
    """いまの時刻を (時, 分) の2桁の文字にする(分は `round_minute`)。"""
    minute = round_minute(now.minute)
    hour = now.hour
    if minute >= 60:
        minute -= 60
        hour = (hour + 1) % 24
    return f"{hour:02d}", f"{minute:02d}"
