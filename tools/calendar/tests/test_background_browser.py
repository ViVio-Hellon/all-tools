"""裏に回ったタブの合図を**本物のブラウザ**で ── 画面(``health.js``)が何を送るか (段階1)

前は ``health.js`` の字(``addEventListener("freeze"``・``beacon: true`` など)を探していた。
ここでは画面を開き、見え方の変化・凍結・スリープ明けを起こして、**送られた合図**を見る。
時計はブラウザの時計を止めて進める(心拍・スリープ明けの見張りを待たずに確かめる)。

- 裏に回る瞬間・凍らされる直前は ``sendBeacon`` で「隠れます」を送る(fetch は届かないことがある)
- 表に戻る(visibilitychange・resume・pageshow・online)と、すぐ心拍を送り接続を確かめ直す
- 表のまま蓋を閉じた(スリープ明け)は時計の飛びで気づく。ふつうに時間が経っただけでは何もしない
- 心拍の間隔は**サーバから受け取る**(見張る側と送る側で別々に持たない)
- 閉じた合図(pagehide)のあとは何も送らない
- 心拍には見え方(state)と画面の名前を添える
"""
from __future__ import annotations

import unittest
from unittest import mock

from calendar_app import idle_exit

from tests._page_browser import PageBrowserCase

#: 見え方を試験が決める(本物のタブを裏に回すことはできないので、読み口を差し替える)
_VISIBILITY = """
Object.defineProperty(document, 'visibilityState', { configurable: true,
    get: () => window.__vis || 'visible' });
Object.defineProperty(document, 'hidden', { configurable: true,
    get: () => (window.__vis || 'visible') === 'hidden' });
"""

#: ブラウザの時計の起点(止めたまま開く)
_START = 1_800_000_000


class BackgroundSignalTests(PageBrowserCase):
    def alive(self) -> list[dict]:
        return self.posts_to("/api/alive")

    def health_checks(self) -> int:
        return sum(1 for m, p, _ in self.sent if m == "GET" and p.startswith("/api/health"))

    def open_paused(self):
        """時計を止めたまま開く。心拍・見張りは `page.clock` で進めたぶんだけ動く。"""
        ctx = self.new_context()
        ctx.add_init_script(_VISIBILITY)
        page = self.open("/calendar", ctx, wait_for="",
                         prepare=lambda p: (p.clock.install(time=_START), p.clock.pause_at(_START + 1)))
        page.wait_for_selector(".day[data-date]")
        self.wait_until(page, lambda: len(self.alive()) == 1, "開いた直後の心拍")
        return page

    @staticmethod
    def show(page, state: str) -> None:
        page.evaluate("(s) => { window.__vis = s; document.dispatchEvent(new Event('visibilitychange')); }",
                      state)

    def expect_resume(self, page, fire: str, why: str) -> None:
        """表に戻った合図を起こし、心拍(見える)と接続の確かめが1本ずつ増えるのを待つ。"""
        beats, checks = len(self.alive()), self.health_checks()
        page.clock.run_for(1100)                 # 同じ瞬間の重なりは1回にまとめるので、間を空ける
        page.evaluate(fire)
        self.wait_until(page, lambda: len(self.alive()) == beats + 1, f"{why}: 心拍")
        self.wait_until(page, lambda: self.health_checks() >= checks + 1, f"{why}: 接続の確かめ")
        self.assertEqual(self.alive()[-1]["state"], "visible", why)

    def test_裏に回る_凍る_表に戻る_閉じたの合図を見え方つきで送り_閉じたあとは送らない(self) -> None:
        page = self.open_paused()
        first = self.alive()[0]
        self.assertEqual(first["state"], "visible")
        self.assertTrue(first["screen_id"])
        screen_id = first["screen_id"]

        # 裏に回る瞬間・凍らされる直前は sendBeacon(fetch では送らない)
        self.show(page, "hidden")
        page.evaluate("document.dispatchEvent(new Event('freeze'))")
        beacons = self.beacons(page)
        self.assertEqual([b["body"] for b in beacons], [
            {"leaving": False, "state": "hidden", "screen_id": screen_id},
            {"leaving": False, "state": "frozen", "screen_id": screen_id}])
        self.assertTrue(all("/api/alive" in b["url"] for b in beacons))
        self.assertEqual(len(self.alive()), 1, "隠れる合図を fetch で送った")

        # 表に戻る(見え方・凍結からの戻り・ネットワークの戻り・戻る/進むのキャッシュ)
        page.evaluate("window.__vis = 'visible'")
        self.expect_resume(page, "document.dispatchEvent(new Event('visibilitychange'))", "visibilitychange")
        self.expect_resume(page, "document.dispatchEvent(new Event('resume'))", "resume")
        self.expect_resume(page, "window.dispatchEvent(new Event('online'))", "online")
        claims = len(self.posts_to("/api/screen/claim"))
        self.expect_resume(page, "window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }))",
                           "pageshow")
        self.assertEqual(len(self.posts_to("/api/screen/claim")), claims + 1, "戻ったら名乗り直す")

        # 閉じた合図。そのあとは見え方が変わっても、心拍の時が来ても送らない
        page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide', { persisted: false }))")
        self.assertEqual(self.beacons(page)[-1]["body"],
                         {"leaving": True, "state": "visible", "screen_id": screen_id})
        sent_beacons, beats = len(self.beacons(page)), len(self.alive())
        self.show(page, "hidden")
        page.clock.run_for(idle_exit.HEARTBEAT_MS + 1000)
        page.wait_for_timeout(300)
        self.assertEqual(len(self.beacons(page)), sent_beacons, "閉じたあとに隠れますを送った")
        self.assertEqual(len(self.alive()), beats, "閉じたあとに心拍を送った")
        self.assertEqual(self.errors, [])

    def test_心拍の間隔はサーバから受け取り_スリープ明けは時計の飛びで気づく(self) -> None:
        with mock.patch.object(idle_exit, "HEARTBEAT_MS", 7000):
            page = self.open_paused()

        # 表のまま蓋を閉じた: どのイベントも来ず、時計だけが飛ぶ
        page.clock.set_system_time(_START + 1 + 70)
        page.clock.run_for(5000)                 # 時計を見る見張りが1周する
        self.wait_until(page, lambda: len(self.alive()) == 2, "スリープ明けの心拍")
        self.assertEqual(self.alive()[-1]["state"], "visible")
        self.wait_until(page, lambda: self.health_checks() >= 2, "スリープ明けの接続の確かめ")

        # 心拍はサーバが決めた 7 秒ごと(画面の既定の 20 秒ではない)
        page.clock.run_for(2000)
        self.wait_until(page, lambda: len(self.alive()) == 3, "7秒目の心拍")

        # ふつうに時間が経っただけ(見張りがまた1周)では、何もしない
        page.clock.run_for(5000)
        page.wait_for_timeout(300)
        self.assertEqual(len(self.alive()), 3)
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
