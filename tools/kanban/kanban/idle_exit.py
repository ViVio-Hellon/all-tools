"""画面が居なくなったら終わる (基盤仕様書 2.8「安全な停止」/ 2.9)

【なぜ要るのか】
このアプリに窓はありません。見えているのはブラウザのタブだけなので、
**タブを閉じたら終わったつもりになります。** ところが Python(``pythonw.exe``)
は動いたままで、しかもウィンドウを持たないためタスクマネージャーの「アプリ」
にも出てきません(「バックグラウンド プロセス」の中に埋もれます)。

その状態で次に起動すると、多重起動の判定が「すでに起動しています」と答え、
古いプロセスのブラウザを開きます ── **利用者からは「閉じたのに開けない」
「どこにも開いていないのに開いている扱い」に見えます。**

実際にそうなりました::

    2026/08/31 09:44:18 | すでに起動しています: site (pid=8372 port=8741)
    2026/08/31 09:44:18 | 起動しません: 同じアプリが起動中

【どう決めるか】
画面が一定の間隔で心拍(``POST /api/alive``)を送ります。**途切れたら誰も
見ていない**と判断して終わります。タブを閉じたことは ``sendBeacon`` で即座に
伝わるので、たいていは待たずに終わります。

【間違って落とさないための4つ】

1. **猶予を置く。** 画面の作り直し(再読込・モード切替による reload)でも
   心拍は一瞬途切れます。閉じた合図が来ても :data:`GRACE_SEC` は待ち、その
   あいだに心拍が戻れば取り消します
2. **処理中は落とさない。** 共有DBへ未反映の操作が残っている、取り込みの
   途中、といった場合は待ちます(``/api/shutdown`` と同じ判断を使う)
3. **1度も繋がっていなければ落とさない。** ``--no-browser`` で立てておく
   使い方(検証・並行運用)を巻き添えにしません
4. **画面を数える。** 誰が送った心拍かを見ます(:mod:`kanban.screen`)

4 が無かったころ、**2 枚開いているうちの 1 枚を閉じただけでアプリごと
終わって**いました。閉じた合図には猶予 8 秒、心拍は 20 秒間隔だったので、
残った 1 枚の次の心拍が猶予に間に合いません。実際に再現します::

    タブA 心拍: 200
    タブB 心拍: 200
    タブA だけ閉じる(sendBeacon leaving=1): 200
      ...
      10秒後 タブBから見たサーバ: Connection refused

いまは :class:`~kanban.screen.Screens` に「どの画面が開いているか」を持たせ、
**最後の 1 枚が閉じるまで終わりません。**

【裏に回った画面・スリープ明けで終わらない】
ブラウザは裏のタブのタイマーを間引く・止めるので、**心拍が途切れたことは
「閉じた」証拠になりません。** 別の道具では、これで開いているのに終了した
ことがありました。

5. **隠れた画面は数えたまま。** 画面は裏に回るときに合図を送り
   (:meth:`kanban.screen.Screens.hide`)、隠れた画面は心拍が無くても
   :data:`kanban.screen.HIDDEN_MAX_SEC` まで「開いている」と数えます
6. **スリープしていた時間は数えない。** 時計が飛んだら飛んだぶんを差し引き
   (:meth:`kanban.screen.Screens._now`)、さらに起きてから
   :data:`RESUME_GRACE_SEC` は終わらせません ── 画面が繋ぎ直すのを待つ
"""

from __future__ import annotations

import threading
from typing import Callable

from . import screen
from .applog import get_logger

log = get_logger("idle_exit")

#: 心拍が途切れてから終わるまで(秒)。画面は :data:`HEARTBEAT_MS` ごとに
#: 送るので、数回落としても持ちこたえる長さにする
IDLE_SEC = 90.0

#: 「閉じました」を受けてから終わるまで(秒)。再読込でも閉じた合図は飛ぶので、
#: 戻ってくるぶんを待つ
GRACE_SEC = 8.0

#: 画面が心拍を送る間隔(ミリ秒)。**画面へ渡す値の出どころはここ1つ** ──
#: 画面とサーバで別に決めると、片方を直しただけで自動終了が誤る
#:
#: 心拍は「別のタブで開いています」を知らせる経路も兼ねる(:mod:`kanban.screen`)
#: ので、**20 秒では遅すぎます** ── 持ち主を移したあと、奪われた側が 20 秒
#: 押せてしまう。中身は手元のメモリを触るだけで共有フォルダには行かないので、
#: 短くしても負荷にはならない
HEARTBEAT_MS = 5_000

#: 見張る間隔(秒)
TICK_SEC = 2.0

#: スリープ・休止から戻ったあと、終わらせずに待つ長さ(秒)。
#: 起きた直後はネットワークもブラウザもまだ戻っていない。画面が繋ぎ直す
#: より先に「心拍が無い」で終わらせない
RESUME_GRACE_SEC = 120.0


class IdleWatch:
    """画面の生き死にを見て、居なくなったら止める。"""

    def __init__(
        self,
        stop: Callable[[], None],
        busy: Callable[[], str],
        *,
        idle_sec: float = IDLE_SEC,
        grace_sec: float = GRACE_SEC,
        tick_sec: float = TICK_SEC,
        stale_sec: float | None = None,
        resume_grace_sec: float = RESUME_GRACE_SEC,
        screens: screen.Screens | None = None,
    ) -> None:
        self._stop = stop
        self._busy = busy
        """止めてはいけない理由を返す関数。空文字なら止めてよい。"""

        self.idle_sec = idle_sec
        self.grace_sec = grace_sec
        self.tick_sec = tick_sec
        self.resume_grace_sec = resume_grace_sec

        #: 開いている画面の一覧。**「誰も見ていないから終わる」と「使って
        #: よい画面はどれか」は、同じ一覧から出す** ── 別々に数えると、
        #: 片方が「まだ開いている」、もう片方が「誰も居ない」になる。
        #: 既定でプロセスに1つのものを借りる(:func:`kanban.screen.get`)
        self.screens = screens or screen.get()

        # **画面が「もう居ない」と見なされるのは、落とすより先でなければ
        # ならない。** 逆だと、返事の無くなった画面がいつまでも一覧に残り、
        # 「まだ誰か見ている」と答え続けて**無通信でも終わらなくなる**。
        # 借りている一覧の設定をここで揃える ── 2 か所で別々に決めると、
        # 片方を直したときにこの関係が崩れる
        self.screens.stale_sec = (
            stale_sec if stale_sec is not None else min(screen.STALE_SEC, idle_sec / 3)
        )

        self._done = threading.Event()
        self._thread: threading.Thread | None = None

    # -- 画面から ------------------------------------------------------
    def beat(self, screen_id: str = "", *, hidden: bool = False) -> screen.Claim:
        """画面が生きている。**閉じた合図も取り消す**(再読込のとき)。

        返すのは「この画面が持ち主か」。画面はこれを見て、使えない旨を出す。
        """
        return self.screens.beat(screen_id, hidden=hidden)

    def take_over(self, screen_id: str = "") -> screen.Claim:
        """この画面を持ち主にする(「こちらの画面を使う」)。"""
        return self.screens.take_over(screen_id)

    def leaving(self, screen_id: str = "") -> None:
        """画面が 1 枚閉じた(``sendBeacon``)。

        **最後の 1 枚が閉じたときだけ**猶予を始める。以前はここで送り主を
        区別していなかったので、2 枚のうち 1 枚を閉じただけで落ちていた。
        """
        if not self.screens.ever:
            return  # 1度も繋がっていない
        self.screens.leave(screen_id)
        rest = len(self.screens.live())
        if rest:
            log.info("画面が1つ閉じました。まだ %d 枚 開いているので続けます", rest)
            return
        log.info("最後の画面が閉じました。%.0f秒 待って終了します", self.grace_sec)

    @property
    def connected(self) -> bool:
        """1度でも画面が繋がったか。"""
        return self.screens.ever

    # -- 見張り --------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="idle-watch", daemon=True
        )
        self._thread.start()
        log.info(
            "自動終了の見張りを始めました(無通信 %.0f秒 / 閉じたら %.0f秒 / "
            "裏に回った画面は %.0f時間まで待つ)",
            self.idle_sec,
            self.grace_sec,
            self.screens.hidden_max_sec / 3600,
        )

    def cancel(self) -> None:
        self._done.set()

    def _loop(self) -> None:
        while not self._done.wait(self.tick_sec):
            why = self.overdue()
            if why is None:
                continue
            busy = ""
            try:
                busy = self._busy() or ""
            except Exception:  # noqa: BLE001 - 判定できないなら止めてよい
                log.exception("処理中かどうかの判定でエラー")
            if busy:
                # **処理中は落とさない。** 書き戻しの途中で終わると、どこまで
                # 送れたのか分からなくなる。終わればまた見に来る
                log.info("誰も見ていませんが、処理中なので待ちます: %s", busy)
                continue
            log.info("誰も見ていないので終了します(%s)", why)
            self._done.set()
            self._stop()
            return

    def overdue(self) -> str | None:
        """終わってよいか。よければ理由、まだなら ``None``。"""
        if not self.screens.ever:
            # 1度も繋がっていない。``--no-browser`` で立てておく使い方を
            # 巻き添えにしない
            return None
        # **この一覧の時計で測る。** スリープしていた時間を飛ばした時刻なので、
        # 起きた直後に「何時間も心拍が無い」と読まない
        now = self.screens.now()
        if self.screens.live():
            # **まだ誰かが見ている。** 何枚閉じられても、1 枚でも残って
            # いれば落とさない(裏に回って心拍が間引かれている画面も含む)
            return None
        resumed = self.screens.resumed_at
        if resumed is not None and now - resumed < self.resume_grace_sec:
            # スリープ明け。画面が繋ぎ直すのを待つ
            return None
        left = self.screens.left_at
        if left is not None and now - left >= self.grace_sec:
            return "画面が閉じられました"
        seen = self.screens.last_seen
        if seen is not None and now - seen >= self.idle_sec:
            return f"{self.idle_sec:.0f}秒 心拍がありません"
        return None


# ------------------------------------------------------------------
# プロセスに1つ
# ------------------------------------------------------------------
_watch: IdleWatch | None = None
_lock = threading.Lock()


def install(stop: Callable[[], None], busy: Callable[[], str], **kwargs) -> IdleWatch:
    """見張りを1つ立てる。2度呼んでも1つ。"""
    global _watch
    with _lock:
        if _watch is None:
            _watch = IdleWatch(stop, busy, **kwargs)
            _watch.start()
        return _watch


def get() -> IdleWatch | None:
    return _watch


def reset() -> None:
    """テスト用。**画面の一覧も一緒に捨てる** ── 見張りだけ作り直すと、
    前の試験で開いた画面が残ったままになる。
    """
    global _watch
    with _lock:
        if _watch is not None:
            _watch.cancel()
        _watch = None
    screen.reset()
