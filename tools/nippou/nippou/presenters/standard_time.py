"""標準作業時間の画面 (`services/standard_time`)

    同サイズ 同用途 同包装仕様の時 / 各班ごとに集計する
    標準作業時間は別タブ管理とし csv出力 同条件作業抽出 作業時間を表示

【画面】レールの「標準作業時間」(`/standard-time`)。面は5つで、並びは
`TABS`(ここで決める):

    標準(条件×班)      条件ごとの標準。行の「抽出」で同条件の作業へ
    同条件の作業        その条件の作業を1件ずつ、作業時間と標準との差つきで
    梱包力              標準を 100 とした速さ(ゲームの DPS のように)。班・作業者・直・日・ライン
    考え方と計算        標準作業時間と梱包力の考え方・計算のしかた・根拠(読むだけの面)
    過去ぶんを入れる    この端末の集計から、期間を指定して入れる

【表】条件(ライン・班・用途コード・包装仕様NO・サイズ)ごとに1行。
ライン全体の「全班」の行が先で、その下に班ごとの行が続きます。
**CSV はこの表をそのまま書きます。**

【絞り込み】ライン・班は**いつも絞り込み**。用途コード・包装仕様NO・サイズは
欄と欄を **AND(すべて一致)/ OR(どれか一致)** から選べます。欄の中は
「,」で区切ると、そのどれか(`logic/standard_time.Criteria`)。標準の面は
部分一致、同条件の作業の面は完全一致(サイズは書き方を揃えて比べる)。

【計算式】「計算式を出す」にすると、表に**数を入れた式**の列が付き、CSV にも
そのまま出ます(中央値・平均・人分/梱包・標準作業時間・標準との差・見積り)。

【作業者名で見る(v4.12.4)】画面の頭のスイッチ(既定は切)を入れると、標準の面に
「まとめ方: 作業者ごと」(条件 × 作業者。実績からその場で作る)、梱包力の面のまとめ方に
「作業者ごと」が出ます。普段は見せないものなので、画面を開き直すと切に戻ります。

【見積り】梱包数と作業人数を入れると、行ごとに
「その条件をその梱包数・人数でやると何分か」の列が付きます
(= 標準(人分/梱包) × 梱包数 ÷ 人数、`logic/standard_time.estimate_minutes`)。

**判断はここだけ**で、画面(`views/standard_time.js`)は受け取った行を並べるだけです。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from statistics import median
from typing import Any, Optional

from ..logic import line_names
from ..logic import operator
from ..logic import standard_time as logic
from ..services import standard_time as service
from .agg_admin import Table

COLUMNS: tuple[tuple[str, str, bool], ...] = (
    # (鍵, 見出し, 数字の列か)
    ("line", "ライン", False),
    ("team", "班", False),
    ("purpose_code", "用途コード", False),
    ("purpose_name", "用途名", False),
    ("packing_spec_no", "包装仕様NO", False),
    ("size", "サイズ(厚×幅×丈)", False),
    ("count", "件数", True),
    ("pkg_median", "標準(人分/梱包)", True),
    ("pkg_mean", "平均(人分/梱包)", True),
    ("pkg_low", "最小(人分/梱包)", True),
    ("pkg_high", "最大(人分/梱包)", True),
    ("sheet_median", "標準(人分/枚)", True),
    ("sheet_mean", "平均(人分/枚)", True),
    ("sheet_low", "最小(人分/枚)", True),
    ("sheet_high", "最大(人分/枚)", True),
    ("provisional", "仮", False),
)

PROVISIONAL_MARK = "仮"
#: 「班」の列の位置(作業者ごとのときは作業者の名前が入り、班はその右隣に足す)
TEAM_AT = [key for key, _, _ in COLUMNS].index("team")

#: 画面の面。(鍵, 見出し, 何をする面か) ── **並びはここで決める**
TABS: tuple[tuple[str, str, str], ...] = (
    ("standards", "標準(条件×班)", "同じ条件・同じ班の、ふつうのペースの作業時間"),
    ("works", "同条件の作業", "同じ条件の作業を1件ずつ、作業時間と標準との差つきで"),
    ("power", "梱包力", "標準を 100 とした梱包の速さ(ゲームの DPS のように)"),
    ("guide", "考え方と計算", "標準作業時間と梱包力の考え方・計算のしかた・根拠"),
    ("backfill", "過去ぶんを入れる", "この端末の集計から、期間を指定して入れる"),
)
DEFAULT_TAB = "standards"


def tabs() -> list[dict[str, str]]:
    return [{"key": k, "label": label, "note": note} for k, label, note in TABS]


def _num(value: Optional[float]) -> str:
    return "" if value is None else f"{value:.1f}"


def _text(value: object) -> str:
    return " ".join(str(value or "").split())


def _number(payload: dict, key: str) -> float:
    try:
        return max(0.0, float(payload.get(key) or 0))
    except (TypeError, ValueError):
        return 0.0


def _flag(value: object) -> bool:
    return value is True or str(value).lower() in ("1", "true", "on", "yes")


def _criteria(payload: dict) -> logic.Criteria:
    return logic.Criteria.parse(payload.get("purpose_code"), payload.get("packing_spec_no"),
                                payload.get("size"), payload.get("join"))


#: まとめ方(標準の面)。**作業者ごとは「作業者名で見る」のときだけ**画面に出す(v4.12.4)。
#: オペレーター構成ごと(v4.15.0)は名前を出さないので、いつでも選べる
GROUP_TEAM = "team"
GROUP_WORKER = "worker"
GROUP_OPERATOR = "operator"
GROUPS = (GROUP_TEAM, GROUP_OPERATOR, GROUP_WORKER)


@dataclass
class Filters:
    line: Optional[str] = None      # None なら全ライン
    team: str = ""
    criteria: logic.Criteria = field(default_factory=logic.Criteria)
    packages: float = 0.0           # 見積り(0 なら列を出さない)
    workers: float = 0.0
    formulas: bool = False          # 計算式の列を付けるか
    group: str = GROUP_TEAM         # 班ごと / オペレーター構成ごと / 作業者ごと

    @classmethod
    def of(cls, payload: dict, line: Optional[str]) -> "Filters":
        group = payload.get("group")
        return cls(line=line, team=_text(payload.get("team")),
                   criteria=_criteria(payload),
                   packages=_number(payload, "packages"), workers=_number(payload, "workers"),
                   formulas=_flag(payload.get("formulas")),
                   group=group if group in GROUPS else GROUP_TEAM)

    @property
    def by_worker(self) -> bool:
        return self.group == GROUP_WORKER

    @property
    def by_operator(self) -> bool:
        return self.group == GROUP_OPERATOR

    @property
    def estimating(self) -> bool:
        return self.packages > 0 and self.workers > 0

    def keep(self, st: logic.Standard, roster: Optional[dict[str, str]] = None) -> bool:
        """ライン・班は絞り込み。欄は AND / OR(**部分一致**)。

        作業者ごとのときは `st.team` が作業者の名前なので、班は名簿で引いて絞ります。
        """
        if self.line and st.line != self.line:
            return False
        if self.by_operator:
            # `st.team` は構成。班は実績を絞ってから作っている(`build`)
            return self.criteria.matches(st.purpose_code, st.packing_spec_no, st.size,
                                         exact=False)
        team = (roster or {}).get(st.team, "") if self.by_worker else st.team
        if self.team and team != self.team:
            return False
        return self.criteria.matches(st.purpose_code, st.packing_spec_no, st.size,
                                     exact=False)

    def note(self) -> str:
        parts = [line_names.label(self.line) if self.line else "全ライン",
                 self.team or "全部の班"]
        if self.by_worker:
            parts.append("作業者ごと")
        if self.by_operator:
            parts.append(f"オペレーター構成ごと({operator.legend()})")
        if not self.criteria.empty:
            parts.append(self.criteria.describe())
        return " / ".join(parts)


@dataclass
class StandardTimeView:
    table: Table
    source: str
    warning: str = ""
    teams: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    samples: int = 0
    excluded: int = 0
    path: str = ""
    estimate_label: str = ""
    #: 行ごとの条件(表の行と同じ並び)。「抽出」で同条件の作業へ渡す
    conditions: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"table": self.table.as_dict(), "source": self.source,
                "warning": self.warning, "teams": self.teams, "lines": self.lines,
                "samples": self.samples, "excluded": self.excluded, "path": self.path,
                "estimate_label": self.estimate_label, "conditions": self.conditions}


#: 標準の表の計算式の列(「計算式を出す」のとき、表の右端に足す)
STANDARD_FORMULA_COLUMNS: tuple[str, ...] = (
    "式: 標準(人分/梱包)", "式: 平均(人分/梱包)", "式: 標準(人分/枚)", "式: 平均(人分/枚)")


def _grouped(samples: list[logic.Sample]) -> dict[tuple[str, ...], list[logic.Sample]]:
    """標準の鍵ごとの実績。班の鍵と「全班」の鍵の両方に入れる(`logic.standards` と同じ)。"""
    out: dict[tuple[str, ...], list[logic.Sample]] = {}
    for s in samples:
        out.setdefault(s.key, []).append(s)
        out.setdefault((s.line, logic.TEAM_ALL, s.purpose_code, s.packing_spec_no,
                        s.size), []).append(s)
    return out


def _grouped_by_worker(samples: list[logic.Sample]) -> dict[tuple[str, ...], list[logic.Sample]]:
    """作業者ごとの標準の鍵 → 実績(1つの作業を、居た全員に)。"""
    out: dict[tuple[str, ...], list[logic.Sample]] = {}
    for s in samples:
        for name in logic.names_of(s.worker):
            out.setdefault((s.line, name, s.purpose_code, s.packing_spec_no, s.size),
                           []).append(s)
    return out


def _grouped_by_operator(samples: list[logic.Sample], kinds: dict[str, str]
                         ) -> dict[tuple[str, ...], list[logic.Sample]]:
    """オペレーター構成ごとの標準の鍵 → 実績。"""
    out: dict[tuple[str, ...], list[logic.Sample]] = {}
    for s in samples:
        out.setdefault((s.line, logic.operator_label(s, kinds), s.purpose_code,
                        s.packing_spec_no, s.size), []).append(s)
    return out


def _warnings(*texts: str) -> str:
    return "\n".join(t for t in texts if t)


def build(db_path: Optional[Path], filters: Filters) -> StandardTimeView:
    readout = service.read(db_path)
    roster: dict[str, str] = {}
    samples: list[logic.Sample] = []
    operators = service.Operators()
    if filters.by_worker or filters.by_operator:
        operators = service.load_operators()
    if filters.by_worker:
        # **作業者ごとは実績から作る**(共有に貯めてある標準は班ごとだけ)
        samples = service.usable_samples(db_path, filters.line)
        roster = service.load_teams()
        source = logic.worker_standards(samples)
    elif filters.by_operator:
        # **オペレーター構成ごとも実績から作る**(いまの名簿で数える ── v4.15.0)。
        # 班で絞るなら、構成を数える前に実績を絞る
        samples = [s for s in service.usable_samples(db_path, filters.line)
                   if not filters.team or s.team == filters.team]
        source = logic.operator_standards(samples, operators.kinds)
    else:
        source = readout.standards
    picked = [st for st in source if filters.keep(st, roster)]

    columns = [label for _, label, _ in COLUMNS]
    numeric = [n for _, _, n in COLUMNS]
    if filters.by_worker:
        # 「班」の列には作業者の名前が入る。班は名簿から右隣に、区分はその右に
        at = columns.index("班")
        columns[at] = "作業者"
        columns.insert(at + 1, "班")
        columns.insert(at + 2, "オペレーター")
        numeric.insert(at + 1, False)
        numeric.insert(at + 2, False)
    if filters.by_operator:
        columns[columns.index("班")] = "オペレーター構成"
    estimate_label = ""
    if filters.estimating:
        estimate_label = f"{filters.packages:g}梱包を{filters.workers:g}人で(分)"
        columns.insert(columns.index("仮"), estimate_label)
        numeric.insert(len(numeric) - 1, True)
    groups: dict[tuple[str, ...], list[logic.Sample]] = {}
    if filters.formulas:
        groups = (_grouped_by_worker(samples) if filters.by_worker
                  else _grouped_by_operator(samples, operators.kinds) if filters.by_operator
                  else _grouped(service.usable_samples(db_path, filters.line)))
        columns += list(STANDARD_FORMULA_COLUMNS)
        numeric += [False] * len(STANDARD_FORMULA_COLUMNS)
        if filters.estimating:
            columns.append("式: 見積り")
            numeric.append(False)

    rows = []
    conditions = []
    for st in picked:
        # 「抽出」は条件で探す。作業者ごとの行からは班を問わずに(作業者の列で見分ける)。
        # オペレーター構成ごとの行からは、絞っている班で(構成は作業の列で見分ける)
        team = (filters.team if filters.by_operator else "" if filters.by_worker else st.team)
        conditions.append({"line": st.line, "team": team,
                           "purpose_code": st.purpose_code,
                           "packing_spec_no": st.packing_spec_no, "size": st.size})
        cells = {
            "line": line_names.label(st.line), "team": st.team, "purpose_code": st.purpose_code,
            "purpose_name": st.purpose_name, "packing_spec_no": st.packing_spec_no,
            "size": st.size, "count": str(st.count),
            "pkg_median": _num(st.per_package.median), "pkg_mean": _num(st.per_package.mean),
            "pkg_low": _num(st.per_package.low), "pkg_high": _num(st.per_package.high),
            "sheet_median": _num(st.per_sheet.median), "sheet_mean": _num(st.per_sheet.mean),
            "sheet_low": _num(st.per_sheet.low), "sheet_high": _num(st.per_sheet.high),
            "provisional": PROVISIONAL_MARK if st.provisional else "",
        }
        row = [cells[key] for key, _, _ in COLUMNS]
        if filters.by_worker:
            row.insert(TEAM_AT + 1, roster.get(st.team, logic.TEAM_UNKNOWN))
            row.insert(TEAM_AT + 2, operators.kinds.get(st.team, operator.NO_KIND))
        if filters.estimating:
            row.insert(len(row) - 1, _num(st.minutes_for(filters.packages, filters.workers)))
        if filters.formulas:
            rows_of = groups.get(st.key, [])
            pkg = [s.per_package for s in rows_of]
            sheet = [s.per_sheet for s in rows_of]
            row += [logic.median_formula(pkg, "人分/梱包"), logic.mean_formula(pkg, "人分/梱包"),
                    logic.median_formula(sheet, "人分/枚"), logic.mean_formula(sheet, "人分/枚")]
            if filters.estimating:
                row.append(logic.estimate_formula(st.per_package.median, filters.packages,
                                                  filters.workers))
        rows.append(row)

    table = Table(
        key="standard_time",
        title=("標準作業時間(作業者ごと)" if filters.by_worker
               else "標準作業時間(オペレーター構成ごと)" if filters.by_operator
               else "標準作業時間"),
        note=filters.note(),
        columns=columns, numeric=numeric, rows=rows,
        empty=("まだ標準がありません ── 「共有へ保存」で送った直の実績から作られます"
               if not readout.standards else "この絞り込みに合う条件はありません"))
    return StandardTimeView(
        table=table, source=readout.source,
        warning=_warnings(readout.warning, operators.warning()),
        teams=readout.teams, lines=readout.lines, samples=readout.samples,
        excluded=readout.excluded, path=str(db_path or ""),
        estimate_label=estimate_label, conditions=conditions)


# ======================================================================
# 同条件の作業
# ======================================================================
WORK_COLUMNS: tuple[tuple[str, str, bool], ...] = (
    ("report_date", "報告日", False),
    ("line", "ライン", False),
    ("shift", "直", False),
    ("team", "班", False),
    ("worker", "作業者", False),
    # 作業者欄の名前を名簿の区分で数えたもの(v4.15.0。「AOP1・BOP2」)
    ("operator", "オペレーター構成", False),
    ("lot_no", "LOT", False),
    ("purpose_code", "用途コード", False),
    ("packing_spec_no", "包装仕様NO", False),
    ("size", "サイズ", False),
    ("packages", "梱包数", True),
    ("sheets", "枚数", True),
    ("workers", "作業人数", True),
    ("work_minutes", "作業時間(分)", True),
    ("stop_minutes", "停止時間(分)", True),
    ("standard_minutes", "標準作業時間(分)", True),
    ("diff", "標準との差(分・+は標準より長い)", True),
    ("per_package", "梱包あたり人分", True),
    ("per_sheet", "枚あたり人分", True),
    ("excluded_reason", "標準に数えない理由", False),
)

#: 同条件の作業の計算式の列(「計算式を出す」のとき、表の右端に足す)
WORK_FORMULA_COLUMNS: tuple[str, ...] = (
    "式: 梱包あたり人分", "式: 枚あたり人分", "式: 標準作業時間", "式: 標準との差")


class BadRequest(ValueError):
    """画面に返す断り。`field` は直してほしい欄。"""

    def __init__(self, message: str, field: str = "", status: int = 422) -> None:
        super().__init__(message)
        self.field = field
        self.status = status


def _date(value: object, name: str) -> Optional[date]:
    text = _text(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise BadRequest("日付は yyyy-mm-dd の形式で入れてください", name, 400) from exc


@dataclass
class WorkFilters:
    criteria: logic.Criteria
    line: str = ""                  # 空なら全ライン
    team: str = ""                  # 空か「全班」なら問わない
    start: Optional[date] = None
    end: Optional[date] = None
    include_excluded: bool = False
    formulas: bool = False

    @classmethod
    def of(cls, payload: dict, default_line: str) -> "WorkFilters":
        """画面から来た条件。**何も入っていなければ断る**(`BadRequest`)。

        ラインは、送ってこなければこの端末のライン、空で送ってきたら全ライン。
        """
        criteria = _criteria(payload)
        if criteria.empty:
            raise BadRequest("用途コード・包装仕様NO・サイズのどれかを入れてください"
                             "(標準の面の表で「抽出」を押すと、条件ごと入ります)",
                             "purpose_code")
        start, end = _date(payload.get("start"), "start"), _date(payload.get("end"), "end")
        if start and end and start > end:
            raise BadRequest("開始日は終了日以前にしてください", "start")
        line = payload.get("line")
        # ラインは正規の呼び名(機側・半角の ﾄｯﾄ)でも前の名前(LS)でも受ける
        written = _text(line)
        return cls(criteria=criteria,
                   line=default_line if line is None else (line_names.to_code(written)
                                                           or line_names.upgrade(written)),
                   team=_text(payload.get("team")), start=start, end=end,
                   include_excluded=_flag(payload.get("include_excluded")),
                   formulas=_flag(payload.get("formulas")))

    @property
    def any_team(self) -> bool:
        return not self.team or self.team == logic.TEAM_ALL

    def note(self) -> str:
        parts = [line_names.label(self.line) if self.line else "全ライン",
                 logic.TEAM_ALL if self.any_team else self.team,
                 self.criteria.describe()]
        if self.start or self.end:
            parts.append(f"{self.start or '…'} 〜 {self.end or '…'}")
        return " / ".join(parts)


@dataclass
class WorksView:
    table: Table
    #: 上に出す数(並びごと渡す ── JSON の辞書は鍵の順に並べ替えられる)
    summary: list[list[str]]
    standard: str
    source: str
    warning: str = ""
    condition: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"table": self.table.as_dict(), "summary": self.summary,
                "standard": self.standard, "source": self.source,
                "warning": self.warning, "condition": self.condition}


def _minutes(value: Optional[float]) -> str:
    return "" if value is None else f"{value:.0f}分"


def _standard_text(st: logic.Standard) -> str:
    return (f"{_num(st.per_package.median)} 人分/梱包 ・ {_num(st.per_sheet.median)} 人分/枚"
            f"(件数 {st.count}{'・仮' if st.provisional else ''})")


def build_works(db_path: Optional[Path], filters: WorkFilters) -> WorksView:
    """条件に当たる作業を1件ずつ。**作業時間と、標準でやったら何分かの差**を並べる。

    各行は**その行の条件の標準**(班を選んだらその班、問わなければ全班)と比べます
    ── OR や「,」で条件が何通りも混ざっても、違う条件の標準と比べない。
    """
    found = service.works(db_path, filters.criteria, line=filters.line, team=filters.team,
                          start=filters.start, end=filters.end,
                          include_excluded=filters.include_excluded)
    operators = service.load_operators()
    columns = [label for _, label, _ in WORK_COLUMNS]
    numeric = [n for _, _, n in WORK_COLUMNS]
    if filters.formulas:
        columns += list(WORK_FORMULA_COLUMNS)
        numeric += [False] * len(WORK_FORMULA_COLUMNS)

    rows = []
    diffs: list[float] = []
    for s in found.samples:
        st = found.standard_for(s)
        standard_minutes = st.minutes_for_sample(s) if (st and s.usable) else None
        diff = (s.work_minutes - standard_minutes) if standard_minutes is not None else None
        if diff is not None:
            diffs.append(diff)
        cells = {
            "report_date": s.report_date, "line": line_names.label(s.line), "shift": s.shift,
            "team": s.team,
            "worker": s.worker, "operator": logic.operator_label(s, operators.kinds),
            "lot_no": s.lot_no, "purpose_code": s.purpose_code,
            "packing_spec_no": s.packing_spec_no, "size": s.size,
            "packages": f"{s.packages:g}", "sheets": f"{s.sheets:g}",
            "workers": f"{s.workers:g}", "work_minutes": f"{s.work_minutes:g}",
            "stop_minutes": f"{s.stop_minutes:g}",
            "standard_minutes": _num(standard_minutes),
            "diff": "" if diff is None else f"{diff:+.1f}",
            "per_package": _num(s.per_package), "per_sheet": _num(s.per_sheet),
            "excluded_reason": s.excluded_reason,
        }
        row = [cells[key] for key, _, _ in WORK_COLUMNS]
        if filters.formulas:
            row += [logic.per_package_formula(s), logic.per_sheet_formula(s),
                    logic.standard_minutes_formula(st, s),
                    logic.diff_formula(s, standard_minutes)]
        rows.append(row)

    minutes = [s.work_minutes for s in found.samples]
    usable = [s for s in found.samples if s.usable]
    summary: list[list[str]] = [["件数", f"{len(found.samples)}件"]]
    if minutes:
        summary += [["作業時間 合計", _minutes(sum(minutes))],
                    ["平均", _minutes(sum(minutes) / len(minutes))],
                    ["中央値", _minutes(median(minutes))],
                    ["最短", _minutes(min(minutes))], ["最長", _minutes(max(minutes))]]
    own = logic.Stat.of(s.per_package for s in usable)
    if own.count:
        summary.append(["この抽出の中央値(人分/梱包)", _num(own.median)])
    if diffs:
        summary.append(["標準との差 平均", f"{sum(diffs) / len(diffs):+.1f}分"])

    keys = {(s.line, s.purpose_code, s.packing_spec_no, s.size) for s in found.samples}
    team = found.standard_team
    if len(keys) == 1:
        st = found.standards.get(next(iter(keys)))
        standard = (f"この条件の標準({team}): {_standard_text(st)}" if st else
                    f"この条件の標準({team})はまだありません(標準に数える作業が溜まると出ます)")
    elif keys:
        missing = sum(1 for k in keys if k not in found.standards)
        standard = (f"条件が {len(keys)}通り あります ── 各行は、その行の条件の標準({team})"
                    "と比べています" + (f"(標準がまだ無い条件: {missing}通り)" if missing else ""))
    else:
        standard = ""
    if standard:
        standard += " ── 標準作業時間(分) = 標準(人分/梱包) × 梱包数 ÷ 作業人数"

    table = Table(
        key="works", title="同条件の作業", note=filters.note(),
        columns=columns, numeric=numeric, rows=rows,
        empty="この条件の作業はありません(期間・班・AND / OR を見直してみてください)")
    c = filters.criteria
    return WorksView(
        table=table, summary=summary, standard=standard, source=found.source,
        warning=_warnings(found.warning, operators.warning()),
        condition={"line": filters.line, "line_label": line_names.label(filters.line),
                   "team": filters.team, "join": c.join,
                   "purpose_code": ",".join(c.purpose_codes),
                   "packing_spec_no": ",".join(c.spec_nos), "size": ",".join(c.sizes),
                   "text": c.describe()})


# ======================================================================
# 梱包力 (v4.10.0) ── ゲームの DPS のように、標準と比べた梱包の速さ
# ======================================================================
#: 表の列 (鍵, 見出し, 数字の列か)。先頭の「まとめ」の見出しは、まとめ方で変わる
POWER_COLUMNS: tuple[tuple[str, str, bool], ...] = (
    ("label", "まとめ", False),
    ("power", "梱包力", True),
    ("compared", "比べた作業(件)", True),
    ("works", "対象の作業(件)", True),
    ("standard_pm", "標準人分", True),
    ("actual_pm", "実人分", True),
    ("diff_pm", "差(人分・+は標準より多くかかった)", True),
    ("packages", "梱包数", True),
    ("per_hour", "1人1時間あたり梱包数", True),
)
POWER_FORMULA_COLUMN = "式: 梱包力"
#: メーター(棒)に出す行の上限。**表には全部**出す
POWER_METER_ROWS = 30
#: メーターの目盛りの最小(100 の線が左寄りになりすぎないように)
POWER_SCALE_MIN = 150
#: 一行の説明(画面とテストで同じ文言を使う)
POWER_LEAD = "標準(全班の中央値)を 100 とした梱包の速さ。120 なら標準の1.2倍"
POWER_DEFINITION = (
    "梱包力 = 標準人分の合計 ÷ 実人分の合計 × 100\n"
    "標準人分 = その条件の標準(人分/梱包) × 梱包数\n"
    "実人分 = 作業時間 × 作業人数\n"
    "標準がまだ無い・「仮」(3件未満)の条件の作業は比べません。\n"
    "難しい仕事(大きい板・手間のかかる包装)ほど標準人分が大きいので、"
    "1時間あたりの梱包数と違って、やった仕事の重さで比べられます。")


def power_groups() -> list[tuple[str, str]]:
    """まとめ方の選択肢 (鍵, 見出し)。先頭が既定。"""
    return list(logic.POWER_GROUPS)


@dataclass
class PowerFilters:
    line: Optional[str] = None      # None なら全ライン
    team: str = ""                  # 空か「全班」なら問わない
    start: Optional[date] = None
    end: Optional[date] = None
    group: str = "team"
    formulas: bool = False

    @classmethod
    def of(cls, payload: dict, line: Optional[str]) -> "PowerFilters":
        start, end = _date(payload.get("start"), "start"), _date(payload.get("end"), "end")
        if start and end and start > end:
            raise BadRequest("開始日は終了日以前にしてください", "start")
        group = _text(payload.get("group"))
        if group not in dict(logic.POWER_GROUPS):
            group = "team"
        team = _text(payload.get("team"))
        return cls(line=line, team="" if team == logic.TEAM_ALL else team,
                   start=start, end=end, group=group,
                   formulas=_flag(payload.get("formulas")))

    @property
    def group_label(self) -> str:
        return dict(logic.POWER_GROUPS)[self.group]

    def note(self) -> str:
        parts = [line_names.label(self.line) if self.line else "全ライン",
                 self.team or logic.TEAM_ALL, f"{self.group_label}ごと"]
        if self.group == "operator":
            parts[-1] += f"({operator.legend()})"
        if self.start or self.end:
            parts.append(f"{self.start or '…'} 〜 {self.end or '…'}")
        return " / ".join(parts)


@dataclass
class PowerView:
    #: いちばん大きく出す数(全体の梱包力)
    hero: dict[str, str]
    #: 棒の並び({label, value, width, hint, muted})
    meter: list[dict[str, Any]]
    #: 目盛り({max, ref}: ref は 100 の線の位置 %)
    scale: dict[str, float]
    table: Table
    group: str
    group_label: str
    source: str
    warning: str = ""
    teams: list[str] = field(default_factory=list)
    more: int = 0                   # メーターに出しきれなかった行の数
    more_note: str = ""             # その一言(「ほか 3件(古いほう)は下の表に」)

    def as_dict(self) -> dict[str, Any]:
        return {"hero": self.hero, "meter": self.meter, "scale": self.scale,
                "table": self.table.as_dict(), "group": self.group,
                "group_label": self.group_label, "source": self.source,
                "warning": self.warning, "teams": self.teams, "more": self.more,
                "more_note": self.more_note}


def _power_text(p: logic.Power) -> str:
    return "" if p.power is None else f"{p.power:.0f}"


def _power_hint(p: logic.Power) -> str:
    """棒に乗せたときの説明(1行目が答え、下に式)。"""
    if p.power is None:
        return f"{p.label}: 標準と比べられる作業がありません(対象 {p.works}件)"
    per_hour = p.packages_per_person_hour
    lines = [f"{p.label}: 梱包力 {p.power:.0f}({logic.power_meaning(p.power)})",
             logic.power_formula(p),
             f"比べた作業 {p.compared}/{p.works}件"]
    if per_hour is not None:
        lines[-1] += f"・1人1時間 {per_hour:.1f}梱包"
    return "\n".join(lines)


def _meter_label(p: logic.Power, group: str) -> str:
    """棒の見出し。直・日は**短く**(「9/4」「9/4 1直」── 狭い画面で棒が潰れないように)。
    年まで入った見出しは、乗せたときの説明と表に出ます。"""
    if group not in logic.POWER_TIMELINE or not p.order:
        return p.label
    try:
        day = date.fromisoformat(str(p.order[0]))
    except ValueError:
        return p.label
    short = f"{day.month}/{day.day}"
    return short if group == "date" else f"{short} {p.order[-1]}"


def build_power(db_path: Optional[Path], filters: PowerFilters) -> PowerView:
    """梱包力の画面。**比べる標準はいつも全班**(班どうしを比べるため)。

    班で絞るときも標準は全班のもの ── 班の標準と比べると、どの班も
    自分の中央値と比べて 100 前後になってしまいます。
    """
    found = service.works(db_path, logic.Criteria(), line=filters.line or "",
                          start=filters.start, end=filters.end)
    samples = found.samples
    teams = sorted({s.team for s in samples if s.team})
    if filters.team:
        samples = [s for s in samples if s.team == filters.team]
    # オペレーター構成・作業者ごとは名簿の区分を使う(v4.15.0)
    operators = (service.load_operators() if filters.group in ("operator", "worker")
                 else service.Operators())
    total, groups = logic.packing_power(samples, found.standard_for, filters.group,
                                        kinds=operators.kinds)

    columns = [label for _, label, _ in POWER_COLUMNS]
    columns[0] = filters.group_label
    numeric = [n for _, _, n in POWER_COLUMNS]
    by_worker = filters.group == "worker"
    if by_worker:
        # 作業者の右隣に、その人の区分
        columns.insert(1, "オペレーター")
        numeric.insert(1, False)
    if filters.formulas:
        columns.append(POWER_FORMULA_COLUMN)
        numeric.append(False)

    def row_of(p: logic.Power) -> list[str]:
        per_hour = p.packages_per_person_hour
        cells = {"label": p.label, "power": _power_text(p),
                 "compared": str(p.compared), "works": str(p.works),
                 "standard_pm": _num(p.standard_pm if p.compared else None),
                 "actual_pm": _num(p.actual_pm if p.compared else None),
                 "diff_pm": f"{p.diff_pm:+.1f}" if p.compared else "",
                 "packages": f"{p.packages:g}" if p.compared else "",
                 "per_hour": _num(per_hour)}
        row = [cells[key] for key, _, _ in POWER_COLUMNS]
        if by_worker:
            row.insert(1, "" if p is total else operators.kinds.get(p.label, operator.NO_KIND))
        if filters.formulas:
            row.append(logic.power_formula(p))
        return row

    rows = [row_of(p) for p in groups]
    if groups:
        rows.append(row_of(total))

    # 棒は30本まで。直・日は**新しいほう**を残す(推移は最近が見たい)
    timeline = filters.group in logic.POWER_TIMELINE
    shown = groups[-POWER_METER_ROWS:] if timeline else groups[:POWER_METER_ROWS]
    more = len(groups) - len(shown)
    top = max((p.power for p in shown if p.power is not None), default=0.0)
    scale_max = float(max(POWER_SCALE_MIN, math.ceil(top / 50) * 50))
    meter = [{"label": _meter_label(p, filters.group), "value": _power_text(p) or "—",
              "width": 0.0 if p.power is None else round(min(p.power, scale_max)
                                                          / scale_max * 100, 1),
              "hint": _power_hint(p), "muted": p.power is None}
             for p in shown]

    if total.power is None:
        hero = {"value": "—", "meaning": ("標準と比べられる作業がまだありません"
                                          if total.works else "この期間の作業はありません"),
                "compared": f"比べた作業 {total.compared}/{total.works}件", "formula": ""}
    else:
        hero = {"value": f"{total.power:.0f}", "meaning": logic.power_meaning(total.power),
                "compared": f"比べた作業 {total.compared}/{total.works}件",
                "formula": logic.power_formula(total)}

    table = Table(
        key="power", title=f"梱包力({filters.group_label}ごと)", note=filters.note(),
        columns=columns, numeric=numeric, rows=rows,
        empty="この期間に、標準と比べられる作業はありません(期間・ラインを見直してみてください)")
    return PowerView(hero=hero, meter=meter,
                     scale={"max": scale_max, "ref": round(logic.POWER_BASE / scale_max * 100, 1)},
                     table=table, group=filters.group, group_label=filters.group_label,
                     source=found.source,
                     warning=_warnings(found.warning, operators.warning()), teams=teams,
                     more=more,
                     more_note=(f"ほか {more}件({'古いほう' if timeline else '下位'})は下の表に"
                                if more else ""))



# ======================================================================
# 考え方と計算 (v4.11.0) ── 標準作業時間と梱包力を、読んで分かるように
# ======================================================================
#     標準時間 梱包力 概念と計算方法は参照できるようにしてください(専用タブ)
#     ちなみに計算方法に根拠はあるんですか？
#
# 面の文章はテンプレート(`standard_time.html`)に書き、**例の数はここで
# 本物の計算に通して出します** ── 説明の数と画面の数が食い違わないように
# (計算を直したら、例もいっしょに変わる)。

#: 例の4作業(架空)。同じ条件・4梱包・2人で、作業時間だけ違う (班, 分)
GUIDE_WORKS: tuple[tuple[str, float], ...] = (("A", 60), ("A", 70), ("B", 80), ("B", 100))
GUIDE_PACKAGES = 4
GUIDE_WORKERS = 2
#: 4つ目がトラブルで長引いたら(中央値は動かず、平均は大きく動く)
GUIDE_TROUBLE_MINUTES = 300
#: 見積りの例(この梱包数を、この人数で)
GUIDE_ESTIMATE = (6, 3)
#: 難しさで均す例 (見出し, 標準 人分/梱包, 梱包数) ── どちらも2人で60分
GUIDE_DIFFICULTY: tuple[tuple[str, float, float], ...] = (
    ("易しい条件(小さい板)", 20.0, 6), ("難しい条件(大きい板)", 40.0, 3))


def _guide_sample(row_no: int, team: str, minutes: float, **over) -> logic.Sample:
    base = dict(report_date="2026年9月1日", line="L-1", shift="1直", page=1, row_no=row_no,
                worker="", team=team, purpose_code="H176", packing_spec_no="1P0001",
                dimension="8×1528×3053", packages=GUIDE_PACKAGES,
                sheets=GUIDE_PACKAGES * 10, workers=GUIDE_WORKERS, work_minutes=minutes)
    base.update(over)
    return logic.sample_of(**base)


def _excluded_reasons() -> list[str]:
    """標準に数えない理由。**計算そのものに聞いて**並べる(文言を二重に持たない)。"""
    probes = (dict(purpose_code=""), dict(packing_spec_no=""), dict(dimension=""),
              dict(workers=0), dict(work_minutes=0), dict(packages=0, sheets=0))
    return [_guide_sample(1, "A", 60, **p).excluded_reason for p in probes]


def _fmt(value: Optional[float], digits: int = 1) -> str:
    return "" if value is None else f"{value:.{digits}f}"


#: オペレーター構成の例の名前(架空。1人目 AOP・あと2人 BOP として数える)
GUIDE_OPERATOR_NAMES: tuple[str, ...] = ("Aさん", "Bさん", "Cさん")


def guide() -> dict[str, Any]:
    """「考え方と計算」の面に出す数。どれも本物の計算(`logic/standard_time`)に通したもの。"""
    works = [_guide_sample(i, team, m) for i, (team, m) in enumerate(GUIDE_WORKS, 1)]
    st = next(s for s in logic.standards(works) if s.team == logic.TEAM_ALL)
    trouble = works[:-1] + [replace(works[-1], work_minutes=GUIDE_TROUBLE_MINUTES)]
    st_trouble = next(s for s in logic.standards(trouble) if s.team == logic.TEAM_ALL)
    packages, workers = GUIDE_ESTIMATE
    total, teams = logic.packing_power(works, lambda s: st, "team")

    def power_row(p: logic.Power) -> dict[str, str]:
        return {"label": p.label, "standard_pm": _fmt(p.standard_pm, 0),
                "actual_pm": _fmt(p.actual_pm, 0), "power": _fmt(p.power, 0),
                "meaning": logic.power_meaning(p.power)}

    difficulty = []
    for i, (label, per_package, count) in enumerate(GUIDE_DIFFICULTY, 1):
        s = _guide_sample(i, "A", 60, packages=count, sheets=count * 10)
        std = logic.Standard("L-1", logic.TEAM_ALL, s.purpose_code, s.packing_spec_no, s.size,
                             count=10, per_package=logic.Stat(count=10, median=per_package))
        p = logic.Power(label)
        p.add(s, logic.standard_person_minutes(std, s))
        difficulty.append({"label": label, "standard": _fmt(per_package), "packages": f"{count:g}",
                           "actual_pm": _fmt(p.actual_pm, 0),
                           "per_hour": _fmt(p.packages_per_person_hour),
                           "standard_pm": _fmt(p.standard_pm, 0), "power": _fmt(p.power, 0)})

    # オペレーター構成の例(架空の3人。区分は名簿の代わりにここで決める)
    example_kinds = dict(zip(GUIDE_OPERATOR_NAMES, ("AOP", "BOP", "BOP")))
    return {
        "operator_kinds": [(k, operator.MEANINGS[k]) for k in operator.KINDS],
        "no_kind": operator.NO_KIND,
        "operator_example": {
            "workers": " ".join(GUIDE_OPERATOR_NAMES),
            "label": operator.composition(GUIDE_OPERATOR_NAMES, example_kinds)},
        "min_samples": logic.MIN_SAMPLES,
        "base": f"{logic.POWER_BASE:.0f}",
        "excluded": _excluded_reasons(),
        "packages": GUIDE_PACKAGES, "workers": GUIDE_WORKERS,
        "works": [{"no": i, "team": s.team, "minutes": f"{s.work_minutes:g}",
                   "person_minutes": f"{s.person_minutes:g}",
                   "per_package": _fmt(s.per_package)} for i, s in enumerate(works, 1)],
        "sorted": " ・ ".join(_fmt(v) for v in sorted(s.per_package for s in works)),
        "median": _fmt(st.per_package.median), "mean": _fmt(st.per_package.mean, 2),
        "trouble_minutes": f"{GUIDE_TROUBLE_MINUTES:g}",
        "trouble_per_package": _fmt(trouble[-1].per_package),
        "trouble_median": _fmt(st_trouble.per_package.median),
        "trouble_mean": _fmt(st_trouble.per_package.mean, 2),
        "estimate_packages": packages, "estimate_workers": workers,
        "estimate": _fmt(st.minutes_for(packages, workers), 0),
        "diff_first": f"{works[0].work_minutes - st.minutes_for_sample(works[0]):+.0f}",
        "standard_first": _fmt(st.minutes_for_sample(works[0]), 0),
        "standard_pm_one": _fmt(st.per_package.median * GUIDE_PACKAGES, 0),
        "teams": [power_row(p) for p in teams],
        "total": power_row(total),
        "difficulty": difficulty,
    }
