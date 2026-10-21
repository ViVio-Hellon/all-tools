"""タブを複数開いたときの送信・受信 (v3.91.0)

    タブを複数開いた状態で送信受信周りをもう一度テストしてください

本物のブラウザで、関係ないタブ3枚(1枚は重い処理を回し続ける)と
アプリのタブを6枚まで開いて通しました(54 / 54)。見つかった不具合は4つで、
どれも**あとから書いた行が黙って消える**か、**打てるタブが2枚になる**形です。

1. **引き継いだタブの表が古い。** 打てるタブAを閉じると、見るだけだった
   タブBが次の心拍で打てる側になります。Bの表はBを開いた時点の中身で、
   Aがあとから保存した行を知りません ── Bで1行打つと自動保存が古い表で
   上書きし、**Aの最後の行が消えていました。**
2. **複製したタブの名札が同じ。** ブラウザは「タブを複製」や
   `window.open` で開いたタブへ `sessionStorage` を写します。名札も
   同じになり、サーバからは1枚に見えて**2枚とも打てる側**でした。
3. **見るだけのタブから、打つ画面の行き先を動かせた。** 開いている直・
   ページはアプリに1つです。見るだけのタブの「記録を見る」から呼び出し
   たりページを移ったりすると、打てるタブの次の自動保存が**そちらへ
   書かれます。**
4. **グラフの画面まで差し替わる。** 打てるタブがグラフを見ているあいだに
   別のタブが「このタブで入力する」を押すと、グラフの画面が「別のタブで
   開いています」の1枚に差し替わっていました(取り合うのは打つ画面だけ)。

ここでは、判断(`logic/tab_lock.py`)・書き込みの口(`app/__init__.py`)・
画面の配線を見たうえで、**本物のブラウザ**でも1と2と4を確かめます。2枚目の差し替え・名札・断られたら読み直すも
本物のブラウザで確かめます(Playwright が無い環境では飛ばします。CI では落とします)。
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HEADERS, WebTestCase  # noqa: E402
from tests._browser import chromium_or_skip  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _read(*parts: str) -> str:
    return ROOT.joinpath(*parts).read_text(encoding="utf-8")


# ----------------------------------------------------------------------
# 判断
# ----------------------------------------------------------------------
class StaleDeskTests(unittest.TestCase):
    """**画面を描いたあとに、ほかのタブが書いているか**(`TabDesk.stale`)。"""

    def setUp(self) -> None:
        from nippou.logic import tab_lock
        self.tab_lock = tab_lock
        self.desk = tab_lock.TabDesk()

    def test_引き継いだタブの表は古い(self) -> None:
        self.desk.claim("A", now=0.0, loaded=0.0)
        self.desk.claim("B", now=1.0, loaded=1.0)        # B は見るだけ
        self.desk.note_write("A", now=2.0)               # A が4行目を保存
        self.desk.release("A", now=3.0)                  # A を閉じる
        verdict = self.desk.ping("B", now=4.0)
        self.assertTrue(verdict.may_edit)                # B が引き継ぐ
        self.assertTrue(verdict.stale)                   # けれど表は古い
        self.assertTrue(self.desk.stale("B", now=4.0))

    def test_描き直せば古くない(self) -> None:
        self.desk.claim("A", now=0.0, loaded=0.0)
        self.desk.claim("B", now=1.0, loaded=1.0)
        self.desk.note_write("A", now=2.0)
        self.desk.release("A", now=3.0)
        verdict = self.desk.claim("B", now=5.0, loaded=4.5)   # 読み直した
        self.assertFalse(verdict.stale)

    def test_自分の書き込みでは古くならない(self) -> None:
        self.desk.claim("A", now=0.0, loaded=0.0)
        self.desk.note_write("A", now=5.0)
        self.assertFalse(self.desk.stale("A", now=6.0))

    def test_描く前の書き込みは古くしない(self) -> None:
        self.desk.claim("A", now=0.0, loaded=0.0)
        self.desk.note_write("A", now=1.0)
        self.desk.claim("B", now=2.0, loaded=2.0)
        self.assertFalse(self.desk.stale("B", now=3.0))

    def test_見るだけの間は古いと言わない(self) -> None:
        """見るだけのタブは打てる側が書くたびに古くなります。**そのたびに
        読み直させない** ── 読み直すのは打てる側になったときだけ。"""
        self.desk.claim("A", now=0.0, loaded=0.0)
        self.desk.claim("B", now=1.0, loaded=1.0)
        self.desk.note_write("A", now=2.0)
        self.assertFalse(self.desk.ping("B", now=3.0).stale)

    def test_名札の無い要求と描いた時刻を知らないタブは見ない(self) -> None:
        self.desk.note_write("A", now=2.0)
        self.assertFalse(self.desk.stale("", now=3.0))
        self.assertFalse(self.desk.stale("X", now=3.0))   # 記録の画面から来た
        self.desk.note_write("", now=4.0)                 # アプリの外から書いた
        self.desk.claim("B", now=0.0, loaded=0.0)
        self.assertFalse(self.desk.stale("B", now=5.0))

    def test_落ちて名乗り直しても描いた時刻は上書きしない(self) -> None:
        """15秒黙って落ちたタブの心拍は「開いた」扱いになります。そこで
        描いた時刻を**いま**にすると、古い表が新しく見えます。"""
        self.desk.claim("A", now=0.0, loaded=0.0)
        self.desk.claim("B", now=1.0, loaded=1.0)
        self.desk.note_write("A", now=2.0)
        self.desk.release("A", now=3.0)
        self.desk.ping("B", now=100.0)                    # B も一度落ちていた
        self.assertTrue(self.desk.stale("B", now=100.0))

    def test_今より先の時刻はいまとみなす(self) -> None:
        """立て直す前のプロセスの時計で描かれた画面。"""
        self.desk.claim("B", now=10.0, loaded=99999.0)
        self.desk.note_write("A", now=11.0)
        self.assertTrue(self.desk.stale("B", now=12.0))

    def test_描いた時刻は溜めない(self) -> None:
        for n in range(self.tab_lock.MAX_STAMPS + 50):
            self.desk.claim(f"T{n}", now=float(n), loaded=float(n))
        self.assertLessEqual(len(self.desk.loaded), self.tab_lock.MAX_STAMPS)


# ----------------------------------------------------------------------
# 書き込みの口
# ----------------------------------------------------------------------
class GuardTests(WebTestCase):
    def as_tab(self, name: str) -> dict:
        return {**HEADERS, "X-Tab": name}

    def call(self, path: str, tab: str, data=None):
        return self.client.post(path, headers=self.as_tab(tab), json=data or {})

    def desk(self):
        from nippou.logic import tab_lock
        return tab_lock.get_desk()

    def now(self) -> float:
        from nippou import awake_clock
        return awake_clock.now()

    def taken_over(self) -> None:
        """A が打ち、B は見るだけ。A が書いてから閉じて、B が引き継ぐ。"""
        self.call("/api/tab/claim", "A", {"loaded": self.now()})
        self.call("/api/tab/claim", "B", {"loaded": self.now()})
        self.desk().note_write("A", now=self.now() + 0.001)
        self.call("/api/tab/release", "A")
        self.call("/api/tab/ping", "B")

    def test_心拍の答えで古いと知らせる(self) -> None:
        self.call("/api/tab/claim", "A", {"loaded": self.now()})
        self.call("/api/tab/claim", "B", {"loaded": self.now()})
        self.desk().note_write("A", now=self.now() + 0.001)
        self.call("/api/tab/release", "A")
        body = self.call("/api/tab/ping", "B").get_json()
        self.assertTrue(body["may_edit"])
        self.assertTrue(body["stale"])

    def test_古い表からの保存は断る(self) -> None:
        self.taken_over()
        res = self.call("/api/entry/save", "B", {"rows": {"1": {"LOT": "N7131T0"}},
                                                 "silent": True})
        self.assertEqual(res.status_code, 409)
        error = res.get_json()["error"]
        self.assertEqual(error["code"], "stale_tab")
        self.assertIn("読み直します", error["message"])

    def test_表を送らない要求は古さを見ない(self) -> None:
        """記録の画面からの呼び出しは表を持っていません。**古いも新しいも
        無い**ので止めない ── 止めると、日報入力を開き直すまで何もできない。"""
        self.taken_over()
        res = self.call("/api/entry/save", "B", {})
        self.assertNotEqual(res.status_code, 409)

    def test_読み直して名乗り直せば通る(self) -> None:
        self.taken_over()
        # 書いたあとに描き直した。**時計が書いた時刻を越えるまで待つ** ── Windows の
        # 時計は 15ms ほどの刻みで進むので、すぐ読むと書いた時刻と並ぶ(先の時刻を
        # 渡しても、アプリは「いま」より先の時刻を「いま」に直す)
        written = self.desk().last_write[1]
        while self.now() <= written:
            time.sleep(0.005)
        self.call("/api/tab/claim", "B", {"loaded": self.now()})
        res = self.call("/api/entry/save", "B", {"rows": {"1": {"LOT": ""}},
                                                 "silent": True})
        self.assertNotEqual(res.status_code, 409)

    def test_通った書き込みを覚える(self) -> None:
        self.call("/api/tab/claim", "A", {"loaded": self.now()})
        self.call("/api/entry/save", "A", {"rows": {"1": {"LOT": ""}}, "silent": True})
        self.assertEqual(self.desk().last_write[0], "A")

    def test_断られた書き込みは覚えない(self) -> None:
        self.call("/api/tab/claim", "A", {"loaded": self.now()})
        self.call("/api/tab/claim", "B", {"loaded": self.now()})
        self.call("/api/entry/save", "B", {"rows": {"1": {"LOT": ""}}})   # 409
        self.assertNotEqual(self.desk().last_write[0], "B")

    def test_描いた時刻を画面が持つ(self) -> None:
        page = self.get("/").get_data(as_text=True)
        self.assertRegex(page, r'data-tab-loaded="\d+\.\d{3}"')

    def test_見るだけのタブから打つ画面の行き先を動かせない(self) -> None:
        self.call("/api/tab/claim", "A")
        self.call("/api/tab/claim", "B")
        for path, data in (("/api/settings/page", {"page": 2}),
                           ("/api/settings/recall", {"report_date": "2026年9月1日",
                                                     "line": "L-1", "shift": "1直",
                                                     "page": 1}),
                           ("/api/settings/back", {}),
                           ("/api/settings/restore-shift", {}),
                           ("/api/settings/import/apply", {})):
            with self.subTest(path=path):
                res = self.call(path, "B", data)
                self.assertEqual(res.status_code, 409)
                self.assertEqual(res.get_json()["error"]["code"], "other_tab")

    def test_打てるタブからは行き先を動かせる(self) -> None:
        self.call("/api/tab/claim", "A")
        self.call("/api/tab/claim", "B")
        res = self.call("/api/settings/page", "A", {"page": 1})
        self.assertNotEqual(res.status_code, 409)

    def test_見るだけのタブでも共有へ保存は押せる(self) -> None:
        """送るのは**保存済みの中身**で、表は触りません。2枚から同時に
        押しても、2つめは「送るものはありませんでした」になるだけです
        (ブラウザで確かめました)。"""
        from app import _tab_guarded
        from flask import Request
        from werkzeug.test import EnvironBuilder
        req = Request(EnvironBuilder(path="/api/settings/push", method="POST").get_environ())
        self.assertFalse(_tab_guarded(req))


# ----------------------------------------------------------------------
# 画面の配線
# ----------------------------------------------------------------------
class WiringTests(unittest.TestCase):
    # 複製したタブの名札・引き継いで古ければ読み直す・断られたら読み直す・差し替えは日報入力の上だけ
    # は、字を探すのをやめて下の `BrowserTests` で本当に動かして確かめます(段階1)
    def test_名札が決まってから名乗る(self) -> None:
        js = _read("app", "static", "js", "tab_lock.js")
        beat = js[js.index("async function beat(path)"):]
        beat = beat[:beat.index("\n}")]
        self.assertLess(beat.index("await tabReady"), beat.index("background.post"))
        self.assertIn("body.loaded = loadedAt()", beat)


# ----------------------------------------------------------------------
# 本物のブラウザ
# ----------------------------------------------------------------------


class BrowserTests(WebTestCase):
    """**本物のブラウザで、同じブラウザの中にタブを何枚も開く。**

    要求は Flask のテストクライアントへ回します(ポートを開かない)。
    タブの名札(`X-Tab`)はそのまま渡します。心拍は5秒ごとなので、
    役が変わるのを待つ試験は1つ10秒ほどかかります。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.pw, cls.browser = chromium_or_skip()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.pw.stop()

    def setUp(self) -> None:
        super().setUp()
        # **サーバの時計を直の中ほどに止める。** 本物の時計のままだと、直の残り5分
        # (06:55・14:55・22:45 から)に流したとき自動確定が走って確認画面へ移り、
        # 2枚目のタブが日報入力にいないので「見るだけ」にならず落ちていた(アプリは仕様どおり)
        from datetime import date, datetime, time as clock_time
        from tests.test_scenario_shift_flow import _Now, install_clock

        _Now.value = datetime.combine(date.today(), clock_time(10, 0))
        install_clock(self)
        self.ctx = self.browser.new_context(viewport={"width": 1400, "height": 900})
        self.addCleanup(self.ctx.close)
        self.ctx.route("http://app.test/**", self._forward)
        self.errors: list[str] = []
        #: 通した要求(方法, 道, 名札)。名札の付け忘れ・心拍が続いているかを見る
        self.seen: list[tuple[str, str, str]] = []
        #: 返した答え(道, HTTP の番号)
        self.replies: list[tuple[str, int]] = []

    def _forward(self, route) -> None:
        req = route.request
        path = req.url.split("http://app.test", 1)[1]
        self.seen.append((req.method, path, req.headers.get("x-tab") or ""))
        headers = dict(HEADERS)
        if req.headers.get("x-tab"):
            headers["X-Tab"] = req.headers["x-tab"]
        res = self.client.open(path, method=req.method, headers=headers,
                               data=req.post_data_buffer,
                               content_type=req.headers.get("content-type"))
        self.replies.append((path, res.status_code))
        try:
            route.fulfill(status=res.status_code, body=res.get_data(),
                          headers={"content-type": res.headers.get("Content-Type", "text/plain")})
        finally:
            res.close()

    def wait_until(self, page, ok, what: str, timeout: float = 8.0) -> None:
        """Python 側の状態(`seen`)を待つ。待つあいだも要求は捌かれる。"""
        deadline = time.monotonic() + timeout
        while not ok():
            if time.monotonic() > deadline:
                self.fail(f"{what} が来ません")
            page.wait_for_timeout(50)

    def open_tab(self):
        page = self.ctx.new_page()
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.on("dialog", lambda d: d.accept())
        page.goto("http://app.test/", wait_until="load")
        page.wait_for_selector("#grid-body")
        page.wait_for_timeout(800)
        return page

    @staticmethod
    def viewer(page) -> bool:
        return page.evaluate("document.body.classList.contains('is-viewer')")

    @staticmethod
    def becomes_viewer(page, timeout: int = 5000) -> bool:
        """見るだけになるか。**名乗った答えが来るまで待つ**(決まった時間で見ない)。

        役は `/api/tab/claim` の答えで塗られます。全部の試験を流していて
        重いときは 0.8 秒では答えが来ておらず、打てる側のままに見えました。
        """
        try:
            page.wait_for_function(
                "document.body.classList.contains('is-viewer')", timeout=timeout)
            return True
        except Exception:                                  # noqa: BLE001 - 待ち切れなかった
            return False

    @staticmethod
    def tab_of(page) -> str:
        return page.evaluate("sessionStorage.getItem('nippou.tab')")

    def save_rows(self, page, lots: list[str]) -> None:
        """その日報ページに行を置き、**そのタブが書いた**ことにする。"""
        from nippou import awake_clock
        from nippou.db.models import DetailRecord, HeaderRecord
        from nippou.logic import tab_lock
        key = page.evaluate("""() => { const d = document.getElementById('sheet-key').dataset;
            return [d.openedDate, d.openedLine, d.openedShift, Number(d.openedPage)]; }""")
        report_date, line, shift, no = key
        at = dict(report_date=report_date, line=line, shift=shift, page=no)
        self.repo().save(HeaderRecord(**at, worker="山田"),
                         [DetailRecord(**at, row_no=n, lot=lot) for n, lot in enumerate(lots, 1)])
        tab_lock.get_desk().note_write(self.tab_of(page), now=awake_clock.now())

    def test_2枚目は札1枚に差し替わり_打つものは押せない(self) -> None:
        """帯では紛らわしかった(表も保存の並びも薄くなっただけでそこにある)。**1枚に差し替える。**

        隠すのは見た目の話で焦点はキーボードで入り込めるので、`#main` の中は押せなくする。
        帯の「終了」(`#main` の外)まで灰色にすると、このタブが壊れたように見える。
        """
        a = self.open_tab()
        b = self.open_tab()
        self.assertTrue(self.becomes_viewer(b))
        self.assertTrue(b.is_visible("#tab-lock"))
        self.assertIn("この日報は、別のタブで開いています", b.text_content("#tab-lock"))
        self.assertTrue(b.is_enabled("#tab-lock-take"))
        self.assertFalse(b.is_visible("#grid-body"), "表が見えたままです")
        for field in ("#hd-worker", "#HIT1", "#save"):
            with self.subTest(field=field):
                self.assertTrue(b.is_disabled(field), f"{field} が押せます")
        self.assertTrue(b.is_enabled("#quit"), "#main の外まで押せなくしています")
        # 1枚目はそのまま打てる
        self.assertFalse(self.viewer(a))
        self.assertFalse(a.is_visible("#tab-lock"))
        self.assertTrue(a.is_enabled("#hd-worker"))
        self.assertEqual(self.errors, [])

    def test_見るだけのタブもほかの画面はふつうに見られる_心拍は続ける(self) -> None:
        """取り合うのは打つ画面だけ。**画面を出たら差し替えを戻す。心拍は止めない**
        (止めるとグラフを見て戻っただけで権利を落とす)。"""
        self.open_tab()
        b = self.open_tab()
        self.assertTrue(self.becomes_viewer(b))
        tab_b = self.tab_of(b)
        b.click('nav.rail a[href="/graph"]')
        b.wait_for_function("location.pathname === '/graph'", timeout=8000)
        b.wait_for_timeout(500)
        self.assertFalse(self.viewer(b), "グラフまで差し替えています")
        self.assertEqual(b.locator("#tab-lock").count(), 0)
        mark = len(self.seen)
        self.wait_until(b, lambda: any(p.startswith("/api/tab/ping") and t == tab_b
                                       for _, p, t in self.seen[mark:]),
                        "グラフにいるあいだの心拍", timeout=8.0)
        b.click('nav.rail a[href="/"]')
        b.wait_for_selector("#grid-body", state="attached")
        self.assertTrue(self.becomes_viewer(b), "日報入力へ戻ったら見るだけに戻ります")
        self.assertEqual(self.errors, [])

    def test_名札は全部の要求に付く(self) -> None:
        """書き込みの口が名札で見るので、**付け忘れる場所を作らない。**名札は読み直しでも変わらない。"""
        a = self.open_tab()
        tab = self.tab_of(a)
        self.assertTrue(tab)
        a.fill("#hd-worker", "山田")
        a.dispatch_event("#hd-worker", "change")
        a.wait_for_timeout(500)
        a.locator("#HIT1").click()
        a.locator("#HIT1").press_sequentially("3", delay=20)
        a.locator("#HIT1").press("Tab")
        a.wait_for_timeout(500)
        a.click("#save")
        a.wait_for_timeout(800)
        a.click('nav.rail a[href="/records"]')
        a.wait_for_function("location.pathname === '/records'", timeout=8000)
        a.wait_for_timeout(500)
        # 起きているかの見張り(`health.js`)は名札を見ない口なので除く
        probes = ("/api/health", "/api/alive")
        calls = [(m, p, t) for m, p, t in self.seen
                 if p.startswith("/api/") and not p.startswith(probes)]
        self.assertTrue(any(p.startswith("/api/entry/save") for _, p, _ in calls), calls)
        missing = [(m, p) for m, p, t in calls if t != tab]
        self.assertEqual(missing, [], "名札の無い(違う)要求があります")
        a.reload(wait_until="load")
        a.wait_for_timeout(500)
        self.assertEqual(self.tab_of(a), tab, "読み直しで名札が変わりました")

    def test_古い表からの保存は断られて_読み直して理由を言う(self) -> None:
        """ほかのタブが書いたあとの表から保存すると、サーバは `stale_tab` で断る。
        画面はそれを受けて**読み直し、読み直した画面で理由を出す**(`api.js`)。"""
        from nippou.logic import tab_lock
        a = self.open_tab()
        tab = self.tab_of(a)
        a.fill("#hd-worker", "山田")
        a.dispatch_event("#hd-worker", "change")
        a.wait_for_timeout(800)
        desk = tab_lock.get_desk()
        self.assertIsNotNone(desk.loaded.get(tab))
        a.evaluate("window.__before = true")                      # 読み直すと消える印
        desk.note_write("other", now=desk.loaded[tab] + 0.001)   # ほかのタブがあとから書いた
        # 打って欄を離れる(自動保存が古い表を送る)。次の心拍(5秒)より先に
        a.locator("#HIT1").click()
        a.locator("#HIT1").press_sequentially("3", delay=20)
        a.locator("#HIT1").press("Tab")
        # 断りの文言は失敗の知らせとしても出るので、**読み直したこと**を印で見る
        a.wait_for_function("window.__before === undefined", timeout=4000)
        a.wait_for_selector("#grid-body")
        # 読み直した画面で、サーバの断り文を出す(心拍が読み直したときの文言とは別)
        a.wait_for_function(
            "[...document.querySelectorAll('.toast--warn .toast__text')]"
            ".some(t => t.textContent.includes('読み直します'))", timeout=4000)
        self.assertIn(("/api/entry/save", 409), [(p.split("?")[0], c) for p, c in self.replies])
        self.assertEqual(self.errors, [])

    def test_引き継いだタブは表を読み直す(self) -> None:
        a = self.open_tab()
        self.save_rows(a, ["N7131T0", "N7132T0", "N7200T0"])
        b = self.open_tab()
        self.assertTrue(self.becomes_viewer(b))
        self.assertFalse(self.viewer(a))
        self.assertEqual(b.input_value("#LOT3"), "N7200T0")
        self.save_rows(a, ["N7131T0", "N7132T0", "N7200T0", "N7201T0"])   # A が4行目を保存
        token_a = self.tab_of(a)
        a.close(run_before_unload=True)                                   # A を閉じる
        # 閉じ際の合図(`sendBeacon`)はこの試験の経路(`route`)を通らないので、
        # サーバへは直接伝える(本物のサーバでは届くことを通しで確かめてあります)
        from nippou import awake_clock
        from nippou.logic import tab_lock
        tab_lock.get_desk().release(token_a, now=awake_clock.now())
        b.wait_for_function("!document.body.classList.contains('is-viewer')", timeout=12000)
        b.wait_for_function("document.getElementById('LOT4')?.value === 'N7201T0'",
                            timeout=5000)
        self.assertEqual(self.errors, [])

    def test_閉じる合図に描いた時刻を添える(self) -> None:
        """読み直したとき、遅れて届いた閉じる合図で権利を外さないため(v4.39.3)。"""
        self.ctx.add_init_script("""
            window.__beacons = [];
            navigator.sendBeacon = (url, blob) => {
              blob.text().then(t => window.__beacons.push({url: String(url), body: t}));
              return true;
            };""")
        a = self.open_tab()
        drawn = a.evaluate("Number(document.getElementById('sheet-key').dataset.tabLoaded)")
        a.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide'))")
        a.wait_for_function("window.__beacons.some(b => b.url.includes('/api/tab/release'))",
                            timeout=3000)
        import json
        sent = [json.loads(b["body"]) for b in a.evaluate("window.__beacons")
                if "/api/tab/release" in b["url"]]
        self.assertEqual(sent[-1]["tab"], self.tab_of(a))
        self.assertAlmostEqual(sent[-1]["loaded"], drawn)
        self.assertEqual(self.errors, [])

    def test_複製したタブは別の名札で見るだけ(self) -> None:
        a = self.open_tab()
        with self.ctx.expect_page() as got:
            a.evaluate("window.open('http://app.test/', '_blank')")
        c = got.value
        c.on("pageerror", lambda e: self.errors.append(str(e)))
        c.wait_for_selector("#grid-body")
        c.wait_for_timeout(1200)
        self.assertNotEqual(self.tab_of(c), self.tab_of(a))
        self.assertTrue(self.becomes_viewer(c))
        self.assertFalse(self.viewer(a))
        a.reload(wait_until="load")                 # F5 は名札そのまま
        a.wait_for_timeout(1200)
        self.assertFalse(self.viewer(a))
        self.assertEqual(self.errors, [])

    def test_ほかの画面は差し替えない(self) -> None:
        a = self.open_tab()
        b = self.open_tab()
        a.click('nav.rail a[href="/graph"]')
        a.wait_for_timeout(800)
        b.click("#tab-lock-take")
        b.wait_for_selector("#grid-body")
        b.wait_for_function("!document.body.classList.contains('is-viewer')", timeout=5000)
        a.wait_for_timeout(6500)                    # A の次の心拍で役が変わる
        self.assertFalse(self.viewer(a))            # グラフはそのまま
        self.assertFalse(a.is_visible("#tab-lock"))
        a.click('nav.rail a[href="/"]')
        try:
            a.wait_for_selector("#tab-lock", state="visible", timeout=8000)
        except Exception as exc:                    # noqa: BLE001 - 何が起きていたかを残す
            # Windows の CI で一度だけ札が出なかった(手元では 0.2 秒で出る)。
            # 次に起きたとき原因が分かるよう、画面とサーバの見ていたものを残す
            self.fail(f"日報入力へ戻っても札が出ません: {exc}\n{self._tab_state(a, b)}")
        self.assertTrue(self.viewer(a))             # 日報入力へ戻れば見るだけ
        self.assertEqual(self.errors, [])

    def _tab_state(self, a, b) -> str:
        """札が出なかったときに残すもの(画面2枚とサーバの名札の帳面)。"""
        from nippou import awake_clock
        from nippou.logic import tab_lock

        lines = []
        for name, page in (("A", a), ("B", b)):
            try:
                lines.append(f"{name}: " + str(page.evaluate(
                    "({path: location.pathname, tab: sessionStorage.getItem('nippou.tab'),"
                    " viewer: document.body.classList.contains('is-viewer'),"
                    " banner: document.getElementById('tab-lock') ? "
                    "!document.getElementById('tab-lock').hidden : null,"
                    " entry: !!document.getElementById('grid-body'),"
                    " visibility: document.visibilityState})")))
            except Exception as err:                # noqa: BLE001
                lines.append(f"{name}: 読めません({err})")
        desk = tab_lock.get_desk()
        now = awake_clock.now()
        lines.append(f"打てる側: {desk.editor or '(なし)'}")
        for token, tab in desk.tabs.items():
            lines.append(f"  {token}: {now - tab.seen_at:.1f}秒前 hidden={tab.hidden}")
        lines.append(f"画面の誤り: {self.errors}")
        return "\n".join(lines)


if __name__ == "__main__":
    unittest.main()
