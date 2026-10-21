"""外枠: 背景・ネオン・版の印・記録の名乗り・起動の見張りを本物のブラウザで (段階1)

前は `theme.js` / `skin.js` / `app.js` / `api.js` / `errors.js` / `base.html` / `nav.js` の
字を探して「仕組みの行があること」だけを見ていました。ここでは画面を開いて確かめます:

    - 帯の「ライト / ダーク」: 押せば `<html data-theme>` が変わり、読み直しても残り、
      コイル・平板の覚えにも書く。**画面を移って帯が作り直されても**押せる
    - グラフのネオン: 外せば元の見た目、読み直しても残る。**覚えられない端末**
      (プライベート窓)でも画面が転ばない
    - 版の印: 設定の「この端末」に画面(JS)の印が出て、サーバの印と一致と言う。
      食い違えば帯に印を付けて「読み直してください」
    - 記録: 要求はどの画面からかを名乗り、見張りの通信は「押していない」と名乗る。
      画面のエラー・握られなかった失敗は記録へ届けて番号を出す
    - 起動の見張り: `app.js` が読めないと1回だけ黙って取り直し、駄目なら断りを出して
      **面を全部開く**。画面のモジュールが読めないときも同じ
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._entry_browser import ORIGIN  # noqa: E402
from tests._screen_browser import ScreenBrowserCase  # noqa: E402

#: 覚える場所を使えない端末(プライベート窓など)。触ると投げる
NO_STORAGE = """
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    get() { throw new DOMException("試験: 覚える場所は使えません", "SecurityError"); },
  });
"""


class ThemeTests(ScreenBrowserCase):
    def theme(self, page) -> str:
        return page.evaluate("document.documentElement.dataset.theme")

    def pressed(self, page) -> dict:
        return page.evaluate("""() => Object.fromEntries([...document.querySelectorAll(
            '#theme-switch [data-theme-choice]')].map(b => [b.dataset.themeChoice,
            b.getAttribute('aria-pressed')]))""")

    def test_帯で選んだ背景は読み直しても画面を移っても効き_コイルにも渡る(self) -> None:
        page = self.open_screen("/records")
        self.assertEqual(self.theme(page), "light")             # 選ぶまではライト
        page.click('#theme-switch [data-theme-choice="dark"]')
        self.assertEqual(self.theme(page), "dark")
        self.assertEqual(self.pressed(page), {"light": "false", "dark": "true"})
        stored = page.evaluate("""() => [localStorage.getItem('nippou.theme'),
                                        localStorage.getItem('vc-calculator.coil.theme')]""")
        self.assertEqual(stored, ["dark", "dark"], "コイル・平板の覚えに渡していません")

        page.reload(wait_until="load")                           # 描く前に当てる(head の小さな script)
        self.assertEqual(self.theme(page), "dark")
        page.wait_for_function("window.APP && window.APP.booted === true", timeout=8000)
        self.assertEqual(self.pressed(page)["dark"], "true")

        # 画面を移ると帯ごと作り直される。**作り直した帯のボタンも効く**
        page.click('nav.rail a[href="/graph"]')
        page.wait_for_function("location.pathname === '/graph'", timeout=8000)
        page.wait_for_timeout(500)
        page.click('#theme-switch [data-theme-choice="light"]')
        self.assertEqual(self.theme(page), "light")
        self.assertEqual(self.pressed(page), {"light": "true", "dark": "false"})
        self.assertEqual(self.errors, [])


class SkinTests(ScreenBrowserCase):
    def skin(self, page) -> str:
        return page.evaluate("document.documentElement.dataset.skin")

    def test_ネオンを外せば元の見た目_読み直しても残る(self) -> None:
        page = self.open_screen("/graph")
        self.assertEqual(self.skin(page), "neon")               # 既定はネオン
        page.uncheck("#neon-skin")
        self.assertEqual(self.skin(page), "plain")
        page.reload(wait_until="load")
        self.assertEqual(self.skin(page), "plain")              # 描く前に当てる
        page.wait_for_function("window.APP && window.APP.booted === true", timeout=8000)
        self.assertFalse(page.is_checked("#neon-skin"))
        page.check("#neon-skin")
        self.assertEqual(self.skin(page), "neon")
        self.assertEqual(self.errors, [])

    def test_覚えられない端末でも転ばずに切り替えられる(self) -> None:
        """プライベート窓では `localStorage` に触ると投げます。**読み書きは必ず try/catch。**"""
        self.ctx.add_init_script(NO_STORAGE)
        page = self.open_screen("/graph")
        self.assertEqual(self.skin(page), "neon")
        page.uncheck("#neon-skin")
        self.assertEqual(self.skin(page), "plain")              # この画面のあいだは効く
        page.click('#theme-switch [data-theme-choice="dark"]')
        self.assertEqual(page.evaluate("document.documentElement.dataset.theme"), "dark")
        page.wait_for_timeout(300)
        self.assertEqual(self.errors, [], "覚える場所が使えないと画面が転びます")


class StampTests(ScreenBrowserCase):
    """**画面(JS)の版の印を、自分の URL の道(`/sv/<印>/js/app.js`)から拾う**(v4.40.2 で直した)。

    前は `?v=` から拾っていて、印が道へ移ったあと**いつも空**でした ── 設定の「この端末」は
    「(取れません)/ 確かめられません」、古い控えを掴んでいても帯に印が付かなかった。
    この2本は、本物のブラウザで開いて初めて見つかりました(字を探す試験は通っていた)。
    """

    def stamp(self) -> str:
        return self.app.config["STATIC_STAMP"]

    def test_画面の印がサーバの印と並んで一致と出る(self) -> None:
        page = self.open_screen("/settings?tab=terminal")
        self.assertEqual(page.text_content("#client-stamp").strip(), self.stamp())
        self.assertEqual(page.text_content("#stamp-verdict").strip(), "一致")
        self.assertIsNone(page.get_attribute("#app-version", "data-stale"))

    def test_印が食い違えば帯に印を付けて読み直しを促す(self) -> None:
        """サーバの印だけ新しい(=画面の JS が古い控え)。"""
        path = "/settings?tab=terminal"
        html = self.get(path).get_data(as_text=True)
        served = f'stamp: "{self.stamp()}"'
        self.assertIn(served, html)
        self.pages[path] = html.replace(served, 'stamp: "9.9.9-deadbeef"')
        page = self.open_screen(path)
        self.assertEqual(page.get_attribute("#app-version", "data-stale"), "1")
        self.assertIn("食い違い", page.text_content("#stamp-verdict"))
        page.wait_for_selector("text=画面が古いままです", timeout=3000)


class EventLogTests(ScreenBrowserCase):
    def test_要求は画面を名乗り_見張りは押していないと名乗り_エラーは番号つきで届く(self) -> None:
        page = self.open_entry()
        mark = len(self.requests)
        self.type_in(page, "MAI1", "10")
        page.locator("#MAI1").press("Tab")
        self.wait_until(page, lambda: any(m == "POST" and p.startswith("/api/entry/")
                                          for m, p, _ in self.requests[mark:]), "打った欄の要求")
        with self.subTest("押した操作はどの画面からかを名乗り、見張りの印は付けない"):
            pressed = next(h for m, p, h in self.requests[mark:]
                           if m == "POST" and p.startswith("/api/entry/"))
            self.assertEqual(pressed.get("x-screen"), "/")
            self.assertNotIn("x-quiet", pressed)
        with self.subTest("1分ごとの見張りは「押していない」と名乗る"):
            watch = self.asked("/api/sound/due", "GET")
            self.assertTrue(watch)
            self.assertEqual(watch[0][2].get("x-quiet"), "1")
            self.assertEqual(watch[0][2].get("x-screen"), "/")

        sent = lambda: [b for p, b in self.sent if p.startswith("/api/log/client")]  # noqa: E731
        with self.subTest("握られなかった失敗も記録へ届く"):
            page.evaluate("() => { Promise.reject(new Error('試験の握られなかった失敗')); }")
            self.wait_until(page, lambda: any(b.get("kind") == "rejection" for b in sent()),
                            "握られなかった失敗の記録")
            body = next(b for b in sent() if b.get("kind") == "rejection")
            self.assertIn("試験の握られなかった失敗", body["message"])
            self.assertEqual(body["screen"], "/")
        with self.subTest("画面のエラーは番号つきで知らせる"):
            page.evaluate("setTimeout(() => { throw new Error('試験の画面のエラー'); })")
            self.wait_until(page, lambda: any(b.get("kind") == "error" for b in sent()),
                            "画面のエラーの記録")
            page.wait_for_selector("text=画面でエラーが起きました(番号", timeout=3000)


class BootWatchTests(ScreenBrowserCase):
    """`type="module"` は読めなかったとき**何も言わずに止まる。** 見張りは素のスクリプト。"""

    def panels_hidden(self, page) -> int:
        return page.evaluate("document.querySelectorAll('.tabpanel[hidden]').length")

    def test_入口が読めなければ1回取り直し_駄目なら断りを出して面を全部開く(self) -> None:
        self.ctx.route("**/js/app.js*", lambda route: route.fulfill(status=404, body=""))
        page = self.watch(self.ctx.new_page())
        page.goto(f"{ORIGIN}/settings", wait_until="load")
        page.wait_for_selector(".msg--boot", timeout=10000)
        self.assertIn("_r=", page.url, "黙って取り直していません")
        self.assertIn("この画面の操作を読み込めませんでした", page.text_content(".msg--boot"))
        self.assertGreater(page.evaluate("document.querySelectorAll('.tabpanel').length"), 1)
        self.assertEqual(self.panels_hidden(page), 0, "開いていない面へ二度と辿り着けません")
        self.wait_until(page, lambda: any(b.get("kind") == "boot" for p, b in self.sent
                                          if p.startswith("/api/log/client")), "記録への届け")

    def test_画面のモジュールが読めなければ断りを出して面を全部開く(self) -> None:
        self.ctx.route("**/js/views/settings.js*", lambda route: route.fulfill(status=404, body=""))
        page = self.watch(self.ctx.new_page())
        page.goto(f"{ORIGIN}/settings", wait_until="load")
        page.wait_for_selector(".msg--wire", timeout=8000)
        self.assertIn("ボタンが効きません", page.text_content(".msg--wire"))
        self.assertEqual(self.panels_hidden(page), 0)


if __name__ == "__main__":
    unittest.main()
