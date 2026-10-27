"""スリープから戻ったとき ── **眠っていた時間を「無通信」に数えない**

【何が起きていたか】
Windows の ``time.monotonic()`` はスリープ中も進みます。目覚めの見張り
(``IdleWatch.woke``)は最後の心拍の時刻しか直していなかったので、次の場面で
**起きた瞬間に終わって**いました(時計を差し替えて再現)。

* 場面a: 再読込の途中で眠った(閉じた合図は届き、新しいページの心拍がまだ)
  → 起きた最初の見回りで「画面が閉じられました」
* 場面b: タブを閉じたあと、未送信を抱えて粘っている途中で眠った
  → 起きた瞬間に 600 秒の粘りが切れた扱いになり、送り直す前に
  「未送信がN件のまま終了」

画面の錠(``screen_lock``)も同じで、起きてから持ち主の心拍が届くまでのあいだ
持ち主が「途切れた」扱いになり、そのあいだにもう一度起動すると新しいタブが
黙って持ち主になって、前の画面に「別の画面で開かれました」が出ていました。

【何を見張るか】
1. 起きていた時間だけを数える時計(``awake_clock``)が、眠っていた分を差し引く
2. 場面a・場面b で、起きた瞬間に終わらない。**時計の差し引きと、目覚めの
   見張りの数え直しの、どちらか片方だけでも**終わらない
3. 場面b では、起きてから送り直す間がある(粘りを起きた時点から数え直す)
4. 戻ってこなければ・送れなければ、やはり終わる(いつまでも生き続けない)
5. 画面の錠は、眠っていたあいだに持ち主を外さない
6. 見張りも錠も、既定でこの時計を使う。ブラウザ版の起動はこの時計を立てる

時計は試験が持ち、2秒ずつ進めて見張りを1周ずつ回す(本物の時間は待たない)。
"""

from __future__ import annotations

import unittest
from unittest import mock

from . import _isolation

_isolation.ensure_isolated()

from calendar_app import awake_clock, idle_exit, screen_lock  # noqa: E402

CLOSED = "画面が閉じられました"
NIGHT = 8 * 3600.0


class _Lab:
    """差し替えた時計と、それで動く見張り。

    :param awake:  見張りに「起きていた時間の時計」を渡すか(False = ただの monotonic)
    :param detect: 見張り自身の目覚めの気づき(``check_wake``)を効かせるか
    """

    def __init__(self, *, unsent: int = 0, awake: bool = True, detect: bool = True,
                 **kwargs) -> None:
        self.mono = 1000.0
        self.wall = 1_800_000_000.0
        # 見張りが見る時計(detect=False なら、眠っても飛ばない)
        self.tick_mono = self.mono
        self.tick_wall = self.wall
        self.detect = detect
        self.unsent = unsent
        self.retried = 0
        self.stopped = 0
        self.exited_with: list[int] = []
        self.clock = awake_clock.AwakeClock(mono=lambda: self.mono, wall=lambda: self.wall)
        self.clock.start(thread=False)
        self.watch = idle_exit.IdleWatch(
            self._stop, lambda: False, lambda: self.unsent, self._retry,
            on_exit=self.exited_with.append,
            clock=self.clock.now if awake else (lambda: self.mono), **kwargs)
        self.watch.step(*self._ticks())

    def _stop(self) -> None:
        self.stopped += 1

    def _retry(self) -> None:
        self.retried += 1

    def _ticks(self) -> tuple[float, float]:
        if self.detect:
            return self.mono, self.wall
        return self.tick_mono, self.tick_wall

    def run(self, sec: float) -> bool:
        """起きたまま ``sec`` 秒。見張りを2秒ごとに回す。終わったら True。"""
        for _ in range(int(sec // 2)):
            self.mono += 2
            self.wall += 2
            self.tick_mono += 2
            self.tick_wall += 2
            self.clock.check()                    # 時計の見張り(本物は別の糸)
            if self.watch.step(*self._ticks()):
                return True
        return False

    def sleep(self, sec: float = NIGHT) -> None:
        """Windows 型のスリープ: monotonic も実時刻も進む。プロセスは止まっている。"""
        self.mono += sec
        self.wall += sec


class AwakeClockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mono, self.wall = 1000.0, 1_800_000_000.0
        self.clock = awake_clock.AwakeClock(mono=lambda: self.mono, wall=lambda: self.wall)

    def test_眠っていた分を差し引く(self) -> None:
        self.clock.start(thread=False)
        before = self.clock.now()
        self.mono += 3600
        self.wall += 3600
        # 見張りの1回ぶん(2秒)だけ起きていた扱い
        self.assertAlmostEqual(self.clock.now() - before, awake_clock.TICK_SEC)
        self.assertTrue(self.clock.last_resume.counted)

    def test_単調時計が止まるOSでは差し引かない(self) -> None:
        """Linux のスリープ: monotonic は止まっていたので、差し引くものが無い。"""
        self.clock.start(thread=False)
        before = self.clock.now()
        self.mono += 2
        self.wall += 3600
        self.assertAlmostEqual(self.clock.now() - before, 2.0)
        self.assertFalse(self.clock.last_resume.counted)

    def test_少し遅れただけでは差し引かない(self) -> None:
        self.clock.start(thread=False)
        before = self.clock.now()
        self.mono += 10
        self.wall += 10
        self.assertAlmostEqual(self.clock.now() - before, 10.0)
        self.assertIsNone(self.clock.last_resume)

    def test_立てていなければただの単調時計(self) -> None:
        """試験・デスクトップ版。呼ばれる間隔がまちまちなので、空いても眠ったとは言えない。"""
        before = self.clock.now()
        self.mono += 3600
        self.assertAlmostEqual(self.clock.now() - before, 3600.0)


class ReloadWhileAsleepTests(unittest.TestCase):
    """場面a: 再読込の途中で眠った。"""

    def start(self, **kwargs) -> _Lab:
        lab = _Lab(**kwargs)
        lab.watch.beat("A", page="p1")
        lab.run(4)
        lab.watch.leaving("A", page="p1")         # 再読込の pagehide
        lab.sleep()
        return lab

    def test_起きた瞬間に終わらない(self) -> None:
        lab = self.start()
        self.assertFalse(lab.run(2), "起きた最初の見回りで終わった")
        lab.watch.beat("A", page="p2")            # 新しいページが読み込み終わった
        self.assertFalse(lab.run(30))
        self.assertEqual(lab.stopped, 0)

    def test_時計の差し引きだけでも終わらない(self) -> None:
        """目覚めの見張りが気づかなくても(1周の間隔では見分けられない場合)。"""
        lab = self.start(detect=False)
        self.assertFalse(lab.run(2), "起きた最初の見回りで終わった")

    def test_目覚めの数え直しだけでも終わらない(self) -> None:
        """時計が差し引かなくても(時計の見張りが立っていない場合)。"""
        lab = self.start(awake=False)
        self.assertFalse(lab.run(2), "起きた最初の見回りで終わった")

    def test_戻ってこなければ猶予のあとで終わる(self) -> None:
        lab = self.start()
        self.assertTrue(lab.run(idle_exit.GRACE_SEC + 4))
        self.assertEqual(lab.watch.overdue(), CLOSED)


class HoldWhileAsleepTests(unittest.TestCase):
    """場面b: タブを閉じたあと、未送信を抱えて粘っている途中で眠った。"""

    #: 粘りはじめてから眠るまで(上限の 600 秒の手前)
    HELD = idle_exit.HOLD_SEC - 10

    def start(self, **kwargs) -> _Lab:
        lab = _Lab(unsent=3, **kwargs)
        lab.watch.beat("A", page="p1")
        lab.watch.leaving("A", page="p1")         # タブを閉じた(3件 送れていない)
        self.assertFalse(lab.run(idle_exit.GRACE_SEC + 2))
        self.assertEqual(lab.retried, 1, "粘りはじめに送り直しを頼む")
        self.assertFalse(lab.run(self.HELD))
        lab.sleep()
        return lab

    def test_起きた瞬間に諦めない(self) -> None:
        lab = self.start()
        self.assertFalse(lab.run(2), "起きた瞬間に「未送信のまま終了」になった")
        self.assertEqual(lab.retried, 2, "起きたら送り直しを頼む")

    def test_起きてから送り直す間がある(self) -> None:
        """起きた直後はネットワークがまだ戻っていないことが多い。粘りは起きた時点から。"""
        lab = self.start()
        self.assertFalse(lab.run(60))
        lab.unsent = 0                            # 送れた
        self.assertTrue(lab.run(2))
        self.assertEqual(lab.exited_with, [0])

    def test_時計の差し引きだけでも起きた瞬間には諦めない(self) -> None:
        lab = self.start(detect=False)
        self.assertFalse(lab.run(2), "起きた瞬間に「未送信のまま終了」になった")

    def test_送れなければやはり諦める(self) -> None:
        """共有が死んでいるときに、いつまでも生き続けない(起きていた時間で数える)。"""
        lab = self.start()
        # 起きると閉じた合図の猶予から数え直し、そのあと粘りも数え直す
        self.assertTrue(lab.run(idle_exit.GRACE_SEC + idle_exit.HOLD_SEC + 4))
        self.assertEqual(lab.exited_with, [3])


class ScreenLockSleepTests(unittest.TestCase):
    """画面の錠も、眠っていたあいだに持ち主を外さない。"""

    def setUp(self) -> None:
        self.mono, self.wall = 1000.0, 1_800_000_000.0
        self.clock = awake_clock.AwakeClock(mono=lambda: self.mono, wall=lambda: self.wall)
        self.clock.start(thread=False)

    def awake(self, sec: float) -> None:
        for _ in range(int(sec // 2)):
            self.mono += 2
            self.wall += 2
            self.clock.check()

    def test_起きてすぐもう一度起動しても_持ち主は外れない(self) -> None:
        lock = screen_lock.ScreenLock(clock=self.clock.now)
        lock.claim("A")
        self.awake(10)
        lock.beat("A")
        self.awake(10)
        self.mono += NIGHT                        # 眠った
        self.wall += NIGHT
        result = lock.claim("B")                  # 起きてすぐ Start.vbs をもう一度
        self.assertFalse(result.ok, "眠っていたあいだに持ち主が外れた")
        self.assertTrue(lock.beat("A"), "前の画面に「別の画面で開かれました」が出る")

    def test_起きてから本当に戻ってこなければ次が入れる(self) -> None:
        lock = screen_lock.ScreenLock(clock=self.clock.now)
        lock.claim("A")
        self.mono += NIGHT
        self.wall += NIGHT
        self.awake(screen_lock.SCREEN_STALE_SEC + 2)
        self.assertTrue(lock.claim("B").ok)


class DefaultClockTests(unittest.TestCase):
    """見張りも錠も、**既定で**スリープを数えない時計を使う。

    差し替えた時計で「起きたまま時間が過ぎれば外れる」ことも見る ── 本物の時計を
    使っていたら、試験のあいだは時間が進まないので、眠った試験だけでは見分けられない。
    """

    def setUp(self) -> None:
        self.mono, self.wall = 1000.0, 1_800_000_000.0
        self.clock = awake_clock.AwakeClock(mono=lambda: self.mono, wall=lambda: self.wall)
        self.clock.start(thread=False)
        patcher = mock.patch.object(awake_clock, "_clock", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def sleep(self) -> None:
        self.mono += NIGHT
        self.wall += NIGHT

    def awake(self, sec: float) -> None:
        for _ in range(int(sec // 2)):
            self.mono += 2
            self.wall += 2
            self.clock.check()

    def test_見張り(self) -> None:
        watch = idle_exit.IdleWatch(lambda: None, lambda: False)
        watch.beat("A", page="p1")
        watch.leaving("A", page="p1")
        self.sleep()
        self.assertIsNone(watch.overdue(), "眠っていた時間を猶予に数えた")
        self.awake(idle_exit.GRACE_SEC + 2)
        self.assertEqual(watch.overdue(), CLOSED, "この時計を見ていない")

    def test_画面の錠(self) -> None:
        lock = screen_lock.ScreenLock()
        lock.claim("A")
        self.sleep()
        self.assertFalse(lock.claim("B").ok, "眠っていた時間を無通信に数えた")
        self.awake(screen_lock.SCREEN_STALE_SEC + 2)
        self.assertTrue(lock.claim("B").ok, "この時計を見ていない")

    def test_プロセスに1つの錠(self) -> None:
        """``/api/alive`` と名乗りが使う錠(``screen_lock.get()``)も同じ。"""
        screen_lock.reset()
        self.addCleanup(screen_lock.reset)
        lock = screen_lock.get()
        lock.claim("A")
        self.sleep()
        self.assertFalse(lock.claim("B").ok, "眠っていた時間を無通信に数えた")


class StartTests(unittest.TestCase):
    def test_ブラウザ版の起動は時計を立てる(self) -> None:
        """立てないと、ただの monotonic のまま(スリープを数えてしまう)。"""
        import start_app

        awake_clock.reset()
        self.addCleanup(awake_clock.reset)
        with mock.patch.object(idle_exit, "install") as install:
            start_app._watch_for_idle(mock.Mock())
        self.assertTrue(install.called)
        self.assertTrue(awake_clock.get().watching)


if __name__ == "__main__":
    unittest.main()
