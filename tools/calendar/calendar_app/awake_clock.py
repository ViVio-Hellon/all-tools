"""起きていた時間だけを数える時計 ── **スリープのあいだを「無通信」に数えない**

【何が起きるか】
自動終了の見張り(``idle_exit``)と画面の錠(``screen_lock``)は、どちらも
「最後の合図から何秒たったか」で決めます。ところが PC がスリープすると、
その間に時計がどう進むかは OS しだいです。

    Windows   ``time.monotonic()`` はスリープ中も進む
              → 起きた瞬間「閉じた合図から 8 秒すぎた」「粘りの 600 秒を
                すぎた」と読んで、画面が戻る前・送り直す前に終わる
              → 使っている画面の錠も「45 秒 心拍が無い」で外れ、そのあいだに
                もう一度起動したタブが黙って持ち主になる
    Linux     スリープ中は止まる(こちらは困らない)

画面側から見ると、**フタを開けたら「繋がりません」**や「別の画面で
開かれました」になっています。心拍が途切れたのはブラウザも一緒に
眠っていたからで、誰も閉じていません。

【どう数えるか】
2秒ごとに時計を見に行く見張りを1本立てます。**2秒のはずが15秒以上
空いていたら、そのあいだプロセスごと止まっていた**(スリープ・休止・
仮想機械の一時停止)とみなし、その分を差し引いた時刻を ``now()`` で返します。

    起きていた時間 = monotonic − 止まっていた時間の合計

止まっていた分を差し引くので、起きた直後の「最後の合図から」は眠る前の
続きになります ── 起きたブラウザが心拍を送るまでの間に、見張りが
先回りして落とすことがありません。

見張りより先に誰かが ``now()`` を呼んでも同じ答えになるよう、見に行くのは
``now()`` の中でもやります(起きた瞬間、見張りと要求のどちらが先に走るかは
決まっていないため)。

**見張りを立てていないあいだは、ただの ``time.monotonic()`` です。** 立てて
いなければ呼ばれる間隔はまちまちで、空いていても眠っていたとは言えません
(試験・デスクトップ版はこちら。デスクトップ版は窓が終わりを決めるので、
自動終了の見張りと一緒にこれも立てない)。

日報の ``tools/nippou/nippou/awake_clock.py`` と同じ作りです。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .logging_utils import get_logger

log = get_logger("awake_clock")

#: 見に行く間隔(秒)
TICK_SEC = 2.0

#: これより空いたら「止まっていた」とみなす(秒)。見張りは2秒ごとに
#: 起きるので、15秒はその7回ぶん ── 重い処理で少し遅れた程度では
#: 眠っていたことにしない。錠の見切り(``screen_lock.SCREEN_STALE_SEC``)・
#: 心拍の見切り(``idle_exit.IDLE_SEC``)より短くして、**それより長く眠ったら
#: 必ず差し引く**
JUMP_SEC = 15.0


@dataclass(frozen=True)
class Resume:
    """止まっていたところから戻った1回。"""

    #: 止まっていた長さ(秒・だいたい)
    seconds: float
    #: 戻ったことに気づいた時刻(``time.time()``)
    at: float
    #: monotonic も一緒に進んでいたか(True = Windows 型。差し引いた)
    counted: bool


class AwakeClock:
    """起きていた時間だけを数える。"""

    def __init__(self, mono: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time, *,
                 tick_sec: float = TICK_SEC, jump_sec: float = JUMP_SEC) -> None:
        self._mono = mono
        self._wall = wall
        self.tick_sec = tick_sec
        self.jump_sec = jump_sec
        self._lock = threading.Lock()
        self._slept = 0.0
        # 最後に見た時刻。None = まだ見張っていない
        self._last: Optional[tuple[float, float]] = None
        self._resume: Optional[Resume] = None
        self._done = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- 時刻 ----------------------------------------------------------
    def now(self) -> float:
        """起きていた時間(秒)。**差を取るためだけに使う**(値そのものに意味は無い)。"""
        with self._lock:
            found = self._observe()
            value = self._mono() - self._slept
        if found is not None:
            self._tell(found)
        return value

    def check(self) -> Optional[Resume]:
        """見張り1回ぶん。止まっていたところから戻ったならそれを返す。"""
        with self._lock:
            found = self._observe()
        if found is not None:
            self._tell(found)
        return found

    @property
    def watching(self) -> bool:
        with self._lock:
            return self._last is not None

    @property
    def last_resume(self) -> Optional[Resume]:
        """最後に戻ったとき(無ければ None)。"""
        with self._lock:
            return self._resume

    @property
    def slept(self) -> float:
        """これまでに差し引いた長さの合計(秒)。"""
        with self._lock:
            return self._slept

    # -- 見張り --------------------------------------------------------
    def start(self, *, thread: bool = True) -> None:
        """見張りを始める。``thread=False`` は試験用(``check()`` を自分で回す)。"""
        with self._lock:
            if self._last is not None:
                return
            self._last = (self._mono(), self._wall())
            if not thread:
                return
            self._thread = threading.Thread(target=self._loop, name="awake-clock",
                                            daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._done.set()

    def _loop(self) -> None:
        while not self._done.wait(self.tick_sec):
            self.check()

    # -- 中で使うもの --------------------------------------------------
    def _observe(self) -> Optional[Resume]:
        """前に見たときから、止まっていたか。**錠の中で呼ぶ。**"""
        if self._last is None:
            return None
        mono, wall = self._mono(), self._wall()
        last_mono, last_wall = self._last
        self._last = (mono, wall)
        gap = mono - last_mono
        if gap > self.jump_sec:
            # monotonic ごと進んでいた(Windows)。**見張りの1回ぶんを
            # 残して差し引く** ── その1回ぶんは本当に起きていた
            lost = gap - self.tick_sec
            self._slept += lost
            self._resume = Resume(seconds=lost, at=wall, counted=True)
            return self._resume
        drift = (wall - last_wall) - gap
        if drift > self.jump_sec:
            # 壁の時計だけ進んでいた(Linux のスリープ・時刻合わせ)。monotonic は
            # 止まっていたので差し引くものは無い ── 戻ったことだけ記す
            self._resume = Resume(seconds=drift, at=wall, counted=False)
            return self._resume
        return None

    @staticmethod
    def _tell(found: Resume) -> None:
        log.info("スリープ(または一時停止)から戻りました: 約%.0f秒 止まっていました%s",
                 found.seconds,
                 "(その間は無通信に数えません)" if found.counted else "")


# ------------------------------------------------------------------
# プロセスに1つ
# ------------------------------------------------------------------
_clock = AwakeClock()


def now() -> float:
    """起きていた時間。**いつ呼んでも、いま立っている時計を見る**(試験で差し替えても効く)。"""
    return _clock.now()


def start() -> AwakeClock:
    """見張りを立てる(ブラウザ版の起動で1度。``start_app._watch_for_idle``)。2度呼んでも1本。"""
    _clock.start()
    return _clock


def get() -> AwakeClock:
    return _clock


def reset() -> None:
    """試験用。見張りを止めて、ただの monotonic に戻す。"""
    global _clock
    _clock.cancel()
    _clock = AwakeClock()
