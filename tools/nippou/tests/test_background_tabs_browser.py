"""裏に回ったタブ・スリープ明けの**画面の側** (本物のブラウザ。段階1)

前は字を探していました(`test_background_tabs` の ScreenSignalTests)。ここでは画面を本当に
裏へ回し・戻し・時計を飛ばして、**サーバへ何が届くか**を見ます:

    - 隠れる瞬間に「裏に回った」を送る(待たずに `sendBeacon` で。凍結の合図でも)
    - 心拍に、いまの見え方・名札・ページ番号を添える。戻る/進むで戻ったらページ番号を取り直す
    - 表に戻る・凍結が解ける・戻る/進む・回線の復帰・スリープ明けで、**待たずに**心拍と生死の
      確かめを送り、打つ権利(`/api/tab/ping`)と直の見張り(`/api/shift/key`)も走る
    - スリープはタイマーの空きで見る。**見えているときだけ**(裏のタブは間引かれて空くのが当たり前)
    - 打つ権利も隠れる瞬間に知らせる。見るだけのタブには、打てるタブが裏に居ると一言出る

裏に回すのはブラウザの合図(`visibilitychange`)をそのとおりに出して行います(見え方は
`document.visibilityState` を差し替える)。時計は Playwright の時計で止めて・飛ばします
(本物の時間は待たない。周期の心拍が混ざらないので「待たずに送った」が見分けられる)。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._entry_browser import ORIGIN, EntryBrowserCase  # noqa: E402

#: 見え方を差し替え、戻ってきた合図(`app:resume`)の理由を `window.__resumes` へ控える
WATCH = """() => {
    window.__vis = 'visible';
    Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => window.__vis});
    Object.defineProperty(document, 'hidden', {configurable: true, get: () => window.__vis === 'hidden'});
    window.__resumes = [];
    window.addEventListener('app:resume', (e) => window.__resumes.push(e.detail && e.detail.why));
}"""


class BackgroundCase(EntryBrowserCase):
    def setUp(self) -> None:
        super().setUp()
        #: ブラウザが出した要求(道, 種類, 本文)。`sendBeacon` は種類が "ping" になる
        self.requests: list[tuple[str, str, dict]] = []

    def open_watched(self, *, viewer: bool = False, pause: bool = True):
        page = self.watch(self.ctx.new_page())
        page.on("request", self._note)
        page.clock.install()                    # 時計は流れたまま。飛ばせるようにするだけ
        page.goto(f"{ORIGIN}/", wait_until="load")
        page.wait_for_selector("#grid-body")
        page.wait_for_timeout(600)
        if viewer:                              # 見るだけのタブは欄が押せない(名前は入れない)
            page.wait_for_function("document.body.classList.contains('is-viewer')", timeout=8000)
        else:
            self.name_worker(page)
        page.evaluate(WATCH)
        # **時計を止める。** 周期の心拍(5秒・20秒)が混ざると「待たずに送った」のか
        # 「次の周期で送った」のかが見分けられない。以後、時計は試験が進めたぶんだけ進む
        if pause:
            self.pause(page)
        return page

    @staticmethod
    def pause(page) -> None:
        """時計を止める(時計は1つの文脈で1つ。タブを何枚開いても止めるのは1回)。"""
        # 少し先で止める。**50ms 先では足りなかった**(重い Windows の CI では読んでから止めるまでに
        # 50ms 過ぎ、「過去へは進められない」で落ちた)。2秒先(この間の周期の心拍は試験が数える前)
        page.clock.pause_at((page.evaluate("Date.now()") + 2000) / 1000)   # Python の側は秒
        page.wait_for_timeout(300)

    def _note(self, request) -> None:
        path = request.url.split(ORIGIN, 1)[-1]
        try:
            body = json.loads(request.post_data or "{}")
        except ValueError:
            body = {}
        self.requests.append((path, request.resource_type, body if isinstance(body, dict) else {}))

    def wait_js(self, page, expr: str, what: str) -> None:
        """画面の中の状態を待つ。**Playwright の待ち(rAF で見直す)は止めた時計では回らない**ので、
        こちらから見に行く。"""
        self.wait_until(page, lambda: page.evaluate(expr), what)

    def since(self, mark: int, prefix: str) -> list[tuple[str, str, dict]]:
        return [r for r in self.requests[mark:] if r[0].startswith(prefix)]

    def wait_sent(self, page, mark: int, prefix: str, ok=lambda r: True, what: str = "") -> tuple:
        self.wait_until(page, lambda: any(ok(r) for r in self.since(mark, prefix)), what or prefix)
        return next(r for r in self.since(mark, prefix) if ok(r))

    @staticmethod
    def set_visibility(page, state: str) -> None:
        page.evaluate("""s => { window.__vis = s;
            document.dispatchEvent(new Event('visibilitychange')); }""", state)

    @staticmethod
    def past_debounce(page) -> None:
        """戻った合図は1秒のうちに重なると1回に束ねる。束ねられない所まで時計を進める
        (そのあいだに来た周期の要求は、出し終えるのを待ってから数え始める)。"""
        page.clock.fast_forward(1200)
        page.wait_for_timeout(400)


class SignalTests(BackgroundCase):
    def test_隠れる瞬間にsendBeaconで裏に回ったと知らせる_凍結でも(self) -> None:
        page = self.open_watched()
        mark = len(self.requests)
        self.set_visibility(page, "hidden")
        alive = self.wait_sent(page, mark, "/api/alive", lambda r: r[2].get("state") == "hidden",
                               "裏に回った合図(/api/alive)")
        self.assertEqual(alive[1], "ping", "待たずに送る(sendBeacon)になっていません")
        self.assertEqual(alive[2].get("why"), "hidden")
        hide = self.wait_sent(page, mark, "/api/tab/hide", what="打つ権利の裏に回った合図")
        self.assertEqual(hide[1], "ping")
        # 凍結(Page Lifecycle)も拾う
        mark = len(self.requests)
        page.evaluate("document.dispatchEvent(new Event('freeze'))")
        self.wait_sent(page, mark, "/api/alive", lambda r: r[2].get("why") == "freeze",
                       "凍結の合図")
        # 裏のまま打つ権利の心拍が出たら、裏だと言い添える
        mark = len(self.requests)
        page.evaluate("window.dispatchEvent(new CustomEvent('app:resume', {detail: {why: 'test'}}))")
        ping = self.wait_sent(page, mark, "/api/tab/ping", what="打つ権利の心拍")
        self.assertIs(ping[2].get("hidden"), True)
        self.assertEqual(self.errors, [])

    def test_心拍に見え方と名札とページ番号_戻る進むで番号を取り直す(self) -> None:
        page = self.open_watched()
        first = next(r for r in self.requests if r[0].startswith("/api/alive"))
        self.assertEqual(first[2].get("state"), "visible")
        tab, number = first[2].get("tab"), first[2].get("page")
        self.assertTrue(tab and number, first)
        mark = len(self.requests)
        self.set_visibility(page, "hidden")
        hidden = self.wait_sent(page, mark, "/api/alive", lambda r: r[2].get("state") == "hidden")
        self.assertEqual((hidden[2].get("tab"), hidden[2].get("page")), (tab, number))
        # 戻る/進むの控えから戻った(閉じた合図を送ったページ番号は使えない)
        self.set_visibility(page, "visible")
        self.past_debounce(page)
        mark = len(self.requests)
        page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
        again = self.wait_sent(page, mark, "/api/alive", what="戻ったあとの心拍")
        self.assertEqual(again[2].get("tab"), tab)
        self.assertNotIn(again[2].get("page"), (None, number), "ページ番号を取り直していません")
        self.wait_sent(page, mark, "/api/tab/claim", what="戻ったあとに名乗り直す")

    def test_戻ったら待たずに心拍と確かめを送り_権利と直の見張りも走る(self) -> None:
        page = self.open_watched()
        self.set_visibility(page, "hidden")
        page.wait_for_timeout(200)
        for why, fire in (("visible", "s => { window.__vis = 'visible'; document.dispatchEvent(new Event('visibilitychange')); }"),
                          ("resume", "() => document.dispatchEvent(new Event('resume'))"),
                          ("pageshow", "() => window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))"),
                          ("online", "() => window.dispatchEvent(new Event('online'))")):
            with self.subTest(why=why):
                self.past_debounce(page)
                mark = len(self.requests)
                page.evaluate(fire, "visible") if why == "visible" else page.evaluate(fire)
                self.wait_js(page, f"window.__resumes.includes({json.dumps(why)})", f"{why} で戻った合図")
                for prefix in ("/api/alive", "/api/health", "/api/tab/ping", "/api/shift/key"):
                    self.wait_sent(page, mark, prefix, what=f"{why} のあとの {prefix}")
        self.assertEqual(self.errors, [])

    def test_スリープはタイマーの空きで見る_見えているときだけ(self) -> None:
        page = self.open_watched()
        self.set_visibility(page, "hidden")
        page.clock.fast_forward(60_000)            # 裏で1分止まっていた(間引かれた)
        page.wait_for_timeout(300)
        self.assertNotIn("sleep", page.evaluate("window.__resumes"),
                         "裏のタブの空きをスリープとみなしています")
        self.set_visibility(page, "visible")
        self.past_debounce(page)
        mark = len(self.requests)
        page.clock.fast_forward(60_000)            # 見えているのに1分止まっていた = スリープ
        self.wait_js(page, "window.__resumes.includes('sleep')", "スリープ明けの合図")
        self.wait_sent(page, mark, "/api/alive", what="スリープ明けの心拍")

    def test_PCの時計が後ろへ飛んでも表に戻れば待たずに送る(self) -> None:
        """戻った合図を1秒に束ねるのを壁の時計(`Date.now()`)で数えていたので、いちど戻った
        あとに PC の時計を合わせ直して1時間戻すと、「前に戻ったのは未来」になり、1時間のあいだ
        表に戻っても心拍・確かめ・`app:resume` を送らなかった(次の周期で取り戻すまで古い状態)。"""
        page = self.open_watched()
        self.set_visibility(page, "hidden")
        page.wait_for_timeout(200)
        self.past_debounce(page)
        self.set_visibility(page, "visible")
        self.wait_js(page, "window.__resumes.filter(w => w === 'visible').length === 1", "1回目に戻った合図")
        # PC の時計を1時間戻す(画面の `Date.now()` だけが戻る。`performance.now()` は進んだまま)
        tick = page.evaluate("performance.now()")
        page.clock.set_system_time((page.evaluate("Date.now()") - 3600_000) / 1000)
        self.assertGreaterEqual(page.evaluate("performance.now()"), tick,
                                "試験の前提: 時計を戻しても performance.now() は戻らない")
        self.set_visibility(page, "hidden")
        page.wait_for_timeout(200)
        self.past_debounce(page)
        mark = len(self.requests)
        self.set_visibility(page, "visible")
        self.wait_js(page, "window.__resumes.filter(w => w === 'visible').length === 2",
                     "時計が後ろへ飛んだあとに戻った合図")
        for prefix in ("/api/alive", "/api/health", "/api/tab/ping"):
            self.wait_sent(page, mark, prefix, what=f"時計が後ろへ飛んだあとの {prefix}")
        self.assertEqual(self.errors, [])


class ViewerNoteTests(BackgroundCase):
    def test_打てるタブが裏に回ると見るだけのタブに一言(self) -> None:
        editor = self.open_watched(pause=False)
        viewer = self.open_watched(viewer=True, pause=False)
        self.pause(viewer)
        note = viewer.locator("#tab-lock .tab-lock__note")
        self.assertTrue(note.is_hidden())
        mark = len(self.requests)
        self.set_visibility(editor, "hidden")
        self.wait_sent(editor, mark, "/api/tab/hide")
        viewer.evaluate("window.dispatchEvent(new CustomEvent('app:resume', {detail: {why: 'test'}}))")
        self.wait_js(viewer, "!document.querySelector('#tab-lock .tab-lock__note').hidden", "見るだけのタブの一言")
        self.assertIn("裏に回っています", note.text_content())


if __name__ == "__main__":
    unittest.main()
