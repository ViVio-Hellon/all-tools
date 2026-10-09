"""印刷の窓には「印刷する」がある (v4.6.0)

    すべての印刷プレビュー画面に 印刷する ボタンがあるかのチェックと
    動作チェック願います

紙を別の窓で開く口は2つです。

    日報の紙             `/report/nippou`(1ページ / `page=all` で直ぶん)
                         日報入力「印刷」・記録を見る「紙を見る」から開く。
                         「当直をDBから復旧」ではファイルにも書き出す
    梱包資材重量計算の紙 `/report/gw`(開いたら印刷の画面も出す)

GW の紙には「印刷する」の帯がありましたが、**日報の紙にはありません
でした**(Ctrl+P を知らないと刷れない)。同じ帯(`reporting/paper.py`)を
両方に付けます。

ここで押さえるのは:

    1. どの紙の窓にも「印刷する」が1つだけある(直ぶんでもページごとには付けない)
    2. 押すとブラウザの印刷の画面が出る(`window.print()`)── **本物のブラウザで押す**
    3. 帯は紙に出ない(印刷の見た目で消える・紙の枚数が増えない)
    4. 刷るものが無い窓(見つからない)には付けない

画面の中から直に刷るもの(別の窓を開かない)は、押すと印刷の画面が出る
ことだけを見ます。

    コイル・平板「印刷」   ここで本物のブラウザで押す(計算書が組まれて1枚)
    VC 早見表「印刷(A4 横)」 サーバの表が要るので `tests/test_web_vc.py`
                             (印刷のあいだの組み方)
"""
from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.models import DetailRecord, HeaderRecord  # noqa: E402
from nippou.reporting import gw_print, paper, print_format  # noqa: E402
from tests._web import WebTestCase  # noqa: E402
from tests._browser import chromium_or_skip  # noqa: E402
from tests.test_paper_margins import daily_html, gw_html  # noqa: E402

BUTTON = f'<button type="button" onclick="window.print()">{paper.PRINT_LABEL}</button>'
KEY = dict(report_date="2026年8月3日", line="L-1", shift="1直")


def _page(n: int, lot: str = "LOT1"):
    return (HeaderRecord(**KEY, page=n, worker="山田"),
            [DetailRecord(**KEY, page=n, row_no=1, lot=lot)])


class ToolbarTests(unittest.TestCase):
    """紙のHTMLに帯が載っているか(ブラウザが無くても見られるもの)。"""

    def test_日報の紙1ページ(self) -> None:
        text = print_format.build_print_html(*_page(1))
        self.assertEqual(text.count(BUTTON), 1)
        self.assertIn("A4 横1枚で刷れます", text)

    def test_日報の紙は直ぶんでも1つだけ(self) -> None:
        """ページごとに付けると、刷る前に押すボタンが3つ並ぶ。"""
        text = print_format.build_shift_html([_page(1), _page(2), _page(3)])
        self.assertEqual(text.count(BUTTON), 1)
        self.assertIn("全3ページ", text)
        self.assertIn("3枚に分かれて刷れます", text)

    def test_帯は紙の外にある(self) -> None:
        """紙1枚ぶんの箱(`.sheet`)の外。余白の測り(test_paper_margins)にも入らない。"""
        text = print_format.build_shift_html([_page(1), _page(2)])
        self.assertLess(text.index('class="toolbar"'), text.index('<section class="sheet">'))

    def test_帯は紙に出ない(self) -> None:
        for name, text in (("日報", daily_html(1)), ("GW", gw_html())):
            with self.subTest(name=name):
                self.assertIn("@media print { .toolbar { display: none !important; } }", text)

    def test_刷るものが無い窓には付けない(self) -> None:
        self.assertNotIn(BUTTON, print_format.build_shift_html([]))
        self.assertNotIn(BUTTON, gw_print.build_missing_html("計算できません"))

    def test_GWの紙と同じ帯(self) -> None:
        """字・押したときの動きを2つの紙でそろえる。"""
        self.assertIn(BUTTON, gw_html())
        self.assertIn(paper.TOOLBAR_CSS, gw_html())
        self.assertIn(paper.TOOLBAR_CSS, daily_html(1))

    def test_書き出したファイルにも付く(self) -> None:
        """「当直をDBから復旧」で書き出す紙。ブラウザで直に開いても刷れる。"""
        with tempfile.TemporaryDirectory() as tmp:
            out = print_format.write_print_html(*_page(1), Path(tmp) / "印刷.html")
            self.assertIn(BUTTON, out.read_text(encoding="utf-8"))

    def test_添え書きは字にして出す(self) -> None:
        self.assertIn("&lt;b&gt;", paper.toolbar_html("<b>"))


class RouteTests(WebTestCase):
    """`GET /report/nippou` の応答に帯が載っているか。"""

    def setUp(self) -> None:
        super().setUp()
        for n in (1, 2):
            self.repo().save(*_page(n, lot=f"LOT{n}"))

    def get_paper(self, page: str):
        return self.get("/report/nippou?report_date=2026年8月3日&line=L-1"
                        f"&shift=1直&page={page}")

    def test_1ページの紙(self) -> None:
        res = self.get_paper("1")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_data(as_text=True).count(BUTTON), 1)

    def test_直ぶんの紙(self) -> None:
        text = self.get_paper("all").get_data(as_text=True)
        self.assertEqual(text.count(BUTTON), 1)
        self.assertIn("全2ページ", text)

    def test_見つからないときは付けない(self) -> None:
        res = self.get("/report/nippou?report_date=なし&line=L-1&shift=1直&page=1")
        self.assertEqual(res.status_code, 404)
        self.assertNotIn(BUTTON, res.get_data(as_text=True))

    def test_ボタンを止める決まりを付けていない(self) -> None:
        """帯は `onclick` で刷る。CSP で書き込みの script を止めると押しても何も起きない。"""
        policy = self.get_paper("1").headers.get("Content-Security-Policy", "")
        if "script-src" in policy:
            self.assertIn("'unsafe-inline'", policy)


_HOOK = "() => { window.__prints = 0; window.print = () => { window.__prints += 1; }; }"
_TOOLBAR_SHOWN = """() => { const el = document.querySelector('.toolbar');
  if (!el || getComputedStyle(el).display === 'none') return null;
  const r = el.getBoundingClientRect();
  return {top: r.top, bottom: r.bottom, height: innerHeight}; }"""


class BrowserTests(unittest.TestCase):
    """**本物のブラウザで「印刷する」を押す。**

    ヘッドレスでは印刷の画面が出ないので、`window.print` を数える物に
    差し替えて、押したら1回呼ばれるかを見ます。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.pw, cls.browser = chromium_or_skip()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.pw.stop()

    def open(self, html: str):
        page = self.browser.new_page(viewport={"width": 1200, "height": 700})
        self.addCleanup(page.close)
        page.set_content(html)
        page.evaluate(_HOOK)
        return page

    def press(self, page) -> int:
        page.get_by_role("button", name=paper.PRINT_LABEL, exact=True).click()
        return page.evaluate("window.__prints")

    def test_日報の紙で押すと印刷の画面が出る(self) -> None:
        page = self.open(daily_html(3))
        self.assertEqual(self.press(page), 1)

    def test_GWの紙で押すと印刷の画面が出る(self) -> None:
        page = self.open(gw_html())
        self.assertEqual(self.press(page), 1)

    def test_下まで送っても押せる(self) -> None:
        """直ぶんの紙は長い。帯は上に貼り付いて、見えたままになる。"""
        page = self.open(daily_html(3))
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        shown = page.evaluate(_TOOLBAR_SHOWN)
        self.assertIsNotNone(shown)
        self.assertGreaterEqual(shown["top"], 0)
        self.assertLessEqual(shown["bottom"], shown["height"])
        self.assertEqual(self.press(page), 1)

    def test_紙では帯が消えて枚数も増えない(self) -> None:
        for name, html, sheets in (("日報", daily_html(3), 3), ("GW", gw_html(), 1)):
            with self.subTest(name=name):
                page = self.open(html)
                self.assertIsNotNone(page.evaluate(_TOOLBAR_SHOWN), "画面では見える")
                page.emulate_media(media="print")
                self.assertIsNone(page.evaluate(_TOOLBAR_SHOWN), "紙に出ています")
                pdf = page.pdf(prefer_css_page_size=True)
                self.assertEqual(len(re.findall(rb"/Type\s*/Page[^s]", pdf)), sheets)

    def test_コイル平板の印刷(self) -> None:
        """画面の中の「印刷」。**計算書だけ**が A4 縦1枚に出る。"""
        coil = (Path(__file__).resolve().parent.parent
                / "app" / "static" / "vc" / "coil" / "index.html")
        page = self.browser.new_page(viewport={"width": 1600, "height": 1000})
        self.addCleanup(page.close)
        page.goto(coil.as_uri())
        page.wait_for_load_state("load")
        page.evaluate(_HOOK)
        for count, (shape, button) in enumerate(
                (("coil", "#coil-print"), ("plate", "#plate-print")), start=1):
            with self.subTest(shape=shape):
                page.click(f'.shape-btn[data-shape="{shape}"]')
                page.click(button)
                self.assertEqual(page.evaluate("window.__prints"), count)
                self.assertTrue(page.evaluate(
                    "document.getElementById('print-sheet').textContent.trim().length"))
                page.emulate_media(media="print")
                shown = page.evaluate("""() => [...document.body.children].filter(el =>
                    el.id !== 'print-sheet' && getComputedStyle(el).display !== 'none').length""")
                self.assertEqual(shown, 0, "計算書のほかに画面の部品が紙に出ています")
                pdf = page.pdf(prefer_css_page_size=True)
                self.assertEqual(len(re.findall(rb"/Type\s*/Page[^s]", pdf)), 1)
                page.emulate_media(media="screen")


if __name__ == "__main__":
    unittest.main()
