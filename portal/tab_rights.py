"""タブ表示権限 ── どのツールのタブを、この端末に出すか

**python-web-tools の「アクセス権限」と同じ考え方の、別の表**(`タブ表示権限`)。
同じ共有の DB(既定 `梱包資材マスタ.sqlite3`)に置くが、アクセス権限の表には混ぜない
(アクセス権限は各ツールがモードやラインを決めるのに使っている。タブの出し入れを
そこへ足すと、どの行がどのツールのためのものか読めなくなる)。

    管理番号 | ログインID | PC名        | 表示タブ           | 既定タブ | 有効 | 備考
    -------- | ---------- | ----------- | ------------------ | -------- | ---- | ----------
        1    |            | LINE1-PC    | 日報, 看板         | 日報     |  1   | 1ライン
        2    |            | WH-PC       | 看板               |          |  1   | 倉庫
        3    | yamada     |             | すべて             |          |  1   | 管理者

【決め方】(アクセス権限と同じ)
- 空欄は「問わない」。大文字小文字・全角半角は区別しない。ワイルドカードは無い
- ログインIDもPC名も空の行は**効かない**(全員に効いてしまうので、書かれていても使わない)
- 当てはまる行の表示タブを**全部足す**(打ち消す行は無い)。`有効=0` の行は飛ばす
- 表示タブは名前(日報 / 看板 / カレンダー / 点検表、正式名・英字の名前でも可)を
  `,` `、` `・` 空白などで区切って並べる。`すべて` は全ツール
- 既定タブ(起動したときに開くタブ)は、当てはまる行のうち**いちばん絞った行**
  (ID と PC の両方 → PC だけ → ID だけ)のもの。無ければ前回開いていたタブ

【当てはまる行が無いとき】
- 表に行がある(= 運用を始めている)のに、この端末の行が無い → **ツールのタブは出さない。**
  大設定に「この端末が登録されていません」と、写せる形で ID と PC名 を出す
- 表がまだ無い・1行も無い・共有に届かず手元の写しも無い → **すべて出す**
  (ラインを止めない。タブの出し入れは見せ方で、業務の守りは各ツールが持っている)
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from .catalog import Catalog, fold
from .identity import Identity

TABLE = "タブ表示権限"

#: 列(順番どおりに作る)。`管理番号` は取り込み元の行の番号ではなく、表の主キー
COLUMNS: tuple[tuple[str, str], ...] = (
    ("管理番号", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("ログインID", "TEXT DEFAULT ''"),
    ("PC名", "TEXT DEFAULT ''"),
    ("表示タブ", "TEXT DEFAULT ''"),
    ("既定タブ", "TEXT DEFAULT ''"),
    ("有効", "INTEGER DEFAULT 1"),
    ("備考", "TEXT DEFAULT ''"),
)
EDITABLE = ("ログインID", "PC名", "表示タブ", "既定タブ", "有効", "備考")

#: 全ツールを指す書き方
ALL_WORDS = frozenset(fold(w) for w in ("すべて", "全部", "全て", "*", "all", "ALL"))

#: 表示タブの区切り
SEPARATORS = re.compile(r"[\s,、，;；|｜/／・]+")

#: 打ち間違いとみなす近さ(`difflib` の比)
TYPO_CUTOFF = 0.6

# 決まり方(画面と記録に出す)
SOURCE_ROWS = "rows"                # 当てはまる行で決めた
SOURCE_UNREGISTERED = "unregistered"  # 行はあるが、この端末の行が無い
SOURCE_UNCONFIGURED = "unconfigured"  # 表が無い・行が無い
SOURCE_UNREADABLE = "unreadable"      # 共有に届かず、手元の写しも無い


def _clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _flag(value: Any) -> bool:
    """有効の欄。空・NULL は有効。0/false/無効/× などは無効(Access の -1 は有効)。"""
    text = fold(_clean(value))
    if text == "":
        return True
    return text not in {"0", "false", "no", "n", "off", "無効", "いいえ", "×", "x"}


@dataclass
class Rule:
    """表の1行。空欄は「問わない」。"""

    key: Optional[int] = None
    login_id: str = ""
    pc_name: str = ""
    tabs: str = ""
    default_tab: str = ""
    enabled: bool = True
    note: str = ""

    @classmethod
    def from_row(cls, row: dict) -> "Rule":
        key = row.get("__行", row.get("管理番号"))
        return cls(
            key=int(key) if str(key or "").lstrip("-").isdigit() else None,
            login_id=_clean(row.get("ログインID")),
            pc_name=_clean(row.get("PC名")),
            tabs=_clean(row.get("表示タブ")),
            default_tab=_clean(row.get("既定タブ")),
            enabled=_flag(row.get("有効")),
            note=_clean(row.get("備考")),
        )

    def has_condition(self) -> bool:
        return bool(self.login_id or self.pc_name)

    def matches(self, identity: Identity) -> bool:
        # 条件が1つも無い行は全員に効いてしまう。書かれていても効かせない
        if not self.enabled or not self.has_condition():
            return False
        if self.login_id and fold(self.login_id) != fold(identity.login_id):
            return False
        if self.pc_name and fold(self.pc_name) != fold(identity.pc_name):
            return False
        return True

    def specificity(self) -> int:
        """どれだけ絞った行か(ID と PC の両方 2 > PC だけ 1 > ID だけ 0)。"""
        if self.login_id and self.pc_name:
            return 2
        return 1 if self.pc_name else 0

    def words(self) -> list[str]:
        return [w for w in SEPARATORS.split(self.tabs) if w]

    def tab_ids(self, catalog: Catalog) -> tuple[list[str], list[str]]:
        """この行が出すツール(表の並び)と、読めなかった語。"""
        ids: list[str] = []
        unknown: list[str] = []
        for word in self.words():
            if fold(word) in ALL_WORDS:
                ids.extend(catalog.ids())
                continue
            tool = catalog.find(word)
            if tool is None:
                unknown.append(word)
            else:
                ids.append(tool.id)
        order = catalog.ids()
        return sorted(set(ids), key=order.index), unknown

    def condition_label(self) -> str:
        parts = []
        if self.login_id:
            parts.append(f"ID {self.login_id}")
        if self.pc_name:
            parts.append(f"PC {self.pc_name}")
        return " かつ ".join(parts) if parts else "(条件なし)"

    def to_dict(self, catalog: Optional[Catalog] = None) -> dict:
        data = {"key": self.key, "login_id": self.login_id, "pc_name": self.pc_name,
                "tabs": self.tabs, "default_tab": self.default_tab, "enabled": self.enabled,
                "note": self.note, "condition": self.condition_label()}
        if catalog is not None:
            ids, unknown = self.tab_ids(catalog)
            data["tab_ids"] = ids
            data["unknown"] = unknown
        return data

    def values(self) -> dict:
        """表へ書く値(列名 → 値)。"""
        return {"ログインID": self.login_id, "PC名": self.pc_name, "表示タブ": self.tabs,
                "既定タブ": self.default_tab, "有効": 1 if self.enabled else 0, "備考": self.note}


@dataclass
class Decision:
    """この端末に出すタブと、そう決めた理由。"""

    tabs: list[str]
    default_tab: str
    source: str
    reason: str
    matched: list[Rule] = field(default_factory=list)

    def to_dict(self, catalog: Catalog) -> dict:
        return {"tabs": list(self.tabs), "default_tab": self.default_tab,
                "source": self.source, "reason": self.reason,
                "matched": [r.to_dict(catalog) for r in self.matched]}


def decide(rules: Iterable[Rule], identity: Identity, catalog: Catalog, *,
           table_state: str = "ok") -> Decision:
    """この端末に出すタブを決める。

    `table_state`: `ok`(表を読めた。空でもよい)/ `missing`(取り込み元に表が無い)/
    `unreadable`(共有に届かず、手元の写しも無い)
    """
    everything = catalog.ids()
    if table_state == "unreadable":
        return Decision(everything, "", SOURCE_UNREADABLE,
                        "共有のタブ表示権限を読めず、手元の写しもありません。"
                        "すべてのタブを出しています(ラインを止めないため)。")
    if table_state == "missing":
        return Decision(everything, "", SOURCE_UNCONFIGURED,
                        "共有の DB にタブ表示権限の表がまだありません。すべてのタブを出しています。"
                        "大設定の「表を作る」で作れます。")
    rules = list(rules)
    effective = [r for r in rules if r.enabled and r.has_condition()]
    if not effective:
        return Decision(everything, "", SOURCE_UNCONFIGURED,
                        "タブ表示権限にまだ1行も登録がありません。すべてのタブを出しています。")
    matched = [r for r in effective if r.matches(identity)]
    if not matched:
        return Decision([], "", SOURCE_UNREGISTERED,
                        f"この端末(ログインID={identity.login_id or '(不明)'} / "
                        f"PC名={identity.pc_name or '(不明)'})はタブ表示権限に登録がありません。"
                        "大設定で登録すると、ツールのタブが出ます。", [])
    chosen: set[str] = set()
    for rule in matched:
        chosen.update(rule.tab_ids(catalog)[0])
    tabs = [t for t in everything if t in chosen]
    default = ""
    for rule in sorted(matched, key=lambda r: -r.specificity()):
        tool = catalog.find(rule.default_tab) if rule.default_tab else None
        if tool is not None and tool.id in tabs:
            default = tool.id
            break
    labels = "、".join(r.condition_label() for r in matched)
    reason = (f"タブ表示権限の {len(matched)} 行({labels})で決めました。"
              if tabs else
              f"タブ表示権限の当てはまる行({labels})に、出すタブが書かれていません。")
    return Decision(tabs, default, SOURCE_ROWS, reason, matched)


def problems(rules: Iterable[Rule], catalog: Catalog) -> list[str]:
    """表の書き方の問題(大設定に出す)。効き方は変えない。"""
    found: list[str] = []
    known = sorted({w for t in catalog.tools for w in t.words()})
    for rule in rules:
        where = f"管理番号 {rule.key}" if rule.key is not None else rule.condition_label()
        if rule.enabled and not rule.has_condition():
            found.append(f"{where}: ログインIDもPC名も空なので効きません(全員に効いてしまうため)。")
        _, unknown = rule.tab_ids(catalog)
        for word in unknown:
            near = difflib.get_close_matches(fold(word), known, n=1, cutoff=TYPO_CUTOFF)
            hint = ""
            if near:
                tool = catalog.find(near[0])
                hint = f"(「{tool.title}」のことですか?)" if tool else ""
            found.append(f"{where}: 表示タブの「{word}」が分かりません{hint}。")
        if rule.default_tab and catalog.find(rule.default_tab) is None:
            found.append(f"{where}: 既定タブの「{rule.default_tab}」が分かりません。")
    return found


def canonical_tabs(ids: Iterable[str], catalog: Catalog) -> str:
    """表に書く表示タブの文字(大設定の画面で選んだもの)。全部なら `すべて`。"""
    chosen = [t for t in catalog.ids() if t in set(ids)]
    if chosen and len(chosen) == len(catalog.ids()):
        return "すべて"
    return ", ".join(catalog.by_id(t).title for t in chosen)
