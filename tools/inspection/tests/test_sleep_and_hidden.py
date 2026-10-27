"""スリープ明け・裏に回ったまま、で誤って終わらない(時計を差し替えて確かめる)

別のツールで「スリープ復帰時の時計のずれや、ブラウザのタブ休止で問い合わせが止まり、
サーバーが誤って終了する」不具合がありました。点検表で見つかったもの:

1. **スリープ明けを壁の時計だけで見分けていた。** Windows は monotonic がスリープ中も
   進むので、起きた瞬間に壁の時計がまだ進んでいないと「眠っていた」と気づかず、
   戻った直後に「90秒 心拍がありません」で終わっていた
   → 見張りの間隔の空きは**壁の時計と monotonic の大きいほう**で見る。数える時計そのものも
     止まっていた分を差し引く(`awake_clock`。日報と同じ作り)
2. **裏に回った画面を上限なしに居るとみなしていた。** 閉じた合図を取りこぼすと
   いつまでも終わらない → 日報と同じく7日で後始末
"""
from __future__ import annotations

import unittest

import tests.helpers  # noqa: F401  (ローカル領域を一時フォルダへ)
from core import awake_clock, idle_exit

HOUR = 3600.0


class FakeTime:
    """monotonic と壁の時計を別々に進められる。"""

    def __init__(self) -> None:
        self.mono = 500.0
        self.wall = 1_700_000_000.0

    def tick(self, sec: float, *, mono: bool = True, wall: bool = True) -> None:
        """`sec` 秒たつ。`mono=False` は Linux のスリープ(monotonic が止まる)、
        `wall=False` は Windows のスリープで起きた瞬間(壁の時計がまだ追いついていない)。"""
        if wall:
            self.wall += sec
        if mono:
            self.mono += sec


class WakeWithLaggingWallClockTests(unittest.TestCase):
    """見張りの1回(`step`)を、時計を差し替えて回す。"""

    def setUp(self) -> None:
        self.time = FakeTime()
        self.stopped: list[int] = []
        # 数える時計は**ただの monotonic**(ここでは見張りの空きの見分けだけを確かめる)
        self.watch = idle_exit.IdleWatch(lambda: self.stopped.append(1), lambda: False,
                                         clock=lambda: self.time.mono,
                                         mono=lambda: self.time.mono, wall=lambda: self.time.wall)

    def run_for(self, sec: float, **kw) -> None:
        for _ in range(int(sec / idle_exit.TICK_SEC)):
            self.time.tick(idle_exit.TICK_SEC, **kw)
            if self.watch.step():
                return                            # 止めた(見張りもここで抜ける)

    def test_起きた瞬間に壁の時計が進んでいなくても終わらない(self) -> None:
        """Windows: スリープ中に monotonic だけ進み、起きた瞬間の壁の時計は眠る前のまま。"""
        self.watch.step()                         # 見張りの始まり
        self.watch.beat("A", visible=True)
        self.run_for(10)
        self.time.tick(2 * HOUR, wall=False)      # フタを閉じていた(壁の時計はまだ)
        self.assertFalse(self.watch.step(), "起きた瞬間に「90秒 心拍がありません」で終わった")
        self.assertEqual(self.stopped, [])
        # 6秒後に壁の時計が追いつく。そのあいだに画面が戻れば続く
        self.time.wall += 2 * HOUR
        self.run_for(6)
        self.assertEqual(self.stopped, [])
        self.watch.beat("A", visible=True)
        self.run_for(60)
        self.assertEqual(self.stopped, [])

    def test_壁の時計だけ進むスリープでも待ち直す(self) -> None:
        """Linux: monotonic は止まり、壁の時計だけ進む(これまでどおり)。"""
        self.watch.step()
        self.watch.beat("A", visible=True)
        self.time.tick(2 * HOUR, mono=False)
        self.assertFalse(self.watch.step())
        self.assertIsNotNone(self.watch._resumed_at)

    def test_起きてからも心拍が無ければ終わる(self) -> None:
        """待ち直すのは idle_sec だけ。ブラウザが落ちていれば終われる。"""
        self.watch.step()
        self.watch.beat("A", visible=True)
        self.time.tick(2 * HOUR, wall=False)
        self.watch.step()
        self.run_for(idle_exit.IDLE_SEC + 2 * idle_exit.TICK_SEC)
        self.assertEqual(self.stopped, [1])

    def test_少し遅れた程度では眠ったことにしない(self) -> None:
        self.watch.step()
        self.time.tick(idle_exit.SLEEP_GAP_SEC, wall=False)
        self.watch.step()
        self.assertIsNone(self.watch._resumed_at)


class AwakeClockTests(unittest.TestCase):
    """止まっていた分を差し引く時計(日報の `awake_clock` と同じ作り)。"""

    def setUp(self) -> None:
        self.time = FakeTime()
        self.clock = awake_clock.AwakeClock(lambda: self.time.mono, lambda: self.time.wall)
        self.addCleanup(self.clock.cancel)

    def test_見張る前はただのmonotonic(self) -> None:
        self.time.tick(10 * HOUR)
        self.assertEqual(self.clock.now(), self.time.mono)
        self.assertFalse(self.clock.watching)

    def test_Windowsのスリープは差し引く(self) -> None:
        self.clock.start(thread=False)
        before = self.clock.now()
        self.time.tick(2 * HOUR, wall=False)      # 壁の時計がまだ追いついていなくても
        self.assertLessEqual(self.clock.now() - before, awake_clock.TICK_SEC + 0.01)
        self.assertTrue(self.clock.last_resume.counted)

    def test_Linuxのスリープは記すだけ(self) -> None:
        self.clock.start(thread=False)
        self.time.tick(2 * HOUR, mono=False)
        found = self.clock.check()
        self.assertIsNotNone(found)
        self.assertFalse(found.counted)
        self.assertEqual(self.clock.slept, 0)

    def test_ふつうに回っているあいだは差し引かない(self) -> None:
        self.clock.start(thread=False)
        start = self.clock.now()
        for _ in range(300):
            self.time.tick(awake_clock.TICK_SEC)
            self.clock.check()
        self.assertAlmostEqual(self.clock.now() - start, 600, delta=0.01)
        self.assertIsNone(self.clock.last_resume)

    def test_スリープの見切りは無通信の見切りより短い(self) -> None:
        self.assertLess(awake_clock.JUMP_SEC, idle_exit.IDLE_SEC)


class SleepWithAwakeClockTests(unittest.TestCase):
    """見張りに止まっていた分を差し引く時計を繋いで(ブラウザ版の起動と同じ)。"""

    def setUp(self) -> None:
        self.time = FakeTime()
        self.clock = awake_clock.AwakeClock(lambda: self.time.mono, lambda: self.time.wall)
        self.addCleanup(self.clock.cancel)
        self.clock.start(thread=False)
        self.watch = idle_exit.IdleWatch(lambda: None, lambda: False, clock=self.clock.now)

    def test_フタを開けた瞬間に落とさない(self) -> None:
        self.watch.beat("A", visible=True)
        self.time.tick(15)
        self.clock.check()
        self.time.tick(3 * HOUR, wall=False)
        self.assertIsNone(self.watch.overdue(), "スリープ明けに『心拍がありません』で落としています")

    def test_起きたあとも黙っていれば見切る(self) -> None:
        self.watch.beat("A", visible=True)
        self.time.tick(3 * HOUR)
        self.clock.check()
        for _ in range(int(idle_exit.IDLE_SEC / awake_clock.TICK_SEC) + 2):
            self.time.tick(awake_clock.TICK_SEC)
            self.clock.check()
        self.assertIn("心拍がありません", self.watch.overdue())

    def test_裏に回った画面の7日はスリープを数えない(self) -> None:
        self.watch.hidden("A")
        self.time.tick(8 * 24 * HOUR)             # 週末を越えてフタを閉じていた
        self.clock.check()
        self.assertIsNone(self.watch.overdue())


class HiddenScreenCleanupTests(unittest.TestCase):
    """裏に回った画面は7日で後始末(閉じた合図を取りこぼしても、いつまでも終わらなくはならない)。"""

    def setUp(self) -> None:
        self.now = 1000.0
        self.watch = idle_exit.IdleWatch(lambda: None, lambda: False, clock=lambda: self.now)

    def test_裏のままの画面は7日は居るとみなす(self) -> None:
        self.watch.hidden("A")
        self.now += idle_exit.HIDDEN_KEEP_SEC - 60
        self.assertIsNone(self.watch.overdue(), "裏に回った画面を7日より前に見切った")

    def test_7日音沙汰が無ければ終わる(self) -> None:
        self.watch.hidden("A")
        self.now += idle_exit.HIDDEN_KEEP_SEC + 1
        why = self.watch.overdue()
        self.assertIsNotNone(why, "裏に回った画面を上限なしに居るとみなしている")
        self.assertIn("7日", why)

    def test_裏の画面から何か届けば数え直す(self) -> None:
        self.watch.hidden("A")
        self.now += idle_exit.HIDDEN_KEEP_SEC - 60
        self.watch.beat("A")                      # 裏のまま、業務の問い合わせ
        self.now += 120
        self.assertIsNone(self.watch.overdue())

    def test_ほかに表の画面があれば終わらない(self) -> None:
        self.watch.hidden("A")
        self.now += idle_exit.HIDDEN_KEEP_SEC + 1
        self.watch.beat("B", visible=True)
        self.assertIsNone(self.watch.overdue())


if __name__ == "__main__":
    unittest.main()
