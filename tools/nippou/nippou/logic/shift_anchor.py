"""入力先の直は、**作業者を選んだ時点で決まる**

【なぜ要るのか】
直は要求のたびに時計から引き直していました。すると 15:00 をまたいだ
瞬間に、**打っている本人は1直のつもりなのに、画面とサーバは2直を指す**
ことになります:

    「作業者登録するまで入力できないんじゃないの？
      作業者登録した時間が入力する直の入力ですよ
      何故またいで入力できるのですか」

そのとおりで、直の始まりを「作業者を選ぶこと」に決めた(v3.54.0)のに、
**入力先だけが時計のままだった**のが食い違いの正体です。時計に任せると
「どちらの直へ入れますか」と訊くしかなくなり、打っている人に、自分が
いま何を打っているのかを訊いていることになります。

そこで**選んだ時点の直に固定します**(`Anchor`)。以後その端末は、
固定した直へ書き続けます。

【固定するのは、選んだ瞬間の時計の直】
15:05 に選んだなら 2直 です。迷いがありません ── 15:00 を過ぎてから
1直の続きを打つのは「過ぎた直を直す」話なので、「記録を見る」から
呼び出す道(管理者)のほうを通します。

【固定は、終わりの時刻ちょうどで切れる】
終わりの時刻を過ぎても打てたままにすると、**忘れて立ち去った人の紙が
開いたまま残り、次に座った人がその続きを打つ羽目になります。**
それは頼めません。

はじめは猶予(30分)を置いていました ── 15:00 ちょうどで手を止められない
だろう、という理屈です。やめました:

    「入力時間が足りなくなった場合しか得がない
      でもほとんどのケースは入力忘れ、保存忘れ
      時間が伸びることによって次の直が尻ぬぐいをすることになる」

得をするのは**まれな側**(打ち終わらなかった直)で、損をするのは
**よくある側**(忘れて帰った直の後始末)です。割に合いません。

**終わりの時刻ちょうどで閉じます。** そのぶん、閉じる前の催促を
頼りにします ── 15分前に声をかけ、5分前に打ってあるぶんを確定し、
綺麗なら共有へも送ります(`services/shift_close.py`)。閉じるころには
機械の側が済ませている、という順序です。

`grace` は残してありますが**既定は 0** です(現場で決める値なので、
`config.SETTINGS.shift_grace_minutes` から動かせます)。

【この層の約束】
ここは**純粋**。時計もDBも触らず、渡された事実だけで決めます。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

#: 終わりの時刻を過ぎてから、見るだけになるまで(分)。**既定は 0** ──
#: 15:00 ちょうどで手が止まります。
#:
#: 長くすると「忘れて立ち去った紙」が開いたまま残る時間が延び、その
#: 後始末を次の直がすることになります。**現場で決める値**なので
#: `config.SETTINGS.shift_grace_minutes` から差し替えられます。
DEFAULT_GRACE_MINUTES = 0


@dataclass(frozen=True)
class Anchor:
    """作業者を選んだ時点で決まった直。**時計ではなく、これが入力先。**"""

    active: bool = False
    report_date: str = ""
    line: str = ""
    shift: str = ""
    #: 選んだ時刻(ISO)。画面に「◯時◯分に始めた直」と出すために持つ
    started_at: str = ""

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.report_date, self.line, self.shift)

    @property
    def filled(self) -> bool:
        return bool(self.active and self.report_date and self.shift)

    def as_dict(self) -> dict:
        return {"active": self.active, "report_date": self.report_date,
                "line": self.line, "shift": self.shift,
                "started_at": self.started_at}


@dataclass(frozen=True)
class Standing:
    """その固定が、いまどうなっているか。**画面はこれを写すだけ。**"""

    #: 固定が効いていて、まだ打てる
    open: bool = False
    #: 終わりの時刻(+ 猶予を置いていればそのぶん)を過ぎた。**見るだけ**
    expired: bool = False
    #: 見るだけになるまで(分)。過ぎていれば 0
    minutes_left: int = 0
    #: 終わりの時刻を過ぎてから何分たったか(過ぎていなければ 0)
    minutes_over: int = 0
    message: str = ""

    def as_dict(self) -> dict:
        return {"open": self.open, "expired": self.expired,
                "minutes_left": self.minutes_left,
                "minutes_over": self.minutes_over, "message": self.message}


def decide(anchor: Anchor, *, now: datetime,
           shift_end: Optional[datetime],
           grace_minutes: int = DEFAULT_GRACE_MINUTES) -> Standing:
    """固定がまだ効いているか。

    `shift_end` は**固定した直**の終わり(日付つき。
    `ShiftCalculator.shift_end_at`)。読めなければ固定を切らしません
    ── 時刻が読めないことを理由に打てなくするのは筋が違います。
    """
    if not anchor.filled:
        return Standing()
    if shift_end is None:
        return Standing(open=True)

    over = int((now - shift_end).total_seconds() // 60)
    if over < 0:
        return Standing(open=True, minutes_left=grace_minutes - over)

    left = grace_minutes - over
    if left > 0:
        return Standing(open=True, minutes_left=left, minutes_over=over,
                        message=_ending_soon(anchor, over, left))
    return Standing(expired=True, minutes_over=over,
                    message=_ended(anchor))


def _ending_soon(anchor: Anchor, over: int, left: int) -> str:
    """終わりを過ぎたが、まだ打てる。**あと何分かを出す。**"""
    return (f"{anchor.shift}の時間は{over}分前に終わっています。"
            f"あと{left}分で見るだけになります ── "
            "打ち終えたら「保存(確定)」と「共有へ保存」まで済ませてください。")


def _ended(anchor: Anchor) -> str:
    """見るだけになった。**次にやることまで書く。**"""
    return (f"{anchor.shift}({anchor.report_date})は終わっています。"
            "この画面は見るだけです ── 打つことも保存することもできません。\n"
            "次の直を始めるときは「作業者を選ぶ」を押してください。"
            "終わった直を直すときは「記録を見る」から呼び出します"
            "(管理者モードが要ります)。")


def start_key(clock_key: tuple[str, str, str], now: datetime) -> Anchor:
    """作業者を選んだ ── **その瞬間の時計の直**に固定する。

    15:05 に選んだなら 2直 です。迷いがありません。
    """
    report_date, line, shift = clock_key
    return Anchor(active=True, report_date=report_date, line=line,
                  shift=shift, started_at=now.isoformat(timespec="seconds"))


def should_restart(anchor: Anchor, clock_key: tuple[str, str, str]) -> bool:
    """固定を**捨てて取り直す**か。

    ラインを変えたときです ── ラインが変われば別の紙なので、前の
    ラインの直へ書き続ける理由がありません(丸徳・トットは常に日勤
    なので、直の名前まで変わります)。

    **時計の直が進んだだけでは取り直しません。** それを取り直しの
    合図にすると、固定した意味がなくなります。
    """
    if not anchor.filled:
        return False
    return anchor.line != clock_key[1]
