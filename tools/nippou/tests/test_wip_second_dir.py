"""仕掛の置き場所を2つ持てる(v4.23.0)

ロット番号から引くとき、**1つ目の置き場所で見つからなければ2つ目を見る**(ファイルごと・
ロットごと):

    - 1つ目に仕掛のファイル(SIKALOT / SIKAHIKI / SIKAODR / LS4LOT)が無い
    - ファイルはあるが、壊れている・0 バイト・中に「仕掛」の表が無い・中身が空
    - ファイルはあるが、そのロット(引当・受注)が無い
    - 1つ目の SIKALOT に BOX最終実績の寸法が無い → 2つ目の SIKALOT の BOX設計の寸法(v4.25.0)

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

    # -- 1つ目の状態ごと(ファイル単位・ロット単位で2つ目を見る) --------
    def test_SIKALOTが無ければSIKALOTだけ2つ目_SIKAHIKIは1つ目のまま(self) -> None:
        self.write(self.first, "SIKAHIKI.sqlite3", HIKI_COLUMNS, [("H5422S0", "AB2234", 100, "", "11111111")])
        self.write(self.first, "SIKAODR.sqlite3", ODR_COLUMNS, [ODR])
        self.all_three(self.second, hiki=(("H5422S0", "AB2234", 100, "", "22222222"),))
        self.use()
        body = self.lookup()
        self.assertEqual(body["lot"]["state"], "filled", body)
        self.assertEqual(body["rows"]["1"]["ZAI"], "52S- R")             # SIKALOT は2つ目
        self.assertEqual(body["lot"]["hiki_no"], "11111111")              # SIKAHIKI は1つ目のまま

    def test_SIKAHIKIにそのロットが無ければ_そのロットの行だけ2つ目から(self) -> None:
        self.all_three(self.first, hiki=(("H0000S0", "ZZ0000", 1, "", "99999999"),))
        self.write(self.second, "SIKAHIKI.sqlite3", HIKI_COLUMNS,
                   [HIKI, ("H0000S0", "YY0000", 1, "", "88888888")])
        self.use()
        body = self.lookup()
        self.assertEqual(body["lot"]["state"], "filled", body)
        self.assertEqual(body["lot"]["hiki_no"], "60717001")

    def test_SIKAODRが無ければSIKAODRだけ2つ目(self) -> None:
        self.write(self.first, "SIKALOT.sqlite3", LOT_COLUMNS, [_lot("H5422S0", material="11S")])
        self.write(self.first, "SIKAHIKI.sqlite3", HIKI_COLUMNS, [HIKI])
        self.all_three(self.second, lots=(_lot("H5422S0", material="99S"),))
        self.use()
        body = self.lookup()
        self.assertEqual(body["rows"]["1"]["ZAI"], "11S- R")             # SIKALOT は1つ目
        self.assertEqual(body["rows"]["1"]["VC"], "A:VE-20NF_B:VE-20NF")  # SIKAODR は2つ目

    def test_壊れている_0バイト_表が無い_中身が空なら_そのファイルだけ2つ目(self) -> None:
        def broken(path: Path) -> None:
            path.write_bytes(b"this is not a sqlite database" * 50)

        def empty_file(path: Path) -> None:
            path.write_bytes(b"")

        def no_table(path: Path) -> None:
            conn = sqlite3.connect(str(path))
            with conn:
                conn.execute("CREATE TABLE ほか (a)")
            conn.close()

        def no_rows(path: Path) -> None:
            self.write(path.parent, path.name, LOT_COLUMNS, [])

        for name, spoil in (("壊れている", broken), ("0バイト", empty_file),
                            ("表が無い", no_table), ("中身が空", no_rows)):
            with self.subTest(name):
                for folder in (self.first, self.second):
                    for f in folder.glob("*.sqlite3"):
                        f.unlink()
                self.write(self.first, "SIKAHIKI.sqlite3", HIKI_COLUMNS, [("H5422S0", "AB2234", 100, "", "11111111")])
                self.write(self.first, "SIKAODR.sqlite3", ODR_COLUMNS, [ODR])
                spoil(self.first / "SIKALOT.sqlite3")
                self.all_three(self.second, hiki=(("H5422S0", "AB2234", 100, "", "22222222"),))
                self.use()
                body = self.lookup()
                self.assertEqual(body["lot"]["state"], "filled", body)
                self.assertEqual(body["rows"]["1"]["ZAI"], "52S- R")
                self.assertEqual(body["lot"]["hiki_no"], "11111111", "ほかのファイルは1つ目のまま")

    # -- BOX の寸法: 1つ目に BOX最終実績が無ければ2つ目の BOX設計(v4.25.0) ------
    BOX_COLUMNS = LOT_COLUMNS + ("BOX最終実績_板厚", "BOX最終実績_板幅", "BOX最終実績_板丈",
                                 "BOX設計_板厚", "BOX設計_板幅", "BOX設計_板丈")

    def box_lot(self, folder: Path, *, final=("", "", ""), design=("", "", "")) -> None:
        row = list(_lot("H5422S0"))
        row[LOT_COLUMNS.index("設計_設備ｺｰｽ")] = "JISN GSS"          # Gコース(BOX の寸法を使う)
        self.write(folder, "SIKALOT.sqlite3", self.BOX_COLUMNS, [tuple(row) + tuple(final) + tuple(design)])

    def box_rest(self, folder: Path) -> None:
        self.write(folder, "SIKAHIKI.sqlite3", HIKI_COLUMNS, [HIKI])
        self.write(folder, "SIKAODR.sqlite3", ODR_COLUMNS, [ODR])

    def test_1つ目にBOX最終実績が無ければ2つ目のBOX設計(self) -> None:
        self.box_lot(self.first, final=("", "0", ""), design=("9", "9", "9"))
        self.box_rest(self.first)
        self.box_lot(self.second, final=("7", "7", "7"), design=("1.5", "1200", "2400"))
        self.use()
        body = self.lookup()
        self.assertEqual(body["lot"]["state"], "filled", body)
        self.assertIn("1.500", body["rows"]["1"]["SIZ"])
        self.assertIn("1200", body["rows"]["1"]["SIZ"])
        self.assertNotIn("7.000", body["rows"]["1"]["SIZ"], "2つ目の BOX最終実績ではなく BOX設計")
        self.assertIn("BOX設計(2つ目)", str(body["lot"]), "画面の一言に出どころを出す")
        self.assertEqual(body["rows"]["1"]["box_course"], "GSS/設計")

    def test_1つ目にBOX最終実績があればそれを使う(self) -> None:
        self.box_lot(self.first, final=("2.5", "1500", "3000"))
        self.box_rest(self.first)
        self.box_lot(self.second, design=("1.5", "1200", "2400"))
        self.use()
        body = self.lookup()
        self.assertIn("2.500", body["rows"]["1"]["SIZ"])
        self.assertEqual(body["rows"]["1"]["box_course"], "GSS")

    def test_ロットが2つ目にしか無ければ2つ目のBOX設計(self) -> None:
        self.box_rest(self.first)
        self.box_lot(self.second, final=("7", "7", "7"), design=("1.5", "1200", "2400"))
        self.use()
        body = self.lookup()
        self.assertIn("1.500", body["rows"]["1"]["SIZ"])

    def test_2つ目を決めていなければこれまでどおり(self) -> None:
        self.box_lot(self.first, design=("1.5", "1200", "2400"))
        self.box_rest(self.first)
        self.use(second=False)
        body = self.lookup()
        self.assertIn("20.000", body["rows"]["1"]["SIZ"], "BOX最終実績が無いので製造の寸法のまま")
        self.assertEqual(body["rows"]["1"]["box_course"], "GSS/製造")

    # -- LS4LOT も仕掛と同じく 1つ目 → 2つ目(v4.25.0) ---------------------
    LS4_COLUMNS = ("ﾛｯﾄ番号", "当工程設計_縦割数", "当工程設計_横割数")

    def coil(self):
        from nippou.access_bridge import gw_master

        return gw_master.search_coil_split("H5422S0")

    def test_LS4LOTが1つ目に無ければ2つ目(self) -> None:
        self.write(self.second, "LS4LOT.sqlite3", self.LS4_COLUMNS, [("H5422S0", "3", "2")])
        self.use()
        found = self.coil()
        self.assertEqual((found.vertical, found.horizontal), ("3", "2"))

    def test_LS4LOTの1つ目にロットが無ければ2つ目(self) -> None:
        self.write(self.first, "LS4LOT.sqlite3", self.LS4_COLUMNS, [("H0000S0", "9", "9")])
        self.write(self.second, "LS4LOT.sqlite3", self.LS4_COLUMNS, [("H5422S0", "3", "2")])
        self.use()
        self.assertEqual(self.coil().vertical, "3")

    def test_LS4LOTは以前の置き場所_梱包資材マスタのフォルダ_も見る(self) -> None:
        material = self.tmp / "資材"
        material.mkdir()
        self.write(material, "LS4LOT.sqlite3", self.LS4_COLUMNS, [("H5422S0", "4", "1")])
        res = self.post("/api/settings/paths", {"password": "nisk", "wip_master_dir": str(self.first),
                                                "wip_master_dir_2": str(self.second),
                                                "material_master_dir": str(material)})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        self.assertEqual(self.coil().vertical, "4")
        # 仕掛の置き場所にあれば、そちらが先
        self.write(self.first, "LS4LOT.sqlite3", self.LS4_COLUMNS, [("H5422S0", "5", "1")])
        self.assertEqual(self.coil().vertical, "5")

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
