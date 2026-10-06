"""多重起動の防止 ── **2つ立たないこと** (`launch_guard`)

【なぜ機械で見るのか】
2つ立っても、画面はどちらも正常に見えます。気づくのは後日、
「打ったはずの日報が無い」という形です ── 片方に入れた12行は、
もう片方の画面には出ません。**壊れたことが見えない不具合**なので、
人が触って確かめることを当てにできません。

ここで押さえるのは3つです。

    1. 印(ロック)は**ポートを取る前**に立つ。判定と印のあいだに
       隙間があると、2つ同時に押されたときに2つとも通る
    2. 立ったばかりで**まだ応答しない**印を、死んだ印と読み違えない
    3. 応答しないまま居座っているときは、**2つ目を立てずに断る**
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import launch_guard
import process_manager  # noqa: F401 - 記録の書き先を先に寄せるため(下)
import start_app  # noqa: F401
from tests._launch_logs import keep_in_temp

# 起動の記録を本物の logs へ書かない(`tests/_launch_logs.py`)
keep_in_temp()


class GuardTestCase(unittest.TestCase):
    """印の置き場所を、その試験だけの場所にする。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.object(launch_guard, "lock_path",
                               lambda: self.tmp / launch_guard.LOCK_NAME)
        patcher.start()
        self.addCleanup(patcher.stop)

    def info(self, **kwargs) -> launch_guard.LockInfo:
        values = dict(app_id="nlm.nippou-tool", pid=os.getpid(), port=0,
                      url="", started_at=time.time(), version="3.37.0",
                      app_root=str(Path(__file__).resolve().parent.parent))
        values.update(kwargs)
        return launch_guard.LockInfo(**values)

    def health(self, **kwargs) -> dict:
        body = {"app_id": "nlm.nippou-tool", "version": "3.37.0"}
        body.update(kwargs)
        return body


class ClaimTests(GuardTestCase):
    """印を立てるのは**1つだけ**。"""

    def test_誰も居なければ立てられる(self) -> None:
        self.assertTrue(launch_guard.claim_lock(self.info()))
        self.assertTrue((self.tmp / launch_guard.LOCK_NAME).is_file())

    def test_2つ目は立てられない(self) -> None:
        """**ここが本体。** 同時に押された2つのうち、通るのは1つ。

        以前は「調べる → ポートを取る → 待ち受け → 印」の順で、
        調べてから印までに1秒以上ありました。その隙間に2つ目が入ると
        両方が「誰も居ない」を見て、両方が進みます。
        """
        self.assertTrue(launch_guard.claim_lock(self.info(pid=111)))
        self.assertFalse(launch_guard.claim_lock(self.info(pid=222)))

    def test_立てられなかったほうは印を汚さない(self) -> None:
        """後から来たほうが上書きすると、**先に動いているほうの居場所が
        消えます** ── `stop.bat` も次の起動も、声の掛け先を失います。"""
        launch_guard.claim_lock(self.info(pid=111))
        launch_guard.claim_lock(self.info(pid=222))
        self.assertEqual(launch_guard.read_lock().pid, 111)

    def test_ポートは後から書き足せる(self) -> None:
        """ポートは印を立てたあとに決まります。決まったら書き足す。"""
        launch_guard.claim_lock(self.info())
        launch_guard.update_lock(port=8733, token="t",
                                 url="http://127.0.0.1:8733/")
        stored = launch_guard.read_lock()
        self.assertEqual((stored.port, stored.token), (8733, "t"))

    def test_人の印は書き換えない(self) -> None:
        launch_guard.claim_lock(self.info(pid=os.getpid() + 1, port=8733))
        launch_guard.update_lock(port=9999)
        self.assertEqual(launch_guard.read_lock().port, 8733)

    def test_印を立てられなくても起動は止めない(self) -> None:
        """**2つ立つことより、一度も立たないことのほうが困ります。**"""
        with patch.object(launch_guard.os, "open",
                          side_effect=OSError("書けません")):
            self.assertTrue(launch_guard.claim_lock(self.info()))

    def test_自分の印だけ片付ける(self) -> None:
        """入れ替えのとき、古いほうの後始末が新しいほうの印を消すと、
        次の起動が「誰も居ない」と読んで2つ目を立てます。"""
        launch_guard.claim_lock(self.info(pid=os.getpid() + 1))
        launch_guard.release_lock()
        self.assertIsNotNone(launch_guard.read_lock())

        launch_guard.remove_lock()
        launch_guard.claim_lock(self.info())
        launch_guard.release_lock()
        self.assertIsNone(launch_guard.read_lock())


class StartingTests(GuardTestCase):
    """立ったばかりの印を、**死んだ印と読み違えない**。"""

    def test_起動中は待ってから合流する(self) -> None:
        """印が立った直後はポートが 0 で、まだ何も答えません。

        以前はそれを「別のアプリが居る」と読んで印を消し、**結局2つ
        立って**いました。少し待って、答えるようになったら合流します。
        """
        launch_guard.claim_lock(self.info(pid=999, port=0))
        calls = {"n": 0}

        def later_port(*_a, **_kw):
            # 3回目の様子見で、相手のポートが決まったことにする
            calls["n"] += 1
            if calls["n"] >= 3:
                stored = launch_guard.read_lock()
                stored.port = 8733
                stored.url = "http://127.0.0.1:8733/"
                launch_guard.write_lock(stored)

        with patch.object(launch_guard, "is_process_alive", return_value=True), \
             patch.object(launch_guard, "probe_health",
                          side_effect=lambda *a, **k: self.health()), \
             patch.object(launch_guard.time, "sleep", later_port):
            result = launch_guard.check_existing()

        self.assertFalse(result.should_start, result.reason)
        self.assertEqual(result.url, "http://127.0.0.1:8733/")

    def test_起動中の印を消さない(self) -> None:
        """待っているあいだに印を消すと、相手が書き足せなくなります。"""
        launch_guard.claim_lock(self.info(pid=999, port=0))
        with patch.object(launch_guard, "is_process_alive", return_value=True), \
             patch.object(launch_guard, "probe_health", return_value=None), \
             patch.object(launch_guard, "STARTUP_GRACE_SEC", 0.4), \
             patch.object(launch_guard, "POLL_INTERVAL_SEC", 0.05):
            launch_guard.check_existing()
        self.assertIsNotNone(launch_guard.read_lock())

    def test_猶予を過ぎた印は待たない(self) -> None:
        """そこまで待って答えないものは、待っても答えません。"""
        old = self.info(pid=999, port=8733,
                        started_at=time.time() - launch_guard.STARTUP_GRACE_SEC - 5)
        launch_guard.claim_lock(old)
        started = time.monotonic()
        with patch.object(launch_guard, "is_process_alive", return_value=True), \
             patch.object(launch_guard, "probe_health", return_value=None), \
             patch.object(launch_guard, "process_command_line",
                          return_value="python start_app.py"):
            result = launch_guard.check_existing()
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertFalse(result.should_start)


class HungTests(GuardTestCase):
    """応答しないまま居座っているとき。**2つ目を立てない。**"""

    def test_応答しない自分のプロセスがあれば起動しない(self) -> None:
        """立ててしまうと2つ動きます ── 片方で打った日報は、もう片方の
        画面に出ません。止まっていることより質が悪い。"""
        launch_guard.claim_lock(self.info(
            pid=999, port=8733,
            started_at=time.time() - launch_guard.STARTUP_GRACE_SEC - 5))
        with patch.object(launch_guard, "is_process_alive", return_value=True), \
             patch.object(launch_guard, "probe_health", return_value=None), \
             patch.object(launch_guard, "process_command_line",
                          return_value="python start_app.py"):
            result = launch_guard.check_existing()
        self.assertFalse(result.should_start)
        self.assertTrue(result.hung)
        self.assertIsNotNone(launch_guard.read_lock(), "印を消しています")

    def test_別のプロセスなら印を消して起動する(self) -> None:
        """PIDは使い回されます。**無関係なプロセスに居座られない。**"""
        launch_guard.claim_lock(self.info(
            pid=999, port=8733,
            started_at=time.time() - launch_guard.STARTUP_GRACE_SEC - 5))
        with patch.object(launch_guard, "is_process_alive", return_value=True), \
             patch.object(launch_guard, "probe_health", return_value=None), \
             patch.object(launch_guard, "process_command_line",
                          return_value="/usr/bin/some-other-app --serve"):
            result = launch_guard.check_existing()
        self.assertTrue(result.should_start, result.reason)
        self.assertIsNone(launch_guard.read_lock())

    def test_コマンドラインが取れなければ見送る(self) -> None:
        """**分からないときは立てない。** 立ててしまうと分かれます。"""
        launch_guard.claim_lock(self.info(
            pid=999, port=8733,
            started_at=time.time() - launch_guard.STARTUP_GRACE_SEC - 5))
        with patch.object(launch_guard, "is_process_alive", return_value=True), \
             patch.object(launch_guard, "probe_health", return_value=None), \
             patch.object(launch_guard, "process_command_line", return_value=""):
            result = launch_guard.check_existing()
        self.assertFalse(result.should_start)

    def test_死んだ印は片付けて起動する(self) -> None:
        launch_guard.claim_lock(self.info(pid=999, port=8733))
        with patch.object(launch_guard, "is_process_alive", return_value=False):
            result = launch_guard.check_existing()
        self.assertTrue(result.should_start)
        self.assertIsNone(launch_guard.read_lock())


class HungReportTests(GuardTestCase):
    """断るときは、**何が残っていて、どう片付けるか**まで出す。"""

    def test_止め方と印の場所を出す(self) -> None:
        import start_app

        guard = launch_guard.GuardResult(
            False, existing=self.info(pid=999, port=8733), hung=True,
            reason="前の起動が終わっていません(応答がありません)")
        text = "\n".join(start_app._hung_report(guard))
        self.assertIn("999", text)
        self.assertIn("stop.bat", text)
        self.assertIn(launch_guard.LOCK_NAME, text)
        # 起動の途中かもしれないので、まず待つことを先に言う
        self.assertIn("もう一度", text)

    def test_断ったことを呼んだ側にも返す(self) -> None:
        import start_app

        guard = launch_guard.GuardResult(
            False, existing=self.info(pid=999), hung=True, reason="応答なし")
        with patch.object(start_app.webbrowser, "open") as opened:
            code = start_app._join_or_explain(guard, open_browser=True)
        self.assertEqual(code, 1)
        opened.assert_not_called()      # 開く先が無い。開くと誤解を招く


class FindRunningTests(GuardTestCase):
    """**印を当てにせず、ポートを舐めて自分を探す。**

    【「合流しました」と出ているのに2つ動いていた、の正体】
    以前ここを見ていたのは印(ロック)だけでした。ところが印には
    **1つぶんしか書けません。** だから、こうなると取り逃します。

        印が無い状態から起動する
          → 印を読む → 無い → 「起動してよい」
          → ポートを選ぶ → 8733 は塞がっている → **8734 へずらす**
          → 2つ目が立つ

    ずらしたのは「塞いでいるのは別のアプリだろう」という前提でしたが、
    塞いでいたのは**自分自身**でした(印が消えた・印を書く前に落ちた・
    古い版の不具合で取り逃した残り)。こうなると印は片方しか指せないので、
    次からの起動は毎回そちらへ合流し、**もう片方は誰にも気づかれない
    まま動き続けます。**

    ログには「合流しました」と出ます。**出ているのに2つ動いている**ので、
    黙って壊れているより質が悪い ── ここはその穴を塞ぎます。
    """

    def ports(self) -> list[int]:
        from nippou import app_config

        return list(app_config.port_candidates())

    def answering(self, *ports: int, **extra):
        """指定したポートだけが**自分として**答える、という世界を作る。"""
        wanted = set(ports)

        def probe(port, timeout=None):              # noqa: ARG001
            if port not in wanted:
                return None
            body = self.health(pid=9000 + port, app_root=str(self.tmp))
            body.update(extra)
            return body

        return patch.object(launch_guard, "probe_health", side_effect=probe)

    def test_答えたものを全部返す(self) -> None:
        first, second = self.ports()[0], self.ports()[1]
        with self.answering(first, second):
            found = launch_guard.find_running()
        self.assertEqual([r.port for r in found], [first, second])
        self.assertEqual(found[0].pid, 9000 + first)

    def test_別のアプリは数えない(self) -> None:
        """同じポートを使う無関係なものを、自分だと思い込まない。"""
        first = self.ports()[0]
        with patch.object(launch_guard, "probe_health",
                          side_effect=lambda p, timeout=None: (
                              {"app_id": "よそのアプリ"} if p == first else None)):
            self.assertEqual(launch_guard.find_running(), [])

    def test_印が無くても動いていれば合流する(self) -> None:
        """**ここが本体。** 印が無い＝起動してよい、ではありません。

        直す前はここで `should_start=True` を返していました。その先で
        `pick_port` が隣の番号へずれて、2つ目が静かに立ちます。
        """
        first = self.ports()[0]
        self.assertIsNone(launch_guard.read_lock())
        with self.answering(first):
            result = launch_guard.check_existing()
        self.assertFalse(result.should_start, result.reason)
        self.assertEqual(result.url, f"http://127.0.0.1:{first}/")

    def test_合流したら印を立て直す(self) -> None:
        """次からは印で見つけられるように。**毎回舐めさせない。**"""
        first = self.ports()[0]
        with self.answering(first):
            launch_guard.check_existing()
        stored = launch_guard.read_lock()
        self.assertIsNotNone(stored, "印を立て直していません")
        self.assertEqual(stored.port, first)
        self.assertEqual(stored.pid, 9000 + first)

    def test_死んだ印でもポートを見てから決める(self) -> None:
        """印は残っているがPIDは不在 ── それでも動いているかもしれない。

        PIDは使い回されるので「不在」はよく起きます。以前はそのまま
        起動していたので、**残っていた本体の上に2つ目**が乗りました。
        """
        second = self.ports()[1]
        launch_guard.claim_lock(self.info(pid=999, port=self.ports()[0]))
        with patch.object(launch_guard, "is_process_alive", return_value=False), \
             self.answering(second):
            result = launch_guard.check_existing()
        self.assertFalse(result.should_start, result.reason)
        self.assertEqual(result.url, f"http://127.0.0.1:{second}/")

    def test_2つ動いていたら黙って合流しない(self) -> None:
        """すでに多重起動している。**残りを見えるところへ出す。**

        1つへ合流して済ませると、もう片方は印にも画面にも出ません ──
        気づくのは翌日「打ったはずの直が無い」という形です。
        """
        first, second = self.ports()[0], self.ports()[1]
        with self.answering(first, second):
            result = launch_guard.check_existing()
        self.assertFalse(result.should_start)
        self.assertEqual([r.port for r in result.extras], [second])

    def test_誰も答えなければ起動してよい(self) -> None:
        with self.answering():
            self.assertTrue(launch_guard.check_existing().should_start)


class PickPortTests(GuardTestCase):
    """**自分の上に積まない。** ずらしてよい相手かを見る。"""

    def ports(self) -> list[int]:
        from nippou import app_config

        return list(app_config.port_candidates())

    def test_塞いでいるのが自分ならずらさない(self) -> None:
        """**「合流したのに2つ動く」のもう半分がここ。**

        直す前は「多重起動は `check_existing()` が弾いているのだから、
        塞いでいるのは別のアプリだろう」という前提でずらしていました。
        印が消えていればその前提は崩れ、8733 を塞いでいるのは自分です。
        """
        first = self.ports()[0]
        with patch.object(launch_guard, "is_port_free",
                          side_effect=lambda p, host="": p != first), \
             patch.object(launch_guard, "probe_health",
                          side_effect=lambda p, timeout=None: (
                              self.health() if p == first else None)):
            self.assertIsNone(launch_guard.pick_port())

    def test_別のアプリが塞いでいるならずらす(self) -> None:
        """こちらは今までどおり。**避けるのが正しい相手**です。"""
        first, second = self.ports()[0], self.ports()[1]
        with patch.object(launch_guard, "is_port_free",
                          side_effect=lambda p, host="": p != first), \
             patch.object(launch_guard, "probe_health",
                          side_effect=lambda p, timeout=None: (
                              {"app_id": "よそのアプリ"} if p == first else None)):
            self.assertEqual(launch_guard.pick_port(), second)

    def test_空いていればそのまま使う(self) -> None:
        with patch.object(launch_guard, "is_port_free", return_value=True):
            self.assertEqual(launch_guard.pick_port(), self.ports()[0])


class ExtrasReportTests(GuardTestCase):
    """2つ動いていることを、**押した人に見える形で**言う。"""

    def test_両方の居場所を出す(self) -> None:
        import start_app

        guard = launch_guard.GuardResult(
            False, url="http://127.0.0.1:8733/",
            existing=self.info(pid=111, port=8733),
            extras=[launch_guard.Running(port=8734, pid=222, version="3.41.0")],
            reason="合流しました")
        text = "\n".join(start_app._extras_report(guard))
        self.assertIn("8733", text)
        self.assertIn("8734", text)
        self.assertIn("222", text)
        self.assertIn("stop.bat", text)

    def test_合流しても黙らない(self) -> None:
        """**ここが効き目の本体。** 合流は成功扱い(0)ですが、2つ
        動いているなら、そのことは必ず画面に出します。"""
        import start_app

        guard = launch_guard.GuardResult(
            False, url="http://127.0.0.1:8733/",
            existing=self.info(pid=111, port=8733),
            extras=[launch_guard.Running(port=8734, pid=222)],
            reason="合流しました")
        with patch.object(start_app.webbrowser, "open"), \
             patch("builtins.print") as printed:
            code = start_app._join_or_explain(guard, open_browser=True)
        self.assertEqual(code, 0)
        said = "\n".join(str(c.args[0]) for c in printed.call_args_list if c.args)
        self.assertIn("8734", said)
        self.assertIn("2つ以上", said)


class StopEveryoneTests(GuardTestCase):
    """`stop.bat` は**動いている全部**を止める。

    印に載っていない残りを見逃すと、止めたつもりの次の起動がそちらへ
    合流します ── 「止めたのにまだ動いている」がそこから続きます。
    """

    def running(self, port: int, pid: int) -> "launch_guard.Running":
        return launch_guard.Running(port=port, pid=pid, version="3.41.0",
                                    app_root=str(self.tmp))

    def test_印に載っていないものも止める(self) -> None:
        import process_manager

        launch_guard.claim_lock(self.info(pid=111, port=8733, token="t"))
        killed: list[int] = []
        with patch.object(launch_guard, "find_running",
                          return_value=[self.running(8733, 111),
                                        self.running(8734, 222)]), \
             patch.object(launch_guard, "is_process_alive", return_value=True), \
             patch.object(process_manager, "_looks_like_our_process",
                          return_value=True), \
             patch.object(process_manager.os, "kill",
                          side_effect=lambda pid, sig: killed.append(pid)), \
             patch.object(process_manager, "_wait_pid_gone", return_value=True), \
             patch.object(process_manager, "_request_shutdown",
                          return_value={"ok": True}), \
             patch.object(process_manager, "_wait_gone", return_value=True):
            result = process_manager.stop()

        self.assertIn(222, killed, "印に載っていないほうが残ります")
        self.assertTrue(result.stopped)
        # 止めたことを**結末の文にも残す**(この先の分岐で消さない)
        self.assertIn("1個", result.message)

    def test_印が無くても動いていれば止める(self) -> None:
        """印が消えた状態こそ、この機能が要る場面です。"""
        import process_manager

        killed: list[int] = []
        with patch.object(launch_guard, "find_running",
                          return_value=[self.running(8734, 222)]), \
             patch.object(launch_guard, "is_process_alive", return_value=True), \
             patch.object(process_manager, "_looks_like_our_process",
                          return_value=True), \
             patch.object(process_manager.os, "kill",
                          side_effect=lambda pid, sig: killed.append(pid)), \
             patch.object(process_manager, "_wait_pid_gone", return_value=True):
            result = process_manager.stop()

        self.assertEqual(killed, [222])
        self.assertTrue(result.stopped)

    def test_照合できないものは巻き添えにしない(self) -> None:
        """落としてよいか分からないときは**落とさない。**

        `launch_guard` 側とわざと逆に倒してあります ── あちらの
        「分からない」は起動を見送る側へ、こちらは止めない側へ。
        """
        import process_manager

        killed: list[int] = []
        with patch.object(launch_guard, "find_running",
                          return_value=[self.running(8734, 222)]), \
             patch.object(launch_guard, "is_process_alive", return_value=True), \
             patch.object(process_manager, "_looks_like_our_process",
                          return_value=False), \
             patch.object(process_manager.os, "kill",
                          side_effect=lambda pid, sig: killed.append(pid)), \
             patch.object(process_manager, "_wait_pid_gone", return_value=True):
            process_manager.stop()

        self.assertEqual(killed, [])

    def test_本当に何も動いていなければ何もしない(self) -> None:
        import process_manager

        with patch.object(launch_guard, "find_running", return_value=[]):
            result = process_manager.stop()
        self.assertEqual(result.method, "none")


class ReallyStartTwiceTests(unittest.TestCase):
    """**本当に同時に起動して数える。**

    ここまでの試験は印の扱いを部品として見たものです。実際に起きたのは
    「バッチを2回押したら2つ動いた」で、それは部品ではなく**順番**の
    問題でした ── 調べてから印を立てるまでの隙間。隙間は作り直すたびに
    戻りうるので、ここでは本当に2つ起動して数えます。

    直す前のこの試験は、次のどちらかで落ちます。

        ・2つが別のポートを取る(8733 と 8734 で静かに両方動く)
        ・先に終わったほうが、**動いているほうの印を消す**
          (`stop.bat` も次の起動も、声の掛け先を失う)
    """

    #: 立ち上がるのを待つ上限(秒)。共有が遠いと十数秒かかる
    WAIT_SEC = 40.0

    def setUp(self) -> None:
        import random

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(__file__).resolve().parent.parent

        # **本番の番号は使わない。** 8733 を使うと、開発中の人が動かして
        # いる本物に当たります。前の試験の後始末で残る TIME_WAIT にも
        # 巻き込まれるので、試験ごとに番号をずらします
        base = random.randint(18800, 19600)
        raw = json.loads(
            (self.root / "config" / "app.json").read_text(encoding="utf-8"))
        raw["server"]["port"] = base
        raw["server"]["port_retry"] = 3
        config = self.tmp / "app.json"
        config.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        self.PORTS = range(base, base + 4)

        self.env = dict(os.environ)
        self.env.update({
            "NIPPOU_LOCAL_DIR": str(self.tmp / "local"),
            "NIPPOU_APP_DIR": str(self.tmp / "app"),
            "NIPPOU_GW_REF_DIR": str(self.tmp / "ref"),
            "NIPPOU_APP_CONFIG": str(config),
        })
        self.app_id = raw["app_id"]
        self.procs: list = []
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        import subprocess

        for proc in self.procs:
            if proc.poll() is None:
                proc.terminate()
        for proc in self.procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:      # noqa: PERF203
                proc.kill()

    def _health(self, port: int):
        import urllib.request

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(f"http://127.0.0.1:{port}/api/health",
                             timeout=0.7) as res:
                return json.loads(res.read().decode("utf-8"))
        except Exception:                          # noqa: BLE001
            return None

    def _ours(self) -> dict:
        """いま待ち受けている**このアプリ**を、ポートごとに。"""
        found = {}
        for port in self.PORTS:
            body = self._health(port)
            if body and body.get("app_id") == self.app_id:
                found[port] = body.get("pid")
        return found

    def _reasons(self) -> str:
        """落ちたときに読む、それぞれの言い分。"""
        lines = []
        for i, proc in enumerate(self.procs, 1):
            if proc.poll() is None:
                lines.append(f"  {i}: 動いています")
                continue
            out = (proc.stdout.read() or "").strip().splitlines()
            lines.append(f"  {i}: 終了 {proc.returncode} :: "
                         + " / ".join(out[-4:])[:300])
        return "\n".join(lines)

    def test_同時に3つ起動しても立つのは1つ(self) -> None:
        """**押しても押しても、動くのは1つ。**

        見張りは Popen より**先に**回し始めます。合流できなかったほうは
        すぐ終わるので、始まってから数え始めると数え落とします。
        """
        import subprocess
        import threading

        seen: dict[int, int] = {}
        peak = 0
        watching = True

        def watch() -> None:
            nonlocal peak
            while watching:
                now = self._ours()
                seen.update(now)
                peak = max(peak, len(now))
                time.sleep(0.15)

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        self.addCleanup(watcher.join, 5)

        def stop_watching() -> None:
            nonlocal watching
            watching = False

        self.addCleanup(stop_watching)

        try:
            self.procs = [
                subprocess.Popen(
                    [sys.executable, str(self.root / "start_app.py"),
                     "--no-browser"],
                    cwd=str(self.root), env=self.env,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                for _ in range(3)]
        except OSError as exc:                     # noqa: BLE001
            self.skipTest(f"起動できない環境です: {exc}")

        # 決着を待つ。**負けたほうが終わる**まで(勝つのはどれでもよい)
        deadline = time.monotonic() + self.WAIT_SEC
        while time.monotonic() < deadline:
            left = sum(1 for p in self.procs if p.poll() is None)
            if (seen and left <= 1) or left == 0:
                break                             # 決着した / 誰も残らなかった
            time.sleep(0.2)
        # 合流したほうが後始末を終えるまで、ひと呼吸。**印を消すのは
        # 終わり際**なので、早く見ると壊れていても気づけません
        time.sleep(1.0)
        stop_watching()

        if not seen:
            self.skipTest(f"1つも起動できませんでした:\n{self._reasons()}")

        self.assertEqual(peak, 1,
                         f"同時に {peak} つ動いています: {seen}\n{self._reasons()}")
        self.assertEqual(len(seen), 1,
                         f"別々のポートに散らばりました: {seen}\n{self._reasons()}")

        # --- 印が、**動いているほう**を指していること ---
        #
        # ここが実際に壊れていたところ。合流したほうも終わり際に印を
        # 片付けていたので、動いているほうの居場所が消えていました。
        # 印が無いと `stop.bat` は止められず、次の起動は「誰も居ない」と
        # 読んで**2つ目を立てます**
        running = self._ours()
        if not running:
            return                                # 全部終わったあとなら見ない
        lock = self.tmp / "local" / "runtime" / launch_guard.LOCK_NAME
        self.assertTrue(lock.is_file(),
                        "動いているのに印がありません(止められなくなります)")
        raw = json.loads(lock.read_text(encoding="utf-8"))
        self.assertIn(raw["port"], running,
                      f"印が別のものを指しています: {raw} / 動いている: {running}")


class LockFileTests(GuardTestCase):
    def test_壊れた印は無視して起動する(self) -> None:
        """読めないことを理由に起動を止めない。"""
        (self.tmp / launch_guard.LOCK_NAME).write_text("{壊れている",
                                                       encoding="utf-8")
        self.assertIsNone(launch_guard.read_lock())
        # 印が読めないときはポートを舐めに行く。**この試験で本物の
        # 8733 を叩かない** ── 開発中の人が動かしているものに当たる
        with patch.object(launch_guard, "find_running", return_value=[]):
            self.assertTrue(launch_guard.check_existing().should_start)

    def test_印は本人だけが読める権限で置く(self) -> None:
        """トークンが入っているため。"""
        if os.name == "nt":                       # Windows には chmod が無い
            self.skipTest("Windows では利用者ごとのフォルダで守る")
        launch_guard.claim_lock(self.info(token="秘密"))
        mode = (self.tmp / launch_guard.LOCK_NAME).stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_印の中身は読み直せる(self) -> None:
        launch_guard.claim_lock(self.info(pid=4321, port=8733, token="t"))
        raw = json.loads(
            (self.tmp / launch_guard.LOCK_NAME).read_text(encoding="utf-8"))
        self.assertEqual(raw["pid"], 4321)
        self.assertEqual(raw["app_id"], "nlm.nippou-tool")


if __name__ == "__main__":
    unittest.main()
