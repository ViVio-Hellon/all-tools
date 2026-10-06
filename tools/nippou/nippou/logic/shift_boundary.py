"""開いたままの画面が、直の変わり目をまたいだかどうか

【何を防ぐためにあるか】
保存先の (報告日, 直) は**要求のたびに時計から引き直します**
(`work_context.current_key` → `TimeCheck` / `TodayCheck`)。VBA の
``NippouDB_GetShiftInfo`` も同じで、そこは合わせてあります。

違うのは**画面の寿命**です。VBA は保存処理の最後で ``Unload UFdaily``
していたので、フォームが 17:00 をまたいで生き残ることがまずありません
でした。Web版は開きっぱなしにできるので、こうなります:

    16:55  1直のつもりで12行を打つ  → 1直 ページ1 へ保存
    17:05  同じ画面のまま保存を押す  → **2直 ページ1 へ保存**(画面は無言)

同じ12行が1直と2直の両方に残ります。3直→1直(08:00)はもっと悪くて、
**直だけでなく報告日も同時に変わります**(3直の朝は前日付)。

【どう止めるか】
画面は**自分が何の直として描かれたか**(`opened`)を送ります。それが
いまの保存先と違えば「またいだ」と見なし、**書く前に止めて選ばせます**:

    打っていた直へ入れる … さっきまで打っていた紙の続き。ページもそのまま
    今の直として入れる  … 直が替わったので、新しい紙として1ページ目から

**どちらも正しい場面があります。** 17:03 に打ち終えたぶんは1直の紙で、
17:05 から始めたぶんは2直の紙です ── どちらかに倒すと必ず片方が困る
ので、**押した人に選ばせる**ほうへ倒しました。選ばせるための言葉と
選択肢は、ここ1か所で作ります(画面にも道(route)にも書かない)。

【自動保存はどうするか】
断りを出しません。**黙って見送ります**(`app/routes/entry.py`)。誰も
押していないところにポップアップを出しても選べませんし、勝手にどちらかへ
書けば、それこそ黙って間違った直に入ります。気づかせる役は1分ごとの
見張り(`/api/shift/key`)が持ちます。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

__all__ = ["CHOICE_OPENED", "CHOICE_CURRENT", "CHOICES", "Key", "Crossing",
           "check", "resolve"]

#: 選べる道。**この2つだけ**
CHOICE_OPENED = "opened"
CHOICE_CURRENT = "current"
CHOICES = (CHOICE_OPENED, CHOICE_CURRENT)


@dataclass(frozen=True)
class Key:
    """保存先の見分け。**ページまで持つ** ── 直が替わればページも1から始まる。"""

    report_date: str = ""
    shift: str = ""
    page: int = 1

    @property
    def filled(self) -> bool:
        """比べられる形か。片方でも空なら比べない(開いた直後・古い画面)。"""
        return bool(self.report_date and self.shift)

    def same_slot(self, other: "Key") -> bool:
        """**ページは見ません。** ページが動いただけならまたいでいない。"""
        return (self.report_date == other.report_date
                and self.shift == other.shift)

    def label(self) -> str:
        return f"{self.report_date} {self.shift}".strip()

    def as_dict(self) -> dict:
        return {"report_date": self.report_date, "shift": self.shift,
                "page": self.page}


@dataclass
class Crossing:
    """またいだかどうかと、またいでいたら何を訊くか。"""

    crossed: bool = False
    opened: Key = field(default_factory=Key)
    current: Key = field(default_factory=Key)
    #: "shift" 直だけ / "date" 報告日だけ / "both" 両方
    kind: str = ""
    message: str = ""
    choices: list[dict] = field(default_factory=list)

    def target(self, choice: str) -> Key:
        """選ばれた道の保存先。**知らない答えは今の直へ倒す**(時計が正)。"""
        return self.opened if choice == CHOICE_OPENED else self.current

    def as_dict(self) -> dict:
        return {
            "crossed": self.crossed, "kind": self.kind,
            "opened": self.opened.as_dict(), "current": self.current.as_dict(),
            "message": self.message, "choices": list(self.choices),
        }


def _kind(opened: Key, current: Key) -> str:
    date_moved = opened.report_date != current.report_date
    shift_moved = opened.shift != current.shift
    if date_moved and shift_moved:
        return "both"
    if date_moved:
        return "date"
    return "shift"


def _message(opened: Key, current: Key, kind: str) -> str:
    """**何が変わったのかを名指しする。**

    「直が変わりました」だけでは、報告日まで変わる 3直→1直 のときに
    片手落ちになります ── 日をまたいだことに気づかないまま「今の直へ」を
    選ぶと、前日の紙が今日のものとして残ります。
    """
    head = {
        "shift": "直が変わりました",
        "date": "報告日が変わりました",
        "both": "報告日と直が変わりました",
    }.get(kind, "直が変わりました")
    return (f"{head}。この画面は {opened.label()} として開いていますが、"
            f"いまは {current.label()} です。"
            "どちらに入れるか選んでください。")


def _choices(opened: Key, current: Key) -> list[dict]:
    """**どちらを選んでも何が起きるかを書く。**

    「はい / いいえ」では、押した人に結果が見えません。直の名前と、
    ページがどうなるかまで、そのまま画面に出せる形で持たせます。
    """
    return [
        {
            "value": CHOICE_OPENED,
            "label": f"{opened.label()} に入れる",
            "note": (f"さっきまで打っていたページの続きとして、ページ{opened.page} へ"
                     "保存します。直の終わりに打ち終えたぶんはこちらです。"),
        },
        {
            "value": CHOICE_CURRENT,
            "label": f"{current.label()} に入れる",
            "note": (f"直が替わったので、{current.label()} の"
                     f"ページ{current.page} として保存します。"),
        },
    ]


def check(opened: Optional[Key], current: Key) -> Crossing:
    """またいだか。またいでいれば、訊くための一式も入れて返す。

    `opened` が無い・空のときは **またいでいない扱い**にします ──
    画面を開いた直後や、この仕組みより前に開かれた画面が相手です。
    そこで断ると、保存できない画面を作ってしまいます。
    """
    if opened is None or not opened.filled or not current.filled:
        return Crossing(opened=opened or Key(), current=current)
    if opened.same_slot(current):
        return Crossing(opened=opened, current=current)
    kind = _kind(opened, current)
    return Crossing(crossed=True, opened=opened, current=current, kind=kind,
                    message=_message(opened, current, kind),
                    choices=_choices(opened, current))


def resolve(crossing: Crossing, choice: str) -> Optional[Key]:
    """選ばれた道の保存先。**まだ選ばれていなければ `None`。**

    呼び出し側はこれが `None` のあいだ**1行も書きません**。「選ばれて
    いない」と「今の直が選ばれた」を同じ値で表すと、聞き返す前に
    書いてしまいます。
    """
    if not crossing.crossed:
        return crossing.current
    if choice not in CHOICES:
        return None
    return crossing.target(choice)
