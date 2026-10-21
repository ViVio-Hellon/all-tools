"""梱包資材重量計算: 画面の配線を**本物のブラウザ**で確かめる (段階1)

前は `views/gw.js` の字を探して「仕組みが入っていること」だけを見ていました
(`test_web_gw_screen`・`test_gw_print`・`test_gw_spin` の一部)。ここでは画面で打って押します:

    - **前のロットの資材を残さない**: 受注で決まったロットのあとに、受注が決まらないロット
      (当たらない・引当が無い・引当が複数で選ぶ前・受注が SIKAODR に無い)を打つと、
      ＶＣは外れ・合紙は付いた既定・縦バンドアングルは本数どおり・自動の印も消える。
      **標準の資材(ダンプレート・外装紙・バンド・ポリシート・ハードボード)は外さない**
    - 梱包図を出す: まだ計算していなければ先に計算して図へ送る。計算が通らなければ黙らず
      「直すところ」へ送る
    - 印刷: 計算するまでは押せず、押せない理由を字で出す。押すとそのまま印刷の画面を出す紙が開く
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _gw_master  # noqa: E402
from tests._other_screen_browser import ORIGIN, ScreenBrowserCase  # noqa: E402
from tests.test_web_gw_screen import HIKI_COLUMNS, LOT_COLUMNS, ODR_COLUMNS  # noqa: E402

#: 標準の資材(どの梱包でも使う)。受注が決まらなくても**外さない**
STANDARD = ("dunplate", "outer_paper", "band", "poly_sheet", "hardboard")


def _lot(lot_no: str, order_no: str) -> tuple:
    return (lot_no, order_no, "K1", "52S", "R", 20.0, 1528.0, 3053.0, "H283", "JISNﾌﾗﾂﾄANF",
            "A-GCT-B", "取引先", "ﾅﾒｶﾜｱﾙﾐ(ｶ", "送り先", "9", "0", "C1")


class GwBrowserCase(ScreenBrowserCase):
    def open_gw(self):
        return self.open_screen("/gw", "#dim-count")

    def checked(self, page, selector: str) -> bool:
        return page.is_checked(selector)


class LeftOverMaterialTests(GwBrowserCase):
    """受注で決まったロット(合紙なし・ＶＣあり)を引いてから、決まらないロットを打つ。"""

    #: ロット → (受注, 引当)。引当の受注が SIKAODR に無いものも混ぜる
    LOTS = {
        "H5422S0": [("AB1234", 100)],                  # 受注で決まる(ＶＣあり・合紙 "0")
        "H5422S1": [],                                 # 引当が無い
        "H5422S2": [("AB2234", 60), ("AB1234", 40)],   # 引当が複数(選ぶ前)
        "H5422S3": [("ZZ0000", 100)],                  # 受注が SIKAODR に無い
    }

    def setUp(self) -> None:
        super().setUp()
        ref = self.tmp / "ref"
        _gw_master.write(ref)
        self.write(ref / "SIKALOT.sqlite3", LOT_COLUMNS,
                   [_lot(lot, (allocs or [("", 0)])[0][0]) for lot, allocs in self.LOTS.items()])
        self.write(ref / "SIKAHIKI.sqlite3", HIKI_COLUMNS,
                   [(lot, order, qty, "", f"6071700{i}") for lot, allocs in self.LOTS.items()
                    for i, (order, qty) in enumerate(allocs)])
        self.write(ref / "SIKAODR.sqlite3", ODR_COLUMNS, [
            ("AB2234", "VE-20NF", "VE-20NF", "1", 250.04314, "取引先", "納入先", "送り先", "1P1186", ""),
            ("AB1234", "テストVC", "", "0", 1626.87679, "取引先2", "納入先2", "送り先2", "1P0001", "1"),
        ])
        self.post("/api/settings/paths", {"gw_reference_dir": str(ref), "password": "nisk"})

    @staticmethod
    def write(path: Path, columns, rows) -> None:
        conn = sqlite3.connect(str(path))
        with conn:
            cols = ", ".join(f'"{c}"' for c in columns)
            marks = ", ".join("?" for _ in columns)
            conn.execute(f'CREATE TABLE "仕掛" ({cols})')
            conn.executemany(f'INSERT INTO "仕掛" VALUES ({marks})', rows)
        conn.close()

    def lookup(self, page, lot_no: str) -> None:
        mark = len(self.sent)
        self.type_in(page, "lot-no", lot_no)              # 7桁そろうと引く
        self.wait_until(page, lambda: any(p == "/api/gw/lot" and b.get("lot_no") == lot_no
                                          for p, b in self.sent[mark:]), f"{lot_no} を引く要求")
        page.wait_for_function("document.getElementById('search-state').textContent === ''",
                               timeout=8000)
        page.wait_for_timeout(300)

    def materials(self, page) -> dict:
        return page.evaluate("""() => ({
            useVc: document.getElementById('use-vc').checked,
            vcA: document.getElementById('vc-a').checked,
            vcB: document.getElementById('vc-b').checked,
            nameA: document.getElementById('vc-name-a').value,
            nameB: document.getElementById('vc-name-b').value,
            spec: document.getElementById('pack-spec').value,
            flags: Object.fromEntries([...document.querySelectorAll('[data-flag]')]
                       .map((el) => [el.dataset.flag, el.checked])),
            auto: document.querySelectorAll('.is-auto').length,
        })""")

    def test_受注が決まらないロットを打つと前のロットの資材を残さない(self) -> None:
        page = self.open_gw()
        for lot_no, why in (("Z999999", "当たらない"), ("H5422S1", "引当が無い"),
                            ("H5422S2", "引当が複数で選ぶ前"), ("H5422S3", "受注が SIKAODR に無い")):
            with self.subTest(why=why):
                self.lookup(page, "H5422S0")
                before = self.materials(page)
                # 前のロット: 受注で ＶＣ が付き、合紙は外れ、自動の印が付いている
                self.assertTrue(before["useVc"], before)
                self.assertEqual(before["nameA"], "テストVC")
                self.assertFalse(before["flags"]["interleaf"], before)
                self.assertGreater(before["auto"], 0, "自動で決めた印が付いていません")
                # 縦バンドを掛けないのに、アングルに印を付けておく(本数どおりへ戻るかを見る)
                page.fill("#dim-vertical_bands", "0")
                page.check('[data-flag="angle"]')
                self.lookup(page, lot_no)
                after = self.materials(page)
                self.assertEqual((after["useVc"], after["vcA"], after["vcB"]), (False, False, False),
                                 f"前のロットのＶＣが残っています: {after}")
                self.assertEqual((after["nameA"], after["nameB"]), ("", ""))
                self.assertEqual(after["spec"], "", "前のロットの梱包仕様が残っています")
                self.assertTrue(after["flags"]["interleaf"], "合紙が配られたときの姿(付いた)に戻りません")
                self.assertFalse(after["flags"]["angle"], "縦バンドアングルが縦バンドの本数どおりになりません")
                self.assertEqual(after["auto"], 0, "自動で決めた印が残っています")
                for name in STANDARD:
                    self.assertTrue(after["flags"][name], f"標準の資材 {name} まで外しています")
        self.assertEqual(self.errors, [])


class FigureAndPrintTests(GwBrowserCase):
    """梱包図を出す・印刷。計算が通るよう資材重量マスタを置く。"""

    DIMS = {"thickness_mm": "1.99", "width_mm": "81", "length_mm": "1000", "count": "40",
            "vertical_bands": "2", "horizontal_bands": "2", "pack_height_mm": "200",
            "pallet_weight_kg": "18"}

    def setUp(self) -> None:
        super().setUp()
        _gw_master.write(self.tmp / "ref")

    def fill_dims(self, page) -> None:
        for name, value in self.DIMS.items():
            page.fill(f"#dim-{name}", value)
            page.dispatch_event(f"#dim-{name}", "change")

    @staticmethod
    def in_view(page, selector: str, *, whole: bool = False) -> bool:
        """見えるところにあるか(`whole` なら頭から終わりまで全部)。"""
        return page.evaluate(f"""() => {{
            const r = document.querySelector({selector!r}).getBoundingClientRect();
            if (r.height === 0) return false;
            return {'true' if whole else 'false'} ? r.top >= 0 && r.bottom <= innerHeight
                                                   : r.top >= 0 && r.top < innerHeight;
        }}""")

    def test_梱包図を出すは計算してから図へ送り_通らなければ直すところへ送る(self) -> None:
        page = self.open_gw()
        page.set_viewport_size({"width": 1400, "height": 480})   # 直すところが押した所の下に隠れる高さ
        # 寸法が空のまま押す: 図は描けない。黙って終わらず、直すところへ送る
        mark = len(self.sent)
        page.click("#show-figure")
        page.wait_for_selector("#calc-problems:not([hidden])", timeout=8000)
        self.assertIn("/api/gw/calculate", self.paths(mark), "押しても計算しません")
        page.wait_for_function("document.getElementById('calc-state').textContent.includes('直すところ')",
                               timeout=5000)
        page.wait_for_timeout(700)                      # なめらかに送る
        self.assertTrue(self.in_view(page, "#calc-problems", whole=True), "直すところが見えるところへ送りません")
        self.assertTrue(page.is_hidden("#figure"))
        # 寸法を入れて、計算せずに押す: 先に計算して、図を出してそこへ送る
        page.evaluate("window.scrollTo(0, 0); document.querySelector('.work')?.scrollTo(0, 0)")
        self.fill_dims(page)
        mark = len(self.sent)
        page.click("#show-figure")
        page.wait_for_selector("#figure:not([hidden])", timeout=8000)
        self.assertIn("/api/gw/calculate", self.paths(mark))
        page.wait_for_timeout(900)
        self.assertTrue(self.in_view(page, "#figure"), "図を出しても見えるところへ送りません")
        self.assertEqual(self.errors, [])

    def test_印刷は計算するまで押せず理由を字で出し_押すと印刷の画面を出す紙が開く(self) -> None:
        self.ctx.add_init_script("window.print = () => { window.__printed = true; };")
        page = self.open_gw()
        self.assertTrue(page.is_disabled("#print-gw"))
        self.assertTrue(page.is_visible("#print-why"), "押せない理由が字で出ていません")
        self.assertIn("先に「計算」を押すと印刷できます", page.inner_text("#print-why"))
        self.fill_dims(page)
        page.click("#calc")
        page.wait_for_function("!document.getElementById('print-gw').disabled", timeout=8000)
        self.assertTrue(page.is_hidden("#print-why"), "押せるのに理由が出たままです")
        page.fill("#print-packs", "12")
        with page.expect_popup() as info:
            page.click("#print-gw")
        paper = info.value
        paper.wait_for_load_state("load")
        url = paper.url.split(ORIGIN, 1)[1]
        self.assertTrue(url.startswith("/report/gw?"), url)
        from urllib.parse import parse_qs, urlsplit
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query.get("print"), ["1"], "紙の窓がそのまま印刷の画面を出しません")
        self.assertEqual(query.get("packs"), ["12"])
        self.assertEqual(self.errors, [])


class StaffModalTests(ScreenBrowserCase):
    def test_作業者を選ぶ窓は広い窓で班を横に並べる(self) -> None:
        page = self.open_entry()
        page.click("#open-staff")
        page.wait_for_selector("#staff-modal:not([hidden])")
        width = page.evaluate("document.querySelector('#staff-modal .modal__box').getBoundingClientRect().width")
        self.assertGreater(width, 1000, "作業者を選ぶ窓がふつうの幅(720)のままです")
        columns = page.evaluate(
            "getComputedStyle(document.getElementById('staff-teams')).gridTemplateColumns.split(' ').length")
        self.assertGreater(columns, 3, "班が横に並びません")


if __name__ == "__main__":
    import unittest

    unittest.main()
