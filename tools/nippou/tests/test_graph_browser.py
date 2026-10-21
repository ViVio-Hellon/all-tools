"""集計・グラフ: 期間を変えたときの塗り直しと、積み上げた棒・線の描き方を本物のブラウザで (段階1)

前は `graph.js` / `chart.js` の字を探していました(`test_web_screens` の
`test_期間を変えたらタイルの札も塗り直す`・`test_formula` の `test_期間を変えても式が消えない`・
`test_tones` の `test_線が棒に溶けない仕掛けがある` / `test_段のあいだに境を入れる` /
`test_目盛りの頭は積んだ高さで決める` / `test_折れ線は太く点は大きく` の JS の部分)。
ここでは2日×2直ぶんを保存して、期間を変えて描かせ、描けた絵を見ます:

    - 期間を変えたら、タイルの札(いつのぶんか)も新しい期間になる
    - 割り算のタイルの「計算内容を見る」は、期間を変えても消えない。足すだけのタイルには付かない
    - 直ごとに積んだ棒は**枠を突き抜けない**(目盛りの頭は積んだ高さで決める)
    - 段のあいだに境(`bar--seg`)、線には地の色の縁(`line--halo`。同じ道を2回描く)、点は半径4
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._screen_browser import ScreenBrowserCase  # noqa: E402

#: (日, 直, 枚数)。1本の高さ(最大 120)より、積んだ高さ(最大 210)が大きい
SHIFTS = (("2026年9月10日", "1直", "120"), ("2026年9月10日", "2直", "90"),
          ("2026年9月11日", "1直", "100"), ("2026年9月11日", "2直", "110"))


class GraphBrowserTests(ScreenBrowserCase):
    def setUp(self) -> None:
        super().setUp()
        from nippou.db.models import DetailRecord, HeaderRecord
        from nippou.services import summary

        repo = self.repo()
        for day, shift, mai in SHIFTS:
            first = shift == "1直"
            repo.save(HeaderRecord(report_date=day, line="L-1", shift=shift, page=1, worker="作業者A"),
                      [DetailRecord(report_date=day, line="L-1", shift=shift, page=1, row_no=1,
                                    lot="1111111", zai="F52S", siz="1.0x1000x2000", ken="9",
                                    kz="8" if first else "16", kh="00",
                                    sz="9" if first else "17", sh="00", hit="1", mai=mai,
                                    tut="2", con=mai, wei="4720", tim="60", s="1", th="120",
                                    ss="イ", ths="24", sth="A", tht="27")])
            summary.refresh_shift(repo, day, "L-1", shift)
        # 保存した直はどれも共有へ渡していないので、開いた途端に全画面確認へ連れて行かれる
        # (それは `shift_end` の仕事)。ここでは確認は無いことにする
        self.answers["/api/graph/review"] = (200, {"due": False})

    def show(self, page, start: str, end: str) -> None:
        """期間を選んで「表示」。描き直し終わるまで待つ。"""
        page.select_option("#mode", "period")
        page.fill("#start", start)
        page.fill("#end", end)
        before = len(self.asked("/api/graph/dashboard", "POST"))
        page.click("#reload")
        self.wait_until(page, lambda: len(self.asked("/api/graph/dashboard", "POST")) > before,
                        "表示の要求")
        page.wait_for_selector('[data-tile-body="trend_cumulative"] svg', timeout=5000)
        page.wait_for_timeout(400)

    def note(self, page, key: str) -> str:
        return page.text_content(f'[data-tile="{key}"] .tile__note').strip()

    def test_期間を変えたら札と計算内容を塗り直し_積んだ棒は枠に収まる(self) -> None:
        page = self.open_screen("/graph")
        self.show(page, "2026-09-01", "2026-09-30")
        with self.subTest("札は新しい期間"):
            self.assertEqual(self.note(page, "period_count"), "2026-09-01 〜 2026-09-30")
        with self.subTest("割り算のタイルには計算内容、足すだけのタイルには無い"):
            calc = page.locator('[data-tile-body="period_rate"] details.calc')
            self.assertEqual(calc.count(), 1)
            self.assertEqual(calc.locator("summary").text_content(), "計算内容を見る")
            for part in ("calc__formula", "calc__sub", "calc__result"):
                self.assertGreater(calc.locator(f".{part}").count(), 0, part)
            self.assertEqual(page.locator('[data-tile-body="period_weight"] details.calc').count(), 0)

        self.show(page, "2026-09-11", "2026-09-30")              # もう一度変える
        with self.subTest("2度目に変えても札と式が付いてくる"):
            self.assertEqual(self.note(page, "period_count"), "2026-09-11 〜 2026-09-30")
            self.assertEqual(page.locator('[data-tile-body="period_rate"] details.calc').count(), 1)

        self.show(page, "2026-09-01", "2026-09-30")
        chart = '[data-tile-body="trend_cumulative"] svg'
        shape = page.evaluate(f"""() => {{
            const svg = document.querySelector('{chart}');
            const ys = (sel) => [...svg.querySelectorAll(sel)].map(e => Number(e.getAttribute('y')));
            const lines = [...svg.querySelectorAll('polyline')];
            return {{
                bars: ys('rect.bar'), segs: svg.querySelectorAll('rect.bar--seg').length,
                top: Math.min(...[...svg.querySelectorAll('line.gridline')].map(e => Number(e.getAttribute('y1')))),
                halos: lines.filter(l => l.classList.contains('line--halo')).map(l => l.getAttribute('points')),
                lines: lines.filter(l => !l.classList.contains('line--halo')).map(l => l.getAttribute('points')),
                dots: [...svg.querySelectorAll('circle.dot')].map(c => c.getAttribute('r')),
            }};
        }}""")
        with self.subTest("直ごとに積んだ棒は枠を突き抜けない"):
            self.assertEqual(len(shape["bars"]), 4, shape)
            self.assertGreaterEqual(min(shape["bars"]), shape["top"] - 0.5, shape)
        with self.subTest("段のあいだに境"):
            self.assertEqual(shape["segs"], 4)
        with self.subTest("線は同じ道を2回描いて縁を付ける・点は半径4"):
            self.assertTrue(shape["lines"])
            self.assertEqual(shape["halos"], shape["lines"])
            self.assertTrue(shape["dots"])
            self.assertEqual(set(shape["dots"]), {"4"})
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
