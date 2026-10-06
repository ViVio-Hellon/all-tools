"""開いている画面を数える ── 「この看板を動かしてよいのは 1 画面だけ」

【プロセスが 1 つ = 画面が 1 つ、ではない】
:mod:`launch_guard` が防ぐのは **プロセスの二重起動** です。この PC で
``pythonw.exe`` が 2 つ動かないようにするもので、そこは守れています。

ところが、このアプリに窓はありません。見えているのはブラウザのタブです。
**1 つのプロセスに、タブは何枚でも繋がります。**

    ・ショートカットをもう一度ダブルクリックする
      (起動側は「すでに起動中」と判断して、既存の URL でブラウザを開く
       ── つまり**タブが 1 枚増える**)
    ・アドレスを控えておいて、別の窓で開く
    ・タブを複製する

こうして 2 枚開くと、**どちらでも押せてしまいます。** 同じラインの同じ
看板を 2 画面で取り合う形になり、次のことが起きます:

    ・片方で発送を押す。もう片方は押す前の盤を見ている
    ・その古い盤で押すと ``rev`` が合わず「別の端末が更新しました」と
      断られる ── **別の端末ではなく自分のもう 1 枚**
    ・一括発送のような「いま何件か」を数える操作は、見ている件数と
      実際の件数が食い違う

VBA 版はフォームが 1 つしか開けなかったので、この形の事故はありませんでした。
Web にした時点で**新しく持ち込んでしまった穴**です。

【どうするか】
画面ごとに名札(``screen id``)を持たせ、**持ち主は 1 枚だけ**にします。
持ち主でない画面からの「変える操作」は断り、画面には「別のタブで開いて
います」と出して、ボタン 1 つで持ち主を移せるようにします。

隠したり閉じさせたりはしません ── 開いてしまったものを黙って無効にすると
「押しても何も起きない」に見えます。**なぜ使えないのかと、どうすれば使えるか**
を出すほうが早く終わります。

【閉じたときの取り違えも、ここで直る】
以前は心拍の送り主を区別していなかったので、**2 枚のうち 1 枚を閉じただけで
アプリごと終わって**いました(閉じた合図に猶予 8 秒、心拍は 20 秒間隔なので、
残った 1 枚の次の心拍が間に合わない)。実際に再現します::

    タブA 心拍: 200
    タブB 心拍: 200
    タブA だけ閉じる(sendBeacon leaving=1): 200
      ...
      10秒後 タブBから見たサーバ: Connection refused

画面を数えるようにしたので、**最後の 1 枚が閉じるまで終わりません。**

【裏に回った画面・スリープ明けを「閉じた」と取り違えない】
ブラウザは**裏のタブのタイマーを間引きます。** Chrome は 5 分隠れたタブの
タイマーを 1 分に 1 回まで落とし、Edge の「スリープ中のタブ」は止めてしまい
ます。別の道具では、これで心拍が途切れて**開いているのに終了した**ことが
ありました。この道具も、心拍が 20 秒途切れた画面を「居ない」と見なしていた
ので同じ形でした。

* 画面は裏に回るときに「隠れます」と伝えます(``hide``)。隠れた画面は
  :data:`HIDDEN_MAX_SEC` のあいだ、心拍が無くても**開いているものとして**
  数えます(自動終了もしない)
* **スリープしていた時間は数えません。** Windows の ``time.monotonic()`` は
  スリープ中も進むので、起きた直後に「何時間も心拍が無い」と読んで、画面が
  繋ぎ直すより先に終わっていました。見張りが一定の間隔で呼ぶ時計が大きく
  飛んだら、飛んだぶんだけ心拍の時刻を後ろへずらします(:meth:`Screens._now`)
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from .applog import get_logger

log = get_logger("screen")


@dataclass(frozen=True)
class Screen:
    """開いている画面 1 枚。"""

    id: str
    opened_at: float
    """最初に名乗った時刻(``monotonic``)。"""

    seen_at: float
    """最後の心拍。"""

    hidden: bool = False
    """裏に回っている(タブが隠れている・ブラウザが最小化されている)。

    隠れた画面は心拍が間引かれる・止まるので、:data:`HIDDEN_MAX_SEC` まで
    待つ。**心拍が無いことを「閉じた」と読まない。**
    """


@dataclass(frozen=True)
class Claim:
    """心拍への返事。画面はこれを見て、使える画面かどうかを決める。"""

    active: bool
    """この画面が持ち主か。``False`` なら操作させない。"""

    others: int
    """自分以外に開いている画面の数。"""

    held_sec: float = 0.0
    """持ち主が開いてからの秒数。「いつから開いているか」を出すため。"""


#: 心拍が途切れてから、その画面を「もう居ない」と見なすまで(秒)。
#: 心拍は :data:`kanban.idle_exit.HEARTBEAT_MS` ごとに来るので、数回落ちても
#: 持ちこたえる長さにする。**短すぎると、少し固まっただけで持ち主が移る。**
STALE_SEC = 20.0

#: 隠れた画面を、心拍が無くても開いているものとして数える長さ(秒)。
#:
#: 裏のタブは心拍が 1 分に 1 回まで間引かれ、ブラウザによっては止まる
#: (Edge のスリープ中のタブ)。**最小化したまま一晩置いても終わらない長さ**
#: にする。スリープしていた時間は含まない(:meth:`Screens._now`)。
#:
#: 上限を置くのは、隠れたままブラウザごと落ちた(閉じた合図が来ない)画面が
#: いつまでもアプリを残さないため。残ったとしても、次に起動すれば既に動いて
#: いるアプリへ繋がるので、閉じたのに開けない、にはならない。
HIDDEN_MAX_SEC = 24 * 60 * 60.0

#: 見張りが時計を見る間隔より、これだけ長く時計が飛んでいたら「止まって
#: いた」(スリープ・休止)と読む(秒)。見張りは 2 秒ごとに見るので、
#: 普段はまず超えない
JUMP_SEC = 15.0

#: 閉じた画面の名札を覚えておく長さ(秒)。閉じるときは「隠れます」と
#: 「閉じます」が続けて飛ぶが、**届く順番は決まっていない**。「閉じます」の
#: あとに「隠れます」が届くと、閉じた画面が隠れた画面として 24 時間残る
TOMBSTONE_SEC = 120.0

#: 名札の長さの上限。画面が名乗る値をそのまま鍵にするので、際限なく
#: 溜まらないように切る
MAX_ID_LEN = 64

#: 名札を名乗らない相手(``curl``・古い画面・試験)をまとめる名前。
#: **区別しない 1 枚として扱う** ── 名乗らないものどうしを別々に数えると、
#: 誰も開いていないのに「2 枚開いています」になる
ANON = "-"


class Screens:
    """開いている画面の一覧と、いまの持ち主。

    :class:`~kanban.idle_exit.IdleWatch` もここを見る。「誰も見ていないから
    終わる」の判断と「使ってよい画面はどれか」の判断は、**同じ一覧から**
    出さなければ食い違う。
    """

    def __init__(
        self,
        *,
        stale_sec: float = STALE_SEC,
        hidden_max_sec: float = HIDDEN_MAX_SEC,
        jump_sec: float = JUMP_SEC,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.stale_sec = stale_sec
        self.hidden_max_sec = hidden_max_sec
        self.jump_sec = jump_sec
        self._clock = clock
        self._lock = threading.Lock()
        self._screens: dict[str, Screen] = {}
        self._holder: str = ""
        self._ever = False
        self._left_at: float | None = None
        self._last_seen: float | None = None
        self._gone: dict[str, float] = {}
        self._clock_at: float | None = None
        self._resumed_at: float | None = None
        self._paused_sec = 0.0

    # -- 画面から ------------------------------------------------------
    def beat(self, screen_id: str = "", *, hidden: bool = False, replaces: str = "") -> Claim:
        """心拍。知らない画面なら迎え入れ、持ち主が居なければ持ち主にする。

        ``hidden`` は「この画面は裏に回っている」。裏に回るときの合図
        (:meth:`hide`)と、裏で間引かれながら届く心拍の両方がこれを付ける。

        ``replaces`` は、ブラウザが**捨てて読み込み直した**タブの前の名札
        (``document.wasDiscarded``)。捨てられたタブは閉じた合図を送れない
        ので、前の名札が隠れた画面として残り、読み込み直した自分自身を
        「別のタブ」と数えてしまう。前の名札を引き取って席も引き継ぐ。
        """
        sid = _clean(screen_id)
        with self._lock:
            now = self._now()
            self._prune(now)
            if hidden and sid in self._gone and sid not in self._screens:
                # 閉じたあとに届いた「隠れます」。迎え入れると 24 時間残る
                return Claim(active=False, others=len(self._screens))
            old = _clean(replaces) if replaces else ""
            if old and old != sid:
                prev = self._screens.get(old)
                if prev is not None and prev.hidden:
                    del self._screens[old]
                    if self._holder == old:
                        self._holder = sid
                    log.info("捨てられて読み込み直したタブを引き継ぎました")
            known = self._screens.get(sid)
            self._screens[sid] = Screen(
                id=sid, opened_at=known.opened_at if known else now, seen_at=now,
                hidden=hidden,
            )
            self._ever = True
            self._last_seen = now
            self._left_at = None
            holder = self._screens.get(self._holder)
            if holder is None:
                # 持ち主が居ない(初めて / 閉じた / 返事が無くなった)。
                # **いま名乗った画面が引き継ぐ** ── 空いた席を空けたままに
                # すると、残った 1 枚まで使えなくなる
                self._holder = sid
            elif (
                not hidden and holder.id != sid and holder.hidden
                and now - holder.seen_at >= self.stale_sec
            ):
                # 持ち主は裏に回ったまま返事が無い(誰も見ていない)。**見えている
                # 画面に席を渡す。** 隠れた画面は一覧には残す(自動終了はしない)
                # ── 戻ってきたら「別のタブで開いています」から取り戻せる
                self._holder = sid
                log.info("持ち主の画面が裏に回ったままなので、見えている画面へ移しました")
            return self._claim(sid, now)

    def hide(self, screen_id: str = "") -> None:
        """画面が裏に回った(タブを切り替えた・最小化した・凍結される)。"""
        self.beat(screen_id, hidden=True)

    def take_over(self, screen_id: str = "") -> Claim:
        """この画面を持ち主にする(「こちらの画面を使う」)。

        奪われた側は次の心拍で ``active=False`` を受け取り、そう表示する。
        """
        sid = _clean(screen_id)
        with self._lock:
            now = self._now()
            self._prune(now)
            known = self._screens.get(sid)
            self._screens[sid] = Screen(
                id=sid, opened_at=known.opened_at if known else now, seen_at=now
            )
            self._ever = True
            self._last_seen = now
            self._left_at = None
            self._holder = sid
            return self._claim(sid, now)

    def leave(self, screen_id: str = "") -> None:
        """その画面が閉じた(``sendBeacon``)。

        **最後の 1 枚が閉じたときだけ**「誰も見ていない」を記録する。
        ここを画面ごとに区別しなかったせいで、2 枚のうち 1 枚を閉じただけで
        アプリごと終わっていた。
        """
        sid = _clean(screen_id)
        with self._lock:
            now = self._now()
            self._screens.pop(sid, None)
            self._gone[sid] = now
            self._prune(now)
            if sid == self._holder:
                # 席を空ける。残っている画面が次の心拍で引き継ぐ
                self._holder = ""
            if not self._screens and self._ever:
                self._left_at = now

    # -- 見る側から ----------------------------------------------------
    def is_holder(self, screen_id: str = "") -> bool:
        """その画面は操作してよいか。

        **持ち主がまだ居ないうちは断らない。** 心拍より先に操作が飛ぶ
        (開いた直後に押す)場面で、正しい画面まで断ってしまう。
        """
        sid = _clean(screen_id)
        with self._lock:
            now = self._now()
            self._prune(now)
            if self._holder not in self._screens:
                return True
            return self._holder == sid

    def holder(self) -> Screen | None:
        with self._lock:
            now = self._now()
            self._prune(now)
            return self._screens.get(self._holder)

    def live(self) -> list[Screen]:
        """いま開いている画面(隠れた画面も含む)。古い順。"""
        with self._lock:
            now = self._now()
            self._prune(now)
            return sorted(self._screens.values(), key=lambda s: s.opened_at)

    @property
    def ever(self) -> bool:
        """1 度でも画面が繋がったか。

        ``--no-browser`` で立てておく使い方を、自動終了の巻き添えにしない
        ための印(:mod:`kanban.idle_exit`)。
        """
        with self._lock:
            return self._ever

    @property
    def left_at(self) -> float | None:
        """最後の 1 枚が閉じた時刻。開いているうちは ``None``。"""
        with self._lock:
            now = self._now()
            self._prune(now)
            return self._left_at

    @property
    def last_seen(self) -> float | None:
        """最後に心拍が来た時刻(どの画面からでも)。"""
        with self._lock:
            return self._last_seen

    def now(self) -> float:
        """この一覧の時計。**スリープしていた時間を飛ばした**時刻。"""
        with self._lock:
            return self._now()

    @property
    def resumed_at(self) -> float | None:
        """最後に「止まっていた」と気付いた時刻(スリープ明け)。無ければ ``None``。"""
        with self._lock:
            return self._resumed_at

    @property
    def paused_sec(self) -> float:
        """最後に止まっていた長さ(秒)。"""
        with self._lock:
            return self._paused_sec

    # -- 中身 ----------------------------------------------------------
    def _now(self) -> float:
        """いまの時刻。**時計が大きく飛んでいたら、飛んだぶんを数えない。**

        呼ぶ側が錠を持っていること。

        見張り(:class:`~kanban.idle_exit.IdleWatch`)は 2 秒ごとにここを
        通るので、それより :data:`JUMP_SEC` 以上あいたら**プロセスが止まって
        いた**(PC のスリープ・休止)と読みます。そのあいだ画面は心拍を送れ
        ません ── 送れなかったのは画面のせいではないので、心拍・閉じた合図の
        時刻を**止まっていたぶんだけ後ろへずらし**、起きた時点から数え直します。

        見張りの居ない使い方(``--no-browser``)では呼ばれる間隔が空くので
        ここも飛びますが、ずらすのは「居なくなったと見なすのを遅らせる」向き
        だけなので害はありません。
        """
        now = self._clock()
        last, self._clock_at = self._clock_at, now
        if last is None:
            return now
        gap = now - last
        if gap < self.jump_sec:
            return now
        self._screens = {
            k: Screen(v.id, v.opened_at, v.seen_at + gap, v.hidden)
            for k, v in self._screens.items()
        }
        if self._last_seen is not None:
            self._last_seen += gap
        if self._left_at is not None:
            self._left_at += gap
        self._gone = {k: t + gap for k, t in self._gone.items()}
        self._resumed_at = now
        self._paused_sec = gap
        if self._ever:
            log.info(
                "約 %.0f 秒 止まっていました(スリープ・休止から戻った)。"
                "そのあいだの時間は、画面が居なくなったと見なすのに数えません", gap,
            )
        return now

    def _prune(self, now: float) -> None:
        """返事の無くなった画面を落とす。**呼ぶ側が錠を持っていること。**

        ここで落ちたものは ``_left_at`` を立てない。閉じた合図が届かないまま
        消えた(ブラウザごと落ちた・スリープ)場合なので、**短い猶予ではなく
        無通信のほうで判断させる** ── 一時的に固まっただけの画面を、閉じたのと
        同じ速さで切り捨てないため。
        """
        for s in list(self._screens.values()):
            limit = self.hidden_max_sec if s.hidden else self.stale_sec
            if now - s.seen_at >= limit:
                del self._screens[s.id]
        for sid in [k for k, t in self._gone.items() if now - t >= TOMBSTONE_SEC]:
            del self._gone[sid]

    def _claim(self, sid: str, now: float) -> Claim:
        holder = self._screens.get(self._holder)
        return Claim(
            active=self._holder == sid,
            others=max(0, len(self._screens) - 1),
            held_sec=round(now - holder.opened_at, 1) if holder else 0.0,
        )


def _clean(screen_id: str) -> str:
    sid = (screen_id or "").strip()[:MAX_ID_LEN]
    return sid or ANON


# ------------------------------------------------------------------
# プロセスに 1 つ
# ------------------------------------------------------------------
_screens: Screens | None = None
_lock = threading.Lock()


def get() -> Screens:
    """このプロセスの画面一覧。**どこから呼んでも同じもの。**"""
    global _screens
    with _lock:
        if _screens is None:
            _screens = Screens()
        return _screens


def reset() -> None:
    """テスト用。"""
    global _screens
    with _lock:
        _screens = None
