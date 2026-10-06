"""標準作業時間 ── 同じ条件・同じ班で「ふつうのペース」の作業時間を出す

    日報入力データで 用途コードと包装仕様NOとワークサイズと梱包数と
    作業人数と作業時間を使って標準作業時間を取りたい
    「その作業を、決められた条件で、通常のペースで行った場合に必要と
    なる時間」として設定 / 同サイズ 同用途 同包装仕様の時 / 各班ごとに集計

【何を「1つの条件」とするか】
ライン・班・用途コード・包装仕様NO・ワークサイズ(厚×幅×丈)の5つです。
班はラインに属する(班員名簿の「担当ライン」)ので、ラインも鍵に入ります。

【1行(1ロット)の指標】
作業時間は人数によって変わるので、まず**人・分**(作業時間 × 作業人数)に
してから、量で割ります:

    1梱包あたりの人・分 = 作業時間 × 作業人数 ÷ 梱包数
    1枚あたりの人・分   = 作業時間 × 作業人数 ÷ 枚数

使うときは逆に戻します: 標準作業時間(分) = 人・分/梱包 × 梱包数 ÷ 作業人数。
作業時間は日報の「作業時間」(開始〜終了から停止時間を引いたもの、
`logic/work_time.py`)なので、停止のぶんは最初から入っていません。

【標準 = 中央値】
「通常のペース」なので、飛び抜けて遅い日・速い日に引っ張られる平均では
なく**中央値**を標準にします。平均・最小・最大・件数も横に出すので、
散らばりは見えます。件数が `MIN_SAMPLES` 未満のものは**仮**の印を付けます。

【標準に数えない行】
用途コード・包装仕様NO・サイズのどれかが空 / 作業人数が0 / 作業時間が0 /
梱包数も枚数も0。**実績としては残します**(除外理由つき)── 数えなかった
ことが、あとから分かるように。

【班の決め方】
作業者欄の名前(空白区切り)を班員名簿で引き、**いちばん多い班**。
同数なら「混成」、名簿に無い名前だけなら「不明」。班ごとの行に加えて、
ライン全体の「全班」の行も作ります(班をまたいで比べる基準に)。

**ここは純粋な計算だけ**で、DBにもファイルにも触りません。読み書きは
`services/standard_time.py` です。
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field, replace
from statistics import median
from typing import Callable, Iterable, Mapping, Optional

from . import operator
from .line_names import label as line_label
from .shift import SHIFT_NAMES, parse_business_date

#: 条件の鍵(この順)
KEY_FIELDS: tuple[str, ...] = ("line", "team", "purpose_code", "packing_spec_no", "size")

#: 班の特別な値
TEAM_MIXED = "混成"
TEAM_UNKNOWN = "不明"
TEAM_ALL = "全班"

#: これより件数が少ない標準は「仮」
MIN_SAMPLES = 3

#: 作業者欄に付く注記(「山田(新人教育)」)。班を引くときは外す
_NAME_NOTE = re.compile(r"[（(].*?[）)]")
_SIZE_SEP = re.compile(r"[×xX＊*]")


def normalize_size(text: object) -> str:
    """ワークサイズを揃える。**書き方が違っても同じサイズは同じ条件に。**

        "8.000×1528.0×3053.0" / "8x1528x3053" / "8.0 × 1528 × 3053" → "8×1528×3053"
        "20.000×1528.0×ｺｲﾙ"                                         → "20×1528×ｺｲﾙ"

    数に読めるものは末尾の 0 を落とし、読めないもの(ｺｲﾙ)はそのまま。
    """
    raw = "" if text is None else str(text).strip()
    if not raw:
        return ""
    parts = []
    for part in _SIZE_SEP.split(raw):
        part = part.strip()
        try:
            value = float(part)
        except ValueError:
            parts.append(part)
            continue
        parts.append(f"{value:.3f}".rstrip("0").rstrip("."))
    return "×".join(parts)


def names_of(worker_text: object) -> list[str]:
    """作業者欄 → 名前の並び(注記「(新人教育)」は外す・重なりは1つに)。"""
    text = _NAME_NOTE.sub("", "" if worker_text is None else str(worker_text))
    out: list[str] = []
    for name in re.split(r"[\s・,、/]+", text):
        if name and name not in out:
            out.append(name)
    return out


def team_of(worker_text: object, teams: dict[str, str]) -> str:
    """作業者欄 → 班。名簿(`名前 → 班`)で引いて、いちばん多い班。

    同数なら「混成」、名簿に無い名前だけなら「不明」。
    """
    names = names_of(worker_text)
    counts = Counter(teams[n] for n in names if teams.get(n))
    if not counts:
        return TEAM_UNKNOWN
    top = counts.most_common()
    if len(top) > 1 and top[0][1] == top[1][1]:
        return TEAM_MIXED
    return top[0][0]


# ======================================================================
# 1行(1ロット)の実績
# ======================================================================
@dataclass(frozen=True)
class Sample:
    """ロット1行ぶんの実績。**標準に数えるかどうかも、ここで決まる。**"""

    report_date: str
    line: str
    shift: str
    page: int
    row_no: int
    team: str
    worker: str = ""
    lot_no: str = ""
    purpose_code: str = ""
    purpose_name: str = ""
    packing_spec_no: str = ""
    size: str = ""                 # 揃えたサイズ(条件の鍵)
    dimension: str = ""            # 日報に書かれたままのサイズ
    packages: float = 0.0          # 梱包数(包数)
    sheets: float = 0.0            # 枚数(実績)
    workers: float = 0.0           # 作業人数
    work_minutes: float = 0.0      # 作業時間(分・停止を引いたあと)
    stop_minutes: float = 0.0

    @property
    def person_minutes(self) -> float:
        return self.work_minutes * self.workers

    @property
    def per_package(self) -> Optional[float]:
        """1梱包あたりの人・分。梱包数が無ければ None。"""
        return self.person_minutes / self.packages if self.packages > 0 else None

    @property
    def per_sheet(self) -> Optional[float]:
        """1枚あたりの人・分。枚数が無ければ None。"""
        return self.person_minutes / self.sheets if self.sheets > 0 else None

    @property
    def excluded_reason(self) -> str:
        """標準に数えない理由。数えるなら空。**理由は1つ目だけ。**"""
        if not self.purpose_code:
            return "用途コードが空"
        if not self.packing_spec_no:
            return "包装仕様NOが空"
        if not self.size:
            return "サイズが空"
        if self.workers <= 0:
            return "作業人数が0"
        if self.work_minutes <= 0:
            return "作業時間が0"
        if self.packages <= 0 and self.sheets <= 0:
            return "梱包数も枚数も0"
        return ""

    @property
    def usable(self) -> bool:
        return not self.excluded_reason

    @property
    def key(self) -> tuple[str, ...]:
        return tuple(getattr(self, name) for name in KEY_FIELDS)


def sample_of(*, report_date: str, line: str, shift: str, page: int, row_no: int,
              worker: str, team: str, lot_no: str = "", purpose_code: str = "",
              purpose_name: str = "", packing_spec_no: str = "", dimension: str = "",
              packages: float = 0.0, sheets: float = 0.0, workers: float = 0.0,
              work_minutes: float = 0.0, stop_minutes: float = 0.0) -> Sample:
    """集計の明細1行から実績を作る。サイズはここで揃える。"""
    return Sample(
        report_date=report_date, line=line, shift=shift, page=page, row_no=row_no,
        team=team, worker=worker, lot_no=lot_no,
        purpose_code=(purpose_code or "").strip(),
        purpose_name=(purpose_name or "").strip(),
        packing_spec_no=(packing_spec_no or "").strip(),
        size=normalize_size(dimension), dimension=(dimension or "").strip(),
        packages=float(packages or 0), sheets=float(sheets or 0),
        workers=float(workers or 0), work_minutes=float(work_minutes or 0),
        stop_minutes=float(stop_minutes or 0))


# ======================================================================
# 標準(条件 × 班)
# ======================================================================
@dataclass(frozen=True)
class Stat:
    """1つの指標の散らばり。中央値が標準。"""

    count: int = 0
    median: Optional[float] = None
    mean: Optional[float] = None
    low: Optional[float] = None
    high: Optional[float] = None

    @classmethod
    def of(cls, values: Iterable[Optional[float]]) -> "Stat":
        found = [v for v in values if v is not None]
        if not found:
            return cls()
        return cls(count=len(found), median=float(median(found)),
                   mean=sum(found) / len(found), low=min(found), high=max(found))


@dataclass(frozen=True)
class Standard:
    line: str
    team: str
    purpose_code: str
    packing_spec_no: str
    size: str
    count: int                      # 標準に数えた行数
    per_package: Stat = field(default_factory=Stat)
    per_sheet: Stat = field(default_factory=Stat)
    purpose_name: str = ""

    @property
    def provisional(self) -> bool:
        """件数が少なく、まだ「仮」か。"""
        return self.count < MIN_SAMPLES

    @property
    def key(self) -> tuple[str, ...]:
        return (self.line, self.team, self.purpose_code, self.packing_spec_no, self.size)

    def minutes_for(self, packages: float, workers: float) -> Optional[float]:
        """この条件を `packages` 梱包・`workers` 人でやると何分か。"""
        return estimate_minutes(self.per_package.median, packages, workers)

    def minutes_for_sample(self, sample: "Sample") -> Optional[float]:
        """その作業(梱包数・枚数・人数)を標準でやると何分か。

        梱包数があれば梱包あたりの標準で、無ければ枚あたりの標準で出します。
        """
        found = estimate_minutes(self.per_package.median, sample.packages, sample.workers)
        if found is None:
            found = estimate_minutes(self.per_sheet.median, sample.sheets, sample.workers)
        return found


def estimate_minutes(per_package: Optional[float], packages: float,
                     workers: float) -> Optional[float]:
    """標準作業時間(分) = 人・分/梱包 × 梱包数 ÷ 作業人数。出せなければ None。"""
    if per_package is None or packages <= 0 or workers <= 0:
        return None
    return per_package * packages / workers


def standards(samples: Iterable[Sample]) -> list[Standard]:
    """実績から、条件 × 班ごとの標準を出す。**ライン全体(全班)の行も付ける。**

    並びは ライン → 用途コード → 包装仕様NO → サイズ → 班(全班が先)。
    """
    groups: dict[tuple[str, ...], list[Sample]] = {}
    for s in samples:
        if not s.usable:
            continue
        groups.setdefault(s.key, []).append(s)
        all_key = (s.line, TEAM_ALL, s.purpose_code, s.packing_spec_no, s.size)
        groups.setdefault(all_key, []).append(s)

    out = []
    for key, rows in groups.items():
        line, team, purpose, spec, size = key
        names = Counter(r.purpose_name for r in rows if r.purpose_name)
        out.append(Standard(
            line=line, team=team, purpose_code=purpose, packing_spec_no=spec,
            size=size, count=len(rows),
            per_package=Stat.of(r.per_package for r in rows),
            per_sheet=Stat.of(r.per_sheet for r in rows),
            purpose_name=names.most_common(1)[0][0] if names else ""))
    out.sort(key=lambda st: (st.line, st.purpose_code, st.packing_spec_no,
                             _size_sort_key(st.size), st.team != TEAM_ALL, st.team))
    return out


def worker_standards(samples: Iterable[Sample]) -> list[Standard]:
    """条件 × **作業者**ごとの標準(v4.12.4、「作業者名で見る」のとき)。

    作業者欄は直ごとに全員の名前なので、1つの作業を**その場に居た全員に**
    数えます(梱包力の作業者ごとと同じ)。`Standard.team` に作業者の名前が
    入ります。全班の行は付けません(班ごとの面にあるので)。
    """
    expanded = [replace(s, team=name) for s in samples if s.usable
                for name in names_of(s.worker)]
    return [st for st in standards(expanded) if st.team != TEAM_ALL]


def operator_label(s: Sample, kinds: Mapping[str, str]) -> str:
    """その作業のオペレーター構成「AOP1・BOP2」(作業者欄の名前を名簿の区分で数える)。"""
    return operator.composition(names_of(s.worker), kinds)


def operator_standards(samples: Iterable[Sample], kinds: Mapping[str, str]) -> list[Standard]:
    """条件 × **オペレーター構成**ごとの標準(v4.15.0)。

    `Standard.team` に構成(「AOP1・BOP2」)が入ります。構成は**いまの名簿**で数える
    ので、名簿を直せば過去の作業にも効きます。全班の行は付けません(班ごとの面にある)。
    並びは条件 → 構成(人数の少ない順 → 検査できる人の多い順)。
    """
    expanded = [replace(s, team=operator_label(s, kinds)) for s in samples if s.usable]
    out = [st for st in standards(expanded) if st.team != TEAM_ALL]
    out.sort(key=lambda st: (st.line, st.purpose_code, st.packing_spec_no,
                             _size_sort_key(st.size), operator.sort_key(st.team)))
    return out


def _size_sort_key(size: str) -> tuple:
    """厚→幅→丈の数の順(文字の順だと 10 が 8 の前に来る)。"""
    parts = []
    for part in size.split("×"):
        try:
            parts.append((0, float(part), ""))
        except ValueError:
            parts.append((1, 0.0, part))
    return tuple(parts)


# ======================================================================
# 探す条件 ── **AND / OR を選べる**
# ======================================================================
#     同条件の作業はAndで探せるのですよね？ or条件に見えるのですが
#     (And or両方できるべきですが)
#
# 欄は3つ(用途コード・包装仕様NO・サイズ)。
#
#     欄と欄のあいだ   AND(すべて満たす)か OR(どれか満たす)を選ぶ
#     欄の中           「,」「、」で区切ると、そのどれか(いつも OR)
#     空の欄           問わない(AND でも OR でも数に入れない)
#
# ライン・班・期間は**いつも絞り込み**(AND)です ── 「L-1 か H176」の
# ようにラインを OR に混ぜると、何を探しているのか読めなくなるので。
JOIN_AND = "and"
JOIN_OR = "or"
JOIN_LABELS = {JOIN_AND: "すべて一致(AND)", JOIN_OR: "どれか一致(OR)"}

_VALUE_SEP = re.compile(r"[,、，;；]+")

#: 欄 (鍵, 見出し)
CRITERIA_FIELDS: tuple[tuple[str, str], ...] = (
    ("purpose_code", "用途コード"), ("packing_spec_no", "包装仕様NO"), ("size", "サイズ"))


def split_values(text: object, *, size: bool = False) -> tuple[str, ...]:
    """欄の中の値を分ける。サイズは書き方を揃える。重なりは1つに(並びは残す)。"""
    raw = "" if text is None else str(text)
    out: list[str] = []
    for part in _VALUE_SEP.split(raw):
        part = normalize_size(part) if size else " ".join(part.split())
        if part and part not in out:
            out.append(part)
    return tuple(out)


@dataclass(frozen=True)
class Criteria:
    purpose_codes: tuple[str, ...] = ()
    spec_nos: tuple[str, ...] = ()
    sizes: tuple[str, ...] = ()
    join: str = JOIN_AND

    @classmethod
    def parse(cls, purpose_code: object = "", packing_spec_no: object = "",
              size: object = "", join: object = JOIN_AND) -> "Criteria":
        return cls(purpose_codes=split_values(purpose_code),
                   spec_nos=split_values(packing_spec_no),
                   sizes=split_values(size, size=True),
                   join=JOIN_OR if str(join or "").lower() == JOIN_OR else JOIN_AND)

    @property
    def fields(self) -> list[tuple[str, str, tuple[str, ...]]]:
        """入っている欄だけ `(鍵, 見出し, 値)`。"""
        values = {"purpose_code": self.purpose_codes, "packing_spec_no": self.spec_nos,
                  "size": self.sizes}
        return [(key, label, values[key]) for key, label in CRITERIA_FIELDS if values[key]]

    @property
    def empty(self) -> bool:
        return not self.fields

    def matches(self, purpose_code: str, packing_spec_no: str, size: str, *,
                exact: bool = True) -> bool:
        """その条件に当たるか。`exact=False` なら部分一致(大文字小文字を問わない)。"""
        have = {"purpose_code": purpose_code, "packing_spec_no": packing_spec_no,
                "size": size}
        hits = [_hit(have[key] or "", values, exact) for key, _, values in self.fields]
        if not hits:
            return True
        return all(hits) if self.join == JOIN_AND else any(hits)

    def describe(self) -> str:
        """「用途コード H176 かつ 包装仕様NO 1P0001・1P0002 のどれか」のように。"""
        word = " かつ " if self.join == JOIN_AND else " または "
        parts = []
        for _, label, values in self.fields:
            parts.append(f"{label} {values[0]}" if len(values) == 1
                          else f"{label} {'・'.join(values)} のどれか")
        return word.join(parts)


def _hit(value: str, wanted: tuple[str, ...], exact: bool) -> bool:
    if exact:
        return value.upper() in {w.upper() for w in wanted}
    return any(w.upper() in value.upper() for w in wanted)


# ======================================================================
# 計算式 ── **数を入れた式**を出す
# ======================================================================
#     計算式も出せるようにしておいて下さい
#
# 表とCSVに「式」の列として出します。答えは表の数と**同じ計算**から
# 出すので食い違いません(`tests/test_standard_time.py` が縛ります)。
def _n(value: float) -> str:
    """式の中の入力値。整数は整数のまま、端数は小数1桁まで。"""
    return f"{value:g}" if float(value).is_integer() else f"{value:.1f}"


def per_package_formula(s: Sample) -> str:
    """「60分 × 2人 ÷ 4梱包 = 30.0 人分/梱包」"""
    if s.per_package is None:
        return "梱包数が0なので出せません"
    return (f"{_n(s.work_minutes)}分 × {_n(s.workers)}人 ÷ {_n(s.packages)}梱包"
            f" = {s.per_package:.1f} 人分/梱包")


def per_sheet_formula(s: Sample) -> str:
    """「60分 × 2人 ÷ 40枚 = 3.0 人分/枚」"""
    if s.per_sheet is None:
        return "枚数が0なので出せません"
    return (f"{_n(s.work_minutes)}分 × {_n(s.workers)}人 ÷ {_n(s.sheets)}枚"
            f" = {s.per_sheet:.1f} 人分/枚")


def standard_minutes_formula(st: Optional["Standard"], s: Sample) -> str:
    """「標準 35.0人分/梱包 × 4梱包 ÷ 2人 = 70.0分」(梱包数が無ければ枚あたり)。"""
    if st is None:
        return "比べる標準がありません"
    if not s.usable:
        return f"標準に数えない作業です({s.excluded_reason})"
    if estimate_minutes(st.per_package.median, s.packages, s.workers) is not None:
        value = st.per_package.median
        return (f"標準 {value:.1f}人分/梱包 × {_n(s.packages)}梱包 ÷ {_n(s.workers)}人"
                f" = {value * s.packages / s.workers:.1f}分")
    if estimate_minutes(st.per_sheet.median, s.sheets, s.workers) is not None:
        value = st.per_sheet.median
        return (f"標準 {value:.1f}人分/枚 × {_n(s.sheets)}枚 ÷ {_n(s.workers)}人"
                f" = {value * s.sheets / s.workers:.1f}分(梱包数が無いので枚あたり)")
    return "標準の値が無いので出せません"


def diff_formula(s: Sample, standard_minutes: Optional[float]) -> str:
    """「60分 − 70.0分 = -10.0分」"""
    if standard_minutes is None:
        return ""
    return (f"{_n(s.work_minutes)}分 − {standard_minutes:.1f}分"
            f" = {s.work_minutes - standard_minutes:+.1f}分")


def median_formula(values: Iterable[Optional[float]], unit: str) -> str:
    """中央値の出し方。**真ん中の値まで書く**(全部は並べない ── 何十件にもなる)。

        「5件を小さい順に並べた3件目 = 35.0 人分/梱包」
        「4件を小さい順に並べた2件目と3件目の平均 = (30.0 + 35.0) ÷ 2 = 32.5 人分/梱包」
    """
    found = sorted(v for v in values if v is not None)
    if not found:
        return ""
    n = len(found)
    if n % 2:
        mid = n // 2
        return f"{n}件を小さい順に並べた{mid + 1}件目 = {found[mid]:.1f} {unit}"
    lo, hi = found[n // 2 - 1], found[n // 2]
    return (f"{n}件を小さい順に並べた{n // 2}件目と{n // 2 + 1}件目の平均"
            f" = ({lo:.1f} + {hi:.1f}) ÷ 2 = {(lo + hi) / 2:.1f} {unit}")


def mean_formula(values: Iterable[Optional[float]], unit: str) -> str:
    """「合計 165.0 ÷ 5件 = 33.0 人分/梱包」"""
    found = [v for v in values if v is not None]
    if not found:
        return ""
    total = sum(found)
    return f"合計 {total:.1f} ÷ {len(found)}件 = {total / len(found):.1f} {unit}"


def estimate_formula(per_package: Optional[float], packages: float, workers: float) -> str:
    """「標準 35.0人分/梱包 × 10梱包 ÷ 2人 = 175.0分」"""
    found = estimate_minutes(per_package, packages, workers)
    if found is None:
        return ""
    return (f"標準 {per_package:.1f}人分/梱包 × {_n(packages)}梱包 ÷ {_n(workers)}人"
            f" = {found:.1f}分")


# ======================================================================
# 梱包力 (v4.10.0) ── ゲームの DPS(ダメージ毎秒)のように
# ======================================================================
#     ゲームでdpsっていう概念があるが(だーめじぱーせかんど)
#     同じように梱包力みたいな概念を作りたい
#     標準梱包時間を集計する機能はすでにあるはずなので、そこと連携させてください
#
# DPS は「1秒あたりに与えたダメージ」です。そのまま「1時間あたりの梱包数」に
# すると、大きい板・難しい包装仕様をやった人ほど低く出ます(硬い敵を殴って
# いるのに DPS が低い、と同じ)。そこで**ダメージの代わりに標準作業時間**を
# 使います ── 標準は「その条件の仕事が、ふつうのペースで何人分かかるか」
# なので、難しい仕事ほど大きいダメージとして数えられます。
#
#     1作業の標準人分 = 標準(人分/梱包) × 梱包数      (梱包数が無ければ 人分/枚 × 枚数)
#     1作業の実人分   = 作業時間 × 作業人数
#     梱包力          = 標準人分の合計 ÷ 実人分の合計 × 100
#
# **100 が標準のペース**(全班の中央値)。120 なら標準の1.2倍の速さ ── 1時間
# 働いて、標準なら1時間12分かかる仕事を済ませた、ということです。
#
# 比べる標準は**全班**のもの(同じライン・用途コード・包装仕様NO・サイズ)。
# 班ごとの標準と比べると、どの班も自分の中央値と比べて 100 前後になって
# しまい、班どうしを比べられません。標準がまだ無い条件と、**「仮」の標準
# (件数 3 未満)**の作業は数えません ── 1件だけの標準はその作業そのもの
# なので、比べると必ず 100 になり、全体を 100 へ引っぱるだけです。
# 数えなかったぶんは「比べた作業 ◯/◯件」に出ます。
POWER_BASE = 100.0

#: まとめ方 (鍵, 見出し)
POWER_GROUPS: tuple[tuple[str, str], ...] = (
    ("team", "班"), ("operator", "オペレーター構成"), ("worker", "作業者"), ("shift", "直"),
    ("date", "日"), ("line", "ライン"))
#: 時間の並びで出すまとめ方(ほかは梱包力の高い順 ── DPS メーターのように)
POWER_TIMELINE = ("shift", "date")


def standard_person_minutes(st: Optional[Standard], s: Sample) -> Optional[float]:
    """その作業を標準のペースでやると何人分か。出せなければ None。

    標準が無い・**「仮」(件数 3 未満)**・数えない作業 のときは出しません。
    """
    if st is None or st.provisional or not s.usable:
        return None
    if st.per_package.median is not None and s.packages > 0:
        return st.per_package.median * s.packages
    if st.per_sheet.median is not None and s.sheets > 0:
        return st.per_sheet.median * s.sheets
    return None


@dataclass
class Power:
    """まとめ1つぶんの梱包力。"""

    label: str
    works: int = 0                 # 対象の作業(標準に数えられる行)
    compared: int = 0              # そのうち標準と比べられた行
    standard_pm: float = 0.0       # 標準人分の合計
    actual_pm: float = 0.0         # 実人分の合計(比べられた行だけ)
    packages: float = 0.0          # 梱包数の合計(比べられた行だけ)
    order: tuple = ()

    @property
    def power(self) -> Optional[float]:
        """梱包力。比べられた作業が無ければ None。"""
        if not self.compared or self.actual_pm <= 0:
            return None
        return self.standard_pm / self.actual_pm * POWER_BASE

    @property
    def packages_per_person_hour(self) -> Optional[float]:
        """1人1時間あたりの梱包数(難しさで均さない、素の速さ)。"""
        if self.actual_pm <= 0:
            return None
        return self.packages / (self.actual_pm / 60)

    @property
    def diff_pm(self) -> float:
        """実人分 − 標準人分(+ は標準より多くかかった)。"""
        return self.actual_pm - self.standard_pm

    def add(self, s: Sample, standard_pm: Optional[float]) -> None:
        self.works += 1
        if standard_pm is None:
            return
        self.compared += 1
        self.standard_pm += standard_pm
        self.actual_pm += s.person_minutes
        self.packages += s.packages


def _power_labels(s: Sample, group: str,
                  kinds: Optional[Mapping[str, str]] = None) -> list[tuple[str, tuple]]:
    """その作業がどのまとめに入るか (見出し, 並びの鍵)。作業者は名前ぶん。"""
    if group == "operator":
        label = operator_label(s, kinds or {})
        return [(label, operator.sort_key(label))]
    if group == "worker":
        return [(name, (name,)) for name in names_of(s.worker)] or [(TEAM_UNKNOWN, ("~",))]
    if group in POWER_TIMELINE:
        day = parse_business_date(s.report_date)
        when = day.isoformat() if day else s.report_date
        if group == "date":
            return [(s.report_date, (when,))]
        shift = SHIFT_NAMES.index(s.shift) if s.shift in SHIFT_NAMES else len(SHIFT_NAMES)
        return [(f"{s.report_date} {s.shift}", (when, shift, s.shift))]
    if group == "line":
        # 見出しも並びの鍵も正規の呼び名(v4.12.5 / v4.13.0)
        return [(line_label(s.line), (s.line,))]
    return [(s.team or TEAM_UNKNOWN, (s.team,))]


def packing_power(samples: Iterable[Sample],
                  standard_for: Callable[[Sample], Optional[Standard]],
                  group: str = "team",
                  kinds: Optional[Mapping[str, str]] = None) -> tuple[Power, list[Power]]:
    """全体と、まとめごとの梱包力。

    作業者でまとめるときは、1つの作業を**その場に居た全員に**数えます
    (作業者欄は直ごとに全員の名前が入っていて、行ごとに誰がやったかは
    分からないため)。全体には1回だけ数えます。

    並びは、班・作業者・ライン・オペレーター構成なら梱包力の高い順(DPS メーターの
    ように)、直・日なら時間の順(推移を見るもの)。オペレーター構成は `kinds`
    (名前 → AOP / ABOP / BOP)で数えます(v4.15.0)。
    """
    if group not in dict(POWER_GROUPS):
        group = "team"
    total = Power("全体")
    found: dict[str, Power] = {}
    for s in samples:
        if not s.usable:
            continue
        spm = standard_person_minutes(standard_for(s), s)
        total.add(s, spm)
        for label, order in _power_labels(s, group, kinds):
            found.setdefault(label, Power(label, order=order)).add(s, spm)
    rows = list(found.values())
    if group in POWER_TIMELINE:
        rows.sort(key=lambda p: p.order)
    else:
        rows.sort(key=lambda p: (p.power is None, -(p.power or 0), p.label))
    return total, rows


def power_formula(p: Power) -> str:
    """「標準 360人分 ÷ 実際 300人分 × 100 = 120」"""
    if p.power is None:
        return "標準と比べられる作業がありません"
    return (f"標準 {p.standard_pm:.0f}人分 ÷ 実際 {p.actual_pm:.0f}人分 × 100"
            f" = {p.power:.0f}")


def power_meaning(power: Optional[float]) -> str:
    """「標準の1.20倍の速さ」/「標準どおり」/「標準の0.85倍(遅め)」"""
    if power is None:
        return ""
    ratio = power / POWER_BASE
    if abs(ratio - 1) < 0.005:
        return "標準どおりの速さ"
    return f"標準の{ratio:.2f}倍の速さ" if ratio > 1 else f"標準の{ratio:.2f}倍(標準より遅め)"
