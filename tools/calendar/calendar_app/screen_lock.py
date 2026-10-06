"""画面は1台につき1つだけ ── タブを2枚開かせない

【何が困るのか】
起動しているプロセスは1つでも、**ブラウザのタブは何枚でも開けます。**
2枚開くと、どちらでも登録できて、どちらも「自分が見ているものが最新」の
顔をします。実際に起きるのはこういうことです。

* 片方で登録 → もう片方は古い月を出したまま(気づくのは次の描き直し)
* 設定を両方で開く → 後から保存したほうが黙って勝つ
  (先に直したほうの人は、直したつもりのままになる)
* 削除のダイアログを開いたまま、もう片方で同じ日を触る

どれも「壊れた」とは見えず、**どちらが本当か分からない**という形で出ます。
プロセスの二重起動(``launch_guard``)とは別の話で、あちらを止めても
こちらは止まりません。

【どうするか】
**画面が使ってよいのは1つだけ**にします。2枚目は開いた時点で断り、
操作させません。ただし**取って代わる道は必ず残します** ── 前の画面が
異常終了して合図を送れなかったとき、何十秒も待たせるほうが現場では
困るためです(「この画面で使う」を押せば入れ替わります)。

【生きているかの見分け方】
画面は20秒ごとに心拍を送ります(``POST /api/alive``)。
``SCREEN_STALE_SEC`` のあいだ心拍が無ければ「もう居ない」とみなし、
次の画面がそのまま入れます。タブを閉じた合図(``sendBeacon``)が届けば
即座に空きます。

**裏に回った画面は「もう居ない」とみなしません。** ブラウザは見えていない
タブのタイマーを間引き(1分に1回)、凍らせることもある(心拍が止まる)ので、
心拍の途切れで空けると、Excel を見ているあいだに2枚目が黙って入れて
しまいます。画面は裏に回る瞬間に合図を送るので(``hidden``)、その画面が
表に戻るか閉じるまでは空けません。閉じた合図が届かずに残ったときは、
2枚目の「この画面で使う」で取って代われます。

【画面の見分け方】
画面ごとの ID はブラウザの ``sessionStorage`` に持ちます ──
**タブごとに別で、画面遷移(カレンダー↔設定)では変わらない**ため、
「同じタブの中で移動しただけ」と「もう1枚開いた」を取り違えません。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional

from .logging_utils import get_logger

log = get_logger("screen_lock")

#: 心拍が途切れてから「もう居ない」とみなすまで(秒)。
#: 画面は20秒ごとに送るので、2回落としても持ちこたえる長さにする
SCREEN_STALE_SEC = 45.0

#: 断りの種別。**文言ではなくこれで見分ける**(設計 §1 の規則4)
REFUSE_OTHER_SCREEN = "other_screen"


@dataclass
class Screen:
    """いま使っている画面1つ。"""

    id: str
    since: float                      # 使い始めた時刻 (単調時計)
    seen: float                       # 最後の心拍 (単調時計)
    #: 画面に出すための実時刻。``since`` は単調時計なので日時にできない
    since_wall: float = 0.0
    #: 裏に回っているか。**裏の画面は心拍が途切れても居なくならない**
    hidden: bool = False

    def idle_sec(self, now: Optional[float] = None) -> float:
        return (now if now is not None else time.monotonic()) - self.seen

    def stale(self, now: Optional[float] = None,
              limit: float = SCREEN_STALE_SEC) -> bool:
        if self.hidden:
            return False                  # 裏の画面の心拍は当てにしない
        return self.idle_sec(now) >= limit

    def since_text(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.since_wall))


@dataclass
class ClaimResult:
    """使ってよいか。駄目なら**誰が使っているか**。"""

    ok: bool
    holder: Optional[Screen] = None
    reason: str = ""
    message: str = ""


class ScreenLock:
    """使ってよい画面を1つだけ通す。**プロセスに1つ**。"""

    def __init__(self, stale_sec: float = SCREEN_STALE_SEC) -> None:
        self._lock = threading.Lock()
        self._active: Optional[Screen] = None
        self.stale_sec = stale_sec

    # ------------------------------------------------------------------
    def claim(self, screen_id: str, *, force: bool = False) -> ClaimResult:
        """この画面で使わせてよいか。

        通すのは4つの場合だけ:

        1. まだ誰も使っていない
        2. **同じ画面**(タブの中で移動しただけ)
        3. 前の画面の心拍が途切れている(もう居ない)
        4. ``force`` ── 利用者が「この画面で使う」を押した
        """
        screen_id = str(screen_id or "").strip()
        if not screen_id:
            return ClaimResult(False, reason="bad_id",
                               message="画面を識別できませんでした。")

        now = time.monotonic()
        with self._lock:
            current = self._active
            if current is not None and current.id != screen_id \
                    and not force and not current.stale(now, self.stale_sec):
                where = ("別の画面(裏に回っているタブ)" if current.hidden
                         else "別の画面")
                return ClaimResult(
                    False, holder=current, reason=REFUSE_OTHER_SCREEN,
                    message=f"この端末では、すでに{where}で開いています。")

            if current is not None and current.id == screen_id:
                current.seen = now                # 同じ画面。心拍を更新するだけ
                current.hidden = False            # 名乗るのは表に居る画面
                return ClaimResult(True, holder=current)

            if current is not None:
                why = "取って代わりました" if force else "前の画面の心拍が途切れていました"
                log.info("画面を入れ替えます(%s)", why)
            self._active = Screen(id=screen_id, since=now, seen=now,
                                  since_wall=time.time())
            return ClaimResult(True, holder=self._active)

    def beat(self, screen_id: str, *, hidden: bool = False) -> bool:
        """心拍。**いま使ってよい画面かどうか**を返す。

        ``False`` が返った画面は、別の画面に取って代わられている。
        画面側はそれを見て操作をやめる(黙って古い表示を続けない)。

        ``hidden`` は「裏に回った」。表に戻る心拍(``hidden=False``)で解く。
        """
        screen_id = str(screen_id or "").strip()
        if not screen_id:
            return True                           # 画面IDを持たない相手は判定しない
        with self._lock:
            current = self._active
            if current is None:
                return True                       # まだ誰も名乗っていない
            if current.id != screen_id:
                return False
            current.seen = time.monotonic()
            current.hidden = hidden
            return True

    def release(self, screen_id: str) -> None:
        """その画面が閉じた。**自分が使っていたときだけ**空ける。"""
        screen_id = str(screen_id or "").strip()
        with self._lock:
            if self._active is not None and self._active.id == screen_id:
                self._active = None
                log.info("画面が閉じられました。次の画面が使えます")

    def is_active(self, screen_id: str) -> bool:
        """その画面が、いま使ってよい画面か(心拍は更新しない)。

        **名前を持たない相手は判定しない。** 画面を通らない経路
        (``stop.bat``・コマンドからの取り込み)を巻き込まないため。
        守りたいのは「2枚目のタブ」で、こちらの画面は必ず名乗る。
        """
        screen_id = str(screen_id or "").strip()
        if not screen_id:
            return True
        with self._lock:
            current = self._active
            if current is None:
                return True
            if current.stale(limit=self.stale_sec):
                return True                       # もう居ない相手に譲らない
            return current.id == screen_id

    def active(self) -> Optional[Screen]:
        with self._lock:
            current = self._active
            if current is not None and current.stale(limit=self.stale_sec):
                return None
            return current


# ---------------------------------------------------------------------------
# プロセスに1つ
# ---------------------------------------------------------------------------
_instance: Optional[ScreenLock] = None
_instance_lock = threading.Lock()


def get() -> ScreenLock:
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = ScreenLock()
        return _instance


def reset() -> None:
    """テスト用。"""
    global _instance
    with _instance_lock:
        _instance = None
