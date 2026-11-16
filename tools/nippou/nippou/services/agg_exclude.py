"""集計から除く作業者・班(v4.50.0)

    日報の集計から特定の班を除く、特定の作業者を除く機能を付けれますか？
    画面の集計、集計CSVから除きたいです
    複数の作業者がいて、その中に除く人が混ざっているとき: 混ざっているときも除外で
    (嘘を平気で入力するからです)

【表】梱包資材マスタの「集計から除く」(全端末で同じ。マスタ管理で直す)。無ければ空で作る。

    作業者   除く人の名前(班員名簿の名前)。空なら班の行
    班       除く班(班員名簿の班)。空なら作業者の行
    有効     0 なら読まない
    備考

【除き方】直の集計を作るところ(`summary.refresh_shift`)で、**作業者欄に除く人(または除く班の人)が
1人でもいるページ**を数えない。集計・グラフ・集計管理・集計CSV・共有の集計の表は、みなその集計を読むので、
全部から外れる。**日報(ページ)そのものは消さない・共有へ保存もそのまま。**
今までのデータは「集計を作り直す」で外れる(集計CSVも書き直す)。

班は**いまの班員名簿**で引く(過去にほかの班だった人も、いまの班で判断する)。
表が読めないときは前に読めた中身を使う(この起動で一度も読めていなければ除かない・記録に残す)。
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

from ..logging_setup import get_logger

log = get_logger("services.agg_exclude")

TABLE = "集計から除く"
SCHEMA = (f'CREATE TABLE IF NOT EXISTS "{TABLE}" ("管理番号" INTEGER PRIMARY KEY, '
          '"作業者" TEXT DEFAULT \'\', "班" TEXT DEFAULT \'\', "有効" INTEGER DEFAULT 1, "備考" TEXT DEFAULT \'\')')
#: 読み直すまでの秒(直の集計を何度も作るので、毎回共有を読まない)
CHECK_EVERY = 30.0

_SPLIT = re.compile(r"[\s・,、/]+")


def names_of(text: object) -> list[str]:
    return [n for n in _SPLIT.split(str(text or "").strip()) if n]


@dataclass
class Rules:
    workers: set[str] = field(default_factory=set)
    teams: set[str] = field(default_factory=set)
    #: 名前 → 班(班員名簿)。班の行があるときだけ読む
    roster: dict[str, str] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.workers or self.teams)

    def why(self, worker_text: object) -> str:
        """そのページを除く理由(除かなければ空)。"""
        for name in names_of(worker_text):
            if name in self.workers:
                return f"作業者 {name}"
            team = self.roster.get(name, "")
            if team and team in self.teams:
                return f"{team}班の {name}"
        return ""


def _enabled(value: object) -> bool:
    return str(value if value is not None else "").strip() not in ("0", "無効", "false", "False", "×")


def parse(rows: Iterable[dict]) -> Rules:
    out = Rules()
    for row in rows:
        if not _enabled(row.get("有効")):
            continue
        for name in names_of(row.get("作業者")):
            out.workers.add(name)
        team = str(row.get("班") or "").strip().removesuffix("班")
        if team:
            out.teams.add(team)
    return out


def ensure_table(path) -> None:
    """梱包資材マスタに表が無ければ空で作る(マスタ管理で行を足せるように)。"""
    from .. import source_db

    if TABLE in source_db.list_tables(path):
        return
    with source_db.connect(path) as conn:
        conn.execute(SCHEMA)
    log.info("梱包資材マスタに「%s」の表を作りました", TABLE)


_lock = threading.Lock()
_cache: dict = {}


def reset() -> None:
    with _lock:
        _cache.clear()


def load(*, force: bool = False) -> Rules:
    """いまの決まり。30秒は読み直さない。読めなければ前に読めた中身。"""
    from .. import source_db
    from ..config import SETTINGS

    now = time.monotonic()
    with _lock:
        got = _cache.get("rules")
        if got is not None and not force and now - _cache.get("at", 0) < CHECK_EVERY:
            return got
    path = SETTINGS.gw_material_master_path
    try:
        if TABLE not in source_db.list_tables(path, strict=True):
            try:
                ensure_table(path)
            except Exception:                     # noqa: BLE001 - 作れなくても集計は作る
                log.exception("「%s」の表を作れませんでした", TABLE)
            rules = Rules()
        else:
            rules = parse(source_db.read_table(path, TABLE))
        if rules.teams:
            from .standard_time import load_teams

            teams = load_teams()
            if teams.problem:
                raise source_db.SourceError(teams.problem)
            rules.roster = dict(teams)
    except Exception as exc:                      # noqa: BLE001 - 前に読めた中身で続ける
        with _lock:
            kept = _cache.get("rules")
        log.warning("「%s」を読めませんでした(%s): %s", TABLE,
                    "前に読めた中身で除きます" if kept is not None else "除かずに集計します", exc)
        return kept if kept is not None else Rules()
    with _lock:
        _cache.update(rules=rules, at=now)
    return rules


def keep(pages: list, rules: Optional[Rules] = None, *, where: str = "") -> list:
    """除くページを外したページの並び(`(見出し, 明細)`)。"""
    rules = load() if rules is None else rules
    if not rules:
        return pages
    out = []
    for header, details in pages:
        why = rules.why(getattr(header, "worker", ""))
        if why:
            log.info("集計から除きました %s 第%sページ(%s)", where, getattr(header, "page", "?"), why)
            continue
        out.append((header, details))
    return out
