"""前の直から続くロット ── 検入枚数を残りの枚数にするか訊く (v4.41.0)

    1直に
    LOTA　20枚　　5枚　１包(×3行)

    2直に
    LOTAを入力した場合に　1直で15枚作業しています　と出し　検入数を変えますか？　と聞いてほしい
    (このケースだと5枚になる)
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from datetime import date, datetime, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.models import DetailRecord  # noqa: E402
from nippou.logic import lot_carry  # noqa: E402
from tests._web import WebTestCase  # noqa: E402
from tests import test_web_entry_lot as lot_case  # noqa: E402  (試験の組は読み込まない)

TODAY = date(2026, 10, 21)
LOT = "H5422S0"


def rows(*pairs, lot=LOT):
    """(枚数, 包数) の行。CON は MAI × TUT。"""
    return [DetailRecord(report_date="", line="", shift="", page=1, row_no=n, lot=lot,
                         mai=str(m), tut=str(t), con=str(m * t))
            for n, (m, t) in enumerate(pairs, 1)]


class DecideTests(unittest.TestCase):
    def test_1直で15枚なら_2直で20枚を5枚にするか訊く(self) -> None:
        saved = {("1直", TODAY): rows((5, 1), (5, 1), (5, 1))}
        worked = lot_carry.walk_back("2直", TODAY, lambda s, d: saved.get((s, d), []), LOT)
        got = lot_carry.decide(row=1, lot_no=LOT, ken="20", worked=worked, today=TODAY)
        self.assertTrue(got["ask"])
        self.assertEqual(got["next"], "5")
        self.assertEqual(got["worked"], 15)
        self.assertIn("1直で15枚作業しています", got["message"])
        self.assertIn("検入数を変えますか?(20枚 → 5枚)", got["message"])

    def test_前の直に無ければ何もしない(self) -> None:
        saved = {("1直", TODAY): rows((5, 1), lot="H9999S0")}
        worked = lot_carry.walk_back("2直", TODAY, lambda s, d: saved.get((s, d), []), LOT)
        self.assertIsNone(lot_carry.decide(row=1, lot_no=LOT, ken="20", worked=worked, today=TODAY))

    def test_続くかぎり遡る_日をまたげば日を添える(self) -> None:
        yesterday = date(2026, 10, 20)
        saved = {("1直", TODAY): rows((4, 1)), ("3直", yesterday): rows((3, 2))}
        worked = lot_carry.walk_back("2直", TODAY, lambda s, d: saved.get((s, d), []), LOT)
        self.assertEqual([(w.shift, w.day, w.sheets) for w in worked],
                         [("3直", yesterday, 6), ("1直", TODAY, 4)])
        got = lot_carry.decide(row=2, lot_no=LOT, ken="20", worked=worked, today=TODAY)
        self.assertEqual(got["next"], "10")
        self.assertIn("3直(10/20)・1直で、合わせて10枚作業しています", got["message"])

    def test_間の空いた直のぶんは数えない(self) -> None:
        """1直に無く、前の日の3直にある ── 続いていないので別の作業。"""
        saved = {("3直", date(2026, 10, 20)): rows((5, 1))}
        self.assertEqual(lot_carry.walk_back("2直", TODAY, lambda s, d: saved.get((s, d), []), LOT), [])

    def test_枚数が空の行は個装単位と包数で数える(self) -> None:
        d = DetailRecord(report_date="", line="", shift="", page=1, row_no=1, lot=LOT, mai="5", tut="2")
        self.assertEqual(lot_carry.sheets_of([d], LOT), 10)

    def test_検入枚数以上なら訊かずに知らせる(self) -> None:
        worked = [lot_carry.Worked("1直", TODAY, 20)]
        got = lot_carry.decide(row=1, lot_no=LOT, ken="20", worked=worked, today=TODAY)
        self.assertFalse(got["ask"])
        self.assertIn("1直で20枚作業しています", got["message"])
        self.assertIn("検入枚数(20)以上です", got["message"])


class CarryRouteTests(WebTestCase):
    """ロット番号を引くと、前の直の保存済みの行を数えて添える(手元の作業用DB)。"""

    # 仕掛のファイルの作り方は、ロットを引く試験のものを借りる(試験そのものは引き継がない)
    hiki_rows = lot_case.LotLookupTests.hiki_rows
    lot_course = odr_export = ""
    odr_interleaf = "1"
    _write = lot_case.LotLookupTests._write
    _make_masters = lot_case.LotLookupTests._make_masters
    lookup = lot_case.LotLookupTests.lookup

    def setUp(self) -> None:
        super().setUp()
        self.ref = self.tmp / "ref"
        self.ref.mkdir(exist_ok=True)
        self._make_masters()
        self.post("/api/settings/paths", {"gw_reference_dir": str(self.ref), "password": "nisk"})
        from tests.test_scenario_shift_flow import _Now, install_clock

        _Now.value = datetime.combine(TODAY, time(16, 0))        # 2直のなか
        install_clock(self)
        con = sqlite3.connect(str(self.ref / "SIKALOT.sqlite3"))
        with con:
            con.execute('UPDATE "仕掛" SET "BOX実績_枚本数" = \'20\'')
        con.close()

    def save_first_shift(self, *pairs) -> None:
        from nippou.db.models import HeaderRecord
        from nippou.services.nippou_service import format_business_date

        at = dict(report_date=format_business_date(TODAY), line=self.line(), shift="1直", page=1)
        details = [DetailRecord(**at, row_no=n, lot=LOT, mai=str(m), tut=str(t), con=str(m * t))
                   for n, (m, t) in enumerate(pairs, 1)]
        self.repo().save(HeaderRecord(**at, worker="山田"), details)

    def line(self) -> str:
        from nippou import work_context
        return work_context.get_context().line

    def test_1直で15枚作業したロットを2直で引くと訊く(self) -> None:
        self.save_first_shift((5, 1), (5, 1), (5, 1))
        body = self.lookup(LOT)
        self.assertEqual(body["lot"]["state"], "filled")
        self.assertEqual(body["rows"]["1"]["KEN"], "20")      # 引いた値はそのまま(変えるのは はい のとき)
        carry = body["lot"]["carry"]
        self.assertTrue(carry["ask"])
        self.assertEqual(carry["next"], "5")
        self.assertIn("1直で15枚作業しています", carry["message"])

    def test_前の直に無ければ添えない(self) -> None:
        self.assertIsNone(self.lookup(LOT)["lot"]["carry"])


if __name__ == "__main__":
    unittest.main()
