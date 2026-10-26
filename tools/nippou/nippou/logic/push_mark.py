"""入力画面の「この直は共有へ保存済み」の印 ── **この直を打ち切ったかを、画面に残す**

    VBAでは直終わりにグラフが出ていることで保存ボタンを押した確認にもなっていたが、
    現ツールでは何をもってこの直を打ち切ったと確認するのか?

「共有へ保存」は、通ってもトースト「共有へ保存しました」が数秒出るだけでした。
目を離していれば何も残らず、開き直した人・次に座った人には、この直を共有へ
渡したのかどうかが画面から読めません。

そこで**開いている直**について、入力画面の上にいつも1行出します:

    保存済み     この直は共有へ保存済み(16:52 山田)
                 (残り5分の自動確定で送った直は「この直は自動で共有へ保存済み(16:55)」)
    直した       共有へ保存したあとに直した行があります。もう一度「共有へ保存」を押してください
    送れなかった 共有へ保存で送れなかったページがあります(16:52 山田)。もう一度…
    まだ         まだ共有へ保存していません(控えめに。ふだんの入力の邪魔をしない)
    出さない     1ページも保存していない

【何を見て決めるか】
「渡したか」は手元の**未送信の印**(`daily_header.dirty`。中身が共有と違えば立つ ──
`logic/fingerprint`)、「いつ・誰が」は**共有保存の履歴**(`push_history`)。
どちらも手元の SQLite にあるので、開き直しても・ほかの人が開いても同じに出ます。

**判断はここだけ。** 数を集めるのは `services/push_history.mark`、画面は写すだけです。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Optional

#: 印の状態
STATE_NONE = "none"        # 出さない(まだ1ページも保存していない)
STATE_UNSENT = "unsent"    # まだ共有へ保存していない
STATE_SAVED = "saved"      # 共有へ保存済み
STATE_STALE = "stale"      # 保存したあとに直した行がある
STATE_FAILED = "failed"    # 前回の共有へ保存で送れなかったページがある

#: 履歴の「結果」(`services/push_history` と同じ字。ここは import しない ── 純粋に保つ)
_SENT = "送れた"
_NOTHING = "送るもの無し"
_PARTIAL = "一部送れなかった"
_FAILED = "送れなかった"
#: 共有へ中身が出たと言える押し方
_REACHED = (_SENT, _NOTHING)
#: 送ろうとした押し方(関門で止めた・引き継いだ、は送ろうとしていない)
_TRIED = (_SENT, _NOTHING, _PARTIAL, _FAILED)
#: 自動で送った行の「押した人」(`push_history.AUTO_PRESSED_BY`)
_AUTO = "(自動確定)"

AGAIN = "もう一度「共有へ保存」を押してください"


@dataclass(frozen=True)
class PushMark:
    """入力画面に出す印。**画面はこれを写すだけ。**"""

    state: str = STATE_NONE
    #: 押した時刻(「16:52」。日が違えば「10/27 01:10」)と押した人
    at: str = ""
    by: str = ""
    #: 残り5分の自動確定(と次の直の拾い)で送ったか。**人が押したのではないと分かるように**
    auto: bool = False

    @property
    def shown(self) -> bool:
        return self.state != STATE_NONE

    @property
    def level(self) -> str:
        """強さ。保存済み = ok(緑)、要るもの = todo(黄)、まだ = quiet(灰)。"""
        if self.state == STATE_SAVED:
            return "ok"
        if self.state in (STATE_STALE, STATE_FAILED):
            return "todo"
        return "quiet"

    @property
    def when(self) -> str:
        """括弧の中身(「16:52 山田」)。無ければ空。"""
        return " ".join(part for part in (self.at, self.by) if part)

    @property
    def text(self) -> str:
        paren = f"({self.when})" if self.when else ""
        if self.state == STATE_SAVED and self.auto:
            return f"この直は自動で共有へ保存済み{paren}"
        if self.state == STATE_SAVED:
            return f"この直は共有へ保存済み{paren}"
        if self.state == STATE_STALE:
            return f"共有へ保存したあとに直した行があります。{AGAIN}"
        if self.state == STATE_FAILED:
            return f"共有へ保存で送れなかったページがあります{paren}。{AGAIN}"
        if self.state == STATE_UNSENT:
            return "この直はまだ共有へ保存していません"
        return ""

    def as_dict(self) -> dict[str, Any]:
        return {"state": self.state, "shown": self.shown, "level": self.level,
                "text": self.text, "at": self.at, "by": self.by, "auto": self.auto}


def _clock(pressed_at: str, now: Optional[datetime]) -> str:
    """押した時刻を短く。**今日なら時:分だけ**、ほかの日なら月/日も添える。"""
    try:
        at = datetime.fromisoformat(str(pressed_at))
    except ValueError:
        return ""
    if now is not None and at.date() == now.date():
        return at.strftime("%H:%M")
    return f"{at.month}/{at.day} {at:%H:%M}"


def _is_auto(row: dict) -> bool:
    return " ".join(str(row.get("pressed_by") or "").split()) == _AUTO


def _who(row: dict) -> str:
    """押した人。自動で送った行は空(文のほうで「自動で」と言う)、押した人が空なら担当者。"""
    if _is_auto(row):
        return ""
    by = " ".join(str(row.get("pressed_by") or "").split())
    return by or " ".join(str(row.get("worker") or "").split())


def decide(*, pages: int, unsynced: int, history: Iterable[dict],
           now: Optional[datetime] = None) -> PushMark:
    """その直の印を決める。

    ``pages`` は保存してあるページ数、``unsynced`` はそのうち共有へ未送信の
    ページ数、``history`` はその直の共有保存の履歴(古い順)。
    """
    if pages <= 0:
        return PushMark()
    rows = list(history)
    reached = [r for r in rows if r.get("result") in _REACHED]
    tried = [r for r in rows if r.get("result") in _TRIED]
    last = reached[-1] if reached else None

    def stamp(row: dict) -> tuple[str, str]:
        return _clock(row.get("pressed_at", ""), now), _who(row)

    if unsynced <= 0:
        if last is None:
            # 共有と同じ中身だが、押した記録が無い(前の版で送った・取り込んだ など)。
            # **いつ・誰が言えないものは「保存済み」と言わない**
            return PushMark()
        return PushMark(STATE_SAVED, *stamp(last), auto=_is_auto(last))
    # 未送信がある。**最後に送ろうとして送れなかった**のか、**送ったあとに直した**のか
    latest = tried[-1] if tried else None
    if latest is not None and latest.get("result") in (_PARTIAL, _FAILED):
        return PushMark(STATE_FAILED, *stamp(latest))
    if last is not None:
        return PushMark(STATE_STALE, *stamp(last))
    return PushMark(STATE_UNSENT)
