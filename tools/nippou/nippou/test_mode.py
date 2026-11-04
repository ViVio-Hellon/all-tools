"""テストモード ── **試しに打った直を「テストデータ」の印付きで残す**

    テストモード: パスワード認証(管理者パスワード)で入る。テストモードのあいだに作った直は
    「テストデータ」の印付きで保存し、共有の日報データへも印付きで送る

【入り方・切れ方】
入るのは管理者パスワードを通したときだけ(`enter`)。**プロセスに1つで、保存しません** ──
起動し直したら切れています(持ち越さない)。管理者モード(`work_context.admin`)とは別に
持ちます: 管理者モードは直の変わり目で落ちますが、テストモードは試しに直をまたいで
打つこともあるので、抜ける釦を押すか起動し直すまで続きます。

【何に印が付くか ── 直の単位】
印は `daily_header.test_mark`(ページごとに持つが、**直の中では揃える**)。決めるのは
`NippouRepository.save` で、

    そのページがもうある             そのページの印のまま
    同じ直にほかのページがある        その直の印を継ぐ
    直の最初のページ                  テストモードなら印を付ける

テストモードに入る**前からある直**へ打っても、普通の直のまま(本番の直がテストの扱いに
ならない)。テストモードで作った直へ、抜けたあとに打っても、テストの直のまま(印が落ちない)。

【記録】
入った・抜けたは出来事の記録(`services/event_log`)に、PC名・ログインIDと一緒に残します。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from .logging_setup import get_logger

log = get_logger("test_mode")

#: 画面の帯に出す一言(利用者に決めてもらった言い方)
BAND_TEXT = "テストモード(この間に保存した直はテストデータです)"
#: 一覧・紙に付ける印
MARK = "テスト"


@dataclass
class State:
    active: bool = False
    since: str = ""          # 入った時刻(ISO)
    terminal: str = ""
    login: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"active": self.active, "since": self.since,
                "since_text": self.since[:16].replace("T", " ") if self.since else "",
                "terminal": self.terminal, "login": self.login,
                "band": BAND_TEXT if self.active else ""}


_lock = threading.Lock()
_state = State()


def is_active() -> bool:
    """いまテストモードか(`repository.save` が直の最初のページで読む)。"""
    return _state.active


def state() -> State:
    with _lock:
        return State(**vars(_state))


def who() -> tuple[str, str]:
    """(PC名, ログインID)。引けなければ空。"""
    try:
        from .services import access_rights

        found = access_rights.identity()
        return found.pc or "", found.login or ""
    except Exception:                             # noqa: BLE001 - 記録は止めない
        return "", ""


def who_text(pc: str, login: str) -> str:
    """出来事の記録の「どこで」に入れる字(PC名・ログインID)。"""
    return f"PC {pc or '(不明)'} / ログインID {login or '(不明)'}"


def note(message: str, *, pc: str = "", login: str = "", **fields: Any) -> None:
    """出来事の記録に残す。**誰が**(PC名・ログインID)は文と「どこで」の欄に入れる
    (記録の欄は決まっているので ── `logic/event_log.FIELDS`)。"""
    try:
        from .services import event_log

        who = who_text(pc, login)
        event_log.note(f"{message}({who})", where=who, **fields)
    except Exception:                             # noqa: BLE001 - 記録は止めない
        log.exception("出来事の記録に残せませんでした: %s", message)


_note = note


def enter(*, now: Optional[datetime] = None) -> State:
    """入る。**パスワードは呼ぶ側が確かめてから**(`app/routes/test_mode`)。"""
    pc, login = who()
    with _lock:
        if not _state.active:
            _state.active = True
            _state.since = (now or datetime.now()).isoformat(timespec="seconds")
            _state.terminal, _state.login = pc, login
            entered = True
        else:
            entered = False
        got = State(**vars(_state))
    if entered:
        log.warning("テストモードに入りました pc=%s login=%s", pc, login)
        _note("テストモードに入りました(この間に作った直はテストデータになります)",
              action="test_mode_on", pc=pc, login=login)
    return got


def leave(*, why: str = "") -> State:
    """抜ける。入っていなければ何もしない。"""
    pc, login = who()
    with _lock:
        was = _state.active
        since = _state.since
        _state.active = False
        _state.since = _state.terminal = _state.login = ""
    if was:
        log.warning("テストモードを抜けました pc=%s login=%s since=%s %s", pc, login, since, why)
        _note("テストモードを抜けました" + (f"({why})" if why else ""),
              action="test_mode_off", pc=pc, login=login, since=since)
    return state()


def reset() -> None:
    """切る(試験・起動し直しと同じ始まり)。記録は残さない。"""
    with _lock:
        _state.active = False
        _state.since = _state.terminal = _state.login = ""
