"""裏に回ったタブ・スリープ明けで**止めない** (v3.79.0)

    別開発のツールがブラウザがバックグラウンドタブのタイマーを間引くことで、
    心拍が途切れてセッションが誤って終了することがありました
    バックグラウンドで心拍が止まっても停止しないようにしてください

【このアプリにも同じ弱みがありました】

    何をした                      起きていたこと
    Excelを全画面・窓を最小化     心拍が間引かれ/止められ、90秒でアプリが終わる
    タブAで打ち、タブBでグラフ    Aの心拍が間引かれて15秒で外れ、押してもいない
                                  のにBへ打つ権利が移る
    2枚開いていて1枚を閉じる      残りの1枚の心拍が8秒以内に来ないとアプリごと
                                  終わる(心拍は20秒ごと ── 半分以上の確率)
    PCをスリープ→フタを開ける     Windows は時計がスリープ中も進むので、起きた
                                  瞬間「90秒 心拍がありません」で終わる

ここで押さえるのは4つです。

    1. 裏に回ったタブは、心拍が止まっても居るものとみなす(自動終了・打つ権利)
    2. 閉じたときは、その1枚だけ外す。最後の1枚のときだけ終わる
    3. スリープのあいだは「無通信」に数えない
    4. 画面は、裏に回る瞬間に知らせ、戻ったことに自分で気づく

**本物の時間は待ちません。** 時計は試験が持ちます(7日も待てないので)。
"""
from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou import awake_clock, idle_exit, running  # noqa: E402
from nippou.logic import tab_lock  # noqa: E402
from tests._web import HAS_FLASK, HEADERS, SKIP_REASON, TOKEN, WebTestCase  # noqa: E402

HOUR = 3600.0
DAY = 24 * HOUR


class Clock:
    """試験の時計。`advance` で進める。"""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, sec: float) -> None:
        self.t += sec


# ------------------------------------------------------------------
# 1. 自動終了 ── 裏に回ったタブは落とさない
# ------------------------------------------------------------------
class WatchCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.stopped: list[int] = []
        self.watch = idle_exit.IdleWatch(
            lambda: self.stopped.append(1), lambda: False, clock=self.clock)
        self.addCleanup(self.watch.cancel)

    def later(self, sec: float) -> None:
        self.clock.advance(sec)


class HiddenTabTests(WatchCase):
    """**裏に回ったタブからは心拍が来ない。** それでも居るものとみなす。"""

    def test_裏に回ったまま心拍が止まっても終わらない(self) -> None:
        self.watch.beat("A")
        self.watch.hide("A")
        # Edge のスリープ中のタブ・Chrome のメモリセーバーは、タイマーごと止める
        for hours in (1, 8, 24, 72):
            with self.subTest(hours=hours):
                self.clock.t = 1000.0 + hours * HOUR
                self.assertIsNone(self.watch.overdue(),
                                  f"裏に回して {hours}時間で落としています")

    def test_見えているタブは90秒で見切る(self) -> None:
        """こちらはこれまでどおり。**ブラウザごと落ちた**ときに終われる。"""
        self.watch.beat("A")
        self.later(idle_exit.IDLE_SEC - 1)
        self.assertIsNone(self.watch.overdue())
        self.later(2)
        self.assertIn("心拍がありません", self.watch.overdue())

    def test_裏の心拍が間引かれて届いても裏のまま(self) -> None:
        """Chrome は隠れて5分で1分に1回。**届いたぶんで表に戻したことにしない。**"""
        self.watch.hide("A")
        for _ in range(10):
            self.later(60)
            self.watch.beat("A", hidden=True)
        self.later(idle_exit.IDLE_SEC * 3)
        self.assertIsNone(self.watch.overdue())

    def test_表に戻れば90秒の見切りに戻る(self) -> None:
        self.watch.hide("A")
        self.later(HOUR)
        self.watch.beat("A")                      # 表に戻った
        self.later(idle_exit.IDLE_SEC + 1)
        self.assertIn("心拍がありません", self.watch.overdue())

    def test_置き去りの後始末は7日(self) -> None:
        """閉じる合図が届かないまま消えたタブ(凍結中にブラウザごと閉じた)。

        **いつまでも残すと、プロセスが永久に終わりません。** 窓を最小化した
        まま週末を越えても落とさない長さにしてあります。
        """
        self.assertGreaterEqual(idle_exit.HIDDEN_KEEP_SEC, 3 * DAY)
        self.watch.hide("A")
        self.later(idle_exit.HIDDEN_KEEP_SEC + 1)
        self.assertIn("裏に回ったまま", self.watch.overdue())

    def test_裏のタブが1枚でも居れば終わらない(self) -> None:
        """見えていた1枚がブラウザごと落ちても、裏のタブが居るうちは続ける。"""
        self.watch.beat("A")
        self.watch.hide("B")
        self.later(idle_exit.IDLE_SEC * 5)
        self.assertIsNone(self.watch.overdue())
        self.assertEqual(self.watch.screens(), {"visible": 0, "hidden": 1})

    def test_見張りが実際に止めない(self) -> None:
        """回している見張りでも同じ。"""
        watch = idle_exit.IdleWatch(
            lambda: self.stopped.append(1), lambda: False,
            idle_sec=0.05, grace_sec=0.05, tick_sec=0.01)
        self.addCleanup(watch.cancel)
        watch.start()
        watch.hide("A")
        import time
        time.sleep(0.3)
        self.assertEqual(self.stopped, [])


class PerTabTests(WatchCase):
    """**タブごとに数える。** 閉じたのが最後の1枚のときだけ終わる。"""

    def test_2枚のうち1枚を閉じても終わらない(self) -> None:
        """これまでは、残りの1枚の心拍が8秒以内に来ないと終わっていた。"""
        self.watch.beat("A")
        self.watch.beat("B")
        self.watch.leaving("A")
        self.later(idle_exit.GRACE_SEC * 2)
        self.assertIsNone(self.watch.overdue(), "1枚閉じただけで落としています")
        self.assertEqual(self.watch.screens(), {"visible": 1, "hidden": 0})

    def test_残りが裏に回っていても終わらない(self) -> None:
        self.watch.beat("A")
        self.watch.hide("B")
        self.watch.leaving("A")
        self.later(HOUR)
        self.assertIsNone(self.watch.overdue())

    def test_最後の1枚を閉じたら猶予のあとで終わる(self) -> None:
        self.watch.beat("A")
        self.watch.beat("B")
        self.watch.leaving("A")
        self.watch.leaving("B")
        self.later(idle_exit.GRACE_SEC - 1)
        self.assertIsNone(self.watch.overdue())
        self.later(2)
        self.assertEqual(self.watch.overdue(), "画面が閉じられました")

    def test_裏に回ったタブを閉じても同じ(self) -> None:
        """裏のタブも、閉じれば `pagehide` がその場で届く。7日は待たない。"""
        self.watch.hide("A")
        self.watch.leaving("A")
        self.later(idle_exit.GRACE_SEC + 1)
        self.assertEqual(self.watch.overdue(), "画面が閉じられました")

    def test_再読込は同じ名札で戻ってくる(self) -> None:
        """名札は `sessionStorage` なので、読み直しても同じ。"""
        self.watch.beat("A")
        self.watch.leaving("A")
        self.later(1)
        self.watch.beat("A")
        self.later(idle_exit.GRACE_SEC * 3)
        self.assertIsNone(self.watch.overdue())

    def test_1枚閉じたあと残りがブラウザごと落ちたら無通信で終わる(self) -> None:
        self.watch.beat("A")
        self.watch.beat("B")
        self.watch.leaving("A")
        self.later(idle_exit.IDLE_SEC + 1)
        self.assertIn("心拍がありません", self.watch.overdue())

    def test_名札の無い古い画面も1枚として数える(self) -> None:
        """入れ替えた直後は、古いJSの画面が名札なしで送ってくる。"""
        self.watch.beat()
        self.watch.leaving()
        self.later(idle_exit.GRACE_SEC + 1)
        self.assertEqual(self.watch.overdue(), "画面が閉じられました")


class LateSignalTests(WatchCase):
    """**閉じたページから遅れて着いた合図で生き返らせない。**

    タブを閉じると、ブラウザは「裏に回った」と「閉じた」を続けて出します。
    どちらも `sendBeacon` で、サーバに着く順は決まっていません。「閉じた」の
    あとに「裏に回った」が着くと、閉じたタブが裏のタブとして7日残り、
    **アプリが終わらなくなります**(本物のブラウザで起きたのを見て足した)。
    """

    def test_閉じたあとに着いた隠れた合図は受け付けない(self) -> None:
        self.watch.beat("A", page="p1")
        self.watch.leaving("A", page="p1")
        self.assertFalse(self.watch.hide("A", page="p1"))
        self.later(idle_exit.GRACE_SEC + 1)
        self.assertEqual(self.watch.overdue(), "画面が閉じられました")

    def test_送りかけの心拍も受け付けない(self) -> None:
        self.watch.beat("A", page="p1")
        self.watch.leaving("A", page="p1")
        self.assertFalse(self.watch.beat("A", page="p1"))
        self.later(idle_exit.GRACE_SEC + 1)
        self.assertEqual(self.watch.overdue(), "画面が閉じられました")

    def test_再読込した新しいページは受け付ける(self) -> None:
        """名札は同じでも、ページ番号は読み込むたびに変わる。"""
        self.watch.beat("A", page="p1")
        self.watch.leaving("A", page="p1")
        self.assertTrue(self.watch.beat("A", page="p2"))
        self.later(idle_exit.GRACE_SEC * 3)
        self.assertIsNone(self.watch.overdue())

    def test_新しいページが先に着いても古い閉じた合図で外さない(self) -> None:
        self.watch.beat("A", page="p1")
        self.watch.beat("A", page="p2")           # 再読込した新しいほうが先
        self.watch.leaving("A", page="p1")        # 古いほうの閉じた合図が後
        self.later(idle_exit.GRACE_SEC * 3)
        self.assertIsNone(self.watch.overdue(), "開いている新しいページを外しました")

    def test_覚えておくのは10分(self) -> None:
        """増え続けない。遅れた合図は長くても数秒で着く。"""
        self.watch.beat("B", page="keep")
        for n in range(5):
            self.watch.beat("A", page=f"p{n}")
            self.watch.leaving("A", page=f"p{n}")
        self.later(idle_exit.GONE_KEEP_SEC + 1)
        self.watch.beat("B", page="keep")
        self.watch.beat("A", page="new")
        self.watch.leaving("A", page="new")
        self.assertEqual(list(self.watch._gone), ["new"])


# ------------------------------------------------------------------
# 2. スリープを数えない時計
# ------------------------------------------------------------------
class FakeTime:
    """monotonic と壁の時計を別々に進められる。"""

    def __init__(self) -> None:
        self.mono = 500.0
        self.wall = 1_700_000_000.0

    def tick(self, sec: float, *, mono: bool = True) -> None:
        """`sec` 秒たつ。`mono=False` は Linux のスリープ(monotonic が止まる)。"""
        self.wall += sec
        if mono:
            self.mono += sec


class AwakeClockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.time = FakeTime()
        self.clock = awake_clock.AwakeClock(lambda: self.time.mono,
                                            lambda: self.time.wall)
        self.addCleanup(self.clock.cancel)

    def start(self) -> None:
        # 見張りは試験が回す(`check()`)
        self.clock.start(thread=False)

    def run_for(self, sec: float) -> None:
        """2秒ごとに見張りが回りながら、`sec` 秒たつ。"""
        steps = int(sec / awake_clock.TICK_SEC)
        for _ in range(steps):
            self.time.tick(awake_clock.TICK_SEC)
            self.clock.check()

    def test_見張る前はただのmonotonic(self) -> None:
        """立てていなければ呼ばれる間隔はまちまち。空いていても眠ったとは言えない。"""
        self.time.tick(10 * HOUR)
        self.assertEqual(self.clock.now(), self.time.mono)
        self.assertFalse(self.clock.watching)

    def test_ふつうに回っているあいだは差し引かない(self) -> None:
        self.start()
        start = self.clock.now()
        self.run_for(600)
        self.assertAlmostEqual(self.clock.now() - start, 600, delta=0.01)
        self.assertIsNone(self.clock.last_resume)

    def test_Windowsのスリープは差し引く(self) -> None:
        """**monotonic ごと進む**(Windows)。起きた瞬間に90秒を超えて見える。"""
        self.start()
        self.run_for(10)
        before = self.clock.now()
        self.time.tick(2 * HOUR)                  # フタを閉じていた
        after = self.clock.now()
        # 数えたのは見張り1回ぶんだけ
        self.assertLessEqual(after - before, awake_clock.TICK_SEC + 0.01)
        found = self.clock.last_resume
        self.assertIsNotNone(found)
        self.assertTrue(found.counted)
        self.assertAlmostEqual(found.seconds, 2 * HOUR - awake_clock.TICK_SEC,
                               delta=0.01)

    def test_Linuxのスリープは記すだけ(self) -> None:
        """monotonic が止まっていたので、差し引くものは無い。"""
        self.start()
        before = self.clock.now()
        self.time.tick(2 * HOUR, mono=False)
        self.time.tick(awake_clock.TICK_SEC)
        found = self.clock.check()
        self.assertIsNotNone(found)
        self.assertFalse(found.counted)
        self.assertAlmostEqual(self.clock.now() - before, awake_clock.TICK_SEC,
                               delta=0.01)
        self.assertEqual(self.clock.slept, 0)

    def test_少し遅れた程度では眠ったことにしない(self) -> None:
        self.start()
        self.time.tick(awake_clock.JUMP_SEC - 1)
        self.assertIsNone(self.clock.check())
        self.assertEqual(self.clock.slept, 0)

    def test_見張りより先に要求が来ても同じ答え(self) -> None:
        """起きた瞬間、見張りと要求のどちらが先に走るかは決まっていない。"""
        self.start()
        before = self.clock.now()
        self.time.tick(HOUR)
        self.assertLessEqual(self.clock.now() - before, awake_clock.TICK_SEC + 0.01)

    def test_打てるタブの見切りより長く眠れば必ず差し引く(self) -> None:
        """見切りは**最後の心拍から**数える。心拍の間隔のぶんを引いても、差し引かない
        止まりのほうが短くないといけない(前は見切りと同じ15秒で、12秒の眠りで外れた)。"""
        self.assertLess(awake_clock.JUMP_SEC + tab_lock.HEARTBEAT_SEC,
                        tab_lock.LOST_AFTER_SEC - 2,
                        "差し引かない止まり+心拍の間隔が、打てるタブの見切りに近すぎます")
        self.assertLess(awake_clock.JUMP_SEC, idle_exit.IDLE_SEC)
        # 1回や2回の遅れでは眠ったことにしない
        self.assertGreater(awake_clock.JUMP_SEC, 2 * awake_clock.TICK_SEC)


class SleepWatchTests(unittest.TestCase):
    """スリープ明けに**起きた瞬間に落とさない**(時計を見張りに繋いで)。"""

    def setUp(self) -> None:
        self.time = FakeTime()
        self.clock = awake_clock.AwakeClock(lambda: self.time.mono,
                                            lambda: self.time.wall)
        self.addCleanup(self.clock.cancel)
        self.clock.start(thread=False)
        self.watch = idle_exit.IdleWatch(lambda: None, lambda: False,
                                         clock=self.clock.now)

    def test_フタを開けた瞬間に落とさない(self) -> None:
        self.watch.beat("A")
        self.time.tick(15)                        # 心拍の直前に眠った
        self.clock.check()
        self.time.tick(3 * HOUR)                  # スリープ
        self.assertIsNone(self.watch.overdue(),
                          "スリープ明けに『心拍がありません』で落としています")

    def test_起きたあとも黙っていれば見切る(self) -> None:
        """差し引くのは眠っていたぶんだけ。ブラウザが落ちていれば終われる。"""
        self.watch.beat("A")
        self.time.tick(3 * HOUR)
        self.clock.check()
        for _ in range(int(idle_exit.IDLE_SEC / awake_clock.TICK_SEC) + 2):
            self.time.tick(awake_clock.TICK_SEC)
            self.clock.check()
        self.assertIn("心拍がありません", self.watch.overdue())

    def test_打てるタブの権利もスリープで外れない(self) -> None:
        desk = tab_lock.TabDesk()
        desk.claim("A", now=self.clock.now())
        desk.claim("B", now=self.clock.now())
        self.time.tick(3 * HOUR)
        # 起きて、先にBの心拍が届いた
        self.assertFalse(desk.ping("B", now=self.clock.now()).may_edit,
                         "スリープ明けに、押してもいないのに権利が移りました")
        self.assertTrue(desk.ping("A", now=self.clock.now()).may_edit)

    def test_短いスリープでも押していないのに権利が移らない(self) -> None:
        """10〜15秒の眠り(短いスリープ・仮想機械の一時停止)。2画面で並べた A(打てる)と
        B(見るだけ)が一緒に眠り、起きて B の心拍が先に着く ── 前は15秒より短い止まりを
        差し引かなかったので、「最後の心拍(最大5秒前)+12秒」で A が外れて B に移った。

        眠り始め(心拍のあと何秒)・見張りの刻み・A が遅れて起きる秒を振って、どの組でも
        A が打てるままであることを見ます(0.5秒刻み。本物の時間は待たない)。"""
        for stop in [x / 2 for x in range(2, 81)]:              # 1〜40秒
            for before in [x / 2 for x in range(1, 10)]:        # 心拍のあと 0.5〜4.5秒で眠る
                for tick_at in (0, 1, 2, 3):                    # 見張りの刻み(半秒)
                    for lag in (1, 2, 3):                       # A が B より遅れて起きる秒
                        moved = self._sleep_two_windows(stop, before, tick_at, lag)
                        if moved:
                            self.fail(f"{stop}秒眠った(心拍のあと{before}秒で眠り、A が{lag}秒"
                                      f"遅れて起きた)だけで、押していないのに権利が {moved}")

    def _sleep_two_windows(self, stop: float, before: float, tick_at: int, lag: int) -> str:
        """A と B を並べて眠らせる。権利が移ったら、どこで移ったかを返す(移らなければ "")。"""
        t = FakeTime()
        clock = awake_clock.AwakeClock(lambda: t.mono, lambda: t.wall)
        clock.start(thread=False)
        desk = tab_lock.TabDesk()
        desk.claim("A", now=clock.now())
        desk.claim("B", now=clock.now())
        half = [0]                                      # いま何個めの半秒か
        beat = int(tab_lock.HEARTBEAT_SEC * 2)          # 心拍の間隔(半秒)
        tick = int(awake_clock.TICK_SEC * 2)

        def run(halves: int, *, a: bool = True) -> str:
            for _ in range(halves):
                t.tick(0.5)
                half[0] += 1
                n = half[0]
                if (n - tick_at) % tick == 0:
                    clock.check()
                if a and n % beat == 0:
                    desk.ping("A", now=clock.now())
                if (n - beat // 2) % beat == 0:         # B は A と半周ずれて送る
                    desk.ping("B", now=clock.now())
                if desk.editor != "A":
                    return f"{desk.editor or '誰も持たない'}へ"
            return ""

        moved = run(40)                                 # 20秒ふつうに動く(A の心拍の直後で止まる)
        moved = moved or run(int(before * 2))
        if moved:
            return "眠る前に" + moved
        t.tick(stop)                                    # サーバも両方のブラウザも止まっていた
        # 起きた。B の心拍が先に着く(表に戻った合図ですぐ送る)
        desk.ping("B", now=clock.now())
        if desk.editor != "A":
            return f"起きた瞬間に {desk.editor or '誰も持たない'}へ"
        moved = run(lag * 2, a=False)                   # A のブラウザはまだ眠っている
        if moved:
            return "A が起きる前に" + moved
        desk.ping("A", now=clock.now())
        return "" if desk.editor == "A" else f"A が起きたあとで {desk.editor}へ"


# ------------------------------------------------------------------
# 3. 打つ権利 ── 裏に回ったタブは持ったまま
# ------------------------------------------------------------------
class HiddenLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.desk = tab_lock.TabDesk()
        self.desk.claim("A", now=0.0)             # 打っているタブ
        self.desk.claim("B", now=1.0)             # 見るだけ

    def test_裏に回ったタブから権利を取らない(self) -> None:
        """**奪うのは押したときだけ。** ブラウザの都合で破らない。"""
        self.desk.hide("A", now=2.0)
        for minutes in (1, 5, 30, 8 * 60):
            now = 2.0 + minutes * 60
            with self.subTest(minutes=minutes):
                self.assertFalse(self.desk.ping("B", now=now).may_edit,
                                 f"裏に回して{minutes}分で権利が移りました")
        self.assertTrue(self.desk.ping("A", now=8 * HOUR).may_edit)

    def test_これまでは15秒で移っていた(self) -> None:
        """隠れた合図が無ければ、心拍3回ぶんで諦める(落ちたタブ)。"""
        self.assertTrue(
            self.desk.ping("B", now=tab_lock.LOST_AFTER_SEC + 2).may_edit)

    def test_間引かれた心拍でも裏のまま(self) -> None:
        self.desk.hide("A", now=2.0)
        self.desk.ping("A", now=62.0, hidden=True)
        self.assertTrue(self.desk.tabs["A"].hidden)
        self.assertFalse(self.desk.ping("B", now=62.0 + HOUR).may_edit)

    def test_見るだけの側にどこに居るかを言う(self) -> None:
        """「どこにも開いていないのに見るだけ」に見えるので。"""
        self.desk.hide("A", now=2.0)
        verdict = self.desk.ping("B", now=3.0)
        self.assertTrue(verdict.editor_hidden)
        self.assertIn("裏に回っています", verdict.note)
        self.assertIn("このタブへ移して", verdict.note)
        body = verdict.as_dict()
        self.assertTrue(body["editor_hidden"])
        self.assertEqual(body["note"], verdict.note)

    def test_表に居るなら何も添えない(self) -> None:
        verdict = self.desk.ping("B", now=3.0)
        self.assertEqual(verdict.note, "")
        self.assertEqual(self.desk.ping("A", now=3.0).note, "")

    def test_押せば奪える(self) -> None:
        """閉じてしまった・見つからないとき。**逃げ道は残す。**"""
        self.desk.hide("A", now=2.0)
        self.assertTrue(self.desk.take("B", now=3.0).may_edit)

    def test_裏のタブは空いた権利を拾わない(self) -> None:
        """見えているほうが「見るだけ」になるので。"""
        desk = tab_lock.TabDesk()
        desk.claim("A", now=0.0)
        desk.claim("B", now=0.0, hidden=True)
        desk.release("A", now=1.0)
        self.assertFalse(desk.ping("B", now=2.0, hidden=True).may_edit)
        # 誰も持っていなければ、書き込みはどのタブからでも通る
        self.assertTrue(desk.may_edit("B", now=2.0))
        # 表に出たら拾う
        self.assertTrue(desk.ping("B", now=3.0).may_edit)

    def test_見えているタブが先に拾う(self) -> None:
        desk = tab_lock.TabDesk()
        desk.claim("A", now=0.0)
        desk.claim("B", now=0.0, hidden=True)
        desk.claim("C", now=0.0)
        desk.release("A", now=1.0)
        desk.ping("B", now=2.0, hidden=True)
        self.assertTrue(desk.ping("C", now=3.0).may_edit)

    def test_知らないタブの隠れた合図は覚えない(self) -> None:
        self.desk.hide("Z", now=2.0)
        self.assertNotIn("Z", self.desk.tabs)

    def test_閉じたタブの送りかけの心拍で生き返らせない(self) -> None:
        """明け渡し(`release`)のあとに、裏から送った心拍が着くことがある。

        覚えると、閉じたタブが「裏に回ったタブ」として7日残ります。
        """
        self.desk.hide("A", now=2.0)
        self.desk.release("A", now=3.0)
        verdict = self.desk.ping("A", now=3.1, hidden=True)
        self.assertNotIn("A", self.desk.tabs)
        self.assertFalse(verdict.may_edit)
        self.assertTrue(self.desk.ping("B", now=4.0).may_edit)

    def test_裏で開いたタブは表に出たときに名乗る(self) -> None:
        desk = tab_lock.TabDesk()
        desk.claim("A", now=0.0, hidden=True)
        self.assertNotIn("A", desk.tabs)
        self.assertTrue(desk.ping("A", now=1.0).may_edit)

    def test_閉じれば待たずに次へ(self) -> None:
        self.desk.hide("A", now=2.0)
        self.desk.release("A", now=3.0)
        self.assertTrue(self.desk.ping("B", now=4.0).may_edit)

    def test_置き去りは7日で外す(self) -> None:
        self.desk.hide("A", now=2.0)
        now = 2.0 + tab_lock.HIDDEN_KEEP_SEC + 1
        self.assertTrue(self.desk.ping("B", now=now).may_edit)

    def test_自動終了と同じ長さ(self) -> None:
        """片方だけ短いと、アプリは続いているのに権利だけ外れる(逆も)。"""
        self.assertEqual(tab_lock.HIDDEN_KEEP_SEC, idle_exit.HIDDEN_KEEP_SEC)


# ------------------------------------------------------------------
# 4. 口(Flask)
# ------------------------------------------------------------------
@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class AliveApiTests(WebTestCase):
    """`POST /api/alive` の本文 `{tab, state, leaving}`。"""

    def setUp(self) -> None:
        super().setUp()
        # Web版の試験は `nippou.*` を読み込み直すので、app が触るほうを掴む
        self.idle = importlib.import_module("nippou.idle_exit")
        self.idle.reset()
        self.addCleanup(self.idle.reset)
        self.clock = Clock()
        self.watch = self.idle.install(lambda: None, lambda: False,
                                       clock=self.clock, tick_sec=3600)

    def alive(self, **body):
        return self.client.post("/api/alive", json=body,
                                headers={"Host": "127.0.0.1"})

    def test_裏に回った合図が届く(self) -> None:
        self.alive(tab="A", state="visible")
        self.alive(tab="A", state="hidden")
        self.assertEqual(self.watch.screens(), {"visible": 0, "hidden": 1})
        self.clock.advance(DAY)
        self.assertIsNone(self.watch.overdue())

    def test_タブごとに閉じる(self) -> None:
        self.alive(tab="A")
        self.alive(tab="B")
        self.alive(tab="A", leaving=True)
        self.clock.advance(self.idle.GRACE_SEC * 2)
        self.assertIsNone(self.watch.overdue())

    def test_閉じたページの遅れた合図は受け付けない(self) -> None:
        self.alive(tab="A", page="p1")
        self.alive(tab="A", page="p1", leaving=True)
        self.alive(tab="A", page="p1", state="hidden")
        self.clock.advance(self.idle.GRACE_SEC + 1)
        self.assertEqual(self.watch.overdue(), "画面が閉じられました")

    def test_healthに居る画面の数が出る(self) -> None:
        self.alive(tab="A", state="hidden")
        self.alive(tab="B")
        body = self.client.get("/api/health", headers={"Host": "127.0.0.1"}).get_json()
        self.assertEqual(body["screens"]["visible"], 1)
        self.assertEqual(body["screens"]["hidden"], 1)
        self.assertIn("resumed", body["screens"])


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class TabHideApiTests(WebTestCase):
    """`POST /api/tab/hide` と、心拍の `hidden`。"""

    def setUp(self) -> None:
        super().setUp()
        self.lock = importlib.import_module("nippou.logic.tab_lock")
        self.lock.reset_desk()
        self.addCleanup(self.lock.reset_desk)

    def post(self, path: str, tab: str, data=None):
        return self.client.post(path, headers={**HEADERS, "X-Tab": tab},
                                json=data or {})

    def test_隠れた合図はsendBeaconの形で届く(self) -> None:
        """**ヘッダは一切付けられない。** 合言葉はクエリ、名札は本文。"""
        self.post("/api/tab/claim", "A")
        res = self.client.post(f"/api/tab/hide?t={TOKEN}",
                               headers={"Host": "127.0.0.1"}, json={"tab": "A"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(self.lock.get_desk().tabs["A"].hidden)

    def test_見るだけの側に一言が返る(self) -> None:
        self.post("/api/tab/claim", "A")
        self.post("/api/tab/claim", "B")
        self.client.post(f"/api/tab/hide?t={TOKEN}",
                         headers={"Host": "127.0.0.1"}, json={"tab": "A"})
        body = self.post("/api/tab/ping", "B").get_json()
        self.assertFalse(body["may_edit"])
        self.assertTrue(body["editor_hidden"])
        self.assertIn("裏に回っています", body["note"])

    def test_裏からの心拍は裏のまま(self) -> None:
        self.post("/api/tab/claim", "A")
        self.post("/api/tab/ping", "A", {"hidden": True})
        self.assertTrue(self.lock.get_desk().tabs["A"].hidden)
        self.post("/api/tab/ping", "A", {"hidden": False})
        self.assertFalse(self.lock.get_desk().tabs["A"].hidden)

    def test_合言葉が無ければ断る(self) -> None:
        res = self.client.post("/api/tab/hide", headers={"Host": "127.0.0.1"},
                               json={"tab": "A"})
        self.assertIn(res.status_code, (401, 403))


# ------------------------------------------------------------------
# 5. 画面 ── 裏に回る瞬間に知らせ、戻ったことに自分で気づく
#
# 隠れる瞬間に sendBeacon で知らせる(凍結でも)・心拍にいまの見え方と名札とページ番号・戻る/進むで
# ページ番号を取り直す・戻ったことに自分で気づく(表に戻る・凍結が解ける・戻る/進む・回線・スリープ)・
# スリープは見えているときだけ・打つ権利も隠れる瞬間に知らせる・見るだけのタブに一言・直の見張りも
# 戻ったときに走る は、**本物のブラウザで裏へ回して確かめます**
# (`test_background_tabs_browser`。守りを外すと落ちる)
# ------------------------------------------------------------------
@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class TabHideLockTests(unittest.TestCase):
    def test_錠の列に並ばない(self) -> None:
        """`/api/tab/hide` も心拍の仲間。長い POST の後ろで待たせない(サーバの書く錠を取らない)。"""
        from app import _is_write

        class Req:
            method = "POST"
            path = "/api/tab/hide"

        self.assertFalse(_is_write(Req()))


if __name__ == "__main__":
    unittest.main()
