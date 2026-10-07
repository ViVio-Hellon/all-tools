"""仕掛の置き場所を2つ持てる(v4.23.0)

ロット番号から引くとき、**1つ目の置き場所で見つからなければ2つ目を見る**:

    - 1つ目に仕掛のファイル(SIKALOT / SIKAHIKI / SIKAODR)が無い
    - ファイルはあるが、そのロット(引当・受注)が無い

2つ目を空にしておけば、これまでどおり1つ目だけを見る。
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import WebTestCase  # noqa: E402
from tests.test_web_entry_lot import HIKI_COLUMNS, LOT_COLUMNS, ODR_COLUMNS  # noqa: E402


def _lot(lot_no: str, material: str = "52S") -> tuple:
    return (lot_no, "AB2234", "K1", material, "R", 20.0, 1528.0, 3053.0, "H283",
            "JISNﾌﾗﾂﾄANF", "", "取引先", "納入先", "送り先", "9", "0", "C1")


HIKI = ("H5422S0", "AB2234", 100, "", "60717001")
ODR = ("AB2234", "VE-20NF", "VE-20NF", "1", 250.04314, "取引先", "納入先", "送り先", "1P1186", "")


class SecondWipDirTests(WebTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.first = self.tmp / "仕掛1"
        self.second = self.tmp / "仕掛2"
        self.first.mkdir()
        self.second.mkdir()

    def use(self, *, second: bool = True) -> None:
        body = {"wip_master_dir": str(self.first), "password": "nisk",
                "wip_master_dir_2": str(self.second) if second else ""}
        res = self.post("/api/settings/paths", body)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))

    def write(self, folder: Path, name: str, columns, rows) -> None:
        conn = sqlite3.connect(str(folder / name))
        with conn:
            cols = ", ".join(f'"{c}"' for c in columns)
            marks = ", ".join("?" for _ in columns)
            conn.execute(f'CREATE TABLE "仕掛" ({cols})')
            conn.executemany(f'INSERT INTO "仕掛" VALUES ({marks})', rows)
        conn.close()

    def all_three(self, folder: Path, *, lots=(_lot("H5422S0"),), hiki=(HIKI,), odr=(ODR,)) -> None:
        self.write(folder, "SIKALOT.sqlite3", LOT_COLUMNS, lots)
        self.write(folder, "SIKAHIKI.sqlite3", HIKI_COLUMNS, hiki)
        self.write(folder, "SIKAODR.sqlite3", ODR_COLUMNS, odr)

    def lookup(self, lot_no: str = "H5422S0"):
        return self.post("/api/entry/lot", {"rows": {"1": {"LOT": lot_no}}, "row": 1,
                                            "hiki_no": ""}).get_json()

    # -- 2つ目を見る ---------------------------------------------------
    def test_1つ目にファイルが無ければ2つ目で引く(self) -> None:
        self.all_three(self.second)
        self.use()
        body = self.lookup()
        self.assertEqual(body["lot"]["state"], "filled", body)
        self.assertEqual(body["rows"]["1"]["ZAI"], "52S- R")
        self.assertEqual(body["rows"]["1"]["VC"], "A:VE-20NF_B:VE-20NF")

    def test_1つ目にファイルはあるがロットが無ければ2つ目で引く(self) -> None:
        self.all_three(self.first, lots=(_lot("H9999S0"),), hiki=(), odr=())
        self.all_three(self.second)
        self.use()
        body = self.lookup()
        self.assertEqual(body["lot"]["state"], "filled", body)
        self.assertEqual(body["rows"]["1"]["UNI"], "250.04314")

    def test_引当と受注も_1つ目に無ければ2つ目で引く(self) -> None:
        """ロットは1つ目にあるが、引当・受注は2つ目にしか無い。"""
        self.all_three(self.first, hiki=(("H5422S0", "", 100, "", "60717009"),), odr=())
        self.write(self.second, "SIKAHIKI.sqlite3", HIKI_COLUMNS, [HIKI])
        self.write(self.second, "SIKAODR.sqlite3", ODR_COLUMNS, [ODR])
        self.use()
        body = self.lookup()
        self.assertEqual(body["lot"]["state"], "filled", body)
        self.assertEqual(body["lot"]["hiki_no"], "60717001")      # 受注番号のある2つ目の引当
        self.assertEqual(body["rows"]["1"]["VC"], "A:VE-20NF_B:VE-20NF")

    def test_1つ目で見つかれば2つ目は見ない(self) -> None:
        self.all_three(self.first, lots=(_lot("H5422S0", material="11S"),))
        self.all_three(self.second, lots=(_lot("H5422S0", material="99S"),))
        self.use()
        self.assertEqual(self.lookup()["rows"]["1"]["ZAI"], "11S- R")

    # -- 2つ目が空ならこれまでどおり -----------------------------------
    def test_2つ目が空なら1つ目だけ(self) -> None:
        self.all_three(self.second)
        self.use(second=False)
        body = self.lookup()
        self.assertNotEqual(body["lot"]["state"], "filled")
        self.assertIn("SIKALOT", body["lot"]["message"])

    def test_どちらにも無ければ無いと言う(self) -> None:
        self.all_three(self.first, lots=(_lot("H9999S0"),))
        self.all_three(self.second, lots=(_lot("H8888S0"),))
        self.use()
        body = self.lookup()
        self.assertNotEqual(body["lot"]["state"], "filled")
        self.assertIn("H5422S0", body["lot"]["message"])

    # -- 設定 ----------------------------------------------------------
    def test_設定画面と配布設定に2つ目の欄がある(self) -> None:
        from nippou import config, distribution

        html = self.get("/settings?tab=paths").get_data(as_text=True)
        self.assertIn("仕掛ロット・引当・受注の置き場所(2つ目)", html)
        self.assertIn(config.KEY_WIP_DIR2, [item.key for item in distribution.ITEMS])

    def test_2つ目を変えるには管理者パスワードが要る(self) -> None:
        res = self.post("/api/settings/paths", {"wip_master_dir_2": str(self.second)})
        self.assertNotEqual(res.status_code, 200)


if __name__ == "__main__":
    unittest.main()
