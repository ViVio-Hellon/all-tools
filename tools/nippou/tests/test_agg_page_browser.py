"""集計管理: **共有保存の履歴はカードの期間で引く・すぐ選ぶボタンの印は1つ・濃紺の島** (本物のブラウザ。段階1)

前は字を探していました(`test_review_fit` の `WiringTests` と `PresetTests` の一部)。
ここでは集計管理の画面を開いて押し、**送った要求と、押されて見えるボタン**を見ます:

    - 共有保存の履歴は、カードの開始日・終了日で引く。上の表の「表示」では引き直さない
    - 30日ある月の30日は「今月」と「直近30日」が同じ幅になる。**2つとも押されて見えない**
      ── 押したほうに印、押していなければ並びの先のほう。日付を打ち直したら外れる
    - ライトの画面でも、グラフの枠・数字のカードは濃紺の島。島の中の地(ボタンを載せた
      ときの地・知らせの地・いま居る場所の地)も濃紺のものにする(明るい字が溶けない)

サーバの時計は 2026年9月30日 10:00 に止めます(同じ幅のボタンが2つになる日)。
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._entry_browser import ORIGIN, EntryBrowserCase  # noqa: E402

#: 濃紺の島の中で、濃紺のものに替える地(明るい画面の値のままだと、明るい字が溶ける)
GROUNDS = ("--btn-hover", "--state-error-bg", "--state-warn-bg", "--state-ok-bg",
           "--state-info-bg", "--accent-soft")


class AggPageTests(EntryBrowserCase):
    def setUp(self) -> None:
        super().setUp()
        from tests.test_scenario_shift_flow import _Now
        _Now.value = datetime(2026, 9, 30, 10, 0)

    def open_agg(self):
        page = self.watch(self.ctx.new_page())
        page.goto(f"{ORIGIN}/agg", wait_until="load")
        page.wait_for_selector("#push-log-presets [data-preset]")
        self.wait_until(page, lambda: any(p == "/api/agg/push-log" for p, _ in self.sent),
                        "開いたときの履歴の引き")
        return page

    @staticmethod
    def pressed(page) -> list[str]:
        return page.evaluate("""[...document.querySelectorAll('#push-log-presets [data-preset]')]
            .filter(b => b.getAttribute('aria-pressed') === 'true').map(b => b.dataset.preset)""")

    def logs(self, since: int) -> list[dict]:
        return [b for p, b in self.sent[since:] if p == "/api/agg/push-log"]

    def test_同じ幅のボタンが2つでも印は1つ_押したほうに付く(self) -> None:
        page = self.open_agg()
        spans = page.evaluate("""Object.fromEntries([...document.querySelectorAll(
            '#push-log-presets [data-preset]')].map(b => [b.dataset.preset, [b.dataset.start, b.dataset.end]]))""")
        self.assertEqual(spans["this_month"], spans["last_30"])     # この日は同じ幅
        self.assertEqual(self.pressed(page), ["this_month"])        # 押していなければ並びの先
        for key in ("last_30", "this_month", "last_30"):
            with self.subTest(key):
                mark = len(self.sent)
                page.click(f'#push-log-presets [data-preset="{key}"]')
                self.wait_until(page, lambda: bool(self.logs(mark)), "履歴の引き直し")
                self.assertEqual(self.pressed(page), [key])
                self.assertEqual((self.logs(mark)[-1]["start"], self.logs(mark)[-1]["end"]),
                                 tuple(spans[key]))
        page.fill("#push-log-start", "2026-09-02")                   # 打ち直したら外れる
        page.evaluate("document.activeElement.blur()")
        page.wait_for_timeout(300)
        self.assertEqual(self.pressed(page), [])

    def test_履歴はカードの期間で引き_上の表示では引き直さない(self) -> None:
        page = self.open_agg()
        page.fill("#start", "2026-09-10")                            # 上の表の期間は別
        mark = len(self.sent)
        page.fill("#push-log-start", "2026-08-01")
        page.evaluate("document.activeElement.blur()")             # 選び終える(離れたときに変わる)
        self.wait_until(page, lambda: bool(self.logs(mark)), "履歴の引き直し")
        self.assertEqual((self.logs(mark)[-1]["start"], self.logs(mark)[-1]["end"]),
                         ("2026-08-01", "2026-09-30"))
        mark = len(self.sent)
        page.click("#reload")
        self.wait_until(page, lambda: any(p == "/api/agg/tables" for p, _ in self.sent[mark:]),
                        "上の表の引き直し")
        page.wait_for_timeout(400)
        self.assertEqual(self.logs(mark), [], "上の表の「表示」で履歴まで引き直しています")

    def test_ライトの画面でも濃紺の島の地は濃紺のもの(self) -> None:
        for path, island in (("/agg", "#push-log-chart"), ("/graph", ".tile")):
            with self.subTest(path):
                page = self.watch(self.ctx.new_page())
                page.goto(f"{ORIGIN}{path}", wait_until="load")
                page.wait_for_selector(island, state="attached")
                got = page.evaluate("""([sel, names]) => {
                    const island = getComputedStyle(document.querySelector(sel));
                    const ground = getComputedStyle(document.body);
                    return names.map(n => [n, ground.getPropertyValue(n).trim(),
                                              island.getPropertyValue(n).trim()]);
                }""", [island, list(GROUNDS)])
                self.assertEqual(page.evaluate("document.documentElement.dataset.theme"), "light")
                for name, ground, inside in got:
                    self.assertNotEqual(inside, ground, f"{name}: 島の中が明るい画面の地のまま")
                page.close()


if __name__ == "__main__":
    unittest.main()
