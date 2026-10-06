"""起動基盤 (基盤仕様書 2章)

起動入口の符号化・多重起動の判定・待機画面・アプリ固有値。
**業務が動く前に効くもの**なので、業務のテストとは分けてある。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path

from . import _isolation

_isolation.ensure_isolated()

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import launch_guard  # noqa: E402
from calendar_app import app_config, boot_screen  # noqa: E402


class LaunchFileTests(unittest.TestCase):
    """起動入口の符号化 (基盤仕様書 2.1)

    【なぜ機械で見張るのか】
    ``.vbs`` は WSH が**システムのANSIコードページ**(日本語Windowsでは932)で
    読む。``.bat`` は cmd.exe が**コンソールのコードページ**で読む。
    UTF-8 で保存すると日本語が化けるだけでなく、CP932 のトレイルバイトには
    ``0x5C``(``\\``)や ``0x40``(``@``)が現れるため、**構文そのものが壊れる**。

    エディタが気を利かせて UTF-8 に直してしまう事故が起きるので、
    ここで固定しておく。
    """

    def _read(self, name: str) -> bytes:
        path = _ROOT / name
        self.assertTrue(path.exists(), f"{name} がありません")
        return path.read_bytes()

    def test_VBSはCP932で読める(self) -> None:
        raw = self._read("Start.vbs")
        raw.decode("cp932")                        # 落ちなければ良い
        self.assertNotEqual(raw[:3], b"\xef\xbb\xbf", "BOM を付けない")

    def test_BATはCP932で読める(self) -> None:
        for name in ("start.bat", "stop.bat"):
            with self.subTest(name=name):
                raw = self._read(name)
                raw.decode("cp932")
                self.assertNotEqual(raw[:3], b"\xef\xbb\xbf", "BOM を付けない")

    def test_改行はCRLF(self) -> None:
        for name in ("Start.vbs", "start.bat", "stop.bat"):
            with self.subTest(name=name):
                raw = self._read(name)
                self.assertNotIn(b"\n", raw.replace(b"\r\n", b""),
                                 f"{name} に LF だけの行があります")

    def test_BATはchcpより前をASCIIに保つ(self) -> None:
        """cmd.exe は**コードページを変える前の行も**そのページで読む。

        ``chcp 932`` より前に非ASCIIバイトがあると、その時点のページ次第で
        化ける(コメントの中であっても)。
        """
        for name in ("start.bat", "stop.bat"):
            with self.subTest(name=name):
                raw = self._read(name)
                head = raw.split(b"chcp 932", 1)[0]
                self.assertTrue(all(byte < 0x80 for byte in head),
                                f"{name} の chcp より前に非ASCIIがあります")

    def test_起動入口は同じPythonを呼ぶ(self) -> None:
        """通常起動と診断起動が別のものを起動しないこと(基盤仕様書 2.1)。"""
        vbs = self._read("Start.vbs").decode("cp932")
        bat = self._read("start.bat").decode("cp932")
        self.assertIn("start_app.py", vbs)
        self.assertIn("start_app.py", bat)

    def test_停止は専用の入口を通る(self) -> None:
        """``python.exe`` をプロセス名だけで一括終了しない(基盤仕様書 2.8)。"""
        stop = self._read("stop.bat").decode("cp932")
        self.assertIn("process_manager.py", stop)
        self.assertNotIn("taskkill", stop.lower())


class AppConfigTests(unittest.TestCase):
    """アプリ固有値 (基盤仕様書 5.2)。"""

    def test_設定ファイルが読める(self) -> None:
        self.assertEqual(app_config.load_error(), "")

    def test_版の書き方が正しい(self) -> None:
        self.assertEqual(app_config.version_problem(), "")

    def test_必要な値が揃っている(self) -> None:
        self.assertTrue(app_config.app_id())
        self.assertTrue(app_config.display_name())
        self.assertGreater(app_config.port(), 0)
        self.assertEqual(app_config.host(), "127.0.0.1")

    def test_ポート候補は連番(self) -> None:
        candidates = app_config.port_candidates()
        self.assertEqual(candidates[0], app_config.port())
        self.assertEqual(len(set(candidates)), len(candidates))

    def test_壊れた設定でも既定値で動く(self) -> None:
        """起動そのものが失敗するより、起動して画面に理由を出すほうがよい。"""
        import tempfile

        broken = Path(tempfile.mkdtemp()) / "app.json"
        broken.write_text("{ これはJSONではない", encoding="utf-8")
        original = app_config.CONFIG_PATH
        try:
            app_config.CONFIG_PATH = broken
            app_config.load(force=True)
            self.assertIn("読めませんでした", app_config.load_error())
            self.assertTrue(app_config.app_id())      # 既定値で動く
        finally:
            app_config.CONFIG_PATH = original
            app_config.load(force=True)

    def test_ローカル領域は環境変数で差し替えられる(self) -> None:
        """本番の領域を汚さずに検証するための逃げ道。"""
        self.assertEqual(str(app_config.local_root()),
                         os.environ["CALENDAR_LOCAL_DIR"])

    def test_知らない領域名は断る(self) -> None:
        with self.assertRaises(ValueError):
            app_config.local_dir("どこか")


def stub_health(case: unittest.TestCase, answers: dict[int, dict | None]) -> None:
    """``/api/health`` の応答を差し替える。

    **本物のポートを叩かせない。** ``check_existing`` はロックが当てに
    ならないとき常用ポートを走査するので、差し替えないと
    「開発機でアプリを起動したまま試験した」だけで結果が変わる。
    """
    original = launch_guard.probe_health
    launch_guard.probe_health = lambda port, **kw: answers.get(port)
    case.addCleanup(setattr, launch_guard, "probe_health", original)


class LockTests(unittest.TestCase):
    """多重起動の判定 (基盤仕様書 2.4)。"""

    def setUp(self) -> None:
        launch_guard.remove_lock()
        self.addCleanup(launch_guard.remove_lock)
        stub_health(self, {})                 # どのポートにも誰も居ない

    def test_ロックが無ければ起動してよい(self) -> None:
        result = launch_guard.check_existing()
        self.assertTrue(result.should_start)

    def test_書いて読める(self) -> None:
        info = launch_guard.build_lock_info(9999, token="t")
        launch_guard.write_lock(info)
        read = launch_guard.read_lock()
        self.assertEqual(read.pid, os.getpid())
        self.assertEqual(read.port, 9999)
        self.assertEqual(read.token, "t")

    def test_死んだロックは掃除して続行(self) -> None:
        """PIDが居なければ、残っていたロックを消して起動する。"""
        info = launch_guard.build_lock_info(9999)
        info.pid = 2 ** 30            # 存在しないPID
        launch_guard.write_lock(info)

        result = launch_guard.check_existing()
        self.assertTrue(result.should_start)
        self.assertIsNone(launch_guard.read_lock())

    def test_生きていてもアプリでなければ起動する(self) -> None:
        """PIDは使い回される。**HTTPの応答まで見て**判断する。"""
        info = launch_guard.build_lock_info(9)   # 誰も居ないポート
        launch_guard.write_lock(info)            # pid は自分自身 = 生きている

        result = launch_guard.check_existing()
        self.assertTrue(result.should_start)
        self.assertIn("別物", result.reason)

    def test_壊れたロックは無視する(self) -> None:
        """読めないことを理由に起動を止めない。"""
        path = launch_guard.lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("これはJSONではない", encoding="utf-8")
        self.assertIsNone(launch_guard.read_lock())
        self.assertTrue(launch_guard.check_existing().should_start)

    def test_app_idで自分かどうかを見分ける(self) -> None:
        self.assertTrue(launch_guard.is_our_app({"app_id": app_config.app_id()}))
        self.assertFalse(launch_guard.is_our_app({"app_id": "nlm.other-tool"}))
        self.assertFalse(launch_guard.is_our_app(None))

    def test_使えるポートを選べる(self) -> None:
        port = launch_guard.pick_port()
        self.assertIn(port, app_config.port_candidates())


class BootScreenTests(unittest.TestCase):
    """起動待機画面 (基盤仕様書 2.2)。"""

    def render(self) -> str:
        return boot_screen.render(
            display_name="テストアプリ", version_label="VER1.2.3",
            token="tok", app_id="nlm.test", poll_ms=500,
            home_url="/calendar", log_dir="C:/logs")

    def test_1往復で出せる(self) -> None:
        """外部への要求(CSS・画像・書体)を1つも出さない。"""
        html = self.render()
        self.assertNotIn("<link", html)
        self.assertNotIn("src=\"http", html)

    def test_段が4つ出る(self) -> None:
        html = self.render()
        for key, label in boot_screen.STEPS:
            self.assertIn(f'data-step="{key}"', html)
            self.assertIn(label, html)

    def test_失敗時にログの場所を出す(self) -> None:
        self.assertIn("C:/logs", self.render())

    def test_待たずに入る道がある(self) -> None:
        self.assertIn("/calendar", self.render())

    def test_app_idを照合する(self) -> None:
        """同じポートに居る別のアプリを自分だと誤認しない。"""
        self.assertIn('"nlm.test"', self.render())

    def test_色はtokensから取る(self) -> None:
        """色の出どころを2つにしない。"""
        self.assertIn("--bar-a", boot_screen.tokens_css())


class BootServerTests(unittest.TestCase):
    """待機画面だけを出すサーバ (基盤仕様書 2.2 / 2.3)。"""

    def setUp(self) -> None:
        from boot_server import BootApp, Handover

        self.boot = BootApp(port=8730, token="tok")
        self.handover = Handover(self.boot)

    def call(self, path: str):
        status = {}

        def start_response(code, headers):
            status["code"] = code
            status["headers"] = dict(headers)

        body = b"".join(self.handover({"PATH_INFO": path}, start_response))
        return status, body

    def test_健康確認は本体と同じ形(self) -> None:
        """形が違うと、待機画面と launch_guard の両方が場合分けを持つ。"""
        status, body = self.call("/api/health")
        self.assertEqual(status["code"], "200 OK")
        payload = json.loads(body)
        for key in ("app_id", "version", "ready", "stage", "stage_key", "pid"):
            self.assertIn(key, payload)
        self.assertFalse(payload["ready"])

    def test_業務の要求にはまだと答える(self) -> None:
        """404 にすると「無い」に見える。**まだであることを言う。**"""
        status, body = self.call("/api/calendar")
        self.assertEqual(status["code"], "503 Service Unavailable")
        self.assertEqual(json.loads(body)["error"]["code"], "starting")

    def test_段を進められる(self) -> None:
        self.boot.mark_stage("同期しています", "sync")
        _status, body = self.call("/api/health")
        payload = json.loads(body)
        self.assertEqual(payload["stage"], "同期しています")
        self.assertEqual(payload["stage_key"], "sync")

    def test_本体へ差し替えられる(self) -> None:
        """**待ち受けを開き直さない**ための仕掛け。"""
        def other(_environ, start_response):
            start_response("200 OK", [("Content-Type", "text/plain")])
            return [b"main"]

        self.handover.install(other)
        self.assertTrue(self.handover.handed_over)
        _status, body = self.call("/")
        self.assertEqual(body, b"main")


class StartAppTests(unittest.TestCase):
    """起動の入口 (基盤仕様書 2.5)。"""

    def test_必須パッケージを確かめている(self) -> None:
        import start_app

        names = {pip for _module, pip in start_app.REQUIRED_PACKAGES}
        self.assertEqual(names, {"Flask", "waitress"})

    def test_pip案内はコンソール側のPythonにする(self) -> None:
        """pythonw には画面が無いので、案内どおり実行しても何も出ない。"""
        import start_app

        original = sys.executable
        try:
            sys.executable = r"C:\Python\pythonw.exe"
            self.assertEqual(start_app.console_python(), "python.exe")
        finally:
            sys.executable = original

    def test_TCPが通っていれば中止しない(self) -> None:
        """応答が取れないのは、プロキシなど**こちら側の確認経路**の都合の
        ことが多い。動いているサーバごと終わらせない。"""
        import server
        import start_app

        self.assertTrue(start_app.should_abort(server.ListenCheck(False, tcp_ok=False)))
        self.assertFalse(start_app.should_abort(server.ListenCheck(False, tcp_ok=True)))
        self.assertFalse(start_app.should_abort(server.ListenCheck(True, tcp_ok=True)))


class StartupLockTests(unittest.TestCase):
    """起動の入口をひとつに絞る

    【なぜロックファイルの中身だけでは足りなかったか】
    判定(``app.lock`` を読む)から書き込みまでに1〜2秒の窓がある。
    ``Start.vbs`` は画面が出ないので、反応が無いと思った人がもう一度
    ダブルクリックする ── ちょうどこの窓に入る。実際に**2つ起動した**。
    """

    def setUp(self) -> None:
        self.first = launch_guard.StartupLock()
        self.addCleanup(self.first.release)

    def test_1つ目は取れる(self) -> None:
        self.assertTrue(self.first.acquire())

    def test_2つ目は取れない(self) -> None:
        """ここが多重起動を止める本体。"""
        self.assertTrue(self.first.acquire())
        second = launch_guard.StartupLock()
        self.addCleanup(second.release)
        self.assertFalse(second.acquire())

    def test_離せば次が取れる(self) -> None:
        self.assertTrue(self.first.acquire())
        self.first.release()
        second = launch_guard.StartupLock()
        self.addCleanup(second.release)
        self.assertTrue(second.acquire())

    def test_同じロックを2度取っても平気(self) -> None:
        self.assertTrue(self.first.acquire())
        self.assertTrue(self.first.acquire())

    def test_離していなくても壊れない(self) -> None:
        """離し忘れても、**プロセスが死ねばOSが離す**。"""
        self.first.release()
        self.first.release()

    def test_ロックを作れなくても起動は止めない(self) -> None:
        """守りが1枚減るだけ。ここで止めると誰も起動できなくなる。"""
        lock = launch_guard.StartupLock(
            Path("/存在しない場所/なので/作れない/instance.lock"))
        self.addCleanup(lock.release)
        self.assertTrue(lock.acquire())


class LostLockTests(unittest.TestCase):
    """ロックが当てにならないとき ── **常用ポートに自分が居ないか見る**

    ロックファイルは消える(手で消された・掃除された・あとから落ちた
    プロセスに持っていかれた・ローカル領域が変わった)。そのたびに
    「ロックが無い = 誰も居ない」と決めると、8730 で自分が動いているのに
    8731 にもう1つ立てる。**実際にそうなった。**
    """

    def setUp(self) -> None:
        launch_guard.remove_lock()
        self.addCleanup(launch_guard.remove_lock)
        self.port = app_config.port_candidates()[0]

    def mine(self, **extra) -> dict:
        return {"app_id": app_config.app_id(), "pid": 4321,
                "version": app_config.version(), **extra}

    def test_ロックが無くても自分を見つけたら合流する(self) -> None:
        stub_health(self, {self.port: self.mine()})
        result = launch_guard.check_existing()
        self.assertFalse(result.should_start)
        self.assertIn(str(self.port), result.url)

    def test_2つ目の候補でも見つける(self) -> None:
        second = app_config.port_candidates()[1]
        stub_health(self, {second: self.mine()})
        self.assertFalse(launch_guard.check_existing().should_start)

    def test_別のアプリなら起動してよい(self) -> None:
        """たまたま同じポートを使っている別のツールを自分と誤認しない。"""
        stub_health(self, {self.port: {"app_id": "nlm.other-tool"}})
        self.assertTrue(launch_guard.check_existing().should_start)

    def test_死んだロックのあとも探す(self) -> None:
        info = launch_guard.build_lock_info(9999)
        info.pid = 2 ** 30
        launch_guard.write_lock(info)
        stub_health(self, {self.port: self.mine()})
        self.assertFalse(launch_guard.check_existing().should_start)

    def test_誰も居なければ起動する(self) -> None:
        stub_health(self, {})
        self.assertTrue(launch_guard.check_existing().should_start)


class LockOwnerTests(unittest.TestCase):
    """**他人のロックを消さない**

    無条件に消していたので、あとから起動して落ちたプロセスが、
    生きているほうのロックを消して去った。残るのは「動いているのに
    ロックが無い」状態で、次の起動が素通りする。
    """

    def setUp(self) -> None:
        launch_guard.remove_lock()
        self.addCleanup(launch_guard.remove_lock)

    def test_自分のものは消す(self) -> None:
        launch_guard.write_lock(launch_guard.build_lock_info(9999))
        launch_guard.remove_lock(only_mine=True)
        self.assertIsNone(launch_guard.read_lock())

    def test_他人のものは消さない(self) -> None:
        info = launch_guard.build_lock_info(9999)
        info.pid = os.getpid() + 1
        launch_guard.write_lock(info)

        launch_guard.remove_lock(only_mine=True)
        self.assertIsNotNone(launch_guard.read_lock())

    def test_掃除のときは消す(self) -> None:
        """死んだロックの掃除は、相手が誰でも消してよい。"""
        info = launch_guard.build_lock_info(9999)
        info.pid = os.getpid() + 1
        launch_guard.write_lock(info)

        launch_guard.remove_lock()
        self.assertIsNone(launch_guard.read_lock())


class LockKeeperTests(unittest.TestCase):
    """**消えたロックは持ち主が書き直す**

    ロックが失われると、外から分かるのは「ポートで応答している」ことまでで
    **起動トークンが失われる**。すると ``stop.bat`` も版の入れ替えも
    「止められない」状態になり、新しい版を入れた日にいつまでも古い版が
    開く ── 実際に 403 で断られて合流した。
    """

    def setUp(self) -> None:
        launch_guard.remove_lock()
        self.addCleanup(launch_guard.remove_lock)
        self.stop = threading.Event()
        self.addCleanup(self.stop.set)

    def keeper(self, info) -> None:
        launch_guard.keep_lock(info, self.stop, interval=0.02)

    def wait_for(self, check, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if check():
                return True
            time.sleep(0.02)
        return False

    def test_消えたら書き直す(self) -> None:
        info = launch_guard.build_lock_info(9999, token="ひみつ")
        launch_guard.write_lock(info)
        self.keeper(info)

        launch_guard.lock_path().unlink()
        self.assertTrue(self.wait_for(lambda: launch_guard.read_lock() is not None))

    def test_トークンごと戻る(self) -> None:
        """トークンが戻らないと、止められないままになる。"""
        info = launch_guard.build_lock_info(9999, token="ひみつ")
        launch_guard.write_lock(info)
        self.keeper(info)

        launch_guard.lock_path().unlink()
        self.assertTrue(self.wait_for(
            lambda: (launch_guard.read_lock() or info).token == "ひみつ"
            and launch_guard.lock_path().exists()))

    def test_生きている他人のものは奪わない(self) -> None:
        """2つ動いてしまったときに、取り合いを始めない。"""
        mine = launch_guard.build_lock_info(9999, token="ひみつ")
        other = launch_guard.build_lock_info(8888, token="よその")
        other.pid = os.getpid()       # 生きているプロセスとして扱わせる
        launch_guard.write_lock(other)
        self.keeper(mine)

        time.sleep(0.2)
        self.assertEqual(launch_guard.read_lock().port, 8888)

    def test_止めたら書かなくなる(self) -> None:
        info = launch_guard.build_lock_info(9999)
        launch_guard.write_lock(info)
        self.keeper(info)
        self.stop.set()
        time.sleep(0.1)

        launch_guard.lock_path().unlink()
        time.sleep(0.2)
        self.assertIsNone(launch_guard.read_lock())


class ListenFailureTests(unittest.TestCase):
    """**待ち受けに失敗したら、そう言う**

    ポートの取り合いに負けたプロセスが「起動しました」と言って終わり、
    生きているほうのロックを消して去った。別スレッドで開始するので、
    例外は呼び出し側に届かない ── 失敗は値で持ち帰る。
    """

    def taken_port(self) -> int:
        """**本当に塞がっているポート**を用意する。

        「開けないはずのポート」を番号で決め打ちすると、権限しだいで
        開けてしまい、試験が待ち受けたまま返らない(実際に固まった)。
        """
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(sock.close)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return sock.getsockname()[1]

    def test_失敗は値で持ち帰る(self) -> None:
        import server

        srv = server.AppServer(self.taken_port())
        self.assertEqual(srv.listen_error, "")
        srv.serve_forever()
        self.assertTrue(srv.listen_error)

    def test_失敗したら待ち受け終了として扱う(self) -> None:
        """待っている側を、戻らないまま放置しない。"""
        import server

        srv = server.AppServer(self.taken_port())
        srv.serve_forever()
        self.assertTrue(srv.wait_stopped(timeout=1))


if __name__ == "__main__":
    unittest.main()
