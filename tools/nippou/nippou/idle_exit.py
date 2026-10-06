"""画面が居なくなったら終わる (基盤仕様書 2.8「自動終了」)

【なぜ要るのか】
このアプリに窓はありません。見えているのはブラウザのタブだけなので、
**タブを閉じたら終わったつもりになります。** ところが Python は動いた
ままで、次に起動すると多重起動の判定が「すでに起動しています」と答え、
古いプロセスのブラウザが開きます ── 入れ替えたはずの新しい版が、
いつまでも動きません。

【どう決めるか】
画面が一定の間隔で心拍(`POST /api/alive`)を送ります。**途切れたら
誰も見ていない**と判断して終わります。タブを閉じたことは `sendBeacon`
で即座に伝わるので、たいていは待たずに終わります。

【間違って落とさないための3つ】
1. **猶予を置く。** 画面の作り直し(再読込・ラインの切り替え)でも心拍は
   一瞬途切れます。閉じた合図が来ても `GRACE_SEC` は待ち、そのあいだに
   心拍が戻れば取り消します
2. **処理中は落とさない。** Accessへの反映(書き戻し)の途中で終わると、
   ヘッダだけ送れて明細が送れていない、という中途半端な状態が
   共有のAccessに残ります(`/api/shutdown` と同じ判断を使う)
3. **1度も繋がっていなければ落とさない。** `--no-browser` で立てておく
   使い方(検証・並行運用)を巻き添えにしません

【裏に回ったタブは、心拍が止まっても落とさない】(v3.79.0)

    別開発のツールがブラウザがバックグラウンドタブのタイマーを間引くことで、
    心拍が途切れてセッションが誤って終了することがありました

このアプリも同じ作りでした。ブラウザは**見えていないタブのタイマーを
間引きます**(Chrome は隠れて5分たつと1分に1回、Edge の「スリープ中の
タブ」や Chrome のメモリセーバーは**止めて**しまう)。Excel を全画面で
開いた・窓を最小化した、だけでタブは「見えていない」になり、止められた
タブからは心拍が来ません ── 90秒で「誰も見ていない」と判断して終わって
いました。戻ってきた人には「繋がりません」だけが見えます。

そこで、画面は**裏に回る瞬間に `hidden` を送ります**(`sendBeacon`)。

    見えているタブ   … これまでどおり。90秒 心拍が無ければ居ないとみなす
    裏に回ったタブ   … **心拍が止まっても居るものとみなす**(`HIDDEN_KEEP_SEC`)
    閉じたタブ       … その1枚だけ外す。**最後の1枚が閉じたときだけ**終わる

**タブごとに数えます。** これまでは「閉じた」を1つ受けたらアプリごと
終わる支度に入っていたので、2枚開いていて1枚を閉じると、残った1枚の
心拍がたまたま8秒以内に来なければ終わっていました(心拍は20秒ごとなので、
半分以上の確率で)。

**時刻はスリープを数えません**(`awake_clock`)。Windows ではスリープ中も
時計が進むので、フタを開けた瞬間「90秒 心拍がありません」で終わって
いました。

**閉じたページからの遅れた合図で生き返らせない。** タブを閉じると、
ブラウザは「裏に回った」と「閉じた」を続けて出します。どちらも
`sendBeacon` なので、サーバに着く順は決まっていません ── 「閉じた」の
あとに「裏に回った」が着くと、閉じたタブが裏のタブとして7日残り、
アプリが終わらなくなります(送りかけの心拍でも同じ)。そこで画面は
**読み込むたびに別のページ番号**(`page`)を添え、閉じたページ番号から
来たものは受け付けません。再読込したページは番号が変わるので、
これまでどおり猶予の内に戻ってこられます。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Optional

from . import awake_clock
from .logging_setup import get_logger

log = get_logger("idle_exit")

# 心拍が途切れてから終わるまで(秒)。画面は `HEARTBEAT_MS` ごとに送るので、
# 数回落としても持ちこたえる長さにする
IDLE_SEC = 90.0

# 「閉じました」を受けてから終わるまで(秒)。
# 再読込でも閉じた合図は飛ぶので、戻ってくるぶんを待つ
GRACE_SEC = 8.0

# 画面が心拍を送る間隔(ミリ秒)。**画面へ渡す値の出どころはここ1つ**
HEARTBEAT_MS = 20_000

# 見張る間隔(秒)
TICK_SEC = 2.0

# 裏に回ったタブを、心拍が無くても居るものとみなす長さ(秒・起きていた時間)。
# **ブラウザが止めたタブからは何も来ない**ので、ここは「待つ」ではなく
# 「置き去りの後始末」の長さ。7日 ── 窓を最小化したまま週末を越えても
# 落とさない。閉じれば `leaving` がその場で届くので、これを待つことはまず無い
HIDDEN_KEEP_SEC = 7 * 24 * 3600.0

# 閉じたページ番号を覚えておく長さ(秒)。遅れた合図は長くても数秒で着くので、
# 十分に長く、しかし増え続けない長さ
GONE_KEEP_SEC = 600.0


@dataclass
class Screen:
    """開いているタブ1枚の様子。"""

    seen: float                 # 最後に合図が来た時刻(`awake_clock`)
    hidden: bool = False        # 最後の合図で「裏に回っている」と言ったか
    page: str = ""              # 読み込みごとのページ番号(古い画面は "")


class IdleWatch:
    """画面の生き死にを見て、居なくなったら止める。"""

    def __init__(self, stop: Callable[[], None],
                 busy: Callable[[], bool],
                 *, idle_sec: float = IDLE_SEC,
                 grace_sec: float = GRACE_SEC,
                 tick_sec: float = TICK_SEC,
                 hidden_sec: float = HIDDEN_KEEP_SEC,
                 clock: Optional[Callable[[], float]] = None) -> None:
        self._stop = stop
        self._busy = busy
        self.idle_sec = idle_sec
        self.grace_sec = grace_sec
        self.tick_sec = tick_sec
        self.hidden_sec = hidden_sec
        # **スリープを数えない時計。** 試験は自分の時計を渡せる
        self._clock = clock or awake_clock.now

        self._lock = threading.Lock()
        # 開いているタブ。鍵は画面の名札(`api.js` の tabId)。
        # 名札を送ってこない古い画面は "" の1枚として数える
        self._screens: dict[str, Screen] = {}
        # 閉じたページ番号と、閉じた時刻。**ここから来た合図は受け付けない**
        self._gone: dict[str, float] = {}
        self._ever = False                      # 1度でも繋がったか
        self._leaving_at: Optional[float] = None
        self._done = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- 画面から ------------------------------------------------------
    def beat(self, tab: str = "", *, hidden: bool = False, page: str = "") -> bool:
        """画面が生きている。**閉じた合図も取り消す**(再読込のとき)。

        `hidden=True` は「裏に回った(見えていない)」。ブラウザが心拍を
        間引く・止める状態なので、**以後は心拍が来なくても居るものとみなす。**

        閉じたページ(`page`)から遅れて着いたものは受け付けない(False)。
        """
        with self._lock:
            if page and page in self._gone:
                return False
            before = self._screens.get(tab)
            self._screens[tab] = Screen(seen=self._clock(), hidden=hidden, page=page)
            self._ever = True
            self._leaving_at = None
        if before is not None and before.hidden != hidden:
            log.debug("画面が%sました: %s", "裏に回り" if hidden else "戻り", tab or "-")
        return True

    def hide(self, tab: str = "", *, page: str = "") -> bool:
        """裏に回った(`visibilitychange` / `freeze`)。"""
        return self.beat(tab, hidden=True, page=page)

    def leaving(self, tab: str = "", *, page: str = "") -> None:
        """画面が閉じた(`sendBeacon`)。**最後の1枚なら**猶予のあとで落とす。

        ほかにタブが残っていれば、その1枚を外すだけ ── 2枚開いていて
        1枚を閉じただけで、アプリごと終わってはいけない。

        同じタブでも**別のページ**(再読込で先に着いた新しいほう)は外さない。
        """
        with self._lock:
            if not self._ever:
                return                          # 1度も繋がっていない
            now = self._clock()
            if page:
                self._gone[page] = now
                for old in [p for p, at in self._gone.items()
                            if now - at > GONE_KEEP_SEC]:
                    del self._gone[old]
            current = self._screens.get(tab)
            if current is not None and (not page or not current.page
                                        or current.page == page):
                del self._screens[tab]
            self._prune(now)
            left = len(self._screens)
            if not left:
                self._leaving_at = now
        if left:
            log.info("画面が1枚閉じました(残り %d枚)", left)
        else:
            log.info("画面が閉じました。%.0f秒 待って終了します", self.grace_sec)

    @property
    def connected(self) -> bool:
        """1度でも画面が繋がったか。"""
        with self._lock:
            return self._ever

    def screens(self) -> dict:
        """いま居るとみなしているタブの数(`/api/health` に出す)。"""
        with self._lock:
            self._prune(self._clock())
            hidden = sum(1 for s in self._screens.values() if s.hidden)
            return {"visible": len(self._screens) - hidden, "hidden": hidden}

    # -- 見張り --------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="idle-watch",
                                        daemon=True)
        self._thread.start()
        log.info("自動終了の見張りを始めました(無通信 %.0f秒 / 閉じたら %.0f秒 / "
                 "裏に回ったタブは %s)",
                 self.idle_sec, self.grace_sec, _span(self.hidden_sec))

    def cancel(self) -> None:
        self._done.set()

    def _loop(self) -> None:
        while not self._done.wait(self.tick_sec):
            why = self.overdue()
            if why is None:
                continue
            if self._busy():
                # **処理中は落とさない。** 書き戻しの途中で終わると、
                # 共有のAccessが中途半端な状態で残る。終わればまた見に来る
                log.info("誰も見ていませんが、処理中なので待ちます")
                continue
            log.info("誰も見ていないので終了します(%s)", why)
            self._done.set()
            self._stop()
            return

    def overdue(self) -> Optional[str]:
        """終わってよいか。よければ理由、まだなら `None`。"""
        with self._lock:
            if not self._ever:
                # 1度も繋がっていない。`--no-browser` で立てておく使い方を
                # 巻き添えにしない
                return None
            now = self._clock()
            leaving = self._leaving_at
            if leaving is not None:
                # 最後の1枚が閉じた。**猶予のうちは待つ**(再読込なら戻ってくる)
                if now - leaving >= self.grace_sec:
                    return "画面が閉じられました"
                return None
            hidden_gone = any(s.hidden for s in self._screens.values())
            self._prune(now)
            if self._screens:
                return None
        if hidden_gone:
            return f"裏に回ったまま {_span(self.hidden_sec)} 音沙汰がありません"
        return f"{self.idle_sec:.0f}秒 心拍がありません"

    def _prune(self, now: float) -> None:
        """もう居ないとみなすタブを外す。**錠の中で呼ぶ。**

        見えているタブは `idle_sec`、裏に回ったタブは `hidden_sec`。
        裏のタブはブラウザが心拍を止めるので、短く見切ると誤って落とす。
        """
        for tab in [t for t, s in self._screens.items()
                    if now - s.seen >= (self.hidden_sec if s.hidden else self.idle_sec)]:
            del self._screens[tab]


def _span(sec: float) -> str:
    """長さを人の言葉で(ログと理由に出す)。"""
    if sec >= 2 * 86400:
        return f"{sec / 86400:.0f}日"
    if sec >= 3600:
        return f"{sec / 3600:.0f}時間"
    return f"{sec:.0f}秒"


# ------------------------------------------------------------------
# プロセスに1つ
# ------------------------------------------------------------------
_watch: Optional[IdleWatch] = None
_lock = threading.Lock()


def install(stop: Callable[[], None], busy: Callable[[], bool],
            **kwargs) -> IdleWatch:
    """見張りを1つ立てる。2度呼んでも1つ。"""
    global _watch
    with _lock:
        if _watch is None:
            _watch = IdleWatch(stop, busy, **kwargs)
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
