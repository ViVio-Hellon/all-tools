"""画面を2枚開いて1枚閉じる・同期の問い合わせが1回つまずく を**本物のブラウザ**で

【1. 画面を2枚開いて、1枚を閉じる】(``TwoScreensCloseTests``)
``Start.vbs`` をもう一度押すと、断られた2枚目のタブが開きます。以前は
使っている画面を閉じても、断られた2枚目を閉じても、**9秒後にアプリごと
終わって**いました(どの画面が閉じたかを見ていなかった)。残った画面には
少しして「繋がりません」が出ます。

ここでは本物のサーバ(``server.AppServer`` = waitress。起動と同じ組み立て)を
空いたポートで立て、Chromium で2枚開いて1枚を閉じます。閉じた合図は
ブラウザが本当に送る ``sendBeacon`` で届きます(差し替えない)。猶予は 0 に
して、時間を待たずに「いま終わってよいか」をサーバに訊きます。

【2. 同期の帯の問い合わせが1回失敗しても、止まらない】(``SyncPollRetryTests``)
以前は ``GET /api/sync`` が1回失敗すると次を予約せず、**帯の更新が止まった
まま戻らなかった**(裏に回して戻すまで)。送信待ちの件数や「送れません」の
表示が古いまま残ります。1回断って、そのあとも問い合わせが続き、帯が新しい
中身に変わることを見ます。時計はブラウザの時計を止めて進めます。

待ち合わせは時間ではなく状態を待ちます(Windows の CI は遅い)。
無い環境では飛ばします(CI では飛ばしません: ``ALLTOOLS_REQUIRE_BROWSER=1``)。
"""
from __future__ import annotations

import socket
import time
import unittest
from typing import Callable
from unittest import mock

import server as server_module
from calendar_app import idle_exit, screen_lock
from tests import _web
from tests._browser import chromium_or_skip
from tests._page_browser import PageBrowserCase

CLOSED = "画面が閉じられました"

#: 状態を待つ上限。時間を計るためではなく、止まったまま待ち続けないため
WAIT = 30.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _Recording(idle_exit.IdleWatch):
    """届いた合図を覚える見張り(見張りの糸は回さない。試験が ``overdue`` を訊く)。"""

    def __init__(self) -> None:
        super().__init__(lambda: None, lambda: False, grace_sec=0.0)
        self.calls: list[tuple[str, str]] = []

    def beat(self, screen_id: str = "", **kwargs):
        self.calls.append(("beat", screen_id))
        return super().beat(screen_id, **kwargs)

    def leaving(self, screen_id: str = "", **kwargs):
        self.calls.append(("leaving", screen_id))
        return super().leaving(screen_id, **kwargs)


class TwoScreensCloseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.pw, cls.browser = chromium_or_skip()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.pw.stop()

    def setUp(self) -> None:
        _web.reset_sync()
        self.addCleanup(_web.reset_sync)
        screen_lock.reset()
        self.addCleanup(screen_lock.reset)
        idle_exit.reset()
        self.addCleanup(idle_exit.reset)
        # 定期の心拍は止めておく(閉じた合図の直後に残った画面の心拍がたまたま
        # 着いて、閉じた扱いを取り消す、を起こさない ── 調べたいのは区別のほう)
        patcher = mock.patch.object(idle_exit, "HEARTBEAT_MS", 600_000)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.port = _free_port()
        self.srv = server_module.AppServer(self.port)
        self.thread = server_module.run_in_background(self.srv)
        self.addCleanup(self._stop_server)
        self.srv.build()
        self.watch = _Recording()
        idle_exit._watch = self.watch
        self.srv.mark_ready(True)
        self.base = f"http://127.0.0.1:{self.port}"

        self.ctx = self.browser.new_context(viewport={"width": 1400, "height": 900})
        self.addCleanup(self.ctx.close)
        self.errors: list[str] = []

    def _stop_server(self) -> None:
        self.srv.stop()
        self.thread.join(timeout=10)

    def wait_for(self, ok: Callable[[], bool], what: str) -> None:
        deadline = time.monotonic() + WAIT
        while not ok():
            if time.monotonic() > deadline:
                self.fail(f"{what} になりません")
            time.sleep(0.05)

    def open(self):
        page = self.ctx.new_page()
        page.set_default_timeout(int(WAIT * 1000))
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.on("dialog", lambda d: d.accept())
        page.goto(f"{self.base}/calendar?t={self.srv.token}", wait_until="load")
        page.wait_for_selector(".day[data-date]")
        sid = page.evaluate("sessionStorage.getItem('screenId')")
        self.assertTrue(sid)
        self.wait_for(lambda: ("beat", sid) in self.watch.calls, "開いた画面の心拍")
        return page, sid

    def open_two(self):
        """使っている画面 A と、断られた2枚目 B(``Start.vbs`` をもう一度押したとき)。"""
        a, a_id = self.open()
        # A の名乗りが着いてから B を開く(心拍と名乗りは同時に飛ぶので、心拍だけ
        # 待つと、B が先に名乗って持ち主になることがある)
        self.wait_for(lambda: getattr(screen_lock.get().active(), "id", "") == a_id,
                      "A が使っている画面")
        b, b_id = self.open()
        self.assertNotEqual(a_id, b_id)
        b.wait_for_selector("#screen-block:not([hidden])")
        self.assertEqual(screen_lock.get().active().id, a_id)
        return (a, a_id), (b, b_id)

    def close(self, page, sid: str) -> None:
        """タブを閉じる。閉じた合図(sendBeacon)がサーバに着くまで待つ。

        **閉じるときの処理を走らせて閉じる**(``run_before_unload``)── 人がタブを閉じたときと同じ。
        Playwright の ``close()`` は初めの決まりでは ``pagehide`` などを走らせないので、閉じた合図が
        出たり出なかったりした(Linux では出て、Windows の CI では出なかった)。
        """
        page.close(run_before_unload=True)
        self.wait_for(lambda: ("leaving", sid) in self.watch.calls, "閉じた合図")

    def server_answers(self, page) -> bool:
        return page.evaluate("fetch('/api/health', {cache: 'no-store'}).then(r => r.ok, () => false)")

    def test_使っている画面を閉じても_断られた2枚目が開いていれば終わらない(self) -> None:
        (a, a_id), (b, b_id) = self.open_two()
        self.close(a, a_id)
        self.assertIsNone(self.watch.overdue(), "1枚閉じただけでアプリごと終わる")
        self.assertTrue(self.server_answers(b))
        # 残った B も閉じれば、これまでどおり終わる
        self.close(b, b_id)
        self.assertEqual(self.watch.overdue(), CLOSED)
        self.assertEqual(self.errors, [])

    def test_断られた2枚目を閉じても_使っている画面が開いていれば終わらない(self) -> None:
        (a, a_id), (b, b_id) = self.open_two()
        self.close(b, b_id)
        self.assertIsNone(self.watch.overdue(), "1枚閉じただけでアプリごと終わる")
        self.assertTrue(self.server_answers(a))
        # A は使っている画面のまま(閉じた2枚目に席を空けられていない)
        self.assertEqual(screen_lock.get().active().id, a_id)
        self.close(a, a_id)
        self.assertEqual(self.watch.overdue(), CLOSED)
        self.assertEqual(self.errors, [])

    def test_再読込は閉じたことにならない(self) -> None:
        """再読込でも閉じた合図は飛ぶ。新しいページの心拍で取り消される。"""
        a, a_id = self.open()
        before = len(self.watch.calls)
        a.reload(wait_until="load")
        a.wait_for_selector(".day[data-date]")
        self.wait_for(lambda: ("leaving", a_id) in self.watch.calls[before:], "再読込の閉じた合図")
        self.wait_for(lambda: self.watch.calls[-1] == ("beat", a_id)
                      and self.watch.overdue() is None, "新しいページの心拍")
        self.assertEqual(self.errors, [])


#: ブラウザの時計の起点(止めたまま開く)
_START = 1_800_000_000

#: 帯の問い合わせの間隔(``sync.js`` の SLOW_MS)より長く進める
_POLL_STEP_MS = 16_000


class SyncPollRetryTests(PageBrowserCase):
    def sync_gets(self) -> int:
        return sum(1 for m, p, _ in self.sent if m == "GET" and p.split("?", 1)[0] == "/api/sync")

    def test_同期の問い合わせが1回失敗しても_帯の更新は止まらない(self) -> None:
        ctx = self.new_context()
        page = self.open("/calendar", ctx, wait_for="",
                         prepare=lambda p: (p.clock.install(time=_START), p.clock.pause_at(_START + 1)))
        page.wait_for_selector(".day[data-date]")
        self.wait_until(page, lambda: self.sync_gets() >= 1, "最初の同期の問い合わせ")
        page.wait_for_function("document.getElementById('rb-sync').textContent.trim() !== ''")

        # 1回だけ断る(サーバが重い・起き直す途中など)
        self.refuse["/api/sync"] = 500
        before = self.sync_gets()
        page.clock.run_for(_POLL_STEP_MS)
        self.wait_until(page, lambda: self.sync_gets() >= before + 1, "定期の問い合わせ(断られる)")
        page.wait_for_timeout(200)                # 断りの返事を画面が受け取る

        # 戻った。**次の問い合わせが来て、帯が新しい中身に変わる**
        del self.refuse["/api/sync"]
        status = _web.json_of(self.client.get("/api/sync", headers=_web.auth()))
        status["text"] = "送信待ち 3 件(試験)"
        self.answers["/api/sync"] = (200, status)
        failed_at = self.sync_gets()
        page.clock.run_for(_POLL_STEP_MS)
        self.wait_until(page, lambda: self.sync_gets() >= failed_at + 1,
                        "断られたあとの問い合わせ(帯の更新が止まった)")
        page.wait_for_function(
            "document.getElementById('rb-sync').textContent.includes('送信待ち 3 件(試験)')")

        # そのあとも続く
        again = self.sync_gets()
        page.clock.run_for(_POLL_STEP_MS)
        self.wait_until(page, lambda: self.sync_gets() >= again + 1, "そのあとの問い合わせ")
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
