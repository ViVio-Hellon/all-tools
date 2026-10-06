"""看板集計 ── 出した看板の回数・届くまでの日数・提出率

``看板履歴.sqlite3``(各端末がボタンの操作ごとに書く出来事。
:mod:`kanban.domain.events` / :mod:`kanban.db.history`)から、次を出します。

* **1 回ごとの明細**(サイクル): 出した日 → 発送した日 → 届いた日(赤を消した日)、
  届くまでの日数、次に出すまでの日数
* **週ごと・月ごとの回数**(看板ごと)
* **届くまでの日数の平均**(ライン × 月・ラインごと・看板ごと。最短・最長も)
* **提出率**(ライン × 月): その月に 1 回以上出した看板の枚数 ÷ そのラインの看板の枚数

【日数の数え方】日付の差です(9/1 に出して 9/3 に届いたら 2 日)。時刻までの
差にすると「1.9 日」のような値になり、紙の記録と突き合わせにくいため。

【数え方の決まり】

* 1 回は「出した日時」で見分ける(端末の時計のずれ・記録の順番に左右されない)
* 取消(発送される前に赤を消した)は**出した回数に数えない**(押し間違いの取り消し)。
  ただし、その回に発送があった・発送する人のいないライン(NS1 など)なら届いたとして数える
  (:func:`build_cycles`)
* 記録を始める前に出していた看板も、届いた・発送した記録に「その回の出した日時」が
  入っているので、その回として組み立てる
* 出したまま、次の回が出された(間の記録が抜けた)ときは、前の回を「届いた日なし」で残す

【週】月曜日から日曜日まで。月曜日の日付で呼ぶ。

【押し間違い】基本は**状態の変わり方**で見分ける(発送前に赤を消した = 取消。時間は問わない)。
状態だけでは区別できない 2 つだけ、:data:`MISTAKE_MINUTES` 分より短ければ押し間違いとして数えない:

* 注文中(黄)を付けて、すぐ外した
* 発送する人のいないライン(NS1 など)で、赤を付けてすぐ消した(このラインは消せば「届いた」になる)

【注文中】倉庫の在庫切れで仕入れ先へ注文した期間。付けた → 外した(発送して自動で外れたも含む)。
【数量】サイズ欄の「:」の右(``2200 × 50M : 3本`` なら 3 本)。読めない看板は数量なしで数える。
【倉庫に表示】出した看板が倉庫の端末に初めて取り込まれた時刻(倉庫の端末が書く)。
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Sequence

from .. import config
from ..db.shared import SharedDb, SharedDbError
from ..domain import events, models

HISTORY_TABLE = "看板履歴"

#: これより短い注文中・発送の無いラインの赤は押し間違いとして数えない(分)。既定値。
#: 設定(``config.mistake_minutes``。看板集計タブで変える)が優先する
MISTAKE_MINUTES = 5


@dataclass
class Event:
    at: datetime
    line: str
    mgmt_no: str
    kind: str
    material: str = ""
    size: str = ""
    ordered_at: datetime | None = None


@dataclass
class Cycle:
    """1 回ぶん(出した → 発送 → 届いた)。"""

    line: str
    mgmt_no: str
    material: str
    size: str
    ordered_at: datetime
    shipped_at: datetime | None = None
    delivered_at: datetime | None = None
    next_ordered_at: datetime | None = None
    seen_at: datetime | None = None
    """倉庫の端末に初めて出た時刻(倉庫の端末が開いていなければ遅れる)。"""
    holds: list[tuple[datetime, datetime | None]] = field(default_factory=list)
    """注文中だった期間(付けた, 外した)。外していなければ外した側は None。押し間違いは除く。"""

    @property
    def quantity(self) -> tuple[float | None, str]:
        return parse_quantity(self.size)

    @property
    def hours_to_seen(self) -> float | None:
        """出してから倉庫の画面に出るまで(時間)。"""
        return _hours(self.ordered_at, self.seen_at)

    @property
    def hours_seen_to_ship(self) -> float | None:
        """倉庫の画面に出てから発送するまで(時間)。"""
        return _hours(self.seen_at, self.shipped_at)

    @property
    def hold_days(self) -> int | None:
        """注文中だった日数の合計(外した分だけ)。注文中が無ければ None。"""
        done = [_days(a, b) for a, b in self.holds if b is not None]
        return sum(d for d in done if d is not None) if done else None

    @property
    def days_to_ship(self) -> int | None:
        return _days(self.ordered_at, self.shipped_at)

    @property
    def days_to_deliver(self) -> int | None:
        """何日で届いたか(出した日 → 赤を消した日)。"""
        return _days(self.ordered_at, self.delivered_at)

    @property
    def days_to_next(self) -> int | None:
        """次に出すまで何日か(届いた日 → 次に出した日)。"""
        return _days(self.delivered_at, self.next_ordered_at)


def _hours(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None or b < a:
        return None
    return round((b - a).total_seconds() / 3600, 1)


def parse_quantity(size: str) -> tuple[float | None, str]:
    """サイズ欄から 1 回の数量と単位。``"2200 × 50M : 3本"`` → ``(3.0, "本")``。読めなければ ``(None, "")``。"""
    text = unicodedata.normalize("NFKC", str(size or ""))
    if ":" not in text:
        return None, ""
    right = text.split(":", 1)[1].strip()
    m = re.match(r"([0-9]+(?:\.[0-9]+)?)\s*(.*)$", right)
    if not m:
        return None, ""
    return float(m.group(1)), m.group(2).strip()


def _days(a: datetime | None, b: datetime | None) -> int | None:
    if a is None or b is None:
        return None
    return (b.date() - a.date()).days


# ------------------------------------------------------------------
# 読む
# ------------------------------------------------------------------
@dataclass
class Dataset:
    available: bool = False
    why: str = ""
    events: list[Event] = field(default_factory=list)
    totals: dict[str, int] = field(default_factory=dict)
    """ライン → いまの看板の枚数(提出率の分母)。"""
    comments: list[dict[str, Any]] = field(default_factory=list)
    """倉庫 ⇔ 現場のコメント(明細 CSV に載せる)。"""
    history_rows: list[dict[str, Any]] = field(default_factory=list)
    """看板履歴の行そのまま(「看板履歴」の CSV に出す)。"""
    history_path: str = ""
    """看板履歴を読んだファイル(画面に出す)。"""


def load(shared: SharedDb | None, history: SharedDb | None = None) -> Dataset:
    """出来事(看板履歴.sqlite3)と、ラインごとの看板の枚数(共有DB)を読む。

    ``history`` が無い・まだファイルが無いときは、共有DBの ``看板履歴``(以前の置き場所)を読む。
    """
    data = Dataset()
    if shared is None or not shared.exists():
        data.why = "共有DBに届きません。「接続先」を確かめてください。"
        return data
    try:
        names = set(shared.table_names())
    except SharedDbError as exc:
        data.why = str(exc)
        return data

    for code in config.all_line_codes():
        table = config.table_name_for(code)
        if table not in names:
            continue
        try:
            rows = shared.read_table(table).rows
        except SharedDbError:
            continue
        data.totals[code] = sum(
            1 for r in rows
            if str(r.get(config.COL_MATERIAL) or "").strip()
            and str(r.get(config.COL_SIZE) or "").strip()
        )

    data.available = True
    data.comments = _load_comments(shared, names)
    source, source_names = shared, names
    if history is not None and history.exists():
        try:
            source, source_names = history, set(history.table_names())
        except SharedDbError as exc:
            data.why = f"看板履歴を読めません: {exc}"
            return data
    data.history_path = source.path
    if HISTORY_TABLE not in source_names:
        data.why = (
            "まだ記録がありません。看板を出す・発送する・届いた(赤を消す)と、"
            "その端末が書き戻すときに記録されます。"
        )
        return data
    try:
        rows = source.read_table(HISTORY_TABLE).rows
    except SharedDbError as exc:
        data.why = str(exc)
        return data
    data.history_rows = rows
    data.events = parse_events(rows)
    return data


COMMENT_TABLE = "看板コメント"


def _load_comments(shared: SharedDb, names: set[str]) -> list[dict[str, Any]]:
    if COMMENT_TABLE not in names:
        return []
    try:
        rows = shared.select(
            f"SELECT [日時] AS at, [ライン] AS line, [管理番号] AS mgmt_no, [書いた側] AS side,"
            f" [本文] AS body FROM [{COMMENT_TABLE}] WHERE [種類] = 'コメント' ORDER BY [日時]"
        )
    except SharedDbError:
        return []
    out = []
    for r in rows:
        at = models.parse_datetime(str(r.get("at") or ""))
        if at is not None:
            out.append({"at": at, "line": str(r.get("line") or ""), "mgmt_no": str(r.get("mgmt_no") or ""),
                        "side": str(r.get("side") or ""), "body": str(r.get("body") or "")})
    return out


def comments_by_cycle(cycles: Sequence[Cycle], comments: Sequence[dict[str, Any]]) -> dict[int, str]:
    """1 回ごとのコメント(前の回が届いたあと 〜 その回が届いたとき)を 1 つの文字列に。"""
    by_kanban: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for c in comments:
        by_kanban[(c["line"], c["mgmt_no"])].append(c)
    groups: dict[tuple[str, str], list[Cycle]] = defaultdict(list)
    for c in cycles:
        groups[(c.line, c.mgmt_no)].append(c)
    out: dict[int, str] = {}
    for key, mine in groups.items():
        mine.sort(key=lambda c: c.ordered_at)
        start = datetime.min
        for c in mine:
            end = c.delivered_at or datetime.max
            texts = [f"{x['at']:%m/%d %H:%M} {x['side']}: {x['body']}"
                     for x in by_kanban.get(key, ()) if start < x["at"] <= end]
            out[id(c)] = " / ".join(texts)
            start = end
    return out


def parse_events(rows: Iterable[dict[str, Any]]) -> list[Event]:
    out: list[Event] = []
    seen: set[str] = set()
    for r in rows:
        uid = str(r.get("ID") or "")
        if uid and uid in seen:
            continue
        seen.add(uid)
        at = models.parse_datetime(str(r.get("日時") or ""))
        kind = str(r.get("出来事") or "")
        if at is None or kind not in events.ALL_KINDS:
            continue
        out.append(Event(
            at=at,
            line=str(r.get("ライン") or ""),
            mgmt_no=str(r.get("管理番号") or ""),
            kind=kind,
            material=str(r.get("資材") or ""),
            size=str(r.get("サイズ") or ""),
            ordered_at=models.parse_datetime(str(r.get("出した日時") or "")),
        ))
    out.sort(key=lambda e: e.at)
    return out


# ------------------------------------------------------------------
# 組み立てる
# ------------------------------------------------------------------
def build_cycles(evts: Sequence[Event], mistake_minutes: int | None = None) -> list[Cycle]:
    """出来事を看板ごとに並べ、1 回ずつ(サイクル)に組み立てる。

    **1 回は「出した日時」で見分ける。** 出した・発送・届いた…のどの記録にも、
    その回の出した日時が入っている(押した端末が、その看板の更新日から写す)。
    記録を時刻の順につなぐだけだと、次のときに回を取り違える。

    * 端末ごとに時計が少しずれている(倉庫の「発送」が現場の「出した」より
      前に並ぶ)
    * 倉庫が発送したあと、現場がそれを取り込む前に赤を消した ── 現場の端末は
      「発送前に消した」と見るので「取消」と記録する。実際は発送された回
    * 発送する人がいないライン(倉庫の画面に無く、現場も発送を押せない)では、
      届いて赤を消しても必ず「取消」になる

    そこで、同じ回(同じ出した日時)に**発送があって**消した、または
    **発送の無いライン**で消した「取消」は、届いたとして数える。発送の無い
    ふつうのラインで消したものだけが、押し間違いの取り消し(数えない)。
    """
    by_kanban: dict[tuple[str, str], list[Event]] = defaultdict(list)
    for e in evts:
        by_kanban[(e.line, e.mgmt_no)].append(e)

    cycles: list[Cycle] = []
    for (line, mgmt_no), items in by_kanban.items():
        items.sort(key=lambda e: e.at)
        groups: dict[datetime, list[Event]] = {}
        for e in items:
            key = e.ordered_at or (e.at if e.kind == events.ORDERED else _open_before(groups, e.at))
            if key is None:
                key = e.at          # どの回か分からない(出した記録が無い)。この記録から始まる回
            groups.setdefault(key, []).append(e)

        ships = line_ships(line)
        minutes = MISTAKE_MINUTES if mistake_minutes is None else max(0, int(mistake_minutes))
        mine = [c for c in (_assemble(line, mgmt_no, key, group, ships, minutes)
                            for key, group in groups.items()) if c is not None]
        mine.sort(key=lambda c: c.ordered_at)
        for before, after in zip(mine, mine[1:]):
            before.next_ordered_at = after.ordered_at
        cycles.extend(mine)
    cycles.sort(key=lambda c: (_line_order(c.line), c.ordered_at))
    return cycles


def line_ships(line: str) -> bool:
    """そのラインに発送する人がいるか(倉庫の画面にある・現場が発送を押せる)。"""
    info = config.line_info(line)
    return info is None or info.warehouse_target or not info.shipping_disabled


def _open_before(groups: dict[datetime, list[Event]], at: datetime) -> datetime | None:
    """出した日時の入っていない記録を、その時点で直近に出した回へつける。"""
    keys = [k for k in groups if k <= at]
    return max(keys) if keys else None


def _assemble(line: str, mgmt_no: str, key: datetime, group: list[Event], ships: bool,
              minutes: int = MISTAKE_MINUTES) -> Cycle | None:
    """1 回ぶんの記録から、出した・発送・届いた・注文中・倉庫に表示を決める。取り消した回は None。"""
    first = next((e for e in group if e.kind == events.ORDERED), group[0])
    cycle = Cycle(line, mgmt_no, first.material, first.size, key)
    ordered_event = next((e.at for e in group if e.kind == events.ORDERED), None)
    cancelled_at: datetime | None = None
    hold_from: datetime | None = None
    mistake = timedelta(minutes=minutes)

    def end_hold(at: datetime) -> None:
        nonlocal hold_from
        if hold_from is not None and at - hold_from >= mistake:   # すぐ外したものは数えない
            cycle.holds.append((hold_from, at))
        hold_from = None

    for e in sorted(group, key=lambda e: e.at):
        if e.kind == events.SHIPPED:
            cycle.shipped_at = e.at
            end_hold(e.at)                  # 発送すれば注文中は終わる(古い記録には「解除」が無い)
        elif e.kind == events.UNSHIPPED:
            cycle.shipped_at = None
        elif e.kind == events.DELIVERED:
            cycle.delivered_at = e.at
            end_hold(e.at)
        elif e.kind == events.CANCELLED:
            cancelled_at = e.at
            end_hold(e.at)
        elif e.kind == events.HOLD:
            if hold_from is None:
                hold_from = e.at
        elif e.kind == events.UNHOLD:
            end_hold(e.at)
        elif e.kind == events.SEEN:
            if cycle.seen_at is None or e.at < cycle.seen_at:
                cycle.seen_at = e.at        # 倉庫の端末が何台あっても、いちばん早く出た時刻
    if hold_from is not None:
        cycle.holds.append((hold_from, None))   # いまも注文中
    if cancelled_at is not None and cycle.delivered_at is None:
        if cycle.shipped_at is None and ships:
            return None                     # 発送前に消した = 押し間違いの取り消し
        if (not ships and cycle.shipped_at is None and ordered_event is not None
                and cancelled_at - ordered_event < mistake):
            return None                     # 発送の無いラインで、すぐ消した = 押し間違い
        cycle.delivered_at = cancelled_at   # 発送されていた/発送の無いライン = 届いた
    return cycle


def _mean(values: Sequence[int]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def lead_stats(cycles: Sequence[Cycle]) -> dict[str, Any]:
    """届くまでの日数(出した日 → 届いた日)の平均・最短・最長。

    **まだ届いていない回は入れない**(日数が決まっていない)。何回ぶんの
    平均かを ``delivered`` で一緒に返す ── 1 回だけの平均と 30 回の平均を
    同じ重みで読まないように。
    """
    days = [c.days_to_deliver for c in cycles if c.days_to_deliver is not None]
    return {
        "delivered": len(days),
        "avg_deliver_days": _mean(days),
        "min_deliver_days": min(days) if days else None,
        "max_deliver_days": max(days) if days else None,
    }


def _line_order(code: str) -> int:
    codes = config.all_line_codes()
    return codes.index(code) if code in codes else len(codes)


# ------------------------------------------------------------------
# 期間
# ------------------------------------------------------------------
def month_key(d: date | datetime) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def week_start(d: date | datetime) -> date:
    day = d.date() if isinstance(d, datetime) else d
    return day - timedelta(days=day.weekday())


def months_between(start: str, end: str) -> list[str]:
    """``YYYY-MM`` の範囲(両端を含む)。逆なら入れ替える。"""
    y1, m1 = (int(x) for x in start.split("-"))
    y2, m2 = (int(x) for x in end.split("-"))
    if (y1, m1) > (y2, m2):
        (y1, m1), (y2, m2) = (y2, m2), (y1, m1)
    out = []
    while (y1, m1) <= (y2, m2):
        out.append(f"{y1:04d}-{m1:02d}")
        y1, m1 = (y1 + 1, 1) if m1 == 12 else (y1, m1 + 1)
    return out


def default_months(today: date | None = None, count: int = 12) -> tuple[str, str]:
    """既定の期間: 今月までの 12 か月。"""
    today = today or date.today()
    y, m = today.year, today.month
    end = f"{y:04d}-{m:02d}"
    for _ in range(count - 1):
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return f"{y:04d}-{m:02d}", end


def parse_month(text: str) -> str | None:
    try:
        y, m = (int(x) for x in str(text).strip().split("-"))
    except ValueError:
        return None
    if not (2000 <= y <= 2100 and 1 <= m <= 12):
        return None
    return f"{y:04d}-{m:02d}"


def in_period(cycles: Iterable[Cycle], months: Sequence[str], line: str = "") -> list[Cycle]:
    wanted = set(months)
    return [
        c for c in cycles
        if month_key(c.ordered_at) in wanted and (not line or c.line == line)
    ]


# ------------------------------------------------------------------
# 集計
# ------------------------------------------------------------------
def rate_table(
    cycles: Sequence[Cycle], totals: dict[str, int], months: Sequence[str], lines: Sequence[str]
) -> dict[str, list[dict[str, Any]]]:
    """ライン × 月の提出率と回数。

    提出率 = その月に 1 回以上出した看板の枚数 ÷ そのラインの看板の枚数(いま)。
    届くまでの平均日数は、**その月に出した回**のうち届いたものの平均。
    """
    submitted: dict[tuple[str, str], set[str]] = defaultdict(set)
    count: dict[tuple[str, str], int] = defaultdict(int)
    grouped: dict[tuple[str, str], list[Cycle]] = defaultdict(list)
    for c in cycles:
        key = (c.line, month_key(c.ordered_at))
        submitted[key].add(c.mgmt_no)
        count[key] += 1
        grouped[key].append(c)
    out: dict[str, list[dict[str, Any]]] = {}
    for line in lines:
        total = totals.get(line, 0)
        rows = []
        for month in months:
            n = len(submitted.get((line, month), ()))
            rows.append({
                "month": month,
                "total": total,
                "submitted": n,
                "count": count.get((line, month), 0),
                "rate": round(n / total * 100, 1) if total else None,
                **lead_stats(grouped.get((line, month), ())),
            })
        out[line] = rows
    return out


def line_summary(cycles: Sequence[Cycle], months: Sequence[str], lines: Sequence[str]) -> list[dict[str, Any]]:
    """ラインごとの要約(期間の中): 回数・週平均・月平均・届くまで・次に出すまでの平均日数。"""
    weeks = max(1, round(len(months) * 30.44 / 7))
    out = []
    for line in lines:
        mine = [c for c in cycles if c.line == line]
        nxt = [c.days_to_next for c in mine if c.days_to_next is not None]
        out.append({
            "line": line,
            "label": config.display_name(line),
            "count": len(mine),
            "per_month": round(len(mine) / max(1, len(months)), 1),
            "per_week": round(len(mine) / weeks, 1),
            **lead_stats(mine),
            "avg_next_days": _mean(nxt),
            "open": sum(1 for c in mine if c.delivered_at is None),
            **hold_stats(mine),
            **warehouse_stats(mine),
        })
    return out


def _mean_f(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def hold_stats(cycles: Sequence[Cycle]) -> dict[str, Any]:
    """注文中(倉庫の在庫切れ)の回数・日数。``holds`` = 回数、``held_kanbans`` = 何枚の看板で。"""
    periods = [(c, a, b) for c in cycles for a, b in c.holds]
    done = [_days(a, b) for _c, a, b in periods if b is not None]
    done = [d for d in done if d is not None]
    return {
        "holds": len(periods),
        "held_kanbans": len({(c.line, c.mgmt_no) for c, _a, _b in periods}),
        "holds_open": sum(1 for _c, _a, b in periods if b is None),
        "avg_hold_days": _mean(done),
        "max_hold_days": max(done) if done else None,
    }


def warehouse_stats(cycles: Sequence[Cycle]) -> dict[str, Any]:
    """出してから倉庫の画面に出るまで・出てから発送までの平均(時間)。"""
    seen = [c.hours_to_seen for c in cycles if c.hours_to_seen is not None]
    ship = [c.hours_seen_to_ship for c in cycles if c.hours_seen_to_ship is not None]
    return {"avg_seen_hours": _mean_f(seen), "avg_seen_to_ship_hours": _mean_f(ship),
            "seen_count": len(seen)}


def per_kanban_holds(cycles: Sequence[Cycle], now: datetime | None = None) -> list[dict[str, Any]]:
    """看板ごとの注文中(在庫切れ)の回数・日数。**回数の多い看板から**(よく切れる資材が上)。"""
    now = now or datetime.now()
    groups: dict[tuple[str, str], list[Cycle]] = defaultdict(list)
    for c in cycles:
        if c.holds:
            groups[(c.line, c.mgmt_no)].append(c)
    rows = []
    for (line, mgmt_no), mine in groups.items():
        last = mine[-1]
        st = hold_stats(mine)
        open_since = next((a for c in reversed(mine) for a, b in reversed(c.holds) if b is None), None)
        rows.append({
            "line": line, "label": config.display_name(line), "mgmt_no": mgmt_no,
            "material": last.material, "size": last.size, **st,
            "total_hold_days": sum(c.hold_days or 0 for c in mine),
            "open_since": open_since.strftime("%Y/%m/%d") if open_since else "",
            "open_days": _days(open_since, now) if open_since else None,
        })
    rows.sort(key=lambda r: (-r["holds"], -(r["total_hold_days"] or 0), _line_order(r["line"]),
                             _natural(r["mgmt_no"])))
    return rows


def hold_periods(cycles: Sequence[Cycle]) -> list[dict[str, Any]]:
    """注文中の 1 回ずつ(CSV 用)。"""
    out = []
    for c in cycles:
        for a, b in c.holds:
            out.append({"line": c.line, "mgmt_no": c.mgmt_no, "material": c.material, "size": c.size,
                        "ordered_at": c.ordered_at, "from": a, "to": b, "days": _days(a, b)})
    out.sort(key=lambda r: (_line_order(r["line"]), r["from"]))
    return out


def per_kanban_usage(cycles: Sequence[Cycle], months: Sequence[str]) -> list[dict[str, Any]]:
    """看板ごとの使用量(期間の中)。1 回の量 × 回数。**月あたりの多い順。**

    1 回の量はその回のサイズ欄から読む(途中で量を変えても、その回の量で足す)。
    """
    groups: dict[tuple[str, str], list[Cycle]] = defaultdict(list)
    for c in cycles:
        groups[(c.line, c.mgmt_no)].append(c)
    rows = []
    span = max(1, len(months))
    for (line, mgmt_no), mine in groups.items():
        last = mine[-1]
        qty, unit = last.quantity
        amounts = [c.quantity for c in mine]
        total = sum(q for q, _u in amounts if q is not None)
        readable = sum(1 for q, _u in amounts if q is not None)
        rows.append({
            "line": line, "label": config.display_name(line), "mgmt_no": mgmt_no,
            "material": last.material, "size": last.size,
            "per_order": _num_text(qty), "unit": unit, "count": len(mine),
            "total": _num_text(total) if readable else None,
            "per_month": _num_text(round(total / span, 1)) if readable else None,
            "unreadable": len(mine) - readable,
        })
    rows.sort(key=lambda r: (r["per_month"] is None, -(float(r["per_month"] or 0) if r["per_month"] else 0),
                             _line_order(r["line"]), _natural(r["mgmt_no"])))
    return rows


def _num_text(v: float | None) -> float | int | None:
    if v is None:
        return None
    return int(v) if float(v).is_integer() else round(v, 1)


def per_kanban_counts(cycles: Sequence[Cycle], by: str) -> list[dict[str, Any]]:
    """看板ごとの回数を、週(``week``)か月(``month``)で数える。"""
    counts: dict[tuple, int] = defaultdict(int)
    names: dict[tuple[str, str], tuple[str, str]] = {}
    for c in cycles:
        period = week_start(c.ordered_at).strftime("%Y/%m/%d") if by == "week" else month_key(c.ordered_at)
        counts[(c.line, c.mgmt_no, period)] += 1
        names[(c.line, c.mgmt_no)] = (c.material, c.size)
    rows = []
    for (line, mgmt_no, period), n in counts.items():
        material, size = names[(line, mgmt_no)]
        rows.append({"line": line, "mgmt_no": mgmt_no, "material": material, "size": size,
                     "period": period, "count": n})
    rows.sort(key=lambda r: (_line_order(r["line"]), r["period"], _natural(r["mgmt_no"])))
    return rows


def per_kanban_lead(cycles: Sequence[Cycle]) -> list[dict[str, Any]]:
    """看板ごとの、届くまでの日数(平均・最短・最長)と次に出すまでの平均日数。

    平均の長い順(届くのが遅い看板が上)。まだ 1 回も届いていない看板は下。
    """
    groups: dict[tuple[str, str], list[Cycle]] = defaultdict(list)
    for c in cycles:
        groups[(c.line, c.mgmt_no)].append(c)
    rows = []
    for (line, mgmt_no), mine in groups.items():
        last = mine[-1]
        nxt = [c.days_to_next for c in mine if c.days_to_next is not None]
        rows.append({
            "line": line, "label": config.display_name(line), "mgmt_no": mgmt_no,
            "material": last.material, "size": last.size,
            "count": len(mine),
            **lead_stats(mine),
            "avg_next_days": _mean(nxt),
            "open": sum(1 for c in mine if c.delivered_at is None),
        })
    rows.sort(key=lambda r: (
        r["avg_deliver_days"] is None, -(r["avg_deliver_days"] or 0),
        _line_order(r["line"]), _natural(r["mgmt_no"]),
    ))
    return rows


def _natural(text: str) -> tuple:
    try:
        return (0, float(text), "")
    except ValueError:
        return (1, 0.0, text)


# ------------------------------------------------------------------
# CSV
# ------------------------------------------------------------------
CSV_KINDS = {
    "cycles": "看板集計_明細",
    "weekly": "看板集計_週",
    "monthly": "看板集計_月",
    "rate": "看板集計_提出率",
    "lead": "看板集計_届くまでの日数",
    "summary": "看板集計_ライン要約",
    "history": "看板履歴",
    "holds": "看板集計_注文中",
    "usage": "看板集計_使用量",
}

#: 「看板履歴」の CSV の列(看板履歴.sqlite3 の列をそのまま。ライン名だけ読める名前を足す)
HISTORY_CSV_COLUMNS = ("日時", "ライン", "ライン名", "管理番号", "出来事", "資材", "サイズ",
                       "出した日時", "端末", "ID")


def _fmt(d: datetime | None) -> str:
    return d.strftime("%Y/%m/%d %H:%M") if d else ""


def _num(v: Any) -> str:
    return "" if v is None else str(v)


def build_csv(kind: str, data: Dataset, months: Sequence[str], line: str = "",
              mistake_minutes: int | None = None) -> str:
    """CSV の中身(文字列)。**Excel で開けるよう、呼ぶ側で BOM 付き UTF-8 にする。**"""
    lines = [line] if line else [c for c in config.all_line_codes() if c in data.totals
                                 or any(e.line == c for e in data.events)]
    all_cycles = build_cycles(data.events, mistake_minutes)
    cycles = in_period(all_cycles, months, line)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    if kind == "cycles":
        w.writerow(["ライン", "管理番号", "資材", "サイズ", "出した日時", "発送日時", "届いた日時",
                    "発送までの日数", "届くまでの日数", "次に出した日時", "次に出すまでの日数", "コメント",
                    "倉庫に出た日時", "倉庫に出るまでの時間", "出てから発送までの時間",
                    "注文中の回数", "注文中の日数", "数量", "単位"])
        # 期間の外の回も渡す(前の回が届いた日から、この回のコメントが始まる)
        notes = comments_by_cycle(all_cycles, data.comments)
        for c in cycles:
            qty, unit = c.quantity
            w.writerow([config.display_name(c.line), c.mgmt_no, c.material, c.size,
                        _fmt(c.ordered_at), _fmt(c.shipped_at), _fmt(c.delivered_at),
                        _num(c.days_to_ship), _num(c.days_to_deliver),
                        _fmt(c.next_ordered_at), _num(c.days_to_next), notes.get(id(c), ""),
                        _fmt(c.seen_at), _num(c.hours_to_seen), _num(c.hours_seen_to_ship),
                        len(c.holds), _num(c.hold_days), _num(_num_text(qty)), unit])
    elif kind in ("weekly", "monthly"):
        by = "week" if kind == "weekly" else "month"
        w.writerow(["ライン", "管理番号", "資材", "サイズ",
                    "週(月曜日)" if by == "week" else "月", "出した回数"])
        for r in per_kanban_counts(cycles, by):
            w.writerow([config.display_name(r["line"]), r["mgmt_no"], r["material"], r["size"],
                        r["period"], r["count"]])
    elif kind == "rate":
        w.writerow(["ライン", "月", "看板の枚数", "出した看板の枚数", "提出率(%)", "出した回数",
                    "届いた回数", "届くまでの平均日数"])
        table = rate_table(cycles, data.totals, months, lines)
        for code in lines:
            for r in table[code]:
                w.writerow([config.display_name(code), r["month"], r["total"], r["submitted"],
                            _num(r["rate"]), r["count"], r["delivered"], _num(r["avg_deliver_days"])])
    elif kind == "lead":
        w.writerow(["ライン", "管理番号", "資材", "サイズ", "出した回数", "届いた回数",
                    "届くまでの平均日数", "最短日数", "最長日数", "次に出すまでの平均日数", "未着"])
        for r in per_kanban_lead(cycles):
            w.writerow([r["label"], r["mgmt_no"], r["material"], r["size"], r["count"], r["delivered"],
                        _num(r["avg_deliver_days"]), _num(r["min_deliver_days"]),
                        _num(r["max_deliver_days"]), _num(r["avg_next_days"]), r["open"]])
    elif kind == "summary":
        w.writerow(["ライン", "看板の枚数", "出した回数", "週平均", "月平均", "届いた回数",
                    "届くまでの平均日数", "最短日数", "最長日数", "次に出すまでの平均日数", "未着",
                    "注文中の回数", "注文中の平均日数", "倉庫に出るまでの平均時間", "出てから発送までの平均時間"])
        for r in line_summary(cycles, months, lines):
            w.writerow([r["label"], data.totals.get(r["line"], 0), r["count"], r["per_week"],
                        r["per_month"], r["delivered"], _num(r["avg_deliver_days"]),
                        _num(r["min_deliver_days"]), _num(r["max_deliver_days"]),
                        _num(r["avg_next_days"]), r["open"], r["holds"], _num(r["avg_hold_days"]),
                        _num(r["avg_seen_hours"]), _num(r["avg_seen_to_ship_hours"])])
    elif kind == "holds":
        w.writerow(["ライン", "管理番号", "資材", "サイズ", "出した日時", "注文中にした日時",
                    "外した日時", "注文中の日数"])
        for r in hold_periods(cycles):
            w.writerow([config.display_name(r["line"]), r["mgmt_no"], r["material"], r["size"],
                        _fmt(r["ordered_at"]), _fmt(r["from"]), _fmt(r["to"]) or "(注文中)", _num(r["days"])])
    elif kind == "usage":
        # 月ごと(Excel で並べ替え・集計しやすいように縦に長い形)
        w.writerow(["ライン", "管理番号", "資材", "サイズ", "月", "出した回数", "数量の合計", "単位",
                    "数量の読めない回"])
        acc: dict[tuple, list] = {}
        for c in cycles:
            qty, unit = c.quantity
            key = (_line_order(c.line), c.line, _natural(c.mgmt_no), c.mgmt_no, month_key(c.ordered_at))
            row = acc.setdefault(key, [c.material, c.size, 0, 0.0, unit, 0])
            row[0], row[1] = c.material, c.size
            row[2] += 1
            if qty is None:
                row[5] += 1
            else:
                row[3] += qty
                row[4] = unit or row[4]
        for key in sorted(acc):
            material, size, n, total, unit, bad = acc[key]
            w.writerow([config.display_name(key[1]), key[3], material, size, key[4], n,
                        _num(_num_text(total)) if n > bad else "", unit, bad])
    elif kind == "history":
        # 看板履歴そのもの(集計する前の 1 件ずつ)。期間は「日時」の月、ラインで絞る。古い順
        w.writerow(HISTORY_CSV_COLUMNS)
        wanted = set(months)
        rows = []
        for r in data.history_rows:
            at = models.parse_datetime(str(r.get("日時") or ""))
            if at is None or month_key(at) not in wanted:
                continue
            if line and str(r.get("ライン") or "") != line:
                continue
            rows.append((at, r))
        # 同じ日時(同じ秒)の中は、書かれた順のまま(並べ替えは安定)
        rows.sort(key=lambda x: x[0])
        for _at, r in rows:
            code = str(r.get("ライン") or "")
            w.writerow([_text(r.get("日時")), code, config.display_name(code) if code else "",
                        _text(r.get("管理番号")), _text(r.get("出来事")), _text(r.get("資材")),
                        _text(r.get("サイズ")), _text(r.get("出した日時")), _text(r.get("端末")),
                        _text(r.get("ID"))])
    else:
        raise ValueError(f"知らない種類です: {kind}")
    return buf.getvalue()


def _text(v: Any) -> str:
    return "" if v is None else str(v)


def csv_file_name(kind: str, months: Sequence[str], line: str = "") -> str:
    """書き出すファイルの名前(拡張子なし)。ダウンロードもフォルダへの保存も同じ名前。"""
    name = f"{CSV_KINDS[kind]}_{months[0]}_{months[-1]}"
    if line:
        name += f"_{config.display_name(line)}"
    return name


# ------------------------------------------------------------------
# 画面
# ------------------------------------------------------------------
def view(data: Dataset, months: Sequence[str], line: str = "",
         mistake_minutes: int | None = None) -> dict[str, Any]:
    """「看板集計」タブに出すもの。"""
    all_lines = [c for c in config.all_line_codes()
                 if c in data.totals or any(e.line == c for e in data.events)]
    lines = [line] if line else all_lines
    minutes = MISTAKE_MINUTES if mistake_minutes is None else max(0, int(mistake_minutes))
    cycles = in_period(build_cycles(data.events, minutes), months, line)
    first = min((e.at for e in data.events), default=None)
    return {
        "available": data.available,
        "why": data.why,
        "months": list(months),
        # slot = 色の番号。**ラインの並び(LINE_MASTER)で決める** ── 絞り込んでも
        # 残ったラインの色が変わらないように(順位や表示順で塗らない)
        "lines": [{"code": c, "label": config.display_name(c), "total": data.totals.get(c, 0),
                   "slot": config.all_line_codes().index(c)}
                  for c in all_lines],
        "selected_lines": lines,
        "rates": rate_table(cycles, data.totals, months, lines),
        "summary": line_summary(cycles, months, lines),
        "overall": lead_stats(cycles),
        "kanbans": per_kanban_lead(cycles),
        "cycle_count": len(cycles),
        "recorded_since": first.strftime("%Y/%m/%d") if first else "",
        "holds_overall": hold_stats(cycles),
        "warehouse": warehouse_stats(cycles),
        "hold_kanbans": per_kanban_holds(cycles),
        "usage": per_kanban_usage(cycles, months),
        "mistake_minutes": minutes,
        "history_path": data.history_path,
    }
