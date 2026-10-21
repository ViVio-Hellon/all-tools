"""梱包資材重量計算(GW): Gコースのロット (本物のブラウザ。段階1)

前は字を探していました(`test_g_course` の「画面に置き場所がある」── `gw.js` に
`body.size.thickness_mm` と `paintGCourse` と `g_course` の字があるか)。ここでは本当に
ロットを引いて、計算して、紙を開いて確かめます:

    - 寸法の欄には **BOX最終実績** の寸法が入る(引いたロットの製造の寸法ではない)
    - 寸法の上に一言(製造の寸法も言う)・寸法の欄に縁
    - 紙にも「Gコース(GSS)」と書く

日報入力のほうの印は `test_entry_screen_browser` の GCourseTests。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _gw_master  # noqa: E402
from tests._entry_browser import ORIGIN, EntryBrowserCase  # noqa: E402
from tests.test_g_course import _Masters  # noqa: E402  SIKALOT など3つのマスタを本物の形で

#: 計算に要る残りの欄(ロットとオーダーから決まらないもの)
TYPED = (("count", "40"), ("vertical_bands", "2"), ("horizontal_bands", "2"),
         ("pack_height_mm", "400"), ("pallet_weight_kg", "18"))


class GwGCourseTests(_Masters, EntryBrowserCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_masters()
        _gw_master.write(self.tmp / "ref")         # 資材重量(計算を断らせない)

    def test_寸法はBOX最終実績_一言と縁_紙にも書く(self) -> None:
        page = self.watch(self.ctx.new_page())
        page.goto(f"{ORIGIN}/gw", wait_until="load")
        page.wait_for_selector("#lot-no")
        page.wait_for_timeout(400)
        self.assertTrue(page.locator("#g-course-note").is_hidden())
        page.fill("#lot-no", "H5422S0")
        page.locator("#lot-no").press("Tab")
        page.wait_for_selector("#g-course-note:not([hidden])", timeout=5000)
        self.assertEqual([self.value(page, f"dim-{n}") for n in ("thickness_mm", "width_mm", "length_mm")],
                         ["8", "1250", "2500"], "製造の寸法(20×1528×3053)が入っています")
        self.assertIn("製造 20.000×1528.0×3053.0", page.text_content("#g-course-text"))
        self.assertTrue(page.evaluate(
            "document.querySelector('[data-dim=\"thickness_mm\"]').classList.contains('is-gsize')"))
        for name, text in TYPED:
            page.fill(f"#dim-{name}", text)
        page.uncheck("#use-vc")
        page.click("#calc")
        page.wait_for_function("!document.getElementById('print-gw').disabled", timeout=5000)
        with page.expect_popup() as opened:
            page.click("#print-gw")
        paper = opened.value
        paper.wait_for_load_state("load")
        self.assertIn("Gコース(GSS)のロット ── 寸法は BOX最終実績", paper.text_content("body"))
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
