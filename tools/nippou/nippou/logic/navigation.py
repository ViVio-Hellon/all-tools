"""Focus-advance / value-carry / highlight logic ported from the VBA
``MoveText``, ``Same_Text`` and ``Point_Change`` Subs (standard module,
~line 5646-5785).

Kept UI-agnostic: callers hand in the current field values plus small
callback hooks (focus a control, set a value, set a background color).
In the web version the caller is :mod:`nippou.presenters.entry`, which
turns those hooks into the ``focus`` / ``rows`` fields of the view model
that ``views/entry.js`` then applies to the DOM -- **the decision stays
here, on the server**.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .. import constants
from .numeric import is_numeric


def field_name(family: str, row: int) -> str:
    return f"{family}{row}"


@dataclass
class MoveTextResult:
    cleared: bool = False  # the field's text was blanked (non-numeric)
    new_text: str = ""
    focused: Optional[str] = None  # widget name that received focus, if any


def move_text(
    family: str,
    row: int,
    current_text: str,
    is_visible: bool = True,
) -> MoveTextResult:
    """Port of ``MoveText(TEX, num)``.

    ``current_text`` is the value in ``family & row`` *after* the
    keystroke/edit that triggered the event. Returns whether the text
    should be cleared (family is numeric-only and the text wasn't
    numeric) and which control (if any) should now receive focus.
    """
    if family not in constants.FOCUS_CHAIN:
        return MoveTextResult(new_text=current_text)

    next_family, required_len = constants.FOCUS_CHAIN[family]

    text = current_text
    cleared = False
    if family not in constants.TEXT_LIKE_FAMILIES:
        if not is_numeric(text) and text != "":
            text = ""
            cleared = True

    if not is_visible:
        return MoveTextResult(cleared=cleared, new_text=text)

    if len(text) != required_len:
        return MoveTextResult(cleared=cleared, new_text=text)

    if family == "TH5" or next_family is None:
        return MoveTextResult(cleared=cleared, new_text=text)

    return MoveTextResult(cleared=cleared, new_text=text, focused=field_name(next_family, row))


def same_text(
    row: int,
    sz_text: str,
    sh_text: str,
    day_temp: str,
    shift_end_times: dict[str, tuple[str, str]],
) -> Optional[tuple[str, str]]:
    """Port of ``Same_Text("SH", num)``: when a row's end time (SZ/SH,
    i.e. hour/minute) is fully entered, carry it forward as the *next*
    row's start time (KZ/KH), unless this is the last row (12) or the
    entered end time is **a shift's official end time**.

    ``shift_end_times`` maps a shift label ("日勤"/"1直"/"2直"/"3直") to
    an ``(hour, minute)`` pair, e.g. ``{"1直": ("15", "00")}`` --
    equivalent of ``End1``/``End2``/``End3``/``End昼`` split into
    front/back 2-digit strings by the original code.

    Returns ``(kz_next, kh_next)`` to write into row+1, or ``None`` if no
    carry should happen.

    【直の終わりの時刻を打ったら、写さない ── 見るのは3つ全部】

    現場の終了時刻は **15:00 / 22:50 / 07:00** です。この3つのどれかが
    入ったということは、**その直の作業がそこで終わった**ということなので、
    次の行に続きはありません。写すと、終わった時刻が次の行の開始として
    残り、打つ人がそれを消すところから始めることになります。

    VBA は「**いま何直か**(``day_temp``)に当たる1つ」としか比べて
    いませんでした。1直で 22:50 と打てば写ってしまいます ── 直の
    変わり目をまたいで打っているとき、画面の直と打っている時刻は
    食い違うので、**3つとも見ます**。

    (日勤の 18:00 は VBA が別に持っていた分岐です。日勤の時刻は
    いま保留なので、そのまま残してあります。)
    """
    if len(sh_text) != 2:
        return None
    if sz_text == "":
        return None
    if row == 12:
        return None

    if (sz_text, sh_text) in set(shift_end_times.values()):
        return None
    if day_temp == "日勤" and sz_text == "18" and sh_text == "00":
        return None

    return (sz_text, sh_text)


#: `carry_decision` の答え。**「何もしない」を空文字にしない** ── 帰り値を
#: そのまま `if` で見ると、書くのと消すのを取り違えます
CARRY_NONE = "none"
CARRY_WRITE = "write"
CARRY_CLEAR = "clear"


@dataclass(frozen=True)
class CarryDecision:
    """次の行の開始時刻を、書くか・消すか・触らないか。"""

    action: str = CARRY_NONE
    row: int = 0          # 書く/消す先の行(`action` が none なら 0)
    kz: str = ""
    kh: str = ""

    @property
    def writes(self) -> bool:
        return self.action == CARRY_WRITE

    @property
    def clears(self) -> bool:
        return self.action == CARRY_CLEAR


def carry_decision(
    row: int,
    sz_text: str,
    sh_text: str,
    day_temp: str,
    shift_end_times: dict[str, tuple[str, str]],
    carried_rows: set[int] | frozenset[int] = frozenset(),
) -> CarryDecision:
    """`same_text` に**後始末**を足したもの。

    【写すだけだと、直したときに古い値が残る】

        14:59 と打って写ったあと 15:00 に直しても、2行目には 14:59 が残る

    `same_text` は書くだけで消さないので、**引き継ぎが断られた瞬間に、
    前に写した値が宙に浮きます**。直の終わりに直したときがまさにそれで、
    いちばん起きてほしくない場面です。

    【「その行が空かどうか」では見分けられない】

    最初は「2行目に他が何も入っていなければ、それは写した値だ」で
    済ませようとしました。**順番が合いません**:

        1行目の終了 14:59 → 2行目の開始に入る
        2行目のロットNoを打つ  → 2行目はもう空ではない
        1行目を 15:00 に直す   → 「空でない」ので触れず、14:59 が残る

    直しに気づくのは先へ進んだあとなので、肝心な場面で効きません。

    【誰が入れた値かを覚えておく】

    ``carried_rows`` は「**ツールが入れた開始時刻が、まだ人に触られずに
    残っている行**」です。画面が持っていて、人がその欄を打った時点で
    落とします。消してよいのはこの印が付いている行だけ ── 人が打った
    値は、何があっても消しません。
    """
    carried = same_text(row=row, sz_text=sz_text, sh_text=sh_text,
                        day_temp=day_temp, shift_end_times=shift_end_times)
    if carried is not None:
        return CarryDecision(CARRY_WRITE, row + 1, carried[0], carried[1])

    # 断られた。**写したぶんが残っているなら、引っ込めます。**
    if row >= constants.ROW_COUNT:
        return CarryDecision()          # 13行目はありません
    if (row + 1) in carried_rows:
        return CarryDecision(CARRY_CLEAR, row + 1)
    return CarryDecision()


def point_change(row: int) -> dict[str, str]:
    """Port of ``Point_Change(TEX, num)`` minus the highlighted field
    itself: returns the background color to apply to every *other* field
    in ``row`` (the caller applies the highlight color to ``TEX & row``
    separately, since this function doesn't know which one triggered
    it -- see :func:`point_change_colors`)."""
    return {field_name(fam, row): DEFAULT_COLOR for fam in constants.ROW_FIELD_FAMILIES}


DEFAULT_COLOR = "#FFFFFF"  # VBA DefColor = 16777215 -> RGB(255,255,255)
HIGHLIGHT_COLOR = "#CCFFFF"  # VBA CheColor = 16777164 -> RGB(204,255,255)


def point_change_colors(trigger_family: str, row: int) -> dict[str, str]:
    """Full ``Point_Change`` behaviour: every field in the row goes back
    to the default background except the one that gained focus, which is
    highlighted."""
    colors = point_change(row)
    colors[field_name(trigger_family, row)] = HIGHLIGHT_COLOR
    return colors
