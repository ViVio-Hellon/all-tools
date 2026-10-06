"""印刷 ── 文字・罫線・色帯を、紙の端から5mm以上内側に置く

【なぜ要るのか】
プリンターは紙の縁から約4mmには印刷できません。ブラウザの印刷
プレビューは紙の端まで描くので、**画面では収まって見えても紙では欠けます。**

以前の作りは ``@page`` の余白(10mm)に頼っていて、しかも1日に数件
登録があると A4 横1枚に収まらず2枚目へこぼれていました。1枚に収めようと
ダイアログで「余白: なし」や倍率を選ぶと、表が紙の端まで寄ります。

【見張ること】
1. 余白はページの内側に自分で取る(``@page`` の余白は 0。ダイアログで
   「なし」を選ばれても変わらない)。**5mm 以上**
2. 描くものはすべて台紙(``.sheet``)の中
3. 中身が多い月も**1枚に収める**(切り捨てず、縮める)
4. ダイアログで余白を足されても(「最小」など)2枚目にこぼれない
5. 色帯はプレビューのとおりに刷る(``print-color-adjust``)
6. 画面には [印刷する]・[閉じる] がある(紙には出ない)

ブラウザでの確かめ(下の ``BrowserTests``)は Chromium で PDF に出し、
何枚になるか・中身が台紙の安全な範囲にあるかを見ます。
"""

from __future__ import annotations

import datetime as dt
import os
import re
import tempfile
import unittest
from pathlib import Path

from calendar_app import config, printing
from calendar_app.repository import DayRecord

CHROME = os.environ.get("CALENDAR_TEST_CHROME",
                        "/opt/pw-browsers/chromium-1194/chrome-linux/chrome")


def _document(per_day: int) -> str:
    def rec(i: int) -> DayRecord:
        return DayRecord(id=i, kubun=config.KUBUN_REST, naiyou=f"作業者{i}",
                         code=str(i), shift="1", overtime="", early="",
                         group="A", line="コイル")
    records = {f"2026/09/{d:02d}": [rec(d * 100 + j) for j in range(per_day)]
               for d in range(1, 31)}
    return printing.render_month_document(
        year=2026, month=9, start=dt.date(2026, 8, 30), records=records,
        my_line="コイル")


class LayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = _document(3)

    def test_安全な余白は5mm以上(self) -> None:
        self.assertGreaterEqual(printing.SAFE_MM, 5)

    def test_ページの余白に頼らない(self) -> None:
        """ダイアログの「余白: なし」で消えるものに頼らない。"""
        self.assertIn("@page { size: A4 landscape; margin: 0; }", self.html)
        self.assertIn(f"padding: {printing.SAFE_MM}mm", self.html)

    def test_描くものはすべて台紙の中(self) -> None:
        body = self.html.split("<body>", 1)[1]
        sheet = body.index('<div class="sheet">')
        for mark in ("<h1>", "<table>", 'class="footer"'):
            self.assertGreater(body.index(mark), sheet, mark)
        # 台紙の外にあるのは、印刷しない案内とスクリプトだけ
        outside = re.sub(r'<div class="sheet">.*</div></div>', "", body, flags=re.S)
        # 案内(.noprint)は入れ子の div を持つので、スクリプトの手前まで
        outside = re.sub(r'<div class="noprint">.*?(?=<script>)', "", outside, flags=re.S)
        outside = re.sub(r"<script>.*?</script>", "", outside, flags=re.S)
        self.assertEqual(outside.replace("</body>", "").replace("</html>", "").strip(), "")

    def test_印刷するボタンがある(self) -> None:
        """Ctrl+P を知らない人でも印刷できるように。**紙には出さない**(.noprint の中)。"""
        notice = self.html.split('<div class="noprint">', 1)[1].split('<div class="sheet">', 1)[0]
        self.assertIn('id="do-print"', notice)
        self.assertIn(">印刷する</button>", notice)
        self.assertIn('id="do-close"', notice)
        self.assertIn("window.print()", self.html)
        self.assertIn(".noprint { display: none; }", self.html)

    def test_色帯をプレビューのとおりに刷る(self) -> None:
        self.assertIn("print-color-adjust: exact", self.html)

    def test_余白を足されても2枚目にこぼれない高さ(self) -> None:
        """Chrome は幅が収まるよう全体を縮める。縮めた高さが余白の内側に収まること。"""
        for margin in (4, 6, 10):
            shrink = (printing.PAPER_W_MM - 2 * margin) / printing.PAPER_W_MM
            self.assertLessEqual(printing.SHEET_H_MM * shrink,
                                 printing.PAPER_H_MM - 2 * margin, margin)

    def test_空の月も6週が台紙に収まる(self) -> None:
        room = printing.SHEET_H_MM - 2 * printing.SAFE_MM
        self.assertLess(6 * printing.ROW_MM + 20, room)   # 20 = 見出し・曜日・フッター


@unittest.skipUnless(Path(CHROME).exists(), "Chromium が無いため省略")
class BrowserTests(unittest.TestCase):
    """Chromium で PDF に出して確かめる。"""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:                       # pragma: no cover
            raise unittest.SkipTest("playwright が無いため省略")
        cls._pw = sync_playwright().start()
        cls._browser = cls._pw.chromium.launch(executable_path=CHROME)
        cls._tmp = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._browser.close()
        cls._pw.stop()
        cls._tmp.cleanup()

    def _open(self, per_day: int, extra_css: str = ""):
        path = Path(self._tmp.name) / f"m{per_day}.html"
        path.write_text(_document(per_day), encoding="utf-8")
        page = self._browser.new_page()
        self.addCleanup(page.close)
        page.goto(path.as_uri())
        page.wait_for_timeout(200)
        if extra_css:
            page.add_style_tag(content=extra_css)
        return page

    def _pages(self, page) -> int:
        pdf = page.pdf(prefer_css_page_size=True, print_background=True)
        return len(re.findall(rb"/Type\s*/Page[^s]", pdf))

    def _inside_safe_area(self, page) -> list[str]:
        """台紙の安全な範囲からはみ出した要素(印刷の見え方で測る)。"""
        page.emulate_media(media="print")
        return page.evaluate("""(safe) => {
            const mm = 96 / 25.4, sheet = document.querySelector('.sheet').getBoundingClientRect();
            const box = { l: sheet.left + safe * mm, t: sheet.top + safe * mm,
                          r: sheet.right - safe * mm, b: sheet.bottom - safe * mm };
            const out = [];
            document.querySelectorAll('.sheet h1, .sheet th, .sheet td, .sheet .footer').forEach((e) => {
                const r = e.getBoundingClientRect();
                if (r.left < box.l - 1 || r.top < box.t - 1 || r.right > box.r + 1 || r.bottom > box.b + 1)
                    out.push(e.tagName + ':' + e.textContent.slice(0, 12));
            });
            return out; }""", printing.SAFE_MM)

    def test_ふつうの月は1枚で安全な範囲の中(self) -> None:
        page = self._open(4)
        self.assertEqual(self._pages(page), 1)
        self.assertEqual(self._inside_safe_area(page), [])

    def test_多い月も縮めて1枚に収める(self) -> None:
        """**切り捨てない。** 以前は2枚目へこぼれていた。"""
        page = self._open(12)
        self.assertEqual(self._pages(page), 1)
        self.assertEqual(self._inside_safe_area(page), [])
        zoom = float(page.evaluate("document.querySelector('.content').style.zoom") or 1)
        self.assertLess(zoom, 1)

    def test_印刷するを押すと印刷ダイアログを開く(self) -> None:
        page = self._open(4)
        # 本物のダイアログは開かないので、呼ばれたことと、その前に収め直したかを見る
        page.evaluate("""() => { window.__printed = 0; window.__fitted = 0;
            window.addEventListener('beforeprint', () => { window.__fitted += 1; });
            window.print = () => { window.dispatchEvent(new Event('beforeprint'));
                                   window.__printed += 1; }; }""")
        self.assertTrue(page.is_visible("#do-print"))
        self.assertEqual(page.evaluate("document.activeElement.id"), "do-print")
        page.click("#do-print")
        self.assertEqual(page.evaluate("window.__printed"), 1)
        self.assertEqual(page.evaluate("window.__fitted"), 1)

    def test_ボタンは紙に出ない(self) -> None:
        page = self._open(4)
        page.emulate_media(media="print")
        self.assertFalse(page.is_visible("#do-print"))
        self.assertFalse(page.is_visible(".noprint"))
        self.assertEqual(self._pages(page), 1)

    def test_開いたタブなら閉じるで閉じる(self) -> None:
        """カレンダー/履歴の [印刷] は window.open で開く。そのタブは閉じられる。"""
        opener = self._open(4)
        path = Path(self._tmp.name) / "m4.html"
        with opener.expect_popup() as info:
            opener.evaluate("(url) => window.open(url, '_blank')", path.as_uri())
        popup = info.value
        popup.wait_for_selector("#do-close")
        with popup.expect_event("close", timeout=3000):
            popup.click("#do-close", no_wait_after=True)
        self.assertTrue(popup.is_closed())

    def test_閉じられないタブなら閉じ方を伝える(self) -> None:
        """アドレス欄から開いたタブは、ブラウザが閉じさせない。黙らない。"""
        page = self._open(4)
        page.click("#do-close")
        page.wait_for_selector("#close-note:not([hidden])")
        self.assertIn("タブの×で閉じてください", page.inner_text("#close-note"))

    def test_余白を足されても1枚(self) -> None:
        """ダイアログで「最小」「カスタム」を選ばれた場合。"""
        for margin in ("4mm", "10mm"):
            page = self._open(6, f"@page {{ margin: {margin}; }}")
            self.assertEqual(self._pages(page), 1, margin)


if __name__ == "__main__":
    unittest.main()
