"""コンバートの決め方 ── 旧名の表(VBA)のページを、正規の名前の表へどう写すか(v4.13.0)

    ラインごと・並行になるので本ツールは正規名、VBAは旧で蓄積されてしまうと思います
    とはいえ … これがあれば任意のタイミングで変えれるんですよね？

【なにをするか】
VBA は共有の日報データに、前の名前の表(`T_日報ヘッダー_LS`)で書き続けます。
このツールは v4.13.0 から正規の名前の表(`T_日報ヘッダー_機側`)を読み書きします。
コンバートは**前の表のページを、正規の表へ写す**だけです。前の表には触りません
(VBA はそのまま動き、何度でもやり直せる)。

【何回押しても同じ ── ページごとに決める】
並行のあいだは何度も押します。押すたびに、ページ(報告日・直・ページ)ごとに
次のどれかに決めます(`decide`):

    写す            正規の表に無い                                  → 写す
    写し直す        前に写したまま(このツールが触っていない)で、
                    VBA の側が直された(保存日時が変わった)          → 入れ替える
    写し済み        前に写したまま、VBA の側も変わっていない          → 何もしない
    このツールの分  正規の表のページを、このツールが書いた            → **残す**
    このツールで消した 前に写したのに、正規の表から無くなっている    → 写し直さない
    VBA で消した    前に写したまま、VBA の側から無くなった            → 写しも消す

「前に写したまま」かどうかは、写したときの保存日時を控えの表(`T_ライン名変換`)
に残しておき、正規の表の保存日時と比べて見分けます。このツールが共有へ保存すると
保存日時が変わるので、**このツールで書いたページは上書きしません。**

【ページの名前を揃える】
ページ・行番号は、VBA が数で入れても字で入れても**数に揃えて**写します。この
ツールは数で書くので、揃えないと同じページが2つ(`1` と `'1'`)になります。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional

#: ページの名前(報告日, 直, ページ)
PageKey = tuple[str, str, object]

COPY = "写す"
RECOPY = "写し直す"
SAME = "写し済み"
KEEP = "このツールの分"
DELETED_HERE = "このツールで消した"
GONE = "VBA で消した"

#: 画面に出す順
KINDS: tuple[str, ...] = (COPY, RECOPY, SAME, KEEP, DELETED_HERE, GONE)


def number_of(value: object) -> object:
    """ページ・行番号を**数に揃える**(`1` / `1.0` / `'1'` / `'１'` → 1)。数でなければ字のまま。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    import unicodedata

    text = unicodedata.normalize("NFKC", str(value)).strip()
    if text.lstrip("-").isdigit():
        return int(text)
    try:
        number = float(text)
    except ValueError:
        return text
    return int(number) if number.is_integer() else text


def text_of(value: object) -> str:
    """報告日・直・保存日時を比べる形(None は空)。"""
    return "" if value is None else str(value)


def page_key(row: Mapping[str, object]) -> PageKey:
    """表の1行 → そのページの名前。"""
    return (text_of(row.get("報告日")), text_of(row.get("直")), number_of(row.get("ページ")))


@dataclass
class Plan:
    """1つのラインで、ページごとに決めたこと。"""

    pages: dict[PageKey, str] = field(default_factory=dict)

    def keys(self, kind: str) -> list[PageKey]:
        return sorted((k for k, v in self.pages.items() if v == kind), key=_sort_key)

    def count(self, kind: str) -> int:
        return sum(1 for v in self.pages.values() if v == kind)

    def counts(self) -> dict[str, int]:
        return {kind: self.count(kind) for kind in KINDS}

    @property
    def writes(self) -> bool:
        """正規の表に書くことがあるか(写す・写し直す・VBA で消した)。"""
        return any(self.count(kind) for kind in (COPY, RECOPY, GONE))


def _sort_key(key: PageKey) -> tuple:
    date, shift, page = key
    return (date, shift, (0, page) if isinstance(page, int) else (1, str(page)))


def decide(old: Mapping[PageKey, str], new: Mapping[PageKey, str],
           copied: Mapping[PageKey, str]) -> Plan:
    """ページごとに決める。

    `old`    前の表(VBA)のページ → 保存日時(見出しが無ければ空)
    `new`    正規の表のページ → 保存日時
    `copied` 前に写したページ → 写したときの保存日時(控えの表)
    """
    plan = Plan()
    for key, saved in old.items():
        mine = copied.get(key)
        if key not in new:
            # 前に写したのに無い = このツールで消した(写し直さない)
            plan.pages[key] = DELETED_HERE if mine is not None else COPY
        elif mine is not None and new[key] == mine:
            plan.pages[key] = SAME if saved == mine else RECOPY
        else:
            plan.pages[key] = KEEP
    for key, mine in copied.items():
        if key in old:
            continue
        if key in new and new[key] == mine:
            plan.pages[key] = GONE            # 写したまま、VBA の側から無くなった
    return plan


@dataclass(frozen=True)
class Check:
    """写したあとの数の突き合わせ(1つのライン)。"""

    expected_pages: int
    found_pages: int
    expected_rows: int
    found_rows: int

    @property
    def ok(self) -> bool:
        return (self.expected_pages == self.found_pages
                and self.expected_rows == self.found_rows)

    def describe(self) -> str:
        return (f"見出し {self.found_pages}/{self.expected_pages} ・"
                f"明細 {self.found_rows}/{self.expected_rows}")


def check(expected: Mapping[PageKey, tuple[bool, int]],
          found: Mapping[PageKey, tuple[bool, int]]) -> Check:
    """写したページの数を、前の表と比べる。`(見出しがあるか, 明細の行数)` を並べたもの。"""
    want_pages = sum(1 for has, _ in expected.values() if has)
    want_rows = sum(rows for _, rows in expected.values())
    got = [found.get(key, (False, 0)) for key in expected]
    return Check(expected_pages=want_pages,
                 found_pages=sum(1 for has, _ in got if has),
                 expected_rows=want_rows,
                 found_rows=sum(rows for _, rows in got))


def describe_key(key: PageKey, line: str = "") -> str:
    """「2026年10月2日 機側 1直 2ページ」の形。"""
    date, shift, page = key
    return " ".join(part for part in (date, line, shift, f"{page}ページ") if part)


def first_problem(plan: Plan) -> Optional[str]:
    """気をつけてほしいこと(このツールの分が前の表にもある、など)。無ければ None。"""
    keep = plan.count(KEEP)
    if keep:
        return (f"{keep}ページは正規の表にこのツールで書いた分があるので、写していません"
                "(このツールの分を残します)")
    return None
