"""画面が居なくなったら終わる (基盤仕様書 2.8 / 2.9)

**このアプリに窓が無いことから来る不具合の対策。** タブを閉じても
``pythonw.exe`` は残り、ウィンドウを持たないのでタスクマネージャーの
「アプリ」にも出てこない。次に起動すると「すでに起動しています」と言われ、
**閉じたのに開けない**状態になる ── 実際に現場で起きた::

    2026/08/31 09:44:18 | すでに起動しています: site (pid=8372 port=8741)
    2026/08/31 09:44:18 | 起動しません: 同じアプリが起動中

時間で落とすので、テストは実時間を待たずに済むよう猶予を極小にして回す。
"""

from __future__ import annotations

import time
import unittest

from kanban import idle_exit, screen


class WatchTestBase(unittest.TestCase):
    def setUp(self) -> None:
        idle_exit.reset()
        self.stopped: list[bool] = []
        self.busy_reason = ""

    def tearDown(self) -> None:
        idle_exit.reset()

    def make(self, **kwargs) -> idle_exit.IdleWatch:
        opts = {"idle_sec": 0.2, "grace_sec": 0.1, "tick_sec": 0.02}
        opts.update(kwargs)
        return idle_exit.IdleWatch(
            lambda: self.stopped.append(True), lambda: self.busy_reason, **opts
        )


class OverdueTest(WatchTestBase):
    """終わってよいかの判断だけを見る(スレッドを回さない)。"""

    def test_never_connected_never_expires(self):
        """1度も繋がっていなければ落とさない。

        ``--no-browser`` で立てておく使い方(検証・並行運用)を巻き添えに
        しない。
        """
        watch = self.make()
        self.assertIsNone(watch.overdue())
        time.sleep(0.3)
        self.assertIsNone(watch.overdue())

    def test_expires_after_silence(self):
        watch = self.make()
        watch.beat()
        self.assertIsNone(watch.overdue())
        time.sleep(0.25)
        self.assertIn("心拍", watch.overdue() or "")

    def test_leaving_expires_after_grace(self):
        watch = self.make()
        watch.beat()
        watch.leaving()
        self.assertIsNone(watch.overdue(), "猶予を置かずに落ちている")
        time.sleep(0.15)
        self.assertEqual(watch.overdue(), "画面が閉じられました")

    def test_beat_cancels_leaving(self):
        """画面の作り直し(再読込・モード切替)でも閉じた合図は飛ぶ。

        戻ってきたら取り消せなければ、再読込のたびに落ちる。
        """
        watch = self.make()
        watch.beat()
        watch.leaving()
        watch.beat()  # 戻ってきた
        time.sleep(0.15)
        self.assertIsNone(watch.overdue())

    def test_leaving_before_any_beat_is_ignored(self):
        watch = self.make()
        watch.leaving()
        time.sleep(0.15)
        self.assertIsNone(watch.overdue())

    def test_connected_flag(self):
        watch = self.make()
        self.assertFalse(watch.connected)
        watch.beat()
        self.assertTrue(watch.connected)

    def test_closing_one_of_two_screens_does_not_expire(self):
        """**2枚のうち1枚を閉じただけでは終わらない。**

        以前は心拍の送り主を区別していなかったので、閉じた合図が来た時点で
        猶予(8秒)が始まり、残った画面の次の心拍(20秒間隔)が間に合わずに
        **開いているのにアプリごと落ちて**いた。動いている本体でも再現した::

            タブA 心拍: 200
            タブB 心拍: 200
            タブA だけ閉じる(sendBeacon leaving=1): 200
              ...
              10秒後 タブBから見たサーバ: Connection refused
        """
        watch = self.make()
        watch.beat("A")
        watch.beat("B")
        watch.leaving("A")
        time.sleep(0.15)  # 猶予(0.1秒)より長く待つ
        self.assertIsNone(watch.overdue(), "1枚残っているのに終わろうとしている")

    def test_closing_the_last_screen_expires(self):
        watch = self.make()
        watch.beat("A")
        watch.beat("B")
        watch.leaving("A")
        watch.leaving("B")
        time.sleep(0.15)
        self.assertEqual(watch.overdue(), "画面が閉じられました")


class LoopTest(WatchTestBase):
    """見張りスレッドを実際に回す。"""

    def _wait_stop(self, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.stopped:
                return True
            time.sleep(0.02)
        return False

    def test_stops_when_the_screen_closes(self):
        watch = self.make()
        watch.start()
        watch.beat()
        watch.leaving()
        self.assertTrue(self._wait_stop(), "閉じたのに止まらない")

    def test_does_not_stop_while_another_screen_is_open(self):
        """見張りを実際に回しても、残った1枚を巻き添えにしないこと。"""
        watch = self.make(idle_sec=3.0, grace_sec=0.05)
        watch.start()
        watch.beat("A")
        watch.beat("B")
        watch.leaving("A")
        for _ in range(10):
            time.sleep(0.04)
            watch.beat("B")  # 残った画面は心拍を送り続ける
        self.assertFalse(self.stopped, "1枚残っているのに止めている")

    def test_stops_after_silence(self):
        watch = self.make()
        watch.start()
        watch.beat()
        self.assertTrue(self._wait_stop(), "無通信でも止まらない")

    def test_does_not_stop_while_busy(self):
        """**処理中は落とさない。**

        書き戻しの途中で終わると、どこまで送れたのか分からなくなる。
        """
        self.busy_reason = "共有DBへ未反映の操作が 3 件あります"
        watch = self.make()
        watch.start()
        watch.beat()
        watch.leaving()
        time.sleep(0.4)
        self.assertFalse(self.stopped, "処理中なのに止めている")

        # 処理が終われば、次の見回りで止まる
        self.busy_reason = ""
        self.assertTrue(self._wait_stop())

    def test_busy_check_error_does_not_block_stop(self):
        """判定できないなら止めてよい(見張りが黙って死なないこと)。"""

        def boom() -> str:
            raise RuntimeError("判定できません")

        watch = idle_exit.IdleWatch(
            lambda: self.stopped.append(True), boom,
            idle_sec=0.2, grace_sec=0.05, tick_sec=0.02,
        )
        watch.start()
        watch.beat()
        watch.leaving()
        self.assertTrue(self._wait_stop())

    def test_never_connected_is_not_stopped(self):
        watch = self.make()
        watch.start()
        time.sleep(0.4)
        self.assertFalse(self.stopped, "繋がっていないのに止めている")


class InstallTest(WatchTestBase):
    def test_install_is_idempotent(self):
        first = idle_exit.install(lambda: None, lambda: "", tick_sec=0.05)
        second = idle_exit.install(lambda: None, lambda: "", tick_sec=0.05)
        self.assertIs(first, second)
        self.assertIs(idle_exit.get(), first)

    def test_get_is_none_before_install(self):
        self.assertIsNone(idle_exit.get())

    def test_heartbeat_interval_is_shorter_than_idle(self):
        """画面の送信間隔より、落とすまでの時間が十分長いこと。

        **出どころはこのモジュールただ1つ**(画面へも同じ値を渡す)。
        逆転すると、開いているのに落ちる。
        """
        self.assertLess(idle_exit.HEARTBEAT_MS / 1000 * 2, idle_exit.IDLE_SEC)

    def test_a_silent_screen_is_dropped_before_the_app_gives_up(self):
        """**画面を諦めるほうが、アプリを諦めるより先。**

        逆だと、返事の無くなった画面が一覧に残ったまま「まだ誰か見ている」と
        答え続け、**無通信でも終わらなくなる**(= 閉じたのに開けない、が戻る)。
        """
        watch = idle_exit.IdleWatch(lambda: None, lambda: "")
        self.assertLess(watch.screens.stale_sec, watch.idle_sec)

    def test_the_screen_list_is_shared_with_the_guard(self):
        """見張りと「押してよい画面か」の判断は**同じ一覧**を見ること。

        別々に持つと、片方が「まだ開いている」、もう片方が「誰も居ない」に
        なる。
        """
        watch = idle_exit.IdleWatch(lambda: None, lambda: "")
        self.assertIs(watch.screens, screen.get())

    def test_a_screen_can_be_frozen_for_a_while_before_it_loses_its_turn(self):
        """固まった画面がすぐ席を失わないこと。

        短すぎると、少し重いだけで持ち主が別のタブへ移ってしまう。
        """
        self.assertGreaterEqual(screen.STALE_SEC, idle_exit.HEARTBEAT_MS / 1000 * 3)


class BackgroundAndSleepTest(WatchTestBase):
    """**裏に回っても、スリープしても終わらない。**

    別の道具では、ブラウザが裏のタブのタイマーを間引いたせいで心拍が途切れ、
    開いているのに終了した。
    """

    def setUp(self) -> None:
        super().setUp()
        self.t = 1000.0
        self.screens = screen.Screens(clock=lambda: self.t)
        self.watch = idle_exit.IdleWatch(
            lambda: self.stopped.append(True), lambda: "",
            idle_sec=90.0, grace_sec=8.0, resume_grace_sec=120.0, screens=self.screens,
        )

    def walk(self, sec: float) -> str | None:
        """見張りと同じく 2 秒ごとに判断しながら進める。最初に出た「終わる理由」を返す。"""
        end = self.t + sec
        while self.t < end:
            self.t = min(end, self.t + 2.0)
            why = self.watch.overdue()
            if why:
                return why
        return None

    def test_a_hidden_screen_does_not_expire(self):
        self.watch.beat("A")
        self.screens.hide("A")
        self.assertIsNone(self.walk(3 * 3600), "裏に回っただけで終わった")

    def test_a_visible_screen_still_expires_when_silent(self):
        """見えている画面の心拍が途切れたら従来どおり終わる(閉じた合図が届かなかったとき)。"""
        self.watch.beat("A")
        self.assertIn("心拍", self.walk(200) or "")

    def test_waking_from_sleep_does_not_stop_the_app(self):
        """**起きた直後に終わらない。** 画面が繋ぎ直すのを待つ。"""
        self.watch.beat("A")
        self.walk(10)
        self.t += 8 * 3600                 # スリープ(見張りも止まる)
        self.assertIsNone(self.walk(100), "スリープ明けに、画面が戻る前に終わった")
        self.watch.beat("A")               # 画面が戻ってきた
        self.assertIsNone(self.walk(60))

    def test_it_gives_up_if_nobody_comes_back_after_waking(self):
        """起きたあと誰も戻らなければ、猶予のあとで終わる(永久に居座らない)。"""
        self.watch.beat("A")
        self.t += 3600
        self.assertIn("心拍", self.walk(400) or "")

    def test_closing_the_last_screen_still_stops_the_app(self):
        """裏に回る合図のあとに閉じても、閉じたことは効く。"""
        self.watch.beat("A")
        self.screens.hide("A")
        self.watch.leaving("A")
        self.assertEqual(self.walk(20), "画面が閉じられました")


if __name__ == "__main__":
    unittest.main()
