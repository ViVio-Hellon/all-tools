"""裏に回ったタブ ── 心拍が止まっても終わらない・空けない

【何が起きたか】
ブラウザは見えていないタブのタイマーを間引きます(Chrome は隠れて5分で
1分に1回まで)。Edge の「スリープ中のタブ」や Chrome の省メモリは、
タブごと凍らせるので**心拍が完全に止まります。** 別の道具で、Excel を
見ているあいだに心拍が途切れて、アプリが終わっていたことがありました。

【何を見張るか】
1. 裏に回る合図(``state=hidden``)が来たら、**心拍が途切れても終わらない**
2. 裏のタブを「もう居ない」とみなして**2枚目を黙って入れない**
3. 裏のまま閉じたら、ふつうどおり終わる(閉じた合図は効く)
4. 表に戻れば、ふつうの見張りに戻る
5. **スリープから戻った瞬間に終わらない**(プロセスごと止まっていた)
6. 画面は裏に回る瞬間・表に戻る瞬間・スリープ明けに合図を送る
"""

from __future__ import annotations

import re
import time
import unittest
from pathlib import Path

from . import _web
from calendar_app import idle_exit, screen_lock

_ROOT = Path(__file__).resolve().parent.parent
_JS = (_ROOT / "app" / "static" / "js" / "health.js").read_text(encoding="utf-8")


class _Box:
    """呼ばれ方を数える置き換え。"""

    def __init__(self, **kwargs) -> None:
        self.stopped = 0
        self.retried = 0
        self.watch = idle_exit.IdleWatch(self._stop, lambda: False,
                                         lambda: 0, self._retry, **kwargs)

    def _stop(self) -> None:
        self.stopped += 1

    def _retry(self) -> None:
        self.retried += 1

    def long_ago(self, sec: float = idle_exit.IDLE_SEC + 1) -> None:
        """最後の心拍を過去にずらす = 心拍が途切れた。"""
        self.watch._seen = time.monotonic() - sec


class IdleTests(unittest.TestCase):
    def test_裏に回ったら心拍が途切れても終わらない(self) -> None:
        box = _Box()
        box.watch.beat("A", hidden=True)
        box.long_ago(8 * 3600)                    # 一晩
        self.assertIsNone(box.watch.overdue())
        self.assertTrue(box.watch.in_background())

    def test_表のままなら今までどおり終わる(self) -> None:
        box = _Box()
        box.watch.beat("A")
        box.long_ago()
        self.assertIsNotNone(box.watch.overdue())

    def test_表に戻ればふつうの見張りに戻る(self) -> None:
        box = _Box()
        box.watch.beat("A", hidden=True)
        box.watch.beat("A")                       # 表に戻った心拍
        self.assertFalse(box.watch.in_background())
        box.long_ago()
        self.assertIsNotNone(box.watch.overdue())

    def test_裏のまま閉じたら終わる(self) -> None:
        """閉じた合図は効く。**残すと、閉じたのに終わらない。**"""
        box = _Box(grace_sec=0.0)
        box.watch.beat("A", hidden=True)
        box.watch.leaving("A")
        self.assertFalse(box.watch.in_background())
        self.assertEqual(box.watch.overdue(), "画面が閉じられました")

    def test_閉じたあとに遅れて届く隠れますでは取り消さない(self) -> None:
        """閉じるときにも見え方が変わるので「隠れます」が飛ぶ。合図は非同期で
        順番が入れ替わる。**取り消すと、裏のまま閉じたタブで終わらない**
        (ブラウザで確かめて実際にそうなった)。"""
        box = _Box(grace_sec=0.0)
        box.watch.beat("A", hidden=True)
        box.watch.leaving("A")
        box.watch.beat("A", hidden=True)           # 遅れて届いた
        self.assertFalse(box.watch.in_background())
        self.assertEqual(box.watch.overdue(), "画面が閉じられました")

    def test_閉じた合図のあと裏から戻れば取り消す(self) -> None:
        """戻る/進むのキャッシュから戻った、など。"""
        box = _Box(grace_sec=0.0)
        box.watch.beat("A")
        box.watch.leaving("A")
        box.watch.beat("A")
        self.assertIsNone(box.watch.overdue())


class WakeTests(unittest.TestCase):
    def test_見張りが長く止まっていたらスリープ明けとみなす(self) -> None:
        box = _Box()
        box.watch.beat("A")
        box.watch.check_wake(mono=100.0, wall=1000.0)
        box.long_ago(8 * 3600)                    # 寝ているあいだ心拍なし
        self.assertTrue(box.watch.check_wake(mono=100.0 + 8 * 3600,
                                             wall=1000.0 + 8 * 3600))
        # **数え直すので、目覚めた瞬間には終わらない**
        self.assertIsNone(box.watch.overdue())
        # 寝ていたあいだの分を、次の定期実行を待たずに同期させる
        self.assertEqual(box.retried, 1)

    def test_単調時計が止まるOSでも実時刻で気づく(self) -> None:
        """休止中に単調時計が進むかは OS による(Linux は止まる)。"""
        box = _Box()
        box.watch.beat("A")
        box.watch.check_wake(mono=100.0, wall=1000.0)
        self.assertTrue(box.watch.check_wake(mono=102.0, wall=1000.0 + 3600))

    def test_ふつうの1周では何もしない(self) -> None:
        box = _Box()
        box.watch.check_wake(mono=100.0, wall=1000.0)
        self.assertFalse(box.watch.check_wake(mono=102.0, wall=1002.0))
        self.assertEqual(box.retried, 0)

    def test_目覚めたあと画面が戻らなければやはり終わる(self) -> None:
        """数え直すだけ。**いつまでも生き続けない。**"""
        box = _Box()
        box.watch.beat("A")
        box.watch.woke()
        box.long_ago()
        self.assertIsNotNone(box.watch.overdue())


class ScreenLockTests(unittest.TestCase):
    def test_裏のタブは心拍が途切れても空けない(self) -> None:
        """空けると、Excel を見ているあいだに2枚目が黙って入れる。"""
        lock = screen_lock.ScreenLock(stale_sec=0.05)
        lock.claim("A")
        lock.beat("A", hidden=True)
        time.sleep(0.15)                      # 余裕を持って(下の試験と同じ理由)
        result = lock.claim("B")
        self.assertFalse(result.ok)
        self.assertIn("裏に回っているタブ", result.message)
        self.assertEqual(lock.active().id, "A")

    def test_裏のタブにも取って代われる(self) -> None:
        """閉じた合図が届かずに残ったときの逃げ道。"""
        lock = screen_lock.ScreenLock(stale_sec=0.05)
        lock.claim("A")
        lock.beat("A", hidden=True)
        self.assertTrue(lock.claim("B", force=True).ok)

    def test_表に戻れば今までどおり(self) -> None:
        lock = screen_lock.ScreenLock(stale_sec=0.05)
        lock.claim("A")
        lock.beat("A", hidden=True)
        lock.beat("A")
        # 余裕を持って待つ。Windows の時計は約 15ms 刻みで、0.06 秒眠っても
        # 0.05 秒に届かないと測られることがある(GitHub Actions で実際に落ちた)
        time.sleep(0.15)
        self.assertTrue(lock.claim("B").ok)


class AliveApiTests(unittest.TestCase):
    """``POST /api/alive`` の ``state``。"""

    def setUp(self) -> None:
        _web.reset_sync()
        screen_lock.reset()
        self.addCleanup(screen_lock.reset)
        idle_exit.reset()
        self.addCleanup(idle_exit.reset)
        self.box = _Box()
        idle_exit._watch = self.box.watch
        _web.bind_db(self)
        self.client = _web.make_client()

    def alive(self, screen_id: str, state: str, leaving: bool = False):
        return self.client.post("/api/alive", json={
            "screen_id": screen_id, "state": state, "leaving": leaving})

    def claim(self, screen_id: str, force: bool = False):
        return self.client.post("/api/screen/claim",
                                json={"screen_id": screen_id, "force": force},
                                headers=_web.auth())

    def test_隠れた合図で終わらなくなる(self) -> None:
        self.claim("A")
        self.alive("A", "hidden")
        self.assertTrue(self.box.watch.in_background())
        self.box.long_ago(3600)
        self.assertIsNone(self.box.watch.overdue())

    def test_凍らされる直前の合図も同じ(self) -> None:
        self.claim("A")
        self.alive("A", "frozen")
        self.assertTrue(self.box.watch.in_background())

    def test_表に戻る心拍で解ける(self) -> None:
        self.claim("A")
        self.alive("A", "hidden")
        self.alive("A", "visible")
        self.assertFalse(self.box.watch.in_background())

    def test_取って代わられた画面の合図は数えない(self) -> None:
        """数えると、使われていない画面のせいでいつまでも終わらない。"""
        self.claim("A")
        self.claim("B", force=True)
        self.alive("A", "hidden")
        self.assertFalse(self.box.watch.in_background())

    def test_別のサイトからの心拍は数えない(self) -> None:
        """同じブラウザの他のタブ(別サイト)からも sendBeacon で届きうる。
        数えると、誰も見ていないのに終わらない・閉じたのに取り消される。"""
        self.claim("A")
        self.box.watch.beat("A")
        self.box.watch.leaving("A")
        res = self.client.post("/api/alive", data='{"screen_id": "A", "state": "hidden"}',
                               content_type="text/plain",
                               headers={"Origin": "https://example.com"})
        self.assertEqual(res.status_code, 403)
        self.assertFalse(self.box.watch.in_background())
        self.assertIsNotNone(self.box.watch._leaving_at, "閉じた扱いが取り消された")

    def test_同じ送り元の心拍は数える(self) -> None:
        self.claim("A")
        res = self.client.post("/api/alive", json={"screen_id": "A", "state": "hidden"},
                               headers={"Origin": "http://localhost"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(self.box.watch.in_background())

    def test_state_を送らない古い画面は今までどおり(self) -> None:
        self.claim("A")
        self.client.post("/api/alive", json={"screen_id": "A"})
        self.assertFalse(self.box.watch.in_background())


class ScreenScriptTests(unittest.TestCase):
    """画面側(``health.js``)が合図を送っているか。"""

    def test_裏に回る瞬間は_sendBeacon_で送る(self) -> None:
        """凍らされる直前は fetch が届かないことがある。"""
        self.assertIn('addEventListener("visibilitychange"', _JS)
        self.assertRegex(_JS, r'send\("hidden", \{ beacon: true \}\)')
        self.assertRegex(_JS, r'addEventListener\("freeze".*beacon: true',)

    def test_表に戻る_スリープ明けを拾う(self) -> None:
        for event in ("resume", "pageshow", "online"):
            self.assertIn(f'addEventListener("{event}"', _JS, event)
        self.assertIn("WAKE_GAP_MS", _JS)
        self.assertIn('resume("wake")', _JS)

    def test_心拍の間隔はサーバから受け取る(self) -> None:
        """見張る側と送る側が別々に持つと、片方だけ変えたときに途切れと誤る。"""
        self.assertIn("window.APP.heartbeatMs", _JS)
        html = (_ROOT / "app" / "templates" / "base.html").read_text(encoding="utf-8")
        self.assertIn("heartbeatMs: {{ heartbeat_ms }}", html)

    def test_閉じた合図のあとは何も送らない(self) -> None:
        self.assertIn("if (closing && !leaving) return;", _JS)
        self.assertRegex(_JS, r"leaving: true, beacon: true \}\);\s*closing = true;")

    def test_心拍には見え方を添える(self) -> None:
        self.assertTrue(re.search(r"JSON\.stringify\(\{ leaving, state, screen_id", _JS))


if __name__ == "__main__":
    unittest.main()
