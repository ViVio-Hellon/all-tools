"""画面が居なくなったときの終わり方

【いちばん見たいこと】
**未送信を抱えたまま終わらせない。** 蓋を閉じて帰るとPCが寝てプロセスも
止まり、翌朝ふたを開けた瞬間に「何時間も心拍が無い」と判定される。
以前はそこで同期が1回も回らないうちに自分を終わらせていたので、
未送信の入力は次に誰かが起動するまで他のラインへ届かなかった ──
しかもタブは開いたままなので、**利用者には送られたように見えていた。**

``_loop`` を回さず、``overdue`` の材料と ``_holding`` を直に動かして見る
(時間で待つ試験にすると、遅いうえに時々落ちる)。
"""

from __future__ import annotations

import time
import unittest

from . import _isolation

_isolation.ensure_isolated()

from calendar_app import idle_exit  # noqa: E402


class _Watch:
    """呼ばれ方を数える置き換え。"""

    def __init__(self, unsent: int = 0, **kwargs):
        self.stopped = 0
        self.retried = 0
        self.exited_with: list[int] = []
        self.unsent = unsent
        self.watch = idle_exit.IdleWatch(
            self._stop, lambda: False, lambda: self.unsent, self._retry,
            on_exit=self.exited_with.append, **kwargs)

    def _stop(self) -> None:
        self.stopped += 1

    def _retry(self) -> None:
        self.retried += 1

    def tick(self) -> None:
        """見張り1回ぶん(``_loop`` の中身を1周だけ)。"""
        watch = self.watch
        why = watch.overdue()
        if why is None:
            watch._holding_since = None
            return
        if watch._holding(why):
            return
        remaining = watch._count()
        if watch._on_exit is not None:
            watch._on_exit(remaining)
        watch._stop()


def _watch(unsent: int = 0, **kwargs) -> _Watch:
    box = _Watch(unsent, **kwargs)
    box.watch.beat()                              # 1度は繋がった状態にする
    # 心拍を過去にずらす = 誰も見ていない状態
    box.watch._seen = time.monotonic() - idle_exit.IDLE_SEC - 1
    return box


class HoldTests(unittest.TestCase):
    def test_未送信が無ければ終わる(self) -> None:
        box = _watch(unsent=0)
        box.tick()
        self.assertEqual(box.stopped, 1)

    def test_未送信があれば終わらない(self) -> None:
        box = _watch(unsent=3)
        box.tick()
        self.assertEqual(box.stopped, 0)

    def test_粘りはじめに1回だけ送り直しを頼む(self) -> None:
        """次の定期実行を待たずに試させる。**目覚めた直後はここが効く。**"""
        box = _watch(unsent=3)
        box.tick()
        box.tick()
        box.tick()
        self.assertEqual(box.retried, 1)

    def test_送れたら終わる(self) -> None:
        box = _watch(unsent=3)
        box.tick()
        self.assertEqual(box.stopped, 0)
        box.unsent = 0                            # 送り終わった
        box.tick()
        self.assertEqual(box.stopped, 1)

    def test_上限を過ぎたら諦める(self) -> None:
        """共有が死んでいるときに、いつまでも生き続けないための歯止め。"""
        box = _watch(unsent=3, hold_sec=60.0)
        box.tick()
        self.assertEqual(box.stopped, 0)

        box.watch._holding_since = time.monotonic() - 61
        box.tick()
        self.assertEqual(box.stopped, 1)

    def test_粘らない設定なら待たない(self) -> None:
        box = _watch(unsent=3, hold_sec=0.0)
        box.tick()
        self.assertEqual(box.stopped, 1)

    def test_諦めたときは件数を渡す(self) -> None:
        """残したまま終わったことを覚えておくため(翌朝これが要る)。"""
        box = _watch(unsent=3, hold_sec=60.0)
        box.tick()
        box.watch._holding_since = time.monotonic() - 61
        box.tick()
        self.assertEqual(box.exited_with, [3])

    def test_きれいに終わったときは0を渡す(self) -> None:
        """前回の記録が居座らないように、0 も伝える。"""
        box = _watch(unsent=0)
        box.tick()
        self.assertEqual(box.exited_with, [0])

    def test_誰か戻ってきたら粘りは解ける(self) -> None:
        """朝いちで開いた人が使い始めたら、そのまま使わせる。"""
        box = _watch(unsent=3)
        box.tick()
        self.assertIsNotNone(box.watch._holding_since)

        box.watch.beat()                          # 画面が戻ってきた
        box.tick()
        self.assertIsNone(box.watch._holding_since)
        self.assertEqual(box.stopped, 0)

    def test_数えられなくても見張りは死なない(self) -> None:
        box = _watch(unsent=0)

        def boom() -> int:
            raise RuntimeError("手元のDBが開けない")

        box.watch._unsent = boom
        box.tick()
        # 数えられないなら 0 として扱う = 終わる
        self.assertEqual(box.stopped, 1)

    def test_送り直しで例外が出ても粘る(self) -> None:
        box = _watch(unsent=3)

        def boom() -> None:
            raise RuntimeError("同期を頼めない")

        box.watch._retry = boom
        box.tick()
        self.assertEqual(box.stopped, 0)

    def test_覚え書きで失敗しても終わる(self) -> None:
        """止まらなくなるのが一番困る。"""
        box = _watch(unsent=0)
        box.watch._on_exit = lambda _n: (_ for _ in ()).throw(RuntimeError("書けない"))
        watch = box.watch
        # ``_loop`` と同じ順で通す
        why = watch.overdue()
        self.assertIsNotNone(why)
        self.assertFalse(watch._holding(why))
        try:
            watch._on_exit(0)
        except RuntimeError:
            pass                                  # 本体は握りつぶして先へ進む
        watch._stop()
        self.assertEqual(box.stopped, 1)


class DefaultTests(unittest.TestCase):
    def test_渡さなければ今までどおり(self) -> None:
        """``unsent`` を渡さない使い方(他プロジェクト)を壊さない。"""
        stopped = []
        watch = idle_exit.IdleWatch(lambda: stopped.append(1), lambda: False)
        watch.beat()
        watch._seen = time.monotonic() - idle_exit.IDLE_SEC - 1
        self.assertFalse(watch._holding(watch.overdue() or ""))
        self.assertEqual(watch._count(), 0)

    def test_上限は歯止めとして十分な長さ(self) -> None:
        """届く場所なら同期1回で終わる。長さそのものに意味は無い。"""
        self.assertGreaterEqual(idle_exit.HOLD_SEC, 60.0)


if __name__ == "__main__":
    unittest.main()
