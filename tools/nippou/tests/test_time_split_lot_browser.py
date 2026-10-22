"""時間を割り振る: EX・段取り・VC交換を本物のブラウザで (v4.42.0)

    - 確かめの窓に「EX のロット…」「ロットの変わり目…1梱包目…」が出て、はいで EX の行が長く埋まる
    - 設定・管理者で倍率を変えて保存できる(管理者モードでないと押せない)
    - LOTで時間を割り振る(v4.43.0): 確かめの窓にロットと始まりを出し、はいでそのロットの行を
      直の開始から押した時刻まで埋める
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import time_split as ts  # noqa: E402
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class TimeSplitLotBrowserTests(WebTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from tests._browser import chromium_or_skip
        cls.pw, cls.browser = chromium_or_skip()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.pw.stop()

    def setUp(self) -> None:
        super().setUp()
        from tests.test_seq_entry import BrowserTests
        self.ctx = self.browser.new_context(viewport={"width": 1400, "height": 900})
        self.addCleanup(self.ctx.close)
        self.ctx.route("http://app.test/**", lambda route: BrowserTests._forward(self, route))
        self.errors: list[str] = []
        self.dialogs: list[str] = []

    def open(self, path: str):
        page = self.ctx.new_page()
        page.on("pageerror", lambda e: self.errors.append(str(e)))

        def answer(dialog):
            self.dialogs.append(dialog.message)
            dialog.accept()
        page.on("dialog", answer)
        page.goto(f"http://app.test{path}", wait_until="load")
        return page

    def span(self, page, n: int) -> int:
        """その行の時間から停止の分を引いたもの(休憩をまたぐ行には休憩「0」60分が入る)。"""
        a = ts.parse_hhmm(f'{page.input_value(f"#KZ{n}")}:{page.input_value(f"#KH{n}")}')
        b = ts.parse_hhmm(f'{page.input_value(f"#SZ{n}")}:{page.input_value(f"#SH{n}")}')
        stops = sum(int(page.input_value(f"#{f}{n}") or 0) for f in ("TH", "THS", "THT", "TH4", "TH5"))
        return (b - a) % 1440 - stops

    def test_確かめの窓に出してEXの行を長く埋める(self) -> None:
        from nippou import user_settings
        from nippou.config import KEY_TIME_SPLIT
        user_settings.save(KEY_TIME_SPLIT, True)

        page = self.open("/")
        page.wait_for_selector("#grid-body")
        page.wait_for_timeout(600)
        if page.evaluate("document.body.classList.contains('needs-worker')"):
            page.fill("#hd-worker", "山田")
            page.dispatch_event("#hd-worker", "change")
            page.wait_for_function("!document.body.classList.contains('needs-worker')", timeout=5000)
        # 1〜2行目: EX、3〜4行目: EX でない。どれも 5枚×1包(ロット№は打たない ── 引きに行かせない)
        for n in (1, 2, 3, 4):
            page.fill(f"#MAI{n}", "5")
            page.dispatch_event(f"#MAI{n}", "blur")
        for n in (1, 2):
            page.fill(f"#ET{n}", "EX")
            page.dispatch_event(f"#ET{n}", "change")
        page.wait_for_timeout(500)
        page.click("#time-split")
        page.wait_for_function("document.getElementById('KZ1').value !== ''", timeout=5000)
        self.assertTrue(self.dialogs, "確かめの窓が出ない")
        self.assertIn("・EX のロットの 2 行は 1.2倍で数えます", self.dialogs[0])
        self.assertGreater(self.span(page, 1) + self.span(page, 2),
                           self.span(page, 3) + self.span(page, 4))
        self.assertEqual(self.errors, [])

    def test_設定で倍率を変える(self) -> None:
        page = self.open("/settings?tab=terminal")
        page.wait_for_selector("#factor-save")
        self.assertTrue(page.is_disabled("#factor-save"), "管理者モードでないのに押せる")

        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        page = self.open("/settings?tab=terminal")
        page.wait_for_selector("#factor-save:not([disabled])")
        page.fill("#factor-ex", "1.4")
        page.fill("#factor-vc", "２")
        page.click("#factor-save")
        page.wait_for_selector(".toast:has-text('割り振りの倍率を変えました')")
        self.assertEqual(page.input_value("#factor-vc"), "2")
        got = ts.Factors.read(self._saved())
        self.assertEqual((float(got.ex), float(got.setup), float(got.vc)), (1.4, 1.5, 2.0))

        # 読めない値は断り、理由を出す(黙って既定に戻さない)
        page.fill("#factor-setup", "9")
        page.click("#factor-save")
        page.wait_for_selector("#time-split-note:not([hidden])")
        self.assertIn("段取り(ロットの変わり目)の倍率は", page.inner_text("#time-split-note"))
        self.assertEqual(float(ts.Factors.read(self._saved()).setup), 1.5)
        self.assertEqual(self.errors, [])

    def test_LOTで時間を割り振る(self) -> None:
        from datetime import date, datetime, time

        from nippou import user_settings, work_context
        from nippou.config import KEY_TIME_SPLIT
        from nippou.db.models import DetailRecord, HeaderRecord
        from nippou.services import shift_check
        from app.routes.entry import build_shift_calculator
        from tests.test_scenario_shift_flow import _Now, install_clock

        user_settings.save(KEY_TIME_SPLIT, True)
        _Now.value = datetime.combine(date(2026, 10, 21), time(8, 12))     # 1直のなか
        install_clock(self)
        day, line, shift = work_context.get_context().current_key(
            build_shift_calculator(self.repo().get_shift_times()))
        at = dict(report_date=day, line=line, shift=shift, page=1)
        self.repo().save(HeaderRecord(**at, worker="山田"),
                         [DetailRecord(**at, row_no=1, lot="LOTA", mai="5", tut="1"),
                          DetailRecord(**at, row_no=2, mai="5", tut="1"),
                          DetailRecord(**at, row_no=3, mai="2", tut="1")])
        start, _end = shift_check.bounds_of(self.repo().get_shift_times(), shift)

        page = self.open("/")
        page.wait_for_selector("#grid-body")
        page.wait_for_function("document.getElementById('LOT1').value === 'LOTA'", timeout=5000)
        page.click("#time-split-lot")
        page.wait_for_function("document.getElementById('SZ3').value !== ''", timeout=5000)
        self.assertTrue(self.dialogs, "確かめの窓が出ない")
        self.assertIn("ロット LOTA の 3 行(1行目〜3行目)に", self.dialogs[0])
        self.assertIn("始まりは直の開始、終わりはいまの時刻", self.dialogs[0])
        self.assertEqual(f'{page.input_value("#KZ1")}:{page.input_value("#KH1")}', start)
        self.assertEqual(f'{page.input_value("#SZ3")}:{page.input_value("#SH3")}', "08:10")
        self.assertEqual(f'{page.input_value("#SZ1")}:{page.input_value("#SH1")}',
                         f'{page.input_value("#KZ2")}:{page.input_value("#KH2")}', "行の時刻はつながる")
        self.assertEqual(page.input_value("#KZ4"), "", "打っていない行は触らない")
        self.assertEqual(self.errors, [])

    def test_LOTで時間を割り振るは選んでいる行のロット(self) -> None:
        """LOTB まで打ったあとで LOTA の行を選んで押す → LOTA を、その最後の行の終了まで(v4.43.1)。"""
        from datetime import date, datetime, time

        from nippou import user_settings, work_context
        from nippou.config import KEY_TIME_SPLIT
        from nippou.db.models import DetailRecord, HeaderRecord
        from app.routes.entry import build_shift_calculator
        from tests.test_scenario_shift_flow import _Now, install_clock

        user_settings.save(KEY_TIME_SPLIT, True)
        _Now.value = datetime.combine(date(2026, 10, 21), time(9, 2))
        install_clock(self)
        day, line, shift = work_context.get_context().current_key(
            build_shift_calculator(self.repo().get_shift_times()))
        at = dict(report_date=day, line=line, shift=shift, page=1)
        self.repo().save(HeaderRecord(**at, worker="山田"),
                         [DetailRecord(**at, row_no=1, lot="LOTA", mai="5", tut="1"),
                          DetailRecord(**at, row_no=2, mai="5", tut="1", sz="08", sh="10"),
                          DetailRecord(**at, row_no=3, lot="LOTB", mai="3", tut="1",
                                       kz="08", kh="10", sz="09", sh="00")])
        page = self.open("/")
        page.wait_for_selector("#grid-body")
        page.wait_for_function("document.getElementById('LOT1').value === 'LOTA'", timeout=5000)
        page.click("#MAI2")
        page.click("#time-split-lot")
        page.wait_for_function("document.getElementById('KZ2').value !== ''", timeout=5000)
        self.assertIn("ロット LOTA の 2 行(1行目〜2行目)に", self.dialogs[0])
        self.assertIn("終わりは2行目の終了", self.dialogs[0])
        self.assertEqual(f'{page.input_value("#SZ2")}:{page.input_value("#SH2")}', "08:10")
        self.assertEqual(f'{page.input_value("#SZ3")}:{page.input_value("#SH3")}', "09:00", "LOTB は触らない")
        self.assertEqual(self.errors, [])

    @staticmethod
    def _saved():
        from nippou import user_settings
        from nippou.config import KEY_TIME_SPLIT_FACTORS
        return user_settings.get(KEY_TIME_SPLIT_FACTORS, None)


if __name__ == "__main__":
    unittest.main()
