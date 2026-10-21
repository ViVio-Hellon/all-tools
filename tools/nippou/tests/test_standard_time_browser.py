"""標準作業時間: 梱包力の面と「しくみ」への飛び先を本物のブラウザで (段階1)

前は `standard_time.js` の字を探していました(`test_packing_power` の `test_画面` の JS の部分・
`test_standard_guide` の `test_しくみから飛べる` の JS の部分)。ここでは押して確かめます:

    - 梱包力の面を開くと、その時に初めて梱包力を訊きに行き(`tab:select`)、答えの数を出す
    - 「CSVに書き出す」で書き出しを頼み、どこへ書いたかを面に残す
    - 「しくみ」を押すと、閉じていた「しくみ」の面を開いて、その節まで連れて行く
      (閉じた面の中へは、ブラウザだけでは飛べない)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._screen_browser import ScreenBrowserCase  # noqa: E402


class StandardTimeBrowserTests(ScreenBrowserCase):
    def setUp(self) -> None:
        super().setUp()
        # 梱包力の答えは試験が決める(数の出し方は `test_packing_power` が見ている)
        self.answers["/api/standard-time/power"] = self.answer_power

    def answer_power(self, req) -> tuple:
        path = req.url.split("app.test", 1)[1].split("?")[0]
        if path == "/api/standard-time/power":
            return 200, self.power_view()
        if path == "/api/standard-time/power/csv":
            return 200, {"message": "書き出しました: 試験.csv", "file": "試験.csv"}
        return 404, {"error": {"code": "not_found", "message": "ありません"}}

    @staticmethod
    def power_view() -> dict:
        return {"hero": {"value": "123", "meaning": "標準より速い(試験)", "compared": "比べた作業 3/3件"},
                "table": {"columns": ["班", "梱包力"], "rows": [["A", "123"]], "note": ""},
                "meter": {"rows": [], "max": 100}, "teams": [], "source": "試験", "warning": "",
                "group_label": "班"}

    def test_梱包力の面は開いたときに訊き_しくみの節へ飛べる(self) -> None:
        page = self.open_screen("/standard-time")
        page.set_viewport_size({"width": 1400, "height": 500})   # 飛ばないと節が画面の外になる高さ
        self.assertEqual(self.asked("/api/standard-time/power", "POST"), [], "開く前から訊いています")

        with self.subTest("面を開くと訊いて、答えの数を出す"):
            page.click("#tab-power")
            self.wait_until(page, lambda: self.asked("/api/standard-time/power", "POST"), "梱包力の要求")
            page.wait_for_function("document.getElementById('power-value').textContent === '123'",
                                   timeout=3000)
            self.assertEqual(page.text_content("#power-compared"), "比べた作業 3/3件")

        with self.subTest("CSVに書き出すと、どこへ書いたかを残す"):
            page.click("#power-csv")
            self.wait_until(page, lambda: self.asked("/api/standard-time/power/csv", "POST"),
                            "書き出しの要求")
            page.wait_for_function(
                "document.getElementById('power-msg').textContent.includes('書き出しました: 試験.csv')",
                timeout=3000)

        with self.subTest("「しくみ」で閉じた面を開いて節まで連れて行く"):
            page.click('#panel-power a[href="#guide-power"]')
            page.wait_for_function("!document.getElementById('panel-guide').hidden", timeout=3000)
            self.assertEqual(page.get_attribute("#tab-guide", "aria-selected"), "true")
            page.wait_for_timeout(300)
            top = page.evaluate("document.getElementById('guide-power').getBoundingClientRect().top")
            height = page.evaluate("innerHeight")
            self.assertTrue(0 <= top < height, f"節が画面の外です(上端 {top} / 高さ {height})")
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
