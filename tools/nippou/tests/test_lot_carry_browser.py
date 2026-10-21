"""前の直から続くロット ── 2直でロットを打つと「1直で15枚作業しています」「検入数を変えますか?」(本物のブラウザ)

    はい   → 検入枚数が 20 → 5(残りの枚数)。保存される中身も 5
    いいえ → 20 のまま(仕掛ロットの枚数)
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from datetime import date, datetime, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import test_web_entry_lot as lot_case  # noqa: E402  (試験の組は読み込まない)
from tests._entry_browser import EntryBrowserCase  # noqa: E402

LOT = "H5422S0"


class LotCarryBrowserTests(EntryBrowserCase):
    hiki_rows = lot_case.LotLookupTests.hiki_rows
    lot_course = odr_export = ""
    odr_interleaf = "1"
    _write = lot_case.LotLookupTests._write
    _make_masters = lot_case.LotLookupTests._make_masters

    def setUp(self) -> None:
        super().setUp()
        from tests.test_scenario_shift_flow import _Now

        _Now.value = datetime.combine(date.today(), time(16, 0))   # 2直のなか
        self.ref = self.tmp / "ref"
        self.ref.mkdir(exist_ok=True)
        self._make_masters()
        con = sqlite3.connect(str(self.ref / "SIKALOT.sqlite3"))
        with con:
            con.execute('UPDATE "仕掛" SET "BOX実績_枚本数" = \'20\'')
        con.close()
        self.post("/api/settings/paths", {"gw_reference_dir": str(self.ref), "password": "nisk"})
        self.answer = True                       # 確かめの窓に「はい」

    def watch(self, page):
        page.on("pageerror", lambda e: self.errors.append(str(e)))

        def answer(dialog) -> None:
            self.dialogs.append(dialog.message)
            dialog.accept() if self.answer else dialog.dismiss()

        page.on("dialog", answer)
        return page

    def save_first_shift(self) -> None:
        """1直に LOTA 5枚1包 × 3行(15枚)。"""
        from nippou import work_context
        from nippou.db.models import DetailRecord, HeaderRecord
        from nippou.services.nippou_service import format_business_date

        at = dict(report_date=format_business_date(date.today()),
                  line=work_context.get_context().line, shift="1直", page=1)
        self.repo().save(HeaderRecord(**at, worker="山田"),
                         [DetailRecord(**at, row_no=n, lot=LOT, ken="20", mai="5", tut="1", con="5")
                          for n in (1, 2, 3)])
        # 1直は共有へ送り済み(現場では送ってから2直が打ち始める。送っていなければ引き継ぎの関門が立つ)
        self.repo().mark_synced((at["report_date"], at["line"], "1直", 1))
        # 終わった1直の確認(直の実績)へ連れて行かれないように、先に確認を済ませる
        for _ in range(3):
            state = self.get("/api/graph/review").get_json()
            if not state.get("due"):
                break
            self.post("/api/graph/review/ack", {"stage": state.get("stage", "")})

    def type_lot(self, page) -> None:
        self.type_in(page, "LOT1", LOT)
        page.locator("#LOT1").press("Tab")
        self.wait_until(page, lambda: self.dialogs, "確かめの窓")

    def test_はいで検入枚数が残りの5枚になる(self) -> None:
        self.save_first_shift()
        page = self.open_entry()
        self.type_lot(page)
        self.assertIn("1直で15枚作業しています", self.dialogs[0])
        self.assertIn("検入数を変えますか?(20枚 → 5枚)", self.dialogs[0])
        page.wait_for_function("document.getElementById('KEN1').value === '5'", timeout=5000)
        page.click("#save")
        self.wait_until(page, lambda: any(b.get("rows", {}).get("1", {}).get("KEN") == "5"
                                          for b in self.saves()), "5 枚で保存")
        self.assertEqual(self.errors, [])

    def test_いいえなら仕掛ロットの枚数のまま(self) -> None:
        self.save_first_shift()
        self.answer = False
        page = self.open_entry()
        self.type_lot(page)
        page.wait_for_timeout(800)
        self.assertEqual(self.value(page, "KEN1"), "20")
        self.assertEqual(self.errors, [])

    def test_前の直に無いロットでは訊かない(self) -> None:
        page = self.open_entry()
        self.type_in(page, "LOT1", LOT)
        page.locator("#LOT1").press("Tab")
        page.wait_for_function("document.getElementById('KEN1').value === '20'", timeout=5000)
        page.wait_for_timeout(500)
        self.assertEqual(self.dialogs, [])


if __name__ == "__main__":
    unittest.main()
