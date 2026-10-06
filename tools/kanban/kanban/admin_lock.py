"""管理者パスワードの鍵 ── 1 度開けたら、しばらく訊かない

【なぜ要るのか】
設定画面の守られた操作(モード・担当ライン・接続先・マスタの修正…)は、
以前は**押すたびに**パスワードを訊いていました。マスタを 5 マス直せば
5 回打つことになり、「必要なときに訊く」とはいえ不便でした。しかも
パスワードを入れるだけの場所が無かったので、先にまとめて開けておく
こともできませんでした。

そこで python-web-tools の「マスタ編集の認証」と同じく、**鍵を開ける**
状態をプロセスに 1 つ持ちます。

* 設定画面の「パスワード認証」で開ける(守られた操作の確認画面で
  パスワードを入れても開く)
* 開いているあいだは、守られた操作でパスワードを訊かない
* **閉じるのは 3 通り** ── 「鍵をかける」を押す / 守られた操作を
  :data:`IDLE_LOCK_SEC` しなかった / アプリを閉じる(プロセスが終わる)

【開けっぱなしにしない】
押すたびに訊いていたのは、開きっぱなしのタブが解錠のまま残るのを避ける
ためでした。現場の端末は 1 日つけっぱなしのことがあるので、使わない
時間が続いたら自動で閉めます。時計は ``time.monotonic()`` で、Windows では
スリープ中も進みます ── **スリープから起きたら閉まっている**のが正しい。
"""

from __future__ import annotations

import threading
import time
from typing import Callable

#: 守られた操作をこれだけしなかったら、鍵を閉める(秒)
IDLE_LOCK_SEC = 30 * 60


class AdminLock:
    """鍵の状態。プロセスに 1 つ(:func:`get`)。"""

    def __init__(self, *, idle_sec: float = IDLE_LOCK_SEC,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.idle_sec = idle_sec
        self._clock = clock
        self._lock = threading.Lock()
        self._until: float | None = None

    def unlock(self) -> None:
        """開ける(パスワードを確かめたあとに呼ぶ)。"""
        with self._lock:
            self._until = self._clock() + self.idle_sec

    def lock(self) -> None:
        with self._lock:
            self._until = None

    def is_unlocked(self, *, touch: bool = False) -> bool:
        """開いているか。``touch`` なら、使ったとして閉まるまでを延ばす。"""
        with self._lock:
            now = self._clock()
            if self._until is None or now >= self._until:
                self._until = None
                return False
            if touch:
                self._until = now + self.idle_sec
            return True

    def remaining_sec(self) -> int:
        """閉まるまでの秒数。閉まっていれば 0。"""
        with self._lock:
            if self._until is None:
                return 0
            return max(0, int(self._until - self._clock()))


_state: AdminLock | None = None
_guard = threading.Lock()


def get() -> AdminLock:
    global _state
    with _guard:
        if _state is None:
            _state = AdminLock()
        return _state


def reset() -> None:
    """試験用。"""
    global _state
    with _guard:
        _state = None
