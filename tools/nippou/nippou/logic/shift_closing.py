"""直の締めくくり ── **その画面で終わらせる**

    共有保存にまで持っていく導線も確保したい
    今のままでは忘れると思う
    vbaでは保存するとグラフが全画面で描写されるのでそれが合図に近い
    ものだったが、いまはなんのメリハリも無い

【何が足りていなかったか】
直の終わりまわりの仕掛けは、実はもう揃っています:

    15分前  画面いっぱいの確認が出る(`logic/shift_review.py`)
     5分前  自動確定 ── チェック → 集計 → 締めた印
            → **7項目が綺麗なら共有へも自動で送る**(`services/shift_close`)

足りないのは**結果が残らないこと**でした。送れたのか、何が残っているのかは
トーストで数秒出て消えます。そして画面いっぱいの確認は、**出口が
「確認しました」だけ**で、そこで終わっていました ── VBA が `graphF` を
最大化して出していた位置に画面はあるのに、締めくくりになっていない。

【ここが決めること】
その画面に**いまの成り行き**を出し、**次に押すもの1つ**を決めます:

    直すところがある … 「直しに戻る」   ── 共有へは出せません
    綺麗でまだ       … 「共有へ保存して終わる」
    もう渡してある   … 「確認しました(終わる)」

**判断はここだけ。** 画面は写すだけです。数(ページ・指摘・未送信)を
集めるのは呼ぶ側で、ここは純粋に組み立てます。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: 次に押すもの
ACT_NONE = "none"      # 押すものがない(まだ1ページも保存されていない)
ACT_FIX = "fix"        # 直しに戻る
ACT_PUSH = "push"      # 共有へ保存して終わる
ACT_DONE = "done"      # もう渡してある。見て終わるだけ

#: 帯の強さ。入力画面の帯(`logic/entry_findings.py`)と同じ言葉を使う
LEVEL_OK = "ok"
LEVEL_TODO = "todo"
LEVEL_ERROR = "error"

_LABELS = {
    ACT_NONE: "確認しました",
    ACT_FIX: "直しに戻る",
    ACT_PUSH: "共有へ保存して終わる",
    ACT_DONE: "確認しました(終わる)",
}


@dataclass(frozen=True)
class Closing:
    """直の締めくくりの成り行き。**画面はこれを写すだけ。**"""

    report_date: str = ""
    line: str = ""
    shift: str = ""
    pages: int = 0
    unsynced: int = 0
    findings: tuple[dict, ...] = ()
    #: 7項目を実際に走らせられたか。**走らせられなければ「済」とは言わない**
    counted: bool = True
    #: 共有へ送ったあとの画面かどうか(送ったその場で作り直す)
    just_pushed: bool = False
    #: 画面が出している日(いまの報告日)。締めくくる直が**別の日**なら、
    #: 直の名前だけでは**いまの直と見分けられない**ので日も添えます
    #: (後から作った前日の1直を、今日の1直に締めくくる、など)
    shown_date: str = ""

    @property
    def who(self) -> str:
        """どの直の話か。**画面の日と違えば日を添える。**"""
        if self.report_date and self.shown_date and self.report_date != self.shown_date:
            return f"{self.report_date} {self.shift}"
        return self.shift

    @property
    def action(self) -> str:
        if self.pages <= 0:
            return ACT_NONE
        if self.findings:
            return ACT_FIX
        if self.unsynced > 0:
            return ACT_PUSH
        return ACT_DONE

    @property
    def label(self) -> str:
        return _LABELS[self.action]

    @property
    def level(self) -> str:
        if self.action == ACT_FIX:
            return LEVEL_ERROR
        if self.action == ACT_PUSH:
            return LEVEL_TODO
        return LEVEL_OK

    @property
    def headline(self) -> str:
        """画面の頭に出す一行。**成り行きを言い切る。**"""
        if self.pages <= 0:
            return f"{self.who} は、まだ1ページも保存されていません"
        if self.findings:
            return (f"{self.who} に直すところが{len(self.findings)}件 "
                    f"あります ── このままでは共有へ保存できません")
        if self.unsynced > 0:
            if not self.counted:
                return (f"{self.who} の{self.pages}ページを、まだ共有へ"
                        f"渡していません(確かめられませんでした)")
            return (f"{self.who} の{self.pages}ページを、まだ共有へ"
                    f"渡していません")
        if self.just_pushed:
            return f"{self.who} を共有へ渡しました({self.pages}ページ)"
        return f"{self.who} は共有へ渡してあります({self.pages}ページ)"

    @property
    def note(self) -> str:
        """一行の下に添える説明。**次に何が起きるかを書く。**"""
        if self.action == ACT_FIX:
            return "直すところを直してから、もう一度ここへ戻ってきてください"
        if self.action == ACT_PUSH:
            return ("押すと、この端末に溜まっているぶんをまとめて共有の"
                    "日報管理へ書き写します")
        if self.action == ACT_DONE:
            return "この直でやることは残っていません"
        return ""

    #: 共有へ出せる状態か。画面はこれでボタンの色を決める
    @property
    def can_push(self) -> bool:
        return self.action == ACT_PUSH

    def as_dict(self) -> dict[str, Any]:
        return {
            "report_date": self.report_date, "line": self.line,
            "shift": self.shift, "pages": self.pages,
            "unsynced": self.unsynced, "findings": list(self.findings),
            "counted": self.counted, "just_pushed": self.just_pushed,
            "action": self.action, "label": self.label, "level": self.level,
            "headline": self.headline, "note": self.note,
            "shown_date": self.shown_date, "who": self.who,
            "can_push": self.can_push,
        }


def build(*, report_date: str = "", line: str = "", shift: str = "",
          pages: int = 0, unsynced: int = 0,
          findings: list | tuple = (), counted: bool = True,
          just_pushed: bool = False, shown_date: str = "") -> Closing:
    """成り行きを組み立てる。

    ``findings`` は**この直のぶんだけ**(`services/shift_check` の結果)。
    ``unsynced`` は共有へ未送信のページ数です。

    **確かめられなかったとき(`counted=False`)も、共有へのボタンは
    出します。** 押した先(`/api/settings/push`)が本物の関門を通すので、
    ここで先回りして止める必要はありません ── 止めると「確かめられない
    日は共有へ出せない」になります。文言のほうでそう断っておきます。
    """
    return Closing(report_date=report_date, line=line, shift=shift,
                   pages=max(0, pages), unsynced=max(0, unsynced),
                   findings=tuple(findings), counted=counted,
                   just_pushed=just_pushed, shown_date=shown_date)
