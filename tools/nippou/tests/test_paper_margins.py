"""紙の端から5mm以上内側に刷る (v3.83.0)

    印刷プレビューでは収まっているが印刷すると見切れています
    プリンターは紙の縁から約4mmの所には印刷できません。
    文字・罫線・バーをすべて紙の端から5mm以上内側に置いてください。

余白を `@page { margin }` に任せていたので、印刷画面で「余白: なし」を
選ぶと紙の端から描いていました(`nippou/reporting/paper.py`)。

ここで押さえるのは、**印刷画面の余白が「なし」でも**:

    1. 文字・罫線・背景のいちばん外側が、紙の端から 5mm 以上内側にある
    2. ブラウザが全体を縮めない(縮めると余白も縮む)
    3. 紙1枚ぶんが紙1枚に収まる(はみ出した続きには余白が無い)

**本物のブラウザ(Chromium)で印刷の見た目を出して測ります。** Playwright が
無い環境では、CSS の決まりだけを見ます。中身は**いちばん詰まった場合**
(長い文字・「◯梱包ぶん」の段・集計の段・12行を超える行)にしてあります。
"""
from __future__ import annotations

import glob
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.models import DetailRecord, HeaderRecord, PackingReport  # noqa: E402
from nippou.logic.packing_agg import StopRow  # noqa: E402
from nippou.reporting import gw_print, paper, print_format  # noqa: E402

MM = 96 / 25.4
#: A4 横(mm)
PAGE_MM = (297, 210)


def gw_html(packs: int = 12) -> str:
    """梱包資材重量計算の紙。**いちばん詰まった形。**"""
    r = gw_print.row
    long_text = "ｱｻﾋｳﾝﾕ(ｶ)ﾅｺﾞﾔｺｳﾘﾕｳｾﾝﾀｰ ﾀﾞｲﾆｿｳｺ ﾆｼｶﾞﾜ ﾌﾞﾂﾘﾕｳｾﾝﾀｰ"
    data = gw_print.GwPrintData(
        product=[r("LotNo", "H573D51"), r("オーダーNo", "04103818"), r("材質", "F52S"),
                 r("調質", "R"), r("用途コード", "H162"), r("包装仕様", "7P0106"),
                 r("単重(kg)", "100.01725"),
                 r("コース", "HOT FS4 ST2 PSW TUR ANF AIM VC KEN SLT CUT INS PKG"),
                 r("得意先", "ﾆﾂｹｲｻﾝｷﾞﾖｳ(ｶ ﾄｳｷﾖｳｼｼﾔ"), r("納入先", "ﾀｲﾜﾝ ｶｵｼﾕﾝ"),
                 r("送り先", long_text)],
        dimensions=[r("板厚", 8.0, "mm"), r("板幅", 1528.0, "mm"), r("板丈", 3053.0, "mm"),
                    r("梱包締枚数", 10, "枚"), r("縦バンド本数", 2, "本"),
                    r("横バンド本数", 4, "本"), r("梱包高さ(パレット含)", 253.0, "mm"),
                    r("パレット+蓋の重量", 88.0, "kg"), r("積み形態", "2山積み"),
                    r("積み枚数", 10, "枚", sub="1山なら梱包締枚数そのまま")],
        materials=[r("ダンプレート", 0.18, "kg"), r("外装紙", 0.85, "kg"),
                   r("合紙", 1.18, "kg"), r("バンド", 0.70, "kg", sub="PETバンド"),
                   r("ポリシート", 1.42, "kg"), r("アングル", 2.05, "kg"),
                   r("ハードボード", 22.27, "kg"),
                   r("VCフィルム", 10.92, "kg", sub="上面 208PRFI / 下面 208PRFI")],
        totals=[r("梱包資材重量", 39.56, "kg", strong=True), r("積合せ重量", 12.5, "kg"),
                r("風袋総重量", 127.56, "kg", strong=True), r("Aインプット重量", 4379.4, "kg"),
                r("GW(総重量)", 4506.96, "kg", strong=True)],
        pack_count=packs,
        pack_rows=[r(name, 1.5 * packs, "kg") for name in (
            "ダンプレート", "外装紙", "合紙", "バンド", "ポリシート", "アングル",
            "ハードボード", "VCフィルム")]
        + [r("資材計", 474.72, "kg", strong=True), r("風袋総重量", 1530.72, "kg", strong=True)],
        subject="LotNo H573D51 / オーダーNo 04103818")
    return gw_print.build_html(data, generated_at="2026-09-25 10:00:00")


def daily_html(pages: int = 2) -> str:
    """日報の紙。**12行ぜんぶ + はみ出した行 + 集計の段 + 長い理由。**"""
    key = dict(report_date="2026年9月25日", line="LVC", shift="2直", page=1)
    header = HeaderRecord(**key, worker="山田 太郎",
                          reason="3行目: 設備調整のため停止 / 7行目: 材料待ち(前工程の遅れ)")
    rows = [DetailRecord(**key, row_no=n, lot="N7131T0", zai="F52S-R",
                         siz="8.00×1528×3053", ken="120", kz="15", kh="00", sz="22",
                         sh="50", hit="3", ai="有", mai="10", tut="12", vc="両面",
                         et="ｽﾄｱ EX ｽﾎﾟｯﾄ", s="ｱ", th="60", ss="ｲ", ths="15",
                         sth="ｳ", tht="5", con="1200", wei="14379.4")
            for n in list(range(1, 13)) + [13, 14]]
    summary = PackingReport(work_date="2026年9月25日", line_name="LVC", shift="2直",
                            pages=pages, total_quantity=14400, total_weight=172552.8,
                            total_lot_count=14, coefficient_lot_count=13.25,
                            work_time=420, equipment_stop_total=60,
                            setup_stop_total=15, handling_stop_total=5,
                            operating_time=400, operating_rate=95.2, productivity=12.34)
    stops = [(StopRow(stop_no=1, stop_code=c, stop_reason=f"設備停止 {c} 長い名前の停止項目",
                      stop_minutes=m), 2)
             for c, m in (("ｱ", 60), ("ｲ", 45), ("ｳ", 30), ("ｴ", 15), ("ｵ", 10), ("ｶ", 5))]
    return print_format.build_shift_html([(header, rows)] * pages,
                                         generated_at="2026-09-25 10:00",
                                         summary=summary, stops=stops)


class RuleTests(unittest.TestCase):
    """CSS の決まり(ブラウザが無くても見られるもの)。"""

    def test_余白は5mm以上(self) -> None:
        self.assertGreaterEqual(paper.INSET_MM, paper.MIN_INSET_MM)
        self.assertGreaterEqual(paper.MIN_INSET_MM, 5)

    def test_余白をpageの規則に任せない(self) -> None:
        """「余白: なし」で無視されるので、紙の中身が余白を持つ。"""
        for name, text, unit in (("GW", gw_html(), ".paper"),
                                 ("日報", daily_html(1), ".sheet")):
            with self.subTest(name=name):
                self.assertIn("@page { size: A4 landscape; margin: 0; }", text)
                self.assertIn(f"{unit} {{ box-sizing: border-box; padding: {paper.INSET_MM}mm; }}",
                              text)
                self.assertNotRegex(text, r"@page\s*\{[^}]*margin:\s*[1-9]")

    def test_GWの中身は余白の箱に入っている(self) -> None:
        text = gw_html()
        self.assertIn('<div class="paper">', text)
        # 画面だけの帯(印刷する)は箱の外。紙には出ない
        self.assertLess(text.index('class="toolbar"'), text.index('<div class="paper">'))


def _chromium():
    """Chromium を立てる。無ければ None(その試験は飛ばす)。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, None
    pw = sync_playwright().start()
    for path in [None] + sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome")):
        try:
            return pw, (pw.chromium.launch(executable_path=path) if path
                        else pw.chromium.launch())
        except Exception:                          # noqa: BLE001 - 次を試す
            continue
    pw.stop()
    return None, None


_MEASURE = """(selector) => {
  const out = [];
  for (const unit of document.querySelectorAll(selector)) {
    const u = unit.getBoundingClientRect();
    const box = {left: 1e9, top: 1e9, right: -1e9, bottom: -1e9};
    for (const el of unit.querySelectorAll('*')) {
      const s = getComputedStyle(el);
      if (s.display === 'none' || s.visibility === 'hidden') continue;
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height) continue;
      box.left = Math.min(box.left, r.left); box.top = Math.min(box.top, r.top);
      box.right = Math.max(box.right, r.right); box.bottom = Math.max(box.bottom, r.bottom);
    }
    const walker = document.createTreeWalker(unit, NodeFilter.SHOW_TEXT);
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      if (!n.textContent.trim()) continue;
      const el = n.parentElement;
      const s = el && getComputedStyle(el);
      if (!s || s.display === 'none' || s.visibility === 'hidden') continue;
      const range = document.createRange(); range.selectNodeContents(n);
      for (const r of range.getClientRects()) {
        if (!r.width || !r.height) continue;
        box.left = Math.min(box.left, r.left); box.top = Math.min(box.top, r.top);
        box.right = Math.max(box.right, r.right); box.bottom = Math.max(box.bottom, r.bottom);
      }
    }
    out.push({top_of_unit: u.top, height: u.height, ...box});
  }
  return {units: out, width: document.documentElement.scrollWidth};
}"""


class PrintedEdgeTests(unittest.TestCase):
    """**本物のブラウザで印刷の見た目を出して測る**(余白「なし」= 紙の端から描く場合)。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.pw, cls.browser = _chromium()
        if cls.browser is None:
            raise unittest.SkipTest("Chromium(Playwright)がありません")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.pw.stop()

    def printed(self, html: str, selector: str):
        w, h = round(PAGE_MM[0] * MM), round(PAGE_MM[1] * MM)
        page = self.browser.new_page(viewport={"width": w, "height": h})
        self.addCleanup(page.close)
        page.set_content(html)
        page.emulate_media(media="print")
        got = page.evaluate(_MEASURE, selector)
        pdf = page.pdf(prefer_css_page_size=True)
        return got, w, h, len(re.findall(rb"/Type\s*/Page[^s]", pdf))

    def check(self, html: str, selector: str, units: int) -> None:
        got, w, h, pdf_pages = self.printed(html, selector)
        self.assertLessEqual(got["width"], w, "紙より広いので、ブラウザが全体を縮めます")
        self.assertEqual(len(got["units"]), units)
        self.assertEqual(pdf_pages, units, "紙1枚ぶんが紙1枚に収まっていません")
        for i, u in enumerate(got["units"], start=1):
            with self.subTest(sheet=i):
                self.assertLessEqual(u["height"], h + 0.5, "1枚ぶんが紙の高さを超えています")
                # 1枚ぶんの箱は紙の上端から始まる(2枚目からは改ページ)。
                # 上下は、その紙の端から測る
                edges = {"左": u["left"] / MM,
                         "右": (w - u["right"]) / MM,
                         "上": (u["top"] - u["top_of_unit"]) / MM,
                         "下": (h - (u["bottom"] - u["top_of_unit"])) / MM}
                for side, mm in edges.items():
                    self.assertGreaterEqual(
                        mm, paper.MIN_INSET_MM,
                        f"{side}の端から {mm:.1f}mm しかありません(5mm 以上内側に)")

    def test_GWの紙(self) -> None:
        self.check(gw_html(packs=12), ".paper", 1)

    def test_GWの紙_1梱包(self) -> None:
        self.check(gw_html(packs=1), ".paper", 1)

    def test_日報の紙(self) -> None:
        self.check(daily_html(pages=3), ".sheet", 3)


if __name__ == "__main__":
    unittest.main()
