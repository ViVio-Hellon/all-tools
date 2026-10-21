"""画面の明るさ(ライト / ダーク)を**本物のブラウザで**確かめる(段階1)。

以前は ``theme.js``・``base.html`` の頭のスクリプト・起動待機画面の中に
``listenShell();``・``localStorage.removeItem(LEGACY_THEME_KEY)``・``'alltools:theme'``
などの字があるかを見ていた(`test_theme.ShellDefaultTest` / `PagesTest`)。ここでは
日報複合ツールの大きなタブの画面と同じに**親の画面から色の既定を送り**、帯のボタンを押し、
CSS が届く前の色まで見る。

- 大設定の既定に付いていく(親の画面からだけ受け付ける)。自分で選んだら、そのまま
- 帯のボタンは ライト → ダーク → 自動 → 既定 と回り、既定へ戻すと大設定に従う
- 外枠の配線(app.js)が途中で止まっても、色の合図は取りこぼさない
- 前の版の選択(``kanban.theme``)は読まずに消す
- CSS より先に、描く前に明るさを決める(どの画面でも)
- 起動待機画面も同じ決め方で、同じ合図を受ける
"""
from __future__ import annotations

import unittest

from tests._kanban_browser import ORIGIN, KanbanBrowserCase

#: 日報複合ツールの大きなタブの画面の代わり(看板をそのまま枠で開く)
FRAME = '<!doctype html><meta charset="utf-8"><iframe id="f" src="{src}" style="width:1300px;height:820px"></iframe>'

#: 手前の「印」の合図が届くまで待つ = それより前に送った合図は処理し終えている
SETTLE = """() => new Promise((done) => {
  const f = (ev) => { if (ev.data === '__settled') { removeEventListener('message', f); done(); } };
  addEventListener('message', f);
  postMessage('__settled', '*');
})"""


class _Theme(KanbanBrowserCase):
    def setUp(self) -> None:
        super().setUp()
        self.pages["/blank"] = "<!doctype html><title>blank</title>"

    def remember(self, page, values: dict) -> None:
        """この端末(看板の出どころ)の localStorage に入れておく。

        同じ画面で開き直すので、前に開いた画面の持ち主の名札も忘れさせる(残ると
        「この画面では操作できません」の覆いが出て押せない)。
        """
        from kanban import screen

        page.goto(f"{ORIGIN}/blank")
        screen.reset()
        page.evaluate("(v) => { localStorage.clear(); for (const [k, x] of Object.entries(v)) localStorage.setItem(k, x); }",
                      values)

    def framed(self, src: str, values: dict | None = None):
        """親の画面の中に ``src`` を開き、(フレーム, 親) を返す。"""
        self.pages["/shell"] = FRAME.format(src=src)
        page = self.watch(self.ctx.new_page())
        self.remember(page, values or {})
        page.goto(f"{ORIGIN}/shell", wait_until="load")
        frame = page.frame_locator("#f")
        frame.locator("html").wait_for(state="attached")
        return next(f for f in page.frames if f.url.startswith(f"{ORIGIN}{src.split('?')[0]}")), page

    @staticmethod
    def send(page, theme: str) -> None:
        """親の画面から「画面の色の既定」を送る(入口の shell.js の sendTheme と同じ形)。"""
        page.evaluate("(t) => document.getElementById('f').contentWindow"
                      ".postMessage({ type: 'alltools:theme', theme: t }, '*')", theme)

    @staticmethod
    def theme(frame) -> str:
        return frame.evaluate("document.documentElement.dataset.theme")

    def wait_theme(self, frame, want: str) -> None:
        frame.wait_for_function("(w) => document.documentElement.dataset.theme === w", arg=want)

    @staticmethod
    def stored(frame, key: str):
        return frame.evaluate("(k) => localStorage.getItem(k)", key)

    @staticmethod
    def wired(frame) -> None:
        """帯のボタンが繋がった(``apply()`` が説明を付け替えた)まで待つ。"""
        frame.wait_for_function(
            "(document.getElementById('theme-toggle') || {}).title?.startsWith('画面の明るさ:')")


class ShellDefaultTest(_Theme):
    def test_大設定の既定に付いていき_選んだらそのまま_既定へ戻すとまた従う(self) -> None:
        frame, shell = self.framed("/board")
        self.wired(frame)
        self.assertEqual(self.theme(frame), "light", "大設定の既定が無いときはライト")

        self.send(shell, "dark")
        self.wait_theme(frame, "dark")
        self.assertEqual(self.stored(frame, "kanban.theme_default"), "dark", "届いた既定を覚えていない")

        with self.subTest("大きなタブの画面(親)以外からの合図は受けない"):
            frame.evaluate("window.postMessage({ type: 'alltools:theme', theme: 'light' }, '*')")
            frame.evaluate(SETTLE)
            self.assertEqual(self.theme(frame), "dark")

        with self.subTest("選んだら、大設定の既定が届いてもそのまま"):
            frame.click("#theme-toggle")            # 既定(いまダーク)→ 見た目が変わるライトへ
            self.wait_theme(frame, "light")
            self.assertEqual(self.stored(frame, "kanban.theme_choice"), "light")
            self.send(shell, "dark")
            frame.evaluate(SETTLE)
            self.assertEqual(self.theme(frame), "light")

        with self.subTest("ダーク → 自動 → 既定 と回り、既定へ戻すと覚えを消して大設定に従う"):
            labels = []
            for _ in range(3):
                frame.click("#theme-toggle")
                labels.append(frame.inner_text("#theme-toggle"))
            self.assertEqual(labels, ["☾ ダーク", "◐ 自動", "◇ 既定"])
            self.assertIsNone(self.stored(frame, "kanban.theme_choice"), "既定へ戻しても覚えが残る")
            self.assertEqual(self.theme(frame), "dark", "既定へ戻したのに大設定の既定(ダーク)に従わない")
            self.send(shell, "light")
            self.wait_theme(frame, "light")

    def test_外枠の配線が途中で止まっても_色の合図は受ける(self) -> None:
        """帯のボタンを繋ぐ処理(app.js)とは切り離して、theme.js を読み込んだ時点で受け付ける。"""
        res = self.client.get("/static/js/desktop.js")
        original = res.get_data(as_text=True)
        res.close()
        self.assertIn("export function install() {", original)
        broken = original.replace(
            "export function install() {",
            "export function install() {\n  throw new Error('試験: 外枠の配線がここで止まる');", 1)
        self.ctx.route("**/static/js/desktop.js*", lambda route: route.fulfill(
            status=200, body=broken, headers={"content-type": "text/javascript; charset=utf-8"}))

        frame, shell = self.framed("/board")
        self.wait_until(shell, lambda: any("外枠の配線がここで止まる" in e for e in self.errors),
                        "配線が止まった状態")
        self.assertFalse(frame.evaluate("(document.getElementById('theme-toggle') || {}).title?.startsWith('画面の明るさ:')"),
                         "試験の前提: 帯のボタンは繋がっていないはず")
        self.send(shell, "dark")
        self.wait_theme(frame, "dark")


class BeforePaintTest(_Theme):
    def test_どの画面もCSSより先に明るさを決め_前の版の選択は読まずに消す(self) -> None:
        """CSS が届くのを止めておき、そのあいだにもう色が決まっていること(白く光ってから暗くならない)。"""
        cases = (
            # (覚えていたもの, CSS が届く前の明るさ)
            ({"kanban.theme_default": "dark", "kanban.theme": "light"}, "dark"),   # 前の版の選択は読まない
            ({"kanban.theme": "dark"}, "light"),
            ({"kanban.theme_default": "dark", "kanban.theme_choice": "light"}, "light"),
        )
        page = self.watch(self.ctx.new_page())
        for path in ("/board", "/settings"):
            for values, want in cases:
                with self.subTest(path=path, values=values):
                    self.remember(page, values)
                    self.hold = ("/static/css/tokens.css",)
                    page.goto(f"{ORIGIN}{path}", wait_until="commit")
                    self.wait_held(page)
                    page.wait_for_function("document.querySelector('link[href*=\"tokens.css\"]')")
                    self.assertEqual(page.evaluate("document.documentElement.dataset.theme"), want,
                                     "CSS より先に明るさが決まっていない")
                    self.hold = ()
                    self._forward(self.held.pop())
                    page.wait_for_load_state("load")
                    self.wired(page)
                    self.assertEqual(page.evaluate("document.documentElement.dataset.theme"), want)
                    self.assertIsNone(self.stored(page, "kanban.theme"), "前の版の選択が残っている")

            with self.subTest(path=path, what="帯のボタンで切り替わる"):
                before = page.evaluate("document.documentElement.dataset.theme")
                page.click("#theme-toggle")
                page.wait_for_function("(b) => document.documentElement.dataset.theme !== b", arg=before)


class BootScreenTest(_Theme):
    """起動待機画面(kanban/boot_screen.py)も業務画面と同じ決め方で、同じ合図を受ける。"""

    def setUp(self) -> None:
        super().setUp()
        from kanban import boot_screen

        self.pages["/boot"] = boot_screen.render(display_name="看板", version_label="VER", token="t",
                                                 app_id="a", poll_ms=300, home_url="/board")
        # まだ立ち上がっていない(本体の画面へは移らない)
        self.answers["/api/health"] = (200, {"ready": False, "app_id": "a", "stage_key": "prepare"})

    def test_起動待機画面も大設定の既定に従い_親の合図だけを受ける(self) -> None:
        frame, shell = self.framed("/boot", {"kanban.theme_default": "dark", "kanban.theme": "light"})
        self.assertEqual(self.theme(frame), "dark", "大設定の既定に従わない(前の版の選択を読んだ)")
        self.send(shell, "light")
        self.wait_theme(frame, "light")
        self.assertEqual(self.stored(frame, "kanban.theme_default"), "light")
        frame.evaluate("window.postMessage({ type: 'alltools:theme', theme: 'dark' }, '*')")
        frame.evaluate(SETTLE)
        self.assertEqual(self.theme(frame), "light", "親以外からの合図を受けた")

        with self.subTest("自分で選んでいれば、合図が来てもそのまま"):
            frame2, shell2 = self.framed("/boot", {"kanban.theme_choice": "light"})
            self.send(shell2, "dark")
            frame2.evaluate(SETTLE)
            self.assertEqual(self.theme(frame2), "light")
            self.assertEqual(self.stored(frame2, "kanban.theme_default"), "dark", "届いた既定を覚えていない")


if __name__ == "__main__":
    unittest.main()
