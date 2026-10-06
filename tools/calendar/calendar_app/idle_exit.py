"""画面が居なくなったら終わる (基盤仕様書 2.8「安全な停止」)

【なぜ要るのか】
このアプリに窓は無い。見えているのはブラウザのタブだけなので、
**タブを閉じたら終わったつもりになる。** ところが Python は動いたままで、
次に起動すると多重起動の判定が「すでに起動しています」と答え、
古いプロセスのブラウザが開く ── 入れ替えたはずの新しい版が、
いつまでも動かない。

【どう決めるか】
画面が一定の間隔で心拍(``POST /api/alive``)を送る。**途切れたら
誰も見ていない**と判断して終わる。タブを閉じたことは ``sendBeacon`` で
即座に伝わるので、たいていは待たずに終わる。

【間違って落とさないための6つ】
1. **猶予を置く。** 画面の作り直し(再読込・画面遷移)でも心拍は
   一瞬途切れる。閉じた合図が来ても ``GRACE_SEC`` は待ち、そのあいだに
   心拍が戻れば取り消す
2. **処理中は落とさない。** 取り込み元への送信中に終わると、送れたかどうかが
   分からないまま終わる(``/api/shutdown`` と同じ判断を使う)
3. **1度も繋がっていなければ落とさない。** ``--no-browser`` で立てておく
   使い方(検証)を巻き添えにしない
4. **まだ外へ出せていない仕事があれば、少し粘る**(``HOLD_SEC``)。
   下の説明を参照
5. **裏に回った画面の心拍は当てにしない。** 下の説明を参照
6. **プロセス自身が止まっていたら(スリープ)、戻った時点から数え直す**

【なぜ粘るのか ── 効くのは「目覚めた瞬間」】
蓋を閉じて帰ると、PC が寝てプロセスも止まる。そのあいだは何も送れない。
翌朝、蓋を開けるとプロセスが動き出すが、``time.monotonic()`` は休止中も
進むので「何時間も心拍が無い」と判定され、**同期が1回も回らないうちに
自分を終わらせていた。** 未送信の入力は、次に誰かが起動するまで他の
ラインへ届かない ── しかもタブは開いたままなので、**利用者には送られた
ように見える。**

粘るのはこの一瞬のためで、届く場所にあれば数十秒で送り終わって終了する。
上限(``HOLD_SEC``)が効くのは共有が本当に死んでいるときだけで、
いつまでも生き続けないための歯止めにすぎない。

**送る先が決まっていないときは粘らない。** 待っても送れないので、
``unsent`` を渡す側が 0 を返す(``sync_service.unsent_count``)。

【裏に回った画面 ── 心拍が止まっても終わらない】
ブラウザは、見えていないタブのタイマーを間引きます。Chrome は隠れて
5分たつと1分に1回まで、Edge の「スリープ中のタブ」や Chrome の省メモリは
タブごと凍らせるので、**心拍が完全に止まります。** 別の道具で実際に、
Excel を見ているあいだにアプリが終わっていた、が起きました。

そこで画面は、**裏に回る瞬間に「隠れます」と合図**します(``sendBeacon``。
凍らされる直前でも届く)。合図を受けた画面が1つでもあるあいだは、
**心拍が途切れても終わりません。** 終わるのは、閉じた合図(``pagehide``)が
来たとき・「終了」を押したとき・``stop.bat`` だけです。

上限は置いていません。閉じた合図が届かないままブラウザごと落ちると
プロセスが残りますが、次に ``Start.vbs`` を押せばそのプロセスに合流し、
版が違えば入れ替わる(``launch_guard``)ので、残って困ることがありません。
上限を置くと、昼休みに裏へ回したタブが戻ったら終わっていた、が起きます。

表に戻ったら、画面はすぐに心拍を送り直します(``visibilitychange`` /
``resume`` / ``pageshow``)。

【スリープから戻ったとき】
Windows の ``time.monotonic()`` は休止中も進むので、蓋を開けた瞬間に
「何時間も心拍が無い」と判定して**画面が心拍を送る前に**終わっていました。
見張りの1周が ``WAKE_GAP_SEC`` 以上あいたら「プロセスごと止まっていた」と
みなし、**そこから数え直します**(画面が戻ってくるのを ``IDLE_SEC`` 待つ)。
あわせて同期を1回頼みます ── 寝ているあいだの他ラインの登録を早く見せる。
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from .logging_utils import get_logger

log = get_logger("idle_exit")

# 心拍が途切れてから終わるまで(秒)。画面は ``HEARTBEAT_MS`` ごとに送るので、
# 数回落としても持ちこたえる長さにする
IDLE_SEC = 90.0

# 「閉じました」を受けてから終わるまで(秒)。
# 再読込でも閉じた合図は飛ぶので、戻ってくるぶんを待つ
GRACE_SEC = 8.0

# 画面が心拍を送る間隔(ミリ秒)。画面へ渡す値の出どころはここ1つ
HEARTBEAT_MS = 20_000

# 見張る間隔(秒)
TICK_SEC = 2.0

# 見張りの1周がこれ以上あいたら、プロセスごと止まっていた(スリープ・
# 休止)とみなす(秒)。1周は ``TICK_SEC`` なので、十分に大きく取る
WAKE_GAP_SEC = 30.0

# まだ外へ出せていない仕事があるとき、どこまで粘るか(秒)。
#
# **長さに大きな意味は無い。** 届く場所にあれば同期1回(既定20秒)で
# 送り終わって終了する。ここが効くのは共有が死んでいるときだけで、
# その場合はいつまで待っても送れないので、歯止めとして置いている。
HOLD_SEC = 600.0


class IdleWatch:
    """画面の生き死にを見て、居なくなったら止める。"""

    def __init__(self, stop: Callable[[], None],
                 busy: Callable[[], bool],
                 unsent: Optional[Callable[[], int]] = None,
                 retry: Optional[Callable[[], None]] = None,
                 *, idle_sec: float = IDLE_SEC,
                 grace_sec: float = GRACE_SEC,
                 tick_sec: float = TICK_SEC,
                 hold_sec: float = HOLD_SEC,
                 on_exit: Optional[Callable[[int], None]] = None) -> None:
        """
        :param busy:   いま処理中か(処理中は落とさない)
        :param unsent: **まだ外へ出せていない仕事の数。** 0 なら粘る理由が
                       無い。送り先が決まっていないときも 0 を返す約束
        :param retry:  粘りはじめに1回だけ頼む再送(待たずに試させる)
        :param on_exit: 終わるときに、その時点の ``unsent`` を渡す。
                       残したまま終わったことを覚えておくために使う
        """
        self._stop = stop
        self._busy = busy
        self._unsent = unsent or (lambda: 0)
        self._retry = retry
        self._on_exit = on_exit
        self.idle_sec = idle_sec
        self.grace_sec = grace_sec
        self.tick_sec = tick_sec
        self.hold_sec = hold_sec

        self._lock = threading.Lock()
        self._seen: Optional[float] = None     # 最後の心拍。None = まだ1度も
        self._leaving_at: Optional[float] = None
        #: 裏に回っている画面(画面ID → 裏に回った時刻)。**1つでもあれば
        #: 心拍の途切れでは終わらない**
        self._background: dict[str, float] = {}
        #: 閉じた合図を送ってきた画面。**あとから届く「隠れます」で閉じた
        #: 扱いを取り消さない**ため(合図は非同期で、順番が入れ替わる)
        self._left: set[str] = set()
        #: 見張りの前の1周(単調時計・実時刻)。スリープを見分けるため
        self._last_tick: Optional[tuple[float, float]] = None
        self._holding_since: Optional[float] = None
        self._done = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- 画面から ------------------------------------------------------
    def beat(self, screen_id: str = "", *, hidden: bool = False) -> None:
        """画面が生きている。**閉じた合図も取り消す**(再読込のとき)。

        ``hidden`` は「この画面は裏に回っている」。裏に回った画面は
        心拍が間引かれたり止まったりするので、その間は途切れで終わらない。
        """
        with self._lock:
            if hidden and screen_id in self._left:
                # 閉じた画面から遅れて届いた「隠れます」。閉じるときにも
                # 見え方が変わるので飛ぶ。**これで閉じた扱いを取り消すと、
                # 裏のまま閉じたタブでいつまでも終わらない**(実際になった)
                return
            self._left.discard(screen_id)
            self._seen = time.monotonic()
            self._leaving_at = None
            was = bool(self._background)
            if hidden:
                self._background.setdefault(screen_id, self._seen)
            else:
                self._background.pop(screen_id, None)
            now = bool(self._background)
        if now != was:
            log.info("画面が裏に回りました。心拍が途切れても終了しません" if now
                     else "画面が表に戻りました。心拍の見張りを再開します")

    def leaving(self, screen_id: str = "") -> None:
        """画面が閉じた(``sendBeacon``)。猶予のあとで落とす。"""
        with self._lock:
            if self._seen is None:
                return                          # 1度も繋がっていない
            self._leaving_at = time.monotonic()
            # 閉じた画面は裏にも居ない。**残すと、閉じたのに終わらない**
            self._background.pop(screen_id, None)
            self._left.add(screen_id)
        log.info("画面が閉じました。%.0f秒 待って終了します", self.grace_sec)

    def in_background(self) -> bool:
        """裏に回っている画面があるか。"""
        with self._lock:
            return bool(self._background)

    def woke(self) -> None:
        """プロセスごと止まっていた(スリープ)。**ここから数え直す。**

        画面も同じだけ止まっていたので、心拍が無かったのは画面のせいではない。
        画面が戻ってくるのを ``idle_sec`` 待つ。
        """
        with self._lock:
            if self._seen is not None:
                self._seen = time.monotonic()
        log.info("スリープから戻りました。画面の心拍を%.0f秒 待ちます", self.idle_sec)
        # 寝ているあいだの送信待ち・他ラインの登録を、次の定期実行を待たずに
        if self._retry is not None:
            try:
                self._retry()
            except Exception as exc:              # noqa: BLE001 - 見張りを止めない
                log.warning("同期を頼めませんでした: %s", exc)

    def check_wake(self, mono: Optional[float] = None,
                   wall: Optional[float] = None) -> bool:
        """前の1周から ``WAKE_GAP_SEC`` 以上あいていたら ``woke`` を呼ぶ。

        単調時計と実時刻の**大きいほう**で見る。休止中に単調時計が進むか
        どうかは OS による(Windows は進む・Linux は止まる)ので、どちらでも
        気づけるように。
        """
        mono = time.monotonic() if mono is None else mono
        wall = time.time() if wall is None else wall
        last, self._last_tick = self._last_tick, (mono, wall)
        if last is None:
            return False
        gap = max(mono - last[0], wall - last[1])
        if gap < self.tick_sec + WAKE_GAP_SEC:
            return False
        log.info("見張りが%.0f秒 止まっていました(スリープ・休止)", gap)
        self.woke()
        return True

    # -- 見張り --------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="idle-watch",
                                        daemon=True)
        self._thread.start()
        log.info("自動終了の見張りを始めました(無通信 %.0f秒 / 閉じたら %.0f秒)",
                 self.idle_sec, self.grace_sec)

    def cancel(self) -> None:
        self._done.set()

    def _loop(self) -> None:
        self.check_wake()
        while not self._done.wait(self.tick_sec):
            self.check_wake()
            why = self.overdue()
            if why is None:
                self._holding_since = None      # 誰か見に来た。粘りは解く
                continue
            if self._busy():
                # **処理中は落とさない。** 取り込み元へ送っている途中で終わると、
                # 送れたかどうかが分からないまま終わる。終わればまた見に来る
                log.info("誰も見ていませんが、処理中なので待ちます")
                continue
            if self._holding(why):
                continue

            remaining = self._count()
            if remaining:
                # **残したまま終わる。** ここを黙って終わると、翌朝
                # 「あの連絡が来ていない」まで誰も気づけない
                log.warning("未送信が%s件のまま終了します(%.0f秒 待ちました / %s)",
                            remaining, self.hold_sec, why)
            else:
                log.info("誰も見ていないので終了します(%s)", why)
            if self._on_exit is not None:
                try:
                    self._on_exit(remaining)
                except Exception as exc:          # noqa: BLE001 - 覚え書きで止めない
                    log.warning("終了時の覚え書きに失敗しました: %s", exc)
            self._done.set()
            self._stop()
            return

    def _holding(self, why: str) -> bool:
        """まだ外へ出せていない仕事があるあいだ、粘るか。

        **粘るのは送り終わるまで**で、上限に達したら諦める。
        送る先が決まっていなければ ``unsent`` が 0 を返すので、粘らない。
        """
        if self.hold_sec <= 0:
            return False                          # 粘らない設定
        if self._count() <= 0:
            self._holding_since = None
            return False

        now = time.monotonic()
        if self._holding_since is None:
            self._holding_since = now
            log.info("未送信が%s件あるので、最大%.0f秒 送り終わるのを待ちます(%s)",
                     self._count(), self.hold_sec, why)
            # 次の定期実行を待たずに1回試させる。**目覚めた直後**はここが効く
            if self._retry is not None:
                try:
                    self._retry()
                except Exception as exc:          # noqa: BLE001 - 待つのが目的
                    log.warning("送り直しを頼めませんでした: %s", exc)
            return True
        return now - self._holding_since < self.hold_sec

    def _count(self) -> int:
        """まだ外へ出せていない仕事の数。数えられなければ 0 として扱う。"""
        try:
            return int(self._unsent())
        except Exception as exc:                  # noqa: BLE001 - 見張りを止めない
            log.warning("未送信の件数を数えられませんでした: %s", exc)
            return 0

    def overdue(self) -> Optional[str]:
        """終わってよいか。よければ理由、まだなら ``None``。"""
        with self._lock:
            seen, leaving = self._seen, self._leaving_at
            background = bool(self._background)
        if seen is None:
            # 1度も繋がっていない。``--no-browser`` で立てておく使い方を
            # 巻き添えにしない
            return None
        now = time.monotonic()
        if leaving is not None and now - leaving >= self.grace_sec:
            return "画面が閉じられました"
        if background:
            # **裏に回った画面の心拍は当てにしない。** ブラウザが間引く・
            # 凍らせるので、途切れても誰も居なくなったとは言えない
            return None
        if now - seen >= self.idle_sec:
            return f"{self.idle_sec:.0f}秒 心拍がありません"
        return None


# ---------------------------------------------------------------------------
# プロセスに1つ
# ---------------------------------------------------------------------------
_watch: Optional[IdleWatch] = None
_lock = threading.Lock()


def install(stop: Callable[[], None], busy: Callable[[], bool],
            unsent: Optional[Callable[[], int]] = None,
            retry: Optional[Callable[[], None]] = None,
            **kwargs) -> IdleWatch:
    """見張りを1つ立てる。2度呼んでも1つ。"""
    global _watch
    with _lock:
        if _watch is None:
            _watch = IdleWatch(stop, busy, unsent, retry, **kwargs)
            _watch.start()
        return _watch


def get() -> Optional[IdleWatch]:
    return _watch


def reset() -> None:
    """テスト用。"""
    global _watch
    with _lock:
        if _watch is not None:
            _watch.cancel()
        _watch = None
