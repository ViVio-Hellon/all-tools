"""日報入力: 欄の動き (本物のブラウザ。段階1)

前は字を探していました(`test_web_entry_screen`・`test_stops5`・`test_line_gate`・
`test_check_on_screen`・`test_g_course` の一部)。ここでは本当に打って・押して確かめます:

    - etc のボタンは**1度押しで効く**。行を選んでいなければ打ちかけの行(無ければ1行目)
    - ロット№は**欄を離れなくても**7桁そろった時点で引く。1〜6桁では引かない・離れても同じ番号は二度引かない
    - 狭い画面では、表の上の2枚・下の2枚が縦に落ちる
    - 作業停止①〜⑤は1組ずつ出し、見出しで切り替える。打って次の組へ進めばその組が出る
    - この端末のラインが決まるまで、表と作業者のカードを伏せる(最初の答えを待たずに)
    - 「この直をチェック」は画面の中身を送る(打った休憩が数えられる)
    - Gコースのロットを引くと、寸法の欄に印が付き、製造の寸法を言う
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._entry_browser import ORIGIN, EntryBrowserCase  # noqa: E402
from tests.test_g_course import _Masters  # noqa: E402  SIKALOT など3つのマスタを本物の形で


def focused(page) -> str:
    return page.evaluate("document.activeElement && document.activeElement.id")


def shown(page, field: str) -> bool:
    return page.locator(f"#{field}").is_visible()


class EtcButtonTests(EntryBrowserCase):
    """etc のボタン。**押せなくしない** ── 無効のボタンは click を出さず「2回押して効く」に見えた。"""

    def press(self, page, key: str = "store") -> None:
        mark = len(self.sent)
        page.locator(f'[data-mark="{key}"]').click(force=True, timeout=2000)
        self.wait_until(page, lambda: any(p == "/api/entry/mark" for p, _ in self.sent[mark:]),
                        "etc の印の要求")
        page.wait_for_timeout(300)

    def test_行を選ばずに1度押せば1行目に付く(self) -> None:
        page = self.open_entry()
        self.assertTrue(page.locator('[data-mark="store"]').is_enabled(), "押せなくしています")
        self.press(page)
        self.assertEqual(page.text_content("#marks-row").strip(), "1")
        self.assertEqual(self.value(page, "ET1"), "ストア")
        self.assertEqual(self.errors, [])

    def test_行を選んでいなければ打ちかけの行に付く_etc欄では数えない(self) -> None:
        """何か入っているいちばん下の行。**etc 欄そのものは数えない**(押すたびに下へずれる)。"""
        res = self.post("/api/entry/save", {
            "rows": {"1": {"HIT": "2"}, "3": {"HIT": "3"}, "5": {"ET": "EX"}},
            "header": {"worker": "山田"}, "checks": {}, "draft": True})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:300])
        page = self.open_entry()
        self.assertEqual(self.value(page, "HIT3"), "3")
        self.assertEqual(self.value(page, "ET5"), "EX")
        self.press(page)
        self.assertEqual(page.text_content("#marks-row").strip(), "3")
        self.assertEqual(self.value(page, "ET3"), "ストア")


class LotLookupTimingTests(EntryBrowserCase):
    """ロット№は**欄を離れなくても引く**(VBA の `LOT*_Change` と同じ、7桁そろった時点)。"""

    def lookups(self) -> int:
        return sum(1 for p, _ in self.sent if p == "/api/entry/lot")

    def test_7桁そろった時点で引き_1から6桁と同じ番号では引かない(self) -> None:
        page = self.open_entry()
        self.type_in(page, "LOT1", "N7131T")
        page.wait_for_timeout(500)
        self.assertEqual(self.lookups(), 0, "7桁そろう前に引いています")
        page.locator("#LOT1").press_sequentially("0", delay=20)
        self.wait_until(page, lambda: self.lookups() == 1, "離れずに7桁で引く")
        self.assertEqual(focused(page), "LOT1")
        # 離れる ── 打ち終わった時点でもう引いた番号なので、引き直さない
        page.wait_for_timeout(300)
        page.locator("#LOT1").press("Tab")
        page.wait_for_timeout(600)
        self.assertEqual(self.lookups(), 1, "同じ番号を二度引いています")

    def test_変換が確定したときにも引く(self) -> None:
        page = self.open_entry()
        page.click("#LOT1")
        page.evaluate("""() => {
            const el = document.getElementById('LOT1');
            el.value = 'Ｎ７１３１Ｔ０';
            el.dispatchEvent(new CompositionEvent('compositionend', {data: 'Ｎ７１３１Ｔ０', bubbles: true}));
        }""")
        self.wait_until(page, lambda: self.lookups() == 1, "変換の確定で引く")
        self.assertEqual(self.value(page, "LOT1"), "N7131T0")
        self.assertEqual(focused(page), "LOT1")


class NarrowScreenTests(EntryBrowserCase):
    def test_狭い画面では表の上と下の2枚が縦に落ちる(self) -> None:
        """横に詰めると、どちらも読めない幅になります。"""
        page = self.open_entry()
        boxes = """sel => [...document.querySelector(sel).children]
                     .filter(el => el.offsetParent !== null)
                     .map(el => { const r = el.getBoundingClientRect(); return [r.left, r.top]; })"""
        for sel in (".entry-panels", ".entry-foot"):
            with self.subTest(wide=sel):
                (x1, y1), (x2, y2) = page.evaluate(boxes, sel)[:2]
                self.assertLess(x1, x2)               # 広い画面では横に並ぶ
                self.assertAlmostEqual(y1, y2, delta=2)
        page.set_viewport_size({"width": 900, "height": 900})
        page.wait_for_timeout(200)
        for sel in (".entry-panels", ".entry-foot"):
            with self.subTest(narrow=sel):
                (x1, y1), (x2, y2) = page.evaluate(boxes, sel)[:2]
                self.assertAlmostEqual(x1, x2, delta=2)
                self.assertLess(y1, y2)


class StopSlotTests(EntryBrowserCase):
    """作業停止①〜⑤ ── **表には1組ずつ出す**(10欄を横に並べると表が横に流れる)。"""

    def test_1組ずつ出して見出しで切り替える(self) -> None:
        page = self.open_entry()
        self.assertTrue(shown(page, "S1"))
        for field in ("SS1", "STH1", "S41", "S51"):          # 欄の名前は 記号+行
            self.assertFalse(shown(page, field), field)
        page.click('[data-stop-to="2"]')
        self.assertTrue(shown(page, "SS1"))
        self.assertTrue(shown(page, "THS1"))
        self.assertFalse(shown(page, "S1"))
        self.assertEqual(page.get_attribute('[data-stop-to="2"]', "aria-pressed"), "true")

    def test_打って次の組へ進むとその組が出る_行の停止の欄から戻れる(self) -> None:
        page = self.open_entry()
        page.click('[data-stop-to="3"]')
        self.type_in(page, "STH1", "レ")
        self.type_in(page, "THT1", "15")
        page.keyboard.press("Enter")                  # ③の時間 → ④の記号
        page.wait_for_function("document.activeElement && document.activeElement.id === 'S41'",
                               timeout=3000)
        self.assertTrue(shown(page, "S41"))
        self.assertFalse(shown(page, "STH1"))
        # 行の「停止」の欄に、どの組に入っているかと合計の分
        self.assertIn("15分", page.text_content('[data-stopsum-row="1"]'))
        page.click('[data-stopsum-row="1"] [data-stop-jump="3"]')
        self.assertTrue(shown(page, "STH1"))
        self.assertEqual(focused(page), "STH1")

    def test_記号を空のままEnterなら残りの組は飛ばす(self) -> None:
        page = self.open_entry()
        page.click("#S1")
        page.keyboard.press("Enter")
        page.wait_for_timeout(200)
        here = page.evaluate("document.activeElement.dataset.family || ''")
        self.assertNotIn(here, {"S", "TH", "SS", "THS", "STH", "THT", "S4", "TH4", "S5", "TH5"},
                         "空の記号から停止の組をくぐらせています")
        self.assertTrue(shown(page, "S1"), "飛ばしたのに組が切り替わっています")


class LineGateTests(EntryBrowserCase):
    """この端末のラインが決まるまで、表と作業者のカードを伏せる。"""

    terminal_line = None

    def test_最初の答えを待たずに表と作業者のカードを伏せる(self) -> None:
        # 開いたあとの答え(`/api/entry/state`)を止める。**サーバが描いた関門をそのまま効かせる**
        self.hold = ("/api/entry/state",)
        page = self.watch(self.ctx.new_page())
        page.goto(f"{ORIGIN}/", wait_until="load")
        page.wait_for_selector("#grid-body")
        page.wait_for_timeout(300)
        self.assertTrue(page.locator("#terminal-line-note").is_visible())
        self.assertTrue(page.evaluate("document.body.classList.contains('needs-line')"))
        for field in ("LOT1", "MAI1", "hd-worker"):
            with self.subTest(field=field):
                self.assertTrue(page.locator(f"#{field}").is_disabled(), f"{field} が打てます")
        opacity = page.evaluate(
            "getComputedStyle(document.querySelector('[data-step-for=\"worker\"]')).opacity")
        self.assertAlmostEqual(float(opacity), 0.45, places=2)
        self.assertTrue(page.locator("#start-shift").is_hidden())   # 作業者の帯よりラインが先


class CheckShiftTests(EntryBrowserCase):
    def test_この直をチェックは画面の中身で見る(self) -> None:
        """打った休憩は「保存(確定)」を押す前でも数える(「入れてるんだけどずっと出てますね」)。"""
        page = self.open_entry()
        for field, text in (("KZ1", "09"), ("KH1", "00"), ("SZ1", "10"), ("SH1", "00"),
                            ("TH1", "60")):
            self.type_in(page, field, text)
        mark = len(self.sent)
        page.click("#check-shift")
        page.wait_for_selector("#check-panel:not([hidden])", timeout=5000)
        sent = [b for p, b in self.sent[mark:] if p == "/api/entry/verify"]
        self.assertTrue(sent)
        self.assertEqual(sent[-1].get("rows", {}).get("1", {}).get("TH"), "60")
        self.assertIn("まだ保存していない中身", page.text_content("#check-head"))


class GCourseTests(_Masters, EntryBrowserCase):
    """Gコースのロット(寸法は BOX最終実績)。引いたときに言い、寸法の欄に印。"""

    def setUp(self) -> None:
        super().setUp()
        self.make_masters()

    def test_引くと寸法の欄にGの印と製造の寸法の一言(self) -> None:
        page = self.open_entry()
        self.assertTrue(page.locator('[data-gmark-row="1"]').is_hidden())
        self.type_in(page, "LOT1", "H5422S0")
        page.wait_for_selector('[data-gmark-row="1"]:not([hidden])', timeout=5000)
        self.assertEqual(self.value(page, "SIZ1"), "8.000×1250.0×2500.0")
        self.assertIn("Gコース(GSS)", page.get_attribute('[data-gmark-row="1"]', "title"))
        self.assertTrue(page.locator('[data-gmark-row="2"]').is_hidden())
        page.wait_for_function(
            "document.body.innerText.includes('製造 20.000×1528.0×3053.0')", timeout=5000)


if __name__ == "__main__":
    unittest.main()
