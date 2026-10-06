"""画面が居なくなったら終わる (`nippou/idle_exit.py`)

【なぜ機械で見るのか】
このアプリに窓は無く、見えているのはブラウザのタブだけ。**タブを閉じたら
終わったつもりになる**が、Python は動いたままで、次の起動が「すでに
起動しています」と答えて古い版の画面を開く ── 入れ替えたはずの新しい版が
いつまでも動かない。

だから終わらせる必要がある。ただし**間違って落とすほうが害が大きい**:
入力の途中で消えたら、その直のぶんを打ち直すことになる。
ここで見るのは、落とす条件と、**落とさない3つの条件**。
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou import idle_exit, running


class WatchTestCase(unittest.TestCase):
    """見張りは短い間隔で作る。**本物の90秒は待たない。**"""

    idle_sec = 0.30
    grace_sec = 0.12

    def setUp(self) -> None:
        idle_exit.reset()
        running.reset()
        self.stopped: list[int] = []
        self.watch = idle_exit.IdleWatch(
            lambda: self.stopped.append(1), running.busy,
            idle_sec=self.idle_sec, grace_sec=self.grace_sec, tick_sec=0.02)
        self.addCleanup(idle_exit.reset)
        self.addCleanup(running.reset)
        self.addCleanup(self.watch.cancel)


class ClosedTests(WatchTestCase):
    """閉じた合図なら猶予のあとで終わる。"""

    def test_閉じた合図から猶予を過ぎたら終わる(self) -> None:
        self.watch.beat()
        self.watch.leaving()
        self.assertIsNone(self.watch.overdue(), "猶予の内に落としています")
        time.sleep(self.grace_sec * 1.5)
        self.assertEqual(self.watch.overdue(), "画面が閉じられました")

    def test_猶予の内に心拍が戻れば取り消す(self) -> None:
        """**再読込・画面遷移でも閉じた合図は飛ぶ。**

        画面を作り直すたびに落ちていたら、まともに使えない。
        """
        self.watch.beat()
        self.watch.leaving()
        time.sleep(self.grace_sec * 0.4)
        self.watch.beat()                       # 戻ってきた
        time.sleep(self.grace_sec * 1.5)
        self.assertIsNone(self.watch.overdue(), "戻ってきたのに落としています")

    def test_見張りが実際に止める(self) -> None:
        self.watch.start()
        self.watch.beat()
        self.watch.leaving()
        deadline = time.monotonic() + 2.0
        while not self.stopped and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.stopped, [1])


class SilenceTests(WatchTestCase):
    """無通信ならもっと長く待ってから終わる。"""

    def test_心拍が途切れたら終わる(self) -> None:
        self.watch.beat()
        time.sleep(self.idle_sec * 1.3)
        self.assertIn("心拍がありません", self.watch.overdue())

    def test_心拍が続いているうちは終わらない(self) -> None:
        self.watch.beat()
        for _ in range(4):
            time.sleep(self.idle_sec * 0.4)
            self.watch.beat()
        self.assertIsNone(self.watch.overdue())

    def test_閉じた合図のほうが早い(self) -> None:
        # 閉じたと分かっているなら、無通信の90秒を待つ理由が無い
        self.assertLess(idle_exit.GRACE_SEC, idle_exit.IDLE_SEC)

    def test_心拍の間隔は無通信の見切りより十分短い(self) -> None:
        """**画面が送るより先に見切らない。** 数回落としても持ちこたえる。"""
        self.assertLessEqual(idle_exit.HEARTBEAT_MS / 1000 * 3, idle_exit.IDLE_SEC)


class NeverConnectedTests(WatchTestCase):
    """**1度も繋がっていなければ落とさない。**

    `--no-browser` で立てておく使い方(検証・並行運用)を巻き添えにしない。
    """

    def test_心拍が1度も無ければ終わらない(self) -> None:
        time.sleep(self.idle_sec * 1.5)
        self.assertIsNone(self.watch.overdue())
        self.assertFalse(self.watch.connected)

    def test_未接続のまま閉じた合図が来ても終わらない(self) -> None:
        self.watch.leaving()
        time.sleep(self.grace_sec * 2)
        self.assertIsNone(self.watch.overdue())

    def test_見張りを回しても止めない(self) -> None:
        self.watch.start()
        time.sleep(self.idle_sec * 2)
        self.assertEqual(self.stopped, [])


class BusyTests(WatchTestCase):
    """**書き戻しの途中では落とさない。**

    途中で終わると、ヘッダだけ送れて明細が送れていない状態が
    **共有のAccessに**残る。手元のSQLiteと違って、そこは他のラインも読む。
    """

    def test_処理中は止めない(self) -> None:
        self.watch.start()
        self.watch.beat()
        with running.running("Accessへの反映"):
            self.watch.leaving()
            time.sleep(self.grace_sec * 3)
            self.assertEqual(self.stopped, [], "書き戻しの途中で落としています")
            # 終わってよい状態ではある ── 待っているだけ
            self.assertEqual(self.watch.overdue(), "画面が閉じられました")

    def test_終わればその後に止める(self) -> None:
        self.watch.start()
        self.watch.beat()
        with running.running("Accessへの反映"):
            self.watch.leaving()
            time.sleep(self.grace_sec * 2)
        deadline = time.monotonic() + 2.0
        while not self.stopped and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.stopped, [1])


class RunningRegistryTests(unittest.TestCase):
    """「いま止めてよいか」の判断は1か所(`nippou/running.py`)。"""

    def setUp(self) -> None:
        running.reset()
        self.addCleanup(running.reset)

    def test_中に入ると忙しい(self) -> None:
        self.assertFalse(running.busy())
        with running.running("反映"):
            self.assertTrue(running.busy())
            self.assertEqual(running.labels(), ["反映"])
        self.assertFalse(running.busy())

    def test_例外が出ても必ず下ろす(self) -> None:
        """**下りないと、そのプロセスは二度と自動で終われない。**"""
        with self.assertRaises(RuntimeError):
            with running.running("反映"):
                raise RuntimeError("途中で落ちた")
        self.assertFalse(running.busy())

    def test_重なっても先に終わったほうで空かない(self) -> None:
        # waitress はスレッドプールで動くので、押し間違い・二重送信で重なる
        with running.running("反映"):
            with running.running("反映"):
                pass
            self.assertTrue(running.busy(), "先に終わったほうで空いています")
        self.assertFalse(running.busy())


class AliveEndpointTests(unittest.TestCase):
    """`POST /api/alive` ── 画面からの心拍。

    Flask が要るのでここだけ別扱いにする(`_web.py` の作法に合わせる)。
    """

    def setUp(self) -> None:
        from tests._web import HAS_FLASK, SKIP_REASON

        if not HAS_FLASK:                            # pragma: no cover
            self.skipTest(SKIP_REASON)
        idle_exit.reset()
        running.reset()
        self.addCleanup(idle_exit.reset)
        self.addCleanup(running.reset)

        import app as app_module

        # **モジュールを引き直す。** Web版のテストが `nippou.*` を
        # 読み込み直すので(`tests/_web.py`)、ファイルの先頭で掴んだ
        # ものは古いことがある。古い見張りに向かって心拍を送っても、
        # `app` が触るのは新しいほうで、いつまでも届かない
        import importlib

        self.idle_exit = importlib.import_module("nippou.idle_exit")
        self.running = importlib.import_module("nippou.running")
        self.idle_exit.reset()
        self.running.reset()
        self.addCleanup(self.idle_exit.reset)
        self.addCleanup(self.running.reset)

        # **一時フォルダの外へ書かない。** 要求ごとの記録(出来事)はログの
        # 置き場所へ書くので、本物の `logs` を指したままにしない
        import os
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        saved = {k: os.environ.get(k) for k in ("NIPPOU_APP_DIR", "NIPPOU_LOCAL_DIR")}
        os.environ["NIPPOU_APP_DIR"] = str(Path(tmp.name) / "app")
        os.environ["NIPPOU_LOCAL_DIR"] = str(Path(tmp.name) / "local")

        def restore_env() -> None:
            event_log = sys.modules.get("nippou.services.event_log")
            if event_log is not None:
                event_log.flush(5.0)
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        # 片付けは逆順に走る。**書き終えてから**環境を戻し、フォルダを消す
        self.addCleanup(restore_env)

        self.app = app_module.create_app(token="test-token", port=8733)
        self.app.config["READY"] = True
        self.client = self.app.test_client()
        self.stopped: list[int] = []
        self.watch = self.idle_exit.install(
            lambda: self.stopped.append(1), self.running.busy,
            idle_sec=0.3, grace_sec=0.1, tick_sec=0.02)

    def _post(self, payload=None, **headers):
        head = {"Host": "127.0.0.1"}
        head.update(headers)
        return self.client.post("/api/alive", json=payload or {}, headers=head)

    def test_トークン無しで通る(self) -> None:
        """**`sendBeacon` はヘッダを一切付けられない。**

        トークンを要求すると、閉じたことが永久に届かない。加えて、
        トークンが切れた画面が黙って死んだ扱いになる ── タブは開いた
        ままなのに、その下でプロセスが落ちる。
        """
        res = self._post()
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["watching"])

    def test_心拍が届く(self) -> None:
        self._post()
        self.assertTrue(self.watch.connected)

    def test_閉じた合図が届く(self) -> None:
        self._post()
        self._post({"leaving": True})
        time.sleep(0.2)
        self.assertEqual(self.watch.overdue(), "画面が閉じられました")

    def test_見張りが無くても答える(self) -> None:
        """画面側に「送っても無駄」と思わせない。"""
        self.idle_exit.reset()
        body = self._post().get_json()
        self.assertTrue(body["ok"])
        self.assertFalse(body["watching"])

    def test_別オリジンからは403(self) -> None:
        # トークンは免除でも、同一オリジンの確認は効かせる
        res = self._post(**{"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(res.status_code, 403)

    def test_Hostが違えば400(self) -> None:
        res = self.client.post("/api/alive", json={},
                               headers={"Host": "evil.example.com"})
        self.assertEqual(res.status_code, 400)

    def test_healthに走っている処理が出る(self) -> None:
        with self.running.running("Accessへの反映"):
            body = self.client.get("/api/health",
                                   headers={"Host": "127.0.0.1"}).get_json()
        self.assertEqual(body["job"], {"label": "Accessへの反映"})

    def test_処理中の停止要求は409(self) -> None:
        """**中断して終了するか**を利用者に選ばせる。"""
        head = {"Host": "127.0.0.1", "X-Tool-Token": "test-token"}
        with self.running.running("Accessへの反映"):
            res = self.client.post("/api/shutdown", json={}, headers=head)
            self.assertEqual(res.status_code, 409)
            self.assertEqual(res.get_json()["running"], ["Accessへの反映"])

    def test_心拍の間隔を画面へ渡している(self) -> None:
        """**出どころは `idle_exit` ただ1つ。** 画面が別に持つと食い違う。"""
        head = {"Host": "127.0.0.1", "X-Tool-Token": "test-token"}
        body = self.client.get("/", headers=head).get_data(as_text=True)
        self.assertIn(f"alivePollMs: {self.idle_exit.HEARTBEAT_MS}", body)


class SingletonTests(unittest.TestCase):
    def setUp(self) -> None:
        idle_exit.reset()
        self.addCleanup(idle_exit.reset)

    def test_2度立てても1つ(self) -> None:
        first = idle_exit.install(lambda: None, lambda: False, tick_sec=9)
        second = idle_exit.install(lambda: None, lambda: False, tick_sec=9)
        self.assertIs(first, second)

    def test_立てていなければNone(self) -> None:
        self.assertIsNone(idle_exit.get())


if __name__ == "__main__":
    unittest.main()
