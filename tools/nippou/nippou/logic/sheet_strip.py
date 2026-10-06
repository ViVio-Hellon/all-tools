"""紙の束 ── その日のライン1本ぶんの紙を、直→ページの順に並べる

【なぜ要るのか】

    直終わりに保存が済み　次直が登録すると前直分はどうなりますか？
    消えるのですか？
    VBAのときはシートは残っているし発行すればシートが切り替わって
    まっさらになるので視覚的に変わった、新規だ　とわかるのですが
    本ツールはそこがわかりにくく　終わっているのか　終わっていないのか
    切り替わったのかそうでないのか　ここが不明瞭

**消えません。** 紙は `報告日・ライン・直・ページ` を鍵にした別々の
記録で、次の直が作業者を登録しても前の直の紙には触りません
(`db/repository.save` の DELETE も、その4つが一致する紙にしか当たり
ません)。中身の持ち方は VBA と同じです。

違うのは**見え方**でした。VBA には Excel のシートタブが並んでいて、
**紙の束が見えていました** ── 発行すれば束に1枚増えて、そこへ移る。
移ったことも、前の紙が残っていることも、目で分かります。

こちらの画面が出していたのは「2直 1ページ目 / 全1ページ」だけで、
しかもこの「全1ページ」は**いまの直のぶんしか数えていません**。
1直の紙は同じ日の同じラインに在るのに、入力画面のどこにも姿が無い。
だから「消えたのか」「切り替わったのか」が読めませんでした。

そこで束を並べます。

    [1直 1ページ 済] [1直 2ページ 済] [2直 1ページ 新 ← いま]

【この層の約束】
ここは**純粋**。DBも時計も触らず、渡された事実だけで並べます。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from .shift import SHIFT_NAMES, short_date as shift_short_date

#: 紙1枚の状態。**記号にしません** ── 「済」「未」だけ書いてあっても、
#: 何が済んでいるのかは読めません(停止記号でそれを言われました)。
STATE_SYNCED = "synced"
STATE_PENDING = "pending"
STATE_NEW = "new"
MARKS = {
    STATE_SYNCED: "共有済",     # 共有へ出た。その直はもう片付いている
    STATE_PENDING: "未送信",    # この端末には在るが、共有へ出ていない
    # 「新しい紙」と書いていました。**聞きなれない言い方でした** ──
    # 「新しい紙　とか聞きなれなさすぎる」。VBA が使っていた言葉に戻します
    STATE_NEW: "発行したて",    # まだ1度も保存していない
}

#: 押したときに何が起きるか
OPEN_NOTHING = ""      # いま開いている紙。押しても動かない
OPEN_PAGE = "page"     # 同じ直の別ページ。**その場で切り替える**
OPEN_PAPER = "paper"   # 別の直の紙。**読むだけ**で開く(誰でも)

#: 直の並び。紙に出る順と同じものを使う(`logic/shift.SHIFT_NAMES`)
_ORDER = {name: i for i, name in enumerate(SHIFT_NAMES)}


#: 「2026年9月12日」→「9月12日」。**書き方は1か所**(`logic/shift`)。
#: 札と帯で言い方が違うと、同じ日のことだと読めません。
short_date = shift_short_date


@dataclass(frozen=True)
class Sheet:
    """束のなかの紙1枚。**画面はこれを札にして並べるだけ。**"""

    shift: str
    page: int
    #: 札の頭に出す日付(「9月12日」)。空なら付けません
    short_date: str = ""
    #: いま開いている紙か
    now: bool = False
    #: 共有へ出たか
    synced: bool = False
    #: この端末に保存されているか(まだなら「発行したての紙」)
    saved: bool = True

    @property
    def state(self) -> str:
        if not self.saved:
            return STATE_NEW
        return STATE_SYNCED if self.synced else STATE_PENDING

    @property
    def mark(self) -> str:
        return MARKS[self.state]

    @property
    def label(self) -> str:
        """札に出る名前。**いつの・どの直の・何ページ目か**を書き切る。

            こういう書き方じゃなく xx月xx日xx直 １ページ目発行 とか
            わかるようにして

        「1直 1ページ」では、いつの話かが札から読めませんでした。
        報告日を渡してあれば頭に付けます(`logic/shift.format_short_date`)。
        """
        head = f"{self.short_date} " if self.short_date else ""
        return f"{head}{self.shift} {self.page}ページ目"

    def as_dict(self, *, current_shift: str = "") -> dict:
        return {"shift": self.shift, "page": self.page,
                "short_date": self.short_date, "now": self.now,
                "synced": self.synced, "saved": self.saved,
                "state": self.state, "mark": self.mark, "label": self.label,
                "open_as": self._open_as(current_shift),
                "note": self._note(current_shift)}

    def _open_as(self, current_shift: str) -> str:
        if self.now:
            return OPEN_NOTHING
        # **同じ直なら、その場で切り替える。** 自分がさっき打った紙を
        # 自分で直しているだけなので、管理者モードは要りません
        if current_shift and self.shift == current_shift:
            return OPEN_PAGE
        # **別の直は読むだけ。** 直すのは「記録を見る」から呼び出す道
        # (管理者モード)で、こちらは目で確かめるための口です
        return OPEN_PAPER

    def _note(self, current_shift: str) -> str:
        """札に添える一行。**押すと何が起きるかまで書く。**"""
        where = self._open_as(current_shift)
        if self.now:
            if not self.saved:
                return f"{self.label} ── いま開いているページです(まだ保存していません)"
            state = "共有へ出ています" if self.synced else "この端末にあります(共有へは未送信)"
            return f"{self.label} ── いま開いているページです。{state}"
        state = "共有へ出ています" if self.synced else "共有へ未送信です"
        if where == OPEN_PAGE:
            return f"{self.label} ── {state}。押すとこのページに切り替わります"
        return f"{self.label} ── {state}。押すと中身を読めます(直せません)"


@dataclass(frozen=True)
class Strip:
    """その日・そのラインの束ぜんぶ。"""

    report_date: str = ""
    line: str = ""
    current_shift: str = ""
    sheets: tuple[Sheet, ...] = field(default_factory=tuple)

    @property
    def count(self) -> int:
        return len(self.sheets)

    @property
    def summary(self) -> str:
        """束の上に出す一行。**「消えていない」を言葉でも言う。**

        **「前の直のぶんも残っています」は、本当に別の直があるときだけ。**
        同じ直の2ページ目が並んでいるだけのときにそう書くと、嘘になります。
        """
        if not self.sheets:
            return ""
        others = [s for s in self.sheets if not s.now]
        if not others:
            return "この日のページは、いま開いているこの1枚だけです"
        head = f"この日のページは{self.count}枚あります"
        if any(s.shift != self.current_shift for s in others):
            head += "(別の直のぶんも残っています ── 押すと読めます)"
        else:
            head += "(押すとそのページに切り替わります)"
        pending = sum(1 for s in others if s.saved and not s.synced)
        if pending:
            head += f"。うち{pending}枚は共有へ未送信です"
        return head

    def as_dict(self) -> dict:
        return {"report_date": self.report_date, "line": self.line,
                "count": self.count, "summary": self.summary,
                "sheets": [s.as_dict(current_shift=self.current_shift)
                           for s in self.sheets]}


def build(saved: Iterable[dict], *, report_date: str, line: str,
          current_shift: str = "", current_page: int = 0,
          shift_order: Optional[Sequence[str]] = None) -> Strip:
    """束を組む。

    `saved` はこの端末に保存されている紙(`repository.saved_keys` の形で
    ``report_date`` / ``line`` / ``shift`` / ``page`` / ``synced``)。
    **この日・このラインのぶんだけ**を拾います。

    いま開いている紙が保存済みの中に無ければ、**それも1枚として足します**
    ── 発行したての紙は保存されていませんが、束から消えていたら
    「切り替わった」が見えません。
    """
    if shift_order is not None:
        order = {name: i for i, name in enumerate(shift_order)}
    else:
        order = _ORDER

    short = short_date(report_date)

    found: dict[tuple[str, int], Sheet] = {}
    for row in saved or ():
        if str(row.get("report_date", "")) != report_date:
            continue
        if str(row.get("line", "")) != line:
            continue
        shift = str(row.get("shift", ""))
        try:
            page = int(row.get("page", 0))
        except (TypeError, ValueError):            # pragma: no cover - 壊れた行
            continue
        if not shift or page < 1:
            continue
        key = (shift, page)
        now = bool(current_shift and shift == current_shift
                   and page == current_page)
        found[key] = Sheet(shift=shift, page=page, now=now,
                           short_date=short, synced=bool(row.get("synced")),
                           saved=True)

    # **発行したての紙も束に置く。** ここが抜けると、次ページを出した
    # 直後や直が変わった直後に、いまの紙だけ姿が無くなります
    if current_shift and current_page >= 1:
        key = (current_shift, current_page)
        if key not in found:
            found[key] = Sheet(shift=current_shift, page=current_page,
                               short_date=short, now=True, synced=False,
                               saved=False)

    sheets = sorted(found.values(),
                    key=lambda s: (order.get(s.shift, len(order)),
                                   s.shift, s.page))
    return Strip(report_date=report_date, line=line,
                 current_shift=current_shift, sheets=tuple(sheets))
