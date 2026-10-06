"""開いている画面を数える (:mod:`kanban.screen`)

**プロセスが1つでも、タブは何枚でも繋がる。** :mod:`launch_guard` が防ぐのは
``pythonw.exe`` の二重起動で、ここで防ぐのは**同じプロセスに2枚繋いで両方から
押せてしまうこと**。別の話なので、試験も別に持つ。
"""

from __future__ import annotations

import unittest

from kanban import screen


class HolderTest(unittest.TestCase):
    """持ち主は1枚だけ。"""

    def setUp(self) -> None:
        self.screens = screen.Screens(stale_sec=10.0)

    def test_the_first_screen_becomes_the_holder(self):
        claim = self.screens.beat("A")
        self.assertTrue(claim.active)
        self.assertEqual(claim.others, 0)

    def test_the_second_screen_cannot_operate(self):
        """**2枚目は開けるが押せない。**

        開くこと自体は止められない(ショートカットをもう一度押せばタブは
        増える)。止めるのは「2枚とも押せる」ほうだけ。
        """
        self.screens.beat("A")
        second = self.screens.beat("B")
        self.assertFalse(second.active, "2枚目が持ち主になっている")
        self.assertEqual(second.others, 1)

        self.assertTrue(self.screens.is_holder("A"))
        self.assertFalse(self.screens.is_holder("B"))

    def test_the_first_screen_keeps_holding_after_a_second_opens(self):
        """**開いただけで持ち主を奪わない。**

        奪うなら、使っている人が押したときだけ。勝手に移ると、作業中の画面が
        黙って使えなくなる。
        """
        self.screens.beat("A")
        self.screens.beat("B")
        self.assertTrue(self.screens.beat("A").active)

    def test_take_over_moves_the_holder(self):
        self.screens.beat("A")
        self.screens.beat("B")
        self.assertTrue(self.screens.take_over("B").active)
        self.assertFalse(self.screens.beat("A").active, "奪われた側がまだ押せる")
        self.assertTrue(self.screens.is_holder("B"))

    def test_closing_the_holder_hands_over_to_the_one_left(self):
        """**残った1枚が引き継ぐ。**

        席を空けたままにすると、2枚目を閉じただけなのに残った画面まで
        使えなくなる。
        """
        self.screens.beat("A")
        self.screens.beat("B")
        self.screens.leave("A")
        self.assertTrue(self.screens.beat("B").active)

    def test_a_frozen_holder_lets_go(self):
        """返事の無くなった画面は席を明け渡す。

        ブラウザごと落ちた・スリープした画面が持ち主のままだと、**開き直しても
        二度と押せなくなる。**
        """
        screens = screen.Screens(stale_sec=0.0)
        screens.beat("A")
        self.assertTrue(screens.beat("B").active, "固まった画面が席を占めたまま")

    def test_an_unnamed_screen_is_counted_as_one(self):
        """名乗らない相手(``curl``・試験)をまとめる。

        別々に数えると、誰も開いていないのに「2枚開いています」になる。
        """
        self.screens.beat("")
        claim = self.screens.beat("")
        self.assertEqual(claim.others, 0)
        self.assertTrue(claim.active)

    def test_nobody_is_refused_before_anyone_claims(self):
        """**持ち主がまだ居ないうちは断らない。**

        心拍より先に操作が飛ぶ(開いた直後に押す)場面で、正しい画面まで
        断ってしまう。
        """
        self.assertTrue(self.screens.is_holder("A"))

    def test_a_long_name_is_cut(self):
        long = "x" * 500
        self.screens.beat(long)
        self.assertEqual(len(self.screens.live()[0].id), screen.MAX_ID_LEN)


class CountTest(unittest.TestCase):
    def setUp(self) -> None:
        self.screens = screen.Screens(stale_sec=10.0)

    def test_live_lists_the_open_screens_oldest_first(self):
        self.screens.beat("A")
        self.screens.beat("B")
        self.assertEqual([s.id for s in self.screens.live()], ["A", "B"])

    def test_leaving_one_of_two_leaves_one(self):
        """**ここが本丸。**

        以前は送り主を区別していなかったので、2枚のうち1枚を閉じただけで
        「画面が閉じられました」になり、アプリごと落ちていた。
        """
        self.screens.beat("A")
        self.screens.beat("B")
        self.screens.leave("B")
        self.assertEqual([s.id for s in self.screens.live()], ["A"])
        self.assertIsNone(self.screens.left_at, "まだ1枚開いているのに閉じた扱い")

    def test_leaving_the_last_one_records_it(self):
        self.screens.beat("A")
        self.screens.leave("A")
        self.assertEqual(self.screens.live(), [])
        self.assertIsNotNone(self.screens.left_at)

    def test_beating_again_cancels_the_close(self):
        """再読込でも閉じた合図は飛ぶ。戻ってきたら取り消せること。"""
        self.screens.beat("A")
        self.screens.leave("A")
        self.screens.beat("A")
        self.assertIsNone(self.screens.left_at)

    def test_ever_is_false_until_someone_connects(self):
        self.assertFalse(self.screens.ever)
        self.screens.beat("A")
        self.assertTrue(self.screens.ever)
        self.screens.leave("A")
        self.assertTrue(self.screens.ever, "閉じたら繋がっていないことになっている")

    def test_a_silent_screen_drops_out(self):
        screens = screen.Screens(stale_sec=0.0)
        screens.beat("A")
        self.assertEqual(screens.live(), [])


class SingletonTest(unittest.TestCase):
    def tearDown(self) -> None:
        screen.reset()

    def test_get_returns_the_same_one(self):
        self.assertIs(screen.get(), screen.get())

    def test_reset_forgets_the_open_screens(self):
        screen.get().beat("A")
        screen.reset()
        self.assertFalse(screen.get().ever)


class FakeClock:
    """進め方を試験が決める時計。スリープや間引きを実時間を待たずに作る。"""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class HiddenScreenTest(unittest.TestCase):
    """**裏に回った画面を「閉じた」と取り違えない。**

    ブラウザは裏のタブのタイマーを間引く(Chrome は 5 分隠れると 1 分に 1 回)・
    止める(Edge のスリープ中のタブ)。別の道具では、これで心拍が途切れて
    開いているのに終了した。
    """

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.screens = screen.Screens(stale_sec=20.0, hidden_max_sec=3600.0, clock=self.clock)

    def walk(self, sec: float) -> None:
        # 見張りと同じく 2 秒ごとに一覧を見ながら進める(スリープとは区別される)
        end = self.clock.t + sec
        while self.clock.t < end:
            self.clock.t = min(end, self.clock.t + 2.0)
            self.screens.live()

    def ids(self) -> list[str]:
        return [s.id for s in self.screens.live()]

    def test_a_visible_screen_without_heartbeats_is_dropped(self):
        """見えている画面は従来どおり。心拍が途切れたら居ないと見なす。"""
        self.screens.beat("A")
        self.walk(25)
        self.assertEqual(self.ids(), [])

    def test_a_hidden_screen_is_kept_without_heartbeats(self):
        """**ここが本題。** 裏に回ると伝えた画面は、心拍が止まっても数えたまま。"""
        self.screens.beat("A")
        self.screens.hide("A")
        self.walk(30 * 60)
        self.assertEqual(self.ids(), ["A"])
        self.assertTrue(self.screens.is_holder("A"), "裏に回っただけで席を失った")

    def test_throttled_heartbeats_keep_it_hidden(self):
        """裏で 1 分に 1 回届く心拍も「隠れています」を付けてくる。"""
        self.screens.beat("A", hidden=True)
        for _ in range(10):
            self.walk(60)
            self.screens.beat("A", hidden=True)
        self.assertEqual(self.ids(), ["A"])

    def test_a_hidden_screen_is_dropped_after_the_limit(self):
        """隠れたままブラウザごと落ちた画面が、いつまでもアプリを残さない。"""
        self.screens.hide("A")
        self.walk(3601)
        self.assertEqual(self.ids(), [])

    def test_coming_back_makes_it_visible_again(self):
        self.screens.hide("A")
        self.walk(600)
        self.assertTrue(self.screens.beat("A").active)
        self.walk(25)
        self.assertEqual(self.ids(), [], "戻ったあとも隠れた扱いのまま")

    def test_a_visible_screen_takes_the_seat_from_a_silent_hidden_one(self):
        """持ち主が裏に回ったまま返事が無い = 誰も見ていない。見えている画面へ席を渡す。

        以前も、返事の無くなった画面は 20 秒で一覧から落ちて席が移っていた。
        それと同じ動きを保つ(隠れた画面は一覧には残す)。
        """
        self.screens.beat("A")
        self.screens.hide("A")
        self.walk(30)
        self.assertTrue(self.screens.beat("B").active)
        self.assertEqual(self.ids(), ["A", "B"], "隠れた画面まで一覧から消した")
        self.assertFalse(self.screens.beat("A").active, "戻ってきた側は「別のタブ」から取り戻す")

    def test_a_recently_hidden_holder_keeps_its_seat(self):
        """タブを切り替えた直後に開いた画面が、勝手に席を奪わない(従来どおり)。"""
        self.screens.beat("A")
        self.screens.hide("A")
        self.walk(4)
        self.assertFalse(self.screens.beat("B").active)

    def test_a_late_hidden_signal_does_not_revive_a_closed_screen(self):
        """閉じるときは「隠れます」と「閉じます」が続けて飛び、届く順は決まっていない。
        「閉じます」が先に届いても、あとの「隠れます」で 24 時間残らないこと。"""
        self.screens.beat("A")
        self.screens.leave("A")
        self.screens.hide("A")
        self.assertEqual(self.ids(), [])
        self.assertIsNotNone(self.screens.left_at)

    def test_a_discarded_tab_takes_over_its_old_name(self):
        """ブラウザが捨てて読み込み直したタブは閉じた合図を送れない。前の名札を引き取る。"""
        self.screens.beat("old")
        self.screens.hide("old")
        self.walk(600)
        claim = self.screens.beat("new", replaces="old")
        self.assertTrue(claim.active)
        self.assertEqual(self.ids(), ["new"])

    def test_replaces_cannot_remove_a_visible_screen(self):
        """見えている(使っている)画面は、名乗り一つで消させない。"""
        self.screens.beat("A")
        self.screens.beat("B", replaces="A")
        self.assertEqual(sorted(self.ids()), ["A", "B"])
        self.assertTrue(self.screens.is_holder("A"))


class SleepTest(unittest.TestCase):
    """**スリープしていた時間は数えない。**

    Windows の ``time.monotonic()`` はスリープ中も進む。起きた直後に「何時間も
    心拍が無い」と読むと、画面が繋ぎ直すより先に居ないことにしてしまう。
    """

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.screens = screen.Screens(stale_sec=20.0, jump_sec=15.0, clock=self.clock)

    def test_a_visible_screen_survives_the_pc_sleeping(self):
        self.screens.beat("A")
        self.clock.t += 8 * 3600          # 一晩スリープ(見張りも止まっている)
        self.assertEqual([s.id for s in self.screens.live()], ["A"])
        self.assertAlmostEqual(self.screens.paused_sec, 8 * 3600, delta=1)
        self.assertIsNotNone(self.screens.resumed_at)

    def test_the_time_after_waking_counts_again(self):
        """起きたあとは普通に数える(永久に居座らない)。"""
        self.screens.beat("A")
        self.clock.t += 3600
        self.screens.live()
        for _ in range(12):
            self.clock.t += 2.0
            self.screens.live()
        self.assertEqual(self.screens.live(), [])

    def test_regular_ticks_are_not_a_sleep(self):
        self.screens.beat("A")
        for _ in range(5):
            self.clock.t += 2.0
            self.screens.live()
        self.assertIsNone(self.screens.resumed_at)


if __name__ == "__main__":
    unittest.main()
